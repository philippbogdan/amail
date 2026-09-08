"""Local drafts and immutable, resumable outreach plans."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from gmail_backend import secure_json
from local_store import MailError
from mail_sender import address, ledger, prepare, send
from policy import WaitRequired, SuppressedRecipient, profile, eligibility


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def initialise(c):
    c.executescript("""create table if not exists batches(id text primary key,snapshot text not null,state text not null,created real not null);
        create table if not exists batch_items(batch text,item text,position integer,request_id text unique,state text,result text,
                                             primary key(batch,item));
        create table if not exists drafts(id text primary key,revision integer,snapshot text not null,created real not null);""")


def freeze_attachment(path, state):
    path = Path(path).resolve()
    if not path.is_file():
        raise MailError("Attachment is not a regular file: " + str(path))
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = state / "assets" / digest / path.name
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise MailError("Stored attachment snapshot failed its integrity check")
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    return {"path": str(target.resolve()), "name": path.name, "sha256": digest, "bytes": len(data)}


def normalise(item, base, state, store, *, require_id=False):
    allowed = {"id", "from", "to", "cc", "bcc", "subject", "body", "body_file", "attachments", "reply_to_ref", "name"}
    if not isinstance(item, dict):
        raise MailError("Message must be a JSON object; see amail schema message")
    unknown = set(item) - allowed
    if unknown:
        raise MailError("Unsupported message fields: " + ", ".join(sorted(unknown)) + "; see amail schema message")
    if require_id and (not isinstance(item.get("id"), str) or not item["id"].strip()):
        raise MailError("Every batch item needs a non-empty string id")
    if not isinstance(item.get("from"),str) or not isinstance(item.get("name",getattr(store,"display_name","")),str):
        raise MailError("Sender and display name must be strings")
    if not isinstance(item.get("attachments",[]),list) or any(not isinstance(p,str) for p in item.get("attachments",[])):
        raise MailError("Attachments must be a list of file paths")
    if "body_file" in item and not isinstance(item["body_file"],str):
        raise MailError("body_file must be a file path")
    if ("body" in item) == ("body_file" in item):
        raise MailError("Specify exactly one of body or body_file")
    body = item.get("body") if "body" in item else (base / item["body_file"]).read_text()
    if not isinstance(body, str) or not isinstance(item.get("subject"), str):
        raise MailError("Body and subject must be strings")
    sender = address(item["from"])
    account = store.resolve(sender)[0]
    def recipients(key):
        values = item.get(key, [])
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            raise MailError(key + " must be a list of email addresses")
        return [address(x) for x in values]
    message = {"id": item.get("id", "draft"), "from": sender, "account_uuid": account["uuid"],
               "to": recipients("to"), "cc": recipients("cc"), "bcc": recipients("bcc"),
               "subject": item["subject"], "body": body, "name": item.get("name", getattr(store,"display_name","")),
               "attachments": [freeze_attachment(base / p, state) for p in item.get("attachments", [])]}
    if item.get("reply_to_ref"):
        from mail_operations import context
        context(store,item["reply_to_ref"])
        message["reply_to_ref"] = item["reply_to_ref"]
    prepare(store, arguments(message, request_id="validation", dry_run=True),state=state)
    return message


def arguments(message, *, request_id, dry_run=False, purpose="personal", timeout=30):
    for attachment in message["attachments"]:
        if hashlib.sha256(Path(attachment["path"]).read_bytes()).hexdigest() != attachment["sha256"]:
            raise MailError("An approved attachment changed; create a new plan or draft revision")
    return argparse.Namespace(sender=message["from"], to=message["to"], cc=message["cc"], bcc=message["bcc"],
        subject=message["subject"], body=message["body"], body_file=None, name=message["name"],
        attach=[a["path"] for a in message["attachments"]], request_id=request_id,
        dry_run=dry_run, cap=None, timeout=timeout, purpose=purpose, reply_to_ref=message.get("reply_to_ref"))


def draft_create(store, state, item, base, draft_id=None):
    snapshot = normalise(item, base, state, store)
    draft_id = draft_id or "draft-" + uuid.uuid4().hex[:16]
    with ledger(state) as c:
        initialise(c)
        previous = c.execute("select revision from drafts where id=?", (draft_id,)).fetchone()
        revision = previous[0] + 1 if previous else 1
        with c:
            c.execute("insert or replace into drafts values(?,?,?,?)", (draft_id, revision, json.dumps(snapshot), time.time()))
    return {"id": draft_id, "revision": revision, "content_hash": fingerprint(snapshot), "message": snapshot}


def draft_get(state, draft_id):
    with ledger(state) as c:
        initialise(c)
        row = c.execute("select * from drafts where id=?", (draft_id,)).fetchone()
        delivery = c.execute("select state from sends where request_id=?",(f'{draft_id}-{row["revision"]}',)).fetchone() if row else None
    if not row:
        raise MailError("Unknown local draft")
    message = json.loads(row["snapshot"])
    return {"id": draft_id, "revision": row["revision"], "state":delivery[0] if delivery else "draft", "content_hash": fingerprint(message), "message": message}


def draft_discard(state, draft_id):
    with ledger(state) as c:
        initialise(c)
        with c:
            changed = c.execute("delete from drafts where id=?", (draft_id,)).rowcount
    if not changed:
        raise MailError("Unknown local draft")
    return {"id": draft_id, "discarded": True}


def batch_plan(store, state, filename):
    path = Path(filename).resolve()
    messages, ids, contacts = [], set(), set()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            message = normalise(json.loads(line), path.parent, state, store, require_id=True)
        except (MailError, OSError, ValueError, KeyError, TypeError) as e:
            raise MailError(f"Invalid batch item on line {number}: {e}") from e
        if message["id"] in ids:
            raise MailError("Duplicate batch item ID: " + message["id"])
        if len(message["to"]) != 1:
            raise MailError("Outreach batches require exactly one To recipient per item")
        contact = message["to"][0]
        if contact in contacts and not message.get("reply_to_ref"):
            raise MailError("Duplicate outreach recipient: " + contact)
        ids.add(message["id"]); contacts.add(contact)
        messages.append(message)
    if not messages or len(messages) > 10000:
        raise MailError("A batch must contain between 1 and 10000 messages")
    rules = {m["account_uuid"]: profile(store.accounts[m["account_uuid"]]) for m in messages}
    snapshot = {"messages": messages, "policies": rules, "purpose": "outreach"}
    batch_id = "batch-" + fingerprint(snapshot)[:24]
    with ledger(state) as c:
        initialise(c)
        with c:
            c.execute("insert or ignore into batches values(?,?,?,?)", (batch_id, json.dumps(snapshot), "planned", time.time()))
            for position, message in enumerate(messages):
                request_id = batch_id + "-" + hashlib.sha256(message["id"].encode()).hexdigest()[:16]
                suppressed = [a for a in message["to"] + message["cc"] + message["bcc"]
                              if c.execute("select 1 from suppressions where address=?", (a,)).fetchone()]
                status = "suppressed" if suppressed else "pending"
                result = json.dumps({"suppressed": suppressed}) if suppressed else None
                c.execute("insert or ignore into batch_items values(?,?,?,?,?,?)",
                          (batch_id, message["id"], position, request_id, status, result))
    return batch_show(state, batch_id)


def batch_show(state, batch_id, *, include_messages=True):
    with ledger(state) as c:
        initialise(c)
        row = c.execute("select * from batches where id=?", (batch_id,)).fetchone()
        items = [dict(r) for r in c.execute("select item,request_id,state,result from batch_items where batch=? order by position", (batch_id,))]
    if not row:
        raise MailError("Unknown batch plan")
    snapshot = json.loads(row["snapshot"])
    if "batch-" + fingerprint(snapshot)[:24] != batch_id:
        raise MailError("Batch plan integrity check failed")
    for item in items:
        item["result"] = json.loads(item["result"]) if item["result"] else None
    result = {"id": batch_id, "state": row["state"], "policies": snapshot["policies"], "items": items,
              "counts": {s: sum(i["state"] == s for i in items) for s in sorted({i["state"] for i in items})}}
    if include_messages:
        result["messages"] = snapshot["messages"]
        result["schedule"] = forecast(state,snapshot,items)
    return result


def forecast(state,snapshot,items):
    now=time.time(); events={}; next_at={}; remaining=[]
    item_states={i["item"]:i["state"] for i in items}
    with ledger(state) as c:
        for account in snapshot["policies"]:
            events[account]=[(r[0],r[1]) for r in c.execute("select created,recipients from sends where account=? and purpose='outreach' and state!='rejected' and created>?",(account,now-86400))]
            row=c.execute("select next_at from pacing where account=?",(account,)).fetchone()
            next_at[account]=row[0] if row else now
    for position,message in enumerate(snapshot["messages"]):
        if item_states[message["id"]] not in {"accepted","cancelled","suppressed"}:
            remaining.append((position,message))
    output=[]; available=now
    while remaining:
        heads={}
        for position,message in remaining:
            heads.setdefault(message["account_uuid"],(position,message))
        choices=[]
        for account,(position,message) in heads.items():
            recipients=len(set(message["to"]+message["cc"]+message["bcc"]))
            rules=snapshot["policies"][account]
            eligible,_=eligibility(events[account],now=available,recipients=recipients,rules=rules,next_at=next_at[account])
            choices.append((eligible,position,message,recipients))
        eligible,position,message,recipients=min(choices,key=lambda v:(v[0],v[1]))
        account=message["account_uuid"]; rules=snapshot["policies"][account]
        output.append({"item":message["id"],"estimated_submit_at":eligible})
        events[account].append((eligible,recipients));available=eligible+1
        next_at[account]=available+(rules["gap_min_seconds"]+rules["gap_max_seconds"])/2
        remaining=[p for p in remaining if p[0]!=position]
    return {"items":output,"estimated_finish_at":available,
            "assumptions":"One second per provider submission and average configured gaps; actual responses, bounces and other sends can change timing."}


def batch_control(state, batch_id, action):
    current = batch_show(state, batch_id, include_messages=False)
    if current["state"] in {"completed", "cancelled"}:
        return current
    target = {"pause": "paused", "cancel": "cancelled"}[action]
    with ledger(state) as c, c:
        c.execute("update batches set state=? where id=?", (target, batch_id))
        if action == "cancel":
            c.execute("update batch_items set state='cancelled' where batch=? and state='pending'", (batch_id,))
    return batch_show(state, batch_id, include_messages=False)


def batch_run(store, state, here, batch_id, *, resume=False, sender=send, clock=time.time, sleeper=time.sleep):
    plan = batch_show(state, batch_id)
    if plan["state"] in {"completed", "cancelled"}:
        return plan
    if plan["state"] == "paused" and not resume:
        raise MailError("Batch is paused; use amail batch resume")
    for account_id, approved in plan["policies"].items():
        if profile(store.accounts[account_id]) != approved:
            raise MailError("The policy changed since review; make a new batch plan")
    lock_path = state / (batch_id + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise MailError("This batch already has a running worker") from e
        with ledger(state) as c, c:
            c.execute("update batches set state='running' where id=?", (batch_id,))
        messages = {m["id"]: m for m in plan["messages"]}
        deferred={}
        try:
            while True:
                current = batch_show(state, batch_id, include_messages=False)
                if current["state"] != "running": return current
                heads={}
                for candidate in current["items"]:
                    if candidate["state"] in {"accepted","suppressed","cancelled"}: continue
                    account_id=messages[candidate["item"]]["account_uuid"]
                    heads.setdefault(account_id,candidate)
                if not heads: break
                item=min(heads.values(),key=lambda candidate:deferred.get(candidate["item"],0))
                eligible=deferred.get(item["item"],0)
                if eligible>clock():
                    sleeper(min(1,max(.01,eligible-clock())))
                    continue
                from feedback import scan
                scan(store,state)
                message=messages[item["item"]]
                args=arguments(message,request_id=item["request_id"],purpose="outreach")
                args.logical_item=message["id"]
                args.retry_rejected=resume
                try:
                    result=sender(store,args,state,here)
                except SuppressedRecipient as e:
                    with ledger(state) as c,c:
                        c.execute("update batch_items set state='suppressed',result=? where batch=? and item=?",(json.dumps({"reason":str(e)}),batch_id,item["item"]))
                    continue
                except WaitRequired as e:
                    deferred[item["item"]]=e.until
                    with ledger(state) as c,c:
                        c.execute("update batch_items set result=? where batch=? and item=?",(json.dumps({"waiting_until":e.until,"reason":e.reason}),batch_id,item["item"]))
                    continue
                except MailError as e:
                    result={"state":"blocked","error":str(e)}
                with ledger(state) as c,c:
                    c.execute("update batch_items set state=?,result=? where batch=? and item=?",(result["state"],json.dumps(result),batch_id,item["item"]))
                    if result["state"]!="accepted":c.execute("update batches set state='paused' where id=?",(batch_id,))
                if result["state"]!="accepted":return batch_show(state,batch_id,include_messages=False)
            with ledger(state) as c, c:
                c.execute("update batches set state='completed' where id=? and state='running'", (batch_id,))
        except KeyboardInterrupt:
            batch_control(state, batch_id, "pause")
            raise
    return batch_show(state, batch_id, include_messages=False)


def input_schema(kind):
    """Account-independent description of the exact accepted manifest contract."""
    properties = {
        "id": {"type": "string", "minLength": 1, "description": "Stable logical item ID; required for batches; keep across replanning to prevent duplicate sends"},
        "from": {"type": "string", "description": "An enabled sender email address"},
        "to": {"type": "array", "items": {"type": "string"}},
        "cc": {"type": "array", "items": {"type": "string"}, "default": []},
        "bcc": {"type": "array", "items": {"type": "string"}, "default": []},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "Complete plain-text body, including greeting and signature"},
        "body_file": {"type": "string", "description": "UTF-8 text file, relative to the manifest directory"},
        "attachments": {"type": "array", "items": {"type": "string"}, "default": [], "description": "File paths relative to the manifest directory; bytes are frozen at planning time"},
        "reply_to_ref": {"type": "string", "description": "Original amail ref for a threaded reply"},
        "name": {"type": "string", "description": "Sender display name; defaults to private configuration"},
    }
    required = ["from", "subject"]
    example = {"from": "you@company.example", "to": ["colleague@example.org"], "subject": "Meeting notes", "body": "Hello,\n\nHere are the notes we discussed.\n"}
    if kind == "batch":
        required += ["id", "to"]
        properties["to"].update(minItems=1, maxItems=1)
        example = {"id": "colleague-001", **example}
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
              "properties": properties, "required": required,
              "oneOf": [{"required": ["body"], "not": {"required": ["body_file"]}},
                        {"required": ["body_file"], "not": {"required": ["body"]}}]}
    return {"format": "JSONL: one object per non-empty line" if kind == "batch" else "JSON object",
            "schema": schema, "example": example,
            "constraints": ["Addresses and enabled sender are validated by amail; at least one recipient is required.",
                            "Exactly one of body and body_file is required.",
                            "Obtain human approval of recipients, subject, body and attachments before sending."] +
                           (["1 to 10000 messages; unique IDs and unique To recipients except explicit threaded replies.",
                             "Planning freezes content and policy but does not send. Review with batch show; batch run sends in the foreground.",
                             "Monitor with batch status; use --details for item evidence. Resume preserves request IDs and never retries ambiguous sends."] if kind == "batch" else [])}


def batch_summary(state, batch_id):
    """Compact monitoring result; retain full evidence in batch status --details."""
    plan = batch_show(state, batch_id, include_messages=False)
    now = time.time()
    waits = []
    exceptions = []
    for item in plan["items"]:
        result = item["result"] or {}
        if item["state"] == "pending" and result.get("waiting_until", 0) > now:
            waits.append({"item": item["item"], "until": result["waiting_until"], "reason": result.get("reason")})
        elif item["state"] not in {"pending", "accepted", "cancelled"}:
            exceptions.append({"item": item["item"], "state": item["state"],
                               **{key: result[key] for key in ("reason", "error", "suppressed") if key in result}})
    worker_running = False
    try:
        fd = os.open(state / (batch_id + ".lock"), os.O_RDONLY)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                worker_running = True
    next_wait = min(waits, key=lambda value: value["until"]) if waits else None
    return {"id": plan["id"], "state": plan["state"], "counts": plan["counts"],
            "worker_running": worker_running, "waiting": waits,
            "next_retry_at": next_wait["until"] if next_wait and worker_running else None,
            "wait_seconds": max(0, round(next_wait["until"] - now, 1)) if next_wait and worker_running else None,
            "exceptions": exceptions,
            "next_step": "Worker is absent; inspect send outcomes before resuming with amail batch resume " + batch_id
                         if plan["state"] == "running" and not worker_running else None}
