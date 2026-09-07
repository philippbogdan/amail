"""Correlated delivery-status reports and explicit opt-out commands."""
import email.utils
import hashlib
import json
import time
from local_store import MailError, parse_emlx, message_text, body_available
from mail_sender import ledger


OPT_OUTS = {"unsubscribe", "please unsubscribe me", "please remove me", "remove me from your list",
            "do not contact me", "please do not contact me", "stop emailing me"}


def classify(message, requests):
    """Return only feedback correlated to known outreach, never arbitrary prose."""
    results = []
    known = {}
    for request_id, account, verification, result in requests:
        for recipient in verification["to"] + verification["cc"] + verification.get("bcc",[]):
            known.setdefault(recipient.casefold(), []).append((request_id, account))
    sender = email.utils.parseaddr(str(message.get("From", "")))[1].casefold()
    first_line = next((s.strip().casefold().rstrip(".! ") for s in message_text(message).splitlines() if s.strip()), "")
    subject = str(message.get("Subject", "")).strip().casefold()
    if sender in known and (subject in OPT_OUTS or first_line in OPT_OUTS):
        request_id, account = known[sender][-1]
        results.append({"type":"opt_out","recipient":sender,"request_id":request_id,"account":account})
    # Match the original Message-ID or our stable request header before applying
    # a DSN. A stray paragraph mentioning an address is not delivery feedback.
    original_ids, request_headers = set(), set()
    for part in message.walk():
        for value in part.get_all("Original-Message-ID", []): original_ids.add(str(value).strip())
        for value in part.get_all("Message-ID", []): original_ids.add(str(value).strip())
        for value in part.get_all("X-Amail-Request-ID", []): request_headers.add(str(value).strip())
    correlated = [(rid, account, verification) for rid,account,verification,result in requests
                  if rid in request_headers or (result.get("message_id") and result["message_id"] in original_ids)
                  or (verification.get("message_id") and verification["message_id"] in original_ids)]
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status": continue
        for block in part.get_payload() if isinstance(part.get_payload(),list) else []:
            if str(block.get("Action", "")).casefold() != "failed" or not str(block.get("Status", "")).startswith("5."):
                continue
            recipient = str(block.get("Final-Recipient", "")).split(";",1)[-1].strip().casefold()
            for rid, account, verification in correlated:
                if recipient in verification["to"] + verification["cc"] + verification.get("bcc",[]):
                    results.append({"type":"hard_bounce","recipient":recipient,"request_id":rid,"account":account,
                                    "status":str(block.get("Status"))})
    return results


def scan(store, state):
    with ledger(state) as c:
        c.execute("create table if not exists feedback_seen(source text primary key,created real)")
        c.execute("create table if not exists feedback(source text,request_id text,type text,recipient text,account text,created real,primary key(source,request_id,type,recipient))")
        c.commit()
        requests = [(r[0],r[1],json.loads(r[2]),json.loads(r[3])) for r in c.execute(
            "select request_id,account,verification,result from sends where purpose='outreach' and created>? order by created",(time.time()-90*86400,))]
        if not requests: return {"processed":0,"feedback":[],"source":"local inbox cache"}
        seen = {r[0] for r in c.execute("select source from feedback_seen")}
    found, processed, unavailable = [], 0, 0
    for row in store.query(mailbox="inbox", since_hours=24*7, limit=10000):
        quick_key="cache:"+store.ref(row)
        if quick_key in seen: continue
        try:
            path=store.path(row)
            if not path: unavailable+=1; continue
            message=parse_emlx(path)
            if not body_available(message) and not any(p.get_content_type()=="message/delivery-status" for p in message.walk()):
                unavailable+=1; continue
            source=str(message.get("Message-ID", "")) or store.ref(row)
            key=hashlib.sha256((store.item(row)["account_uuid"]+source).encode()).hexdigest()
            if key in seen: continue
            events=classify(message,requests)
        except (MailError,OSError,ValueError): unavailable+=1; continue
        with ledger(state) as c, c:
            c.execute("insert or ignore into feedback_seen values(?,?)",(key,time.time()))
            c.execute("insert or ignore into feedback_seen values(?,?)",(quick_key,time.time()))
            for event in events:
                c.execute("insert or ignore into feedback values(?,?,?,?,?,?)",(key,event["request_id"],event["type"],event["recipient"],event["account"],time.time()))
                c.execute("insert or replace into suppressions values(?,?,?)",(event["recipient"],event["type"],time.time()))
                if event["type"]=="hard_bounce":
                    recent=[r[0] for r in c.execute("select request_id from sends where account=? and purpose='outreach' and state='accepted' order by created desc limit 20",(event["account"],))]
                    if recent:
                        count=c.execute("select count(distinct request_id) from feedback where type='hard_bounce' and request_id in ("+",".join("?" for _ in recent)+")",recent).fetchone()[0]
                        if count>=2:
                            c.execute("insert or replace into account_holds values(?,?,?)",(event["account"],"two hard bounces in the last 20 accepted outreach messages",time.time()))
                found.append(event)
        processed+=1
    return {"processed":processed,"feedback":found,"unavailable_messages":unavailable,"source":"local inbox cache; server freshness is unknown"}
