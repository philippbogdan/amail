"""Programmatic submission with no UI automation and explicit verification states."""
import contextlib
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from local_store import MailError, parse_emlx


def normal_body(body):
    return body.replace("\r\n","\n").replace("\r","\n").strip()


def display_body(body):
    # Mail/Exchange can reflow paragraph breaks. Preserve all non-whitespace text.
    return " ".join(body.split())


def canonical_address(store,value):
    try:
        return store.resolve(value)[0]["uuid"]
    except MailError:
        return value.casefold()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def process_start(pid):
    try:
        p = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, timeout=2)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError,subprocess.TimeoutExpired):
        return ""


def owner_alive(row):
    if not row["owner_pid"]:
        return False
    try:
        os.kill(row["owner_pid"], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    observed = process_start(row["owner_pid"])
    return not (observed and row["owner_start"] and observed != row["owner_start"])


def address(value):
    if any(ord(ch)<32 or ord(ch)==127 for ch in value) or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+",value):
        raise MailError("Use plain email addresses without display names for recipients and --from")
    from email.headerregistry import Address
    try: Address(addr_spec=value)
    except ValueError as e: raise MailError("Invalid email address") from e
    return value.casefold()


@contextlib.contextmanager
def ledger(state):
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    file = state/"sends.sqlite"
    fd = os.open(file,os.O_CREAT|os.O_WRONLY,0o600)
    os.close(fd)
    c=sqlite3.connect(file,timeout=2)
    c.row_factory=sqlite3.Row
    c.execute("begin immediate")
    c.execute("""create table if not exists sends(request_id text primary key,account text,created real,state text,recipients integer,
                 request_hash text,verification text,result text)""")
    from policy import initialise
    initialise(c)
    c.commit()
    try:
        yield c
    finally:
        c.close()


def prepare(store,args,state=None,here=None):
    sender=address(args.sender)
    account=store.resolve(sender)[0]
    if sender not in account["addresses"]:
        raise MailError("Sender is not a configured email alias")
    body=args.body if args.body is not None else (Path(args.body_file).read_text() if args.body_file else sys.stdin.read())
    if any(ord(ch)<32 or ord(ch)==127 for ch in args.subject+args.name):
        raise MailError("Subject and sender name must not contain control characters")
    if not args.to or not 0 < args.timeout <= 120 or (args.cap is not None and args.cap < 1):
        raise MailError("Require a recipient, a timeout from 0 to 120 seconds, and a positive cap")
    request={"sender":sender,"formatted_sender":email.utils.formataddr((args.name,sender)),"subject":args.subject,"body":body,
             "to":[address(x) for x in args.to],"cc":[address(x) for x in args.cc],"bcc":[address(x) for x in args.bcc],"attach":[],
             "account_id":account["uuid"],"auth_account":account.get("auth_address") or sender}
    if getattr(args, "reply_to_ref", None):
        if args.reply_to_ref.startswith("gmail:"):
            from mail_operations import load_message
            selected_state = state or Path(os.environ.get("MAIL_STATE_DIR", str(Path.home()/"Library/Application Support/amail")))
            source, _ = load_message(store,args.reply_to_ref,selected_state,here or Path(__file__).parent)
        else:
            source, _ = store.read(args.reply_to_ref)
        original_id = source["message_id"].strip()
        if not re.fullmatch(r"<[^<>\r\n]+>", original_id):
            raise MailError("Original Message-ID is unavailable or malformed; cannot preserve reply threading")
        request["in_reply_to"] = original_id
        request["references"] = " ".join((source["references"] + " " + original_id).split())
        if account["kind"] != "com.apple.account.Google":
            if "storage_mailbox" not in source:
                cached = []
                for row in store.query(account=source["account_uuid"],mailbox="*",subject=source["subject"],limit=1000):
                    try:
                        item,_=store.read(store.ref(row))
                        if item["message_id"]==original_id: cached.append(item)
                    except (MailError,OSError): continue
                if len(cached)!=1:
                    raise MailError("Sync the original message into Mail before replying from this account")
                source=cached[0]
            request["reply_source"] = {"account": source["account_uuid"], "id": source["id"],
                                       "mailbox": source["storage_mailbox"],
                                       "message_id": original_id, "subject": source["subject"]}
    attachments=[]
    for value in args.attach:
        path=Path(value).expanduser().resolve()
        if not path.is_file():
            raise MailError("Attachment is not a regular file: "+str(path))
        content=path.read_bytes()
        request["attach"].append(str(path))
        description={"name":path.name,"sha256":hashlib.sha256(content).hexdigest(),"bytes":len(content)}
        if path.suffix.casefold() in {".txt",".csv",".md"}:
            try:
                description["text_hash"]=digest(content.decode("utf-8").replace("\r\n","\n"))
            except UnicodeDecodeError:
                pass
        attachments.append(description)
    verification={"account":account["uuid"],"sender":sender,"subject":args.subject,"body_hash":digest(normal_body(body)),"display_body_hash":digest(display_body(body)),
                  "to":sorted(request["to"]),"cc":sorted(request["cc"]),"bcc":sorted(request["bcc"]),"attachments":attachments}
    if request.get("in_reply_to"):
        verification["in_reply_to"] = request["in_reply_to"]
    fingerprint=digest(json.dumps([request,attachments],sort_keys=True))
    return account,request,verification,fingerprint


def update(state,request_id,result):
    with ledger(state) as c, c:
        previous = c.execute("select account,purpose,state from sends where request_id=?", (request_id,)).fetchone()
        if previous and previous["purpose"] == "outreach" and previous["state"] != result["state"] and result["state"] in {"accepted", "rejected"}:
            c.execute("update pacing set next_at=max(next_at,?+gap_seconds) where account=?", (time.time(), previous["account"]))
        c.execute("update sends set state=?,result=? where request_id=?",(result["state"],json.dumps(result),request_id))


def matches_content(store,item,parts,verification):
    to=sorted(canonical_address(store,a) for _,a in email.utils.getaddresses([item["to"]]) if a)
    cc=sorted(canonical_address(store,a) for _,a in email.utils.getaddresses([item["cc"]]) if a)
    expected_to=sorted(canonical_address(store,a) for a in verification["to"])
    expected_cc=sorted(canonical_address(store,a) for a in verification["cc"])
    exact_body=digest(normal_body(item["body"]))==verification["body_hash"]
    reflowed=digest(display_body(item["body"]))==verification.get("display_body_hash")
    if to!=expected_to or cc!=expected_cc or not (exact_body or reflowed):
        return False
    if verification.get("in_reply_to") and item.get("in_reply_to") != verification["in_reply_to"]:
        return False
    unmatched=list(verification["attachments"])
    for part in parts:
        if not part["available"]:
            return False
        payload=part["_path"].read_bytes() if part["_path"] else part["_payload"]
        if payload is None:
            return False
        actual_hash=hashlib.sha256(payload).hexdigest()
        try:
            text_hash=digest(payload.decode("utf-8").replace("\r\n","\n")) if part["type"].startswith("text/") else None
        except UnicodeDecodeError:
            text_hash=None
        match=next((p for p in unmatched if p["name"]==part["name"] and (p["sha256"]==actual_hash or (text_hash and p.get("text_hash")==text_hash))),None)
        if match is None:
            return False
        unmatched.remove(match)
    return not unmatched


def observed_acceptance(store,verification):
    candidates=store.query(account=verification["account"],mailbox="sent",subject=verification["subject"],limit=200,since_hours=1)
    matched=[]
    for row in candidates:
        if row["id"] in verification["before_ids"] or row["subject"]!=verification["subject"]:
            continue
        if canonical_address(store,row["address"] or "")!=canonical_address(store,verification["sender"]):
            continue
        try:
            item,parts=store.read(store.ref(row))
        except (MailError,OSError):
            continue
        if matches_content(store,item,parts,verification):
            matched.append((row,item))
        # Self/cross-account tests can establish stronger evidence: a received
        # copy with the same Message-ID, transport headers, and verified content.
        for recipient in verification["to"]+verification["cc"]:
            try:
                targets=store.resolve(recipient)
            except MailError:
                continue
            for received in store.query(account=targets[0]["uuid"],mailbox="inbox",subject=verification["subject"],limit=100,since_hours=1):
                try:
                    incoming,incoming_parts=store.read(store.ref(received))
                    incoming_file=store.path(received)
                    headers=parse_emlx(incoming_file).get_all("Received",[])
                except (MailError,OSError):
                    continue
                if incoming["message_id"] and incoming["message_id"]==item["message_id"] and headers and matches_content(store,incoming,incoming_parts,verification):
                    return {"sent_ref":item["ref"],"received_ref":incoming["ref"],"message_id":item["message_id"],
                            "effective_from":incoming["from"],"evidence":{"type":"received_copy_with_transport_headers","received_header_count":len(headers)},
                            "verification_source":"Provider-delivered copy in a configured recipient inbox"}
    if len(matched)!=1:
        return None
    row,item=matched[0]
    account=store.accounts[verification["account"]]
    proof=None
    if account["kind"]=="com.apple.account.Google":
        boxes=[b["id"] for b in store.mailboxes(account["uuid"],"sent")]
        slots=",".join("?" for _ in boxes)
        with store.connect() as c:
            remote=c.execute(f"""select sm.remote_id from server_messages sm where sm.message=? and sm.deleted=0 and sm.remote_id>0
                         and (sm.mailbox in ({slots}) or exists(select 1 from server_labels sl where sl.server_message=sm.ROWID and sl.label in ({slots})))""",[row["id"]]+boxes+boxes).fetchone()
        if remote:
            proof={"type":"synced_imap_sent_membership","remote_id":str(remote[0])}
    elif account["kind"]=="com.apple.account.Exchange" and isinstance(row["remote_id"],str) and len(row["remote_id"])>10:
        proof={"type":"synced_exchange_sent_item","remote_id_sha256":digest(row["remote_id"])}
    if proof:
        return {"sent_ref":item["ref"],"message_id":item["message_id"],"effective_from":item["from"],"evidence":proof,
                "verification_source":"Mail's synced server metadata; not a fresh provider API response"}
    return None


def send(store,args,state,here):
    started=time.perf_counter()
    account,request,verification,fingerprint=prepare(store,args,state,here)
    request_id=args.request_id or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}",request_id):
        raise MailError("Invalid request ID")
    if args.dry_run:
        return {"state":"dry_run","request_id":request_id,"request":request,"attachments":verification["attachments"],"account":account["name"]}
    request["message_id"] = f"<amail.{request_id}@{request['sender'].split('@', 1)[1]}>"
    request["request_id"] = request_id
    verification["message_id"] = request["message_id"]
    verification["before_ids"]=[r["id"] for r in store.query(account=account["uuid"],mailbox="sent",subject=args.subject,limit=100000)]
    now=time.time()
    purpose = getattr(args, "purpose", "personal")
    with ledger(state) as c:
        c.execute("begin immediate")
        prior=c.execute("select * from sends where request_id=?",(request_id,)).fetchone()
        logical_item=getattr(args,"logical_item",None)
        if not prior and logical_item:
            existing=c.execute("select request_id from sends where account=? and logical_item=? and request_hash=? and state!='rejected' order by created desc limit 1",
                               (account["uuid"],logical_item,fingerprint)).fetchone()
            if existing:
                c.rollback()
                return dict(status(existing[0],state,store),deduplicated_from=existing[0],requested_id=request_id)
        if prior:
            if prior["request_hash"]!=fingerprint:
                raise MailError("Request ID is already used for different message content")
            previous = json.loads(prior["result"])
            if prior["state"] == "rejected" and getattr(args, "retry_rejected", False):
                from policy import WaitRequired
                if previous.get("retry_at", 0) > now:
                    raise WaitRequired(previous["retry_at"], "provider Retry-After")
            else:
                c.rollback()
                return status(request_id,state,store)
        pending=c.execute("select request_id from sends where account=? and state in ('submitting','provider_acceptance_unverified','outcome_unknown')",(account["uuid"],)).fetchone()
        if pending:
            raise MailError("A previous send needs reconciliation: "+pending[0]+"; use amail send-status")
        used=c.execute("select coalesce(sum(recipients),0) from sends where account=? and created>=? and state!='rejected'",(account["uuid"],now-86400)).fetchone()[0]
        recipients=len(set(request["to"]+request["cc"]+request["bcc"]))
        if args.cap is not None and used+recipients>args.cap:
            raise MailError("Rolling recipient cap would be exceeded")
        from policy import reserve
        reserve(c, account, recipients, request["to"] + request["cc"] + request["bcc"], purpose, now=now)
        result={"request_id":request_id,"state":"submitting","requested_sender":request["sender"],
                "attempt":(previous.get("attempt", 1) + 1) if prior else 1}
        if prior:
            result["previous_result"] = previous
        c.execute("insert or replace into sends(request_id,account,created,state,recipients,request_hash,verification,result,purpose,owner_pid,owner_start,logical_item) values(?,?,?,?,?,?,?,?,?,?,?,?)",(request_id,account["uuid"],now,"submitting",recipients,fingerprint,json.dumps(verification),json.dumps(result),purpose,os.getpid(),process_start(os.getpid()),logical_item))
        c.commit()
    try:
        from workflows import freeze_attachment
        frozen=[]
        for path, expected in zip(request["attach"],verification["attachments"]):
            attachment=freeze_attachment(Path(path),state)
            if attachment["sha256"]!=expected["sha256"]:
                from gmail_backend import ProviderError
                raise ProviderError("Attachment changed before submission; review the new content")
            frozen.append(attachment["path"])
        request["attach"]=frozen
        if time.perf_counter()-started>=args.timeout:
            from gmail_backend import ProviderError
            raise ProviderError("Deadline elapsed before submission; no provider send was attempted")
        if account["kind"] == "com.apple.account.Google":
            from gmail_backend import submit
            remaining = max(.1, args.timeout - (time.perf_counter() - started))
            result.update(submit(request, remaining, state))
            result["submission_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["elapsed_ms"] = result["submission_ms"]
            update(state, request_id, result)
            return result
        # The payload stays in a mode-0600 temporary file, not the process arguments.
        with tempfile.TemporaryDirectory(prefix="send-",dir=state) as directory:
            path=Path(directory)/"request.json"
            with open(path,"x") as f:
                os.chmod(path,0o600)
                json.dump(request,f)
            remaining=max(.1,args.timeout-(time.perf_counter()-started))
            p=subprocess.run(["osascript",str(here/"send.applescript"),str(path)],capture_output=True,text=True,timeout=remaining)
        if p.returncode:
            result.update(state="outcome_unknown",error="Mail script failed; check status before any further send")
        else:
            reply=json.loads(p.stdout)
            result["submission_ms"]=round((time.perf_counter()-started)*1000,1)
            result["mail_script"]=reply
            if reply.get("mail_send_result") is True:
                result["state"]="provider_acceptance_unverified"
                update(state,request_id,result)
                while time.perf_counter()-started<args.timeout:
                    evidence=observed_acceptance(store,verification)
                    if evidence:
                        result.update(state="accepted",**evidence)
                        break
                    time.sleep(.1)
            elif reply.get("stage")=="compose":
                result["state"]="rejected"
            else:
                result["state"]="outcome_unknown"
    except MailError as e:
        result.update(state="outcome_unknown" if getattr(e, "uncertain", True) else "rejected", error=str(e))
        if getattr(e, "retry_after", None) is not None:
            result["retry_at"] = time.time() + e.retry_after
    except (subprocess.TimeoutExpired,OSError,ValueError):
        result.update(state="outcome_unknown",error="Submission/verification interrupted; do not resend without reconciling")
    result["elapsed_ms"]=round((time.perf_counter()-started)*1000,1)
    update(state,request_id,result)
    return result


def describe(row,state,store,result=None):
    result=dict(result if result is not None else json.loads(row["result"]))
    result.update(requested_sender=json.loads(row["verification"]).get("sender"),created=row["created"],
                  account=store.accounts[row["account"]]["name"])
    if not result.get("effective_from") and result.get("sent_ref"):
        try: result["effective_from"]=store.read(result["sent_ref"])[0]["from"]
        except (MailError,OSError): pass
    result.setdefault("effective_from",None)
    with ledger(state) as c:
        exists=c.execute("select 1 from sqlite_master where name='feedback' and type='table'").fetchone()
        result["feedback"]=[dict(r) for r in c.execute("select type,recipient,created from feedback where request_id=? order by created",(row["request_id"],))] if exists else []
    result["feedback_source"]="observed local inbox feedback; missing server telemetry remains unknown"
    return result


def status(request_id,state,store):
    with ledger(state) as c:
        row=c.execute("select * from sends where request_id=?",(request_id,)).fetchone()
    if not row:
        raise MailError("Unknown send request ID")
    result=json.loads(row["result"])
    if result["state"] not in {"accepted","rejected","legacy_reported_sent"}:
        evidence=observed_acceptance(store,json.loads(row["verification"]))
        if evidence:
            result.update(state="accepted",**evidence)
            update(state,request_id,result)
        elif result["state"] == "submitting" and not owner_alive(row):
            result.update(state="outcome_unknown", error="The submitting process exited; do not resend without reconciliation")
            update(state,request_id,result)
    return describe(row,state,store,result)
