"""Reconcile observable Sent items with the send ledger without claiming a server quota."""
import email.utils
import json
import time
from local_store import MailError, parse_emlx
from mail_sender import matches_content


def observed_usage(store, connection, account):
    records = [dict(r) for r in connection.execute("select request_id,created,recipients,verification,result from sends where account=? and state!='rejected' and created>=?",
                                                   (account["uuid"],time.time()-86400))]
    for record in records:
        record["verification"]=json.loads(record["verification"])
        record["result"]=json.loads(record["result"])
    matched=set(); seen=set(); recipients=0; missing=0
    for row in store.query(account=account["uuid"],mailbox="sent",since_hours=24,limit=100000):
        try:
            item,parts=store.read(store.ref(row))
            path=store.path(row);message=parse_emlx(path)
        except (MailError,OSError,ValueError):
            missing+=1;continue
        identity=item["message_id"] or item["ref"]
        if identity in seen:continue
        seen.add(identity)
        recipients+=len({a.casefold() for _,a in email.utils.getaddresses([item["to"],item["cc"],str(message.get("Bcc",""))]) if a})
        request_header=str(message.get("X-Amail-Request-ID",""))
        exact=[r for r in records if r["request_id"] not in matched and
               (r["request_id"]==request_header or r["result"].get("message_id")==identity)]
        if not exact:
            exact=[r for r in records if r["request_id"] not in matched and abs(r["created"]-(row["date_sent"] or row["date_received"] or 0))<=120
                   and r["verification"].get("subject")==row["subject"] and r["verification"].get("body_hash")
                   and matches_content(store,item,parts,r["verification"])]
        if len(exact)==1:matched.add(exact[0]["request_id"])
    return {"ledger_messages_24h":len(records),"observable_sent_messages_24h":len(seen),"matched_messages":len(matched),
            "unmatched_ledger_messages":len(records)-len(matched),"unmatched_cached_sent_messages":len(seen)-len(matched),
            "combined_message_count_range":[max(len(records),len(seen)),len(records)+len(seen)-len(matched)],
            "observable_sent_recipients":recipients,"unavailable_cached_messages":missing,
            "limits_of_observation":"Local Sent cache may lag or omit deleted mail and BCC recipients. Tenant usage and provider anti-abuse limits are not visible."}
