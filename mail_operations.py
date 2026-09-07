"""Mailbox changes and explicit network fetches. No UI automation."""
import email
import email.policy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import re
import urllib.parse
from gmail_backend import Gmail
from local_store import MailError, parse_emlx, attachment_parts, message_text
from mail_sender import ledger


def bridge(account_id, operation, here, state, **fields):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="operation-", dir=state) as directory:
        payload = Path(directory) / "request.json"
        with open(payload, "x") as f:
            os.chmod(payload, 0o600)
            json.dump(dict(account=account_id, operation=operation, **fields), f)
        p = subprocess.run(["osascript", "-l", "JavaScript", str(here / "mail_operations.jxa"), str(payload)],
                           capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise MailError(p.stderr.strip() or "Mail scripting operation failed")
    return json.loads(p.stdout)


def context(store, ref):
    if ref.startswith("gmail:"):
        fields = ref.split(":")
        if len(fields) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fields[2]):
            raise MailError("Invalid Gmail message ref")
        account = store.resolve(fields[1])[0]
        if account["kind"] != "com.apple.account.Google":
            raise MailError("A Gmail ref cannot target a non-Gmail account")
        return {"id":fields[2]}, account, {"api_id":fields[2], "message_id":""}
    row = store.locate(ref)
    path = store.path(row)
    message_id = str(parse_emlx(path).get("Message-ID", "")) if path else ""
    account = store.accounts[urllib.parse.urlsplit(row["url"]).netloc]
    return row, account, {"id": row["id"], "mailbox": urllib.parse.unquote(urllib.parse.urlsplit(row["url"]).path).lstrip("/"),
                           "subject": row["subject"], "message_id": message_id}


def gmail_id(client, message_id):
    if not message_id:
        raise MailError("Message-ID is not available in the local cache")
    hits = client.find("rfc822msgid:" + message_id.strip("<>"), maximum=10)
    if len(hits) != 1:
        raise MailError("Server message lookup is missing or ambiguous")
    return hits[0]["id"]


def server_message(store, ref, state, here):
    row, account, args = context(store, ref)
    if account["kind"] == "com.apple.account.Google":
        client = Gmail((account.get("auth_address") or account["addresses"][0]), state)
        identifier = args.get("api_id") or gmail_id(client, args["message_id"])
        raw, metadata = client.raw(identifier)
        return email.message_from_bytes(raw, policy=email.policy.default), {"provider": "gmail", "provider_id": identifier,
                "labels": metadata.get("labelIds", []), "source": "live API", "account":account["name"], "account_uuid":account["uuid"],
                "date":dt.datetime.fromtimestamp(int(metadata.get("internalDate",0))/1000,dt.timezone.utc).isoformat(),
                "ref":"gmail:"+account["uuid"]+":"+identifier, "provider_thread_id":metadata.get("threadId")}
    result = bridge(account["uuid"], "source", here, state, **args)
    return email.message_from_string(result["source"], policy=email.policy.default), {"provider": "apple_mail", "source": "Mail application"}


def load_message(store, ref, state, here, *, fetch=False):
    if not fetch and not ref.startswith("gmail:"):
        return store.read(ref)
    message, provenance = server_message(store, ref, state, here)
    from local_store import body_available
    parts = attachment_parts(message, state / "0.emlx")
    item = {} if ref.startswith("gmail:") else store.item(store.locate(ref))
    item.update({"ref":ref,"from":str(message.get("From","")),"to":str(message.get("To","")),"cc":str(message.get("Cc","")),
        "subject":str(message.get("Subject","")),"message_id":str(message.get("Message-ID","")),"body":message_text(message),
        "body_available":body_available(message),"reply_to":str(message.get("Reply-To","")),
        "references":str(message.get("References","")),"in_reply_to":str(message.get("In-Reply-To","")),
        "attachments":[{k:v for k,v in p.items() if not k.startswith("_")} for p in parts],"attachments_complete":all(p["available"] for p in parts)})
    item.update(provenance)
    if provenance.get("provider") == "gmail":
        item.update(read="UNREAD" not in provenance["labels"],flagged="STARRED" in provenance["labels"],server_freshness="queried now")
    return item, parts


def thread(store, ref, state, here):
    if not ref.startswith("gmail:"): return store.thread(ref)
    _, account, args = context(store, ref)
    client = Gmail(account.get("auth_address") or account["addresses"][0], state)
    metadata = client.request("/messages/"+args["api_id"],query={"format":"minimal"})
    data = client.request("/threads/"+urllib.parse.quote(metadata["threadId"],safe=""),query={"format":"metadata"})
    result = []
    for message in data.get("messages",[]):
        headers = {h["name"].casefold():h["value"] for h in message.get("payload",{}).get("headers",[])}
        result.append({"ref":"gmail:"+account["uuid"]+":"+message["id"],"account":account["name"],"from":headers.get("from",""),
                       "subject":headers.get("subject",""),"date":headers.get("date",""),"message_id":headers.get("message-id",""),
                       "read":"UNREAD" not in message.get("labelIds",[]),"source":"live Gmail thread"})
    return result


def fetch_attachments(store, ref, output, state, here):
    row, account, args = context(store, ref)
    directory = Path(output).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    saved = []
    if account["kind"] == "com.apple.account.Google":
        message, metadata = server_message(store, ref, state, here)
        parts = attachment_parts(message, directory / "0.emlx")
        for part in parts:
            if not part["available"]:
                raise MailError("Provider did not supply the attachment payload")
            name = Path(part["name"].replace("\\", "/")).name
            if name in {"", ".", ".."}: name = "attachment"
            for suffix in range(10000):
                target = directory / (name if suffix == 0 else f"{Path(name).stem}-{suffix}{Path(name).suffix}")
                try:
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    break
                except FileExistsError:
                    continue
            else:
                raise MailError("Too many attachment filename collisions")
            data = part["_payload"]
            with os.fdopen(fd, "wb") as f: f.write(data)
            saved.append({"file": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), **metadata})
    else:
        attachments = bridge(account["uuid"], "attachments", here, state, **args)
        for attachment in attachments:
            name = Path(attachment["name"].replace("\\", "/")).name
            if name in {"", ".", ".."}: name = "attachment"
            # Native Mail writes only inside a new private directory. The final
            # copy uses O_EXCL, so no existing user file can be overwritten.
            with tempfile.TemporaryDirectory(dir=state) as temporary:
                native = Path(temporary) / name
                bridge(account["uuid"], "save_attachment", here, state, **args,
                       index=attachment["index"], name=attachment["name"], output=str(native))
                if not native.is_file(): raise MailError("Mail did not finish downloading the attachment")
                data = native.read_bytes()
            for suffix in range(10000):
                target = directory / (name if suffix == 0 else f"{Path(name).stem}-{suffix}{Path(name).suffix}")
                try:
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    break
                except FileExistsError: continue
            else: raise MailError("Too many attachment filename collisions")
            with os.fdopen(fd, "wb") as f: f.write(data)
            saved.append({"file": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "source": "Mail attachment download"})
    return saved


def mutate(store, ref, action, state, here, destination=None):
    row, account, args = context(store, ref)
    if account["kind"] != "com.apple.account.Google" and not args["message_id"]:
        identity = bridge(account["uuid"], "identity", here, state, **args)
        args["message_id"] = "<" + identity["message_id"].strip("<>") + ">"
    with ledger(state) as c:
        c.execute("""create table if not exists mailbox_changes(id integer primary key,account text,message_id text,action text,before_state text,created real)""")
        c.commit()
        history = c.execute("select before_state from mailbox_changes where account=? and message_id=? and action='trash' order by id desc limit 1",
                            (account["uuid"], args["message_id"])).fetchone()
    if account["kind"] == "com.apple.account.Google":
        client = Gmail((account.get("auth_address") or account["addresses"][0]), state)
        identifier = args.get("api_id") or gmail_id(client, args["message_id"])
        resource = "/messages/" + urllib.parse.quote(identifier, safe="")
        metadata = client.request(resource, query={"format": "metadata", "metadataHeaders":"Message-ID"})
        if not args["message_id"]:
            args["message_id"] = next((h["value"] for h in metadata.get("payload",{}).get("headers",[]) if h["name"].casefold()=="message-id"),"")
            with ledger(state) as c:
                history = c.execute("select before_state from mailbox_changes where account=? and message_id=? and action='trash' order by id desc limit 1",
                                    (account["uuid"],args["message_id"])).fetchone()
        before = {"labels": metadata.get("labelIds", [])}
        additions, removals = [], []
        if action == "read": removals = ["UNREAD"]
        elif action == "unread": additions = ["UNREAD"]
        elif action == "flag": additions = ["STARRED"]
        elif action == "unflag": removals = ["STARRED"]
        elif action == "archive": removals = ["INBOX"]
        elif action == "move":
            if destination.casefold() in {"inbox", "archive", "all mail", "[gmail]/all mail"}:
                additions, removals = (["INBOX"], []) if destination.casefold() == "inbox" else ([], ["INBOX"])
            else:
                labels = client.request("/labels").get("labels", [])
                hits = [l["id"] for l in labels if l["name"].casefold() == destination.casefold()]
                if len(hits) != 1: raise MailError("Gmail destination label is missing or ambiguous")
                additions, removals = hits, ["INBOX"]
        elif action in {"trash", "restore"}:
            metadata = client.request(resource + ("/trash" if action == "trash" else "/untrash"), payload={})
            if action == "restore" and history:
                prior = json.loads(history[0])
                additions, removals = (["INBOX"], []) if "INBOX" in prior.get("labels", []) else ([], ["INBOX"])
        else: raise MailError("Unsupported mailbox action")
        if additions or removals:
            metadata = client.request(resource + "/modify", payload={"addLabelIds": additions, "removeLabelIds": removals})
        result = {"state": "confirmed", "action": action, "provider": "gmail", "provider_id": identifier,
                  "ref":"gmail:"+account["uuid"]+":"+identifier,
                  "label_ids": metadata.get("labelIds", []), "cache": "Apple Mail may reflect this after its next sync"}
    else:
        before = {"mailbox": urllib.parse.unquote(urllib.parse.urlsplit(row["url"]).path.lstrip("/"))}
        operation = action
        if action in {"move", "archive", "restore", "trash"}:
            operation = "move"
            target = destination or ("trash" if action == "trash" else json.loads(history[0])["mailbox"] if action == "restore" and history else "inbox" if action == "restore" else "archive")
            boxes = store.mailboxes(account["uuid"], target)
            if len(boxes) != 1: raise MailError("Destination mailbox is missing or ambiguous")
            args["destination"] = boxes[0]["path"]
        result = bridge(account["uuid"], operation, here, state, **args)
        result["provider_sync"] = "pending; application state is reported"
    with ledger(state) as c, c:
        c.execute("insert into mailbox_changes(account,message_id,action,before_state,created) values(?,?,?,?,?)",
                  (account["uuid"], args["message_id"], action, json.dumps(before), time.time()))
    return result
