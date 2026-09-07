"""Command-line interface for amail."""
import argparse
import email.utils
import json
import os
import plistlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from local_store import Store, MailError, message_text, attachment_parts, body_available

HERE = Path(__file__).resolve().parent


def parser():
    common = argparse.ArgumentParser(add_help=False)
    style = common.add_mutually_exclusive_group()
    for flag in ("json", "plain", "tsv"):
        style.add_argument("--" + flag, action="store_true", default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="amail", description="Fast email reads and verified, paced sending.", parents=[common])
    p.add_argument("--version", action="version", version="amail 0.1.0")
    sub = p.add_subparsers(dest="command", required=True)
    def command(name, **kwargs): return sub.add_parser(name, parents=[common], **kwargs)
    account_list = command("accounts"); account_list.add_argument("--available", action="store_true")
    setup = command("setup"); setup.add_argument("--accounts", nargs="+"); setup.add_argument("--name")
    setup.add_argument("--gmail-client-json"); setup.add_argument("--connect", action="store_true")
    command("config", help="Show the private configuration and its path")
    boxes = command("mailboxes"); boxes.add_argument("account")
    unread = command("unread"); unread.add_argument("--account", default="*")
    for name in ("doctor", "sync", "limits"):
        q = command(name); q.add_argument("--account", default="*")
    connect = command("connect"); connect.add_argument("account")
    for name in ("list", "search"):
        q = command(name)
        if name == "search":
            q.add_argument("query"); q.add_argument("--body", action="store_true")
        q.add_argument("--account", default="*")
        q.add_argument("--mailbox", default="inbox" if name == "list" else "*")
        q.add_argument("--limit", type=int, default=20)
        q.add_argument("--sender", "--from", dest="sender")
        q.add_argument("--to", dest="recipient")
        q.add_argument("--subject")
        q.add_argument("--unread", action="store_true")
        q.add_argument("--since-hours", type=float, default=0)
        q.add_argument("--after"); q.add_argument("--before")
        q.add_argument("--cursor"); q.add_argument("--page", action="store_true")
    read = command("read"); read.add_argument("ref"); read.add_argument("--body-only", action="store_true"); read.add_argument("--fetch", action="store_true")
    thread = command("thread"); thread.add_argument("ref")
    att = command("attachments"); att.add_argument("ref"); att.add_argument("--out"); att.add_argument("--fetch", action="store_true")
    command("index", help="Refresh the decoded body search snapshot")
    send = command("send")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", nargs="+", required=True)
    send.add_argument("--cc", nargs="*", default=[]); send.add_argument("--bcc", nargs="*", default=[])
    send.add_argument("--subject", required=True)
    body = send.add_mutually_exclusive_group(); body.add_argument("--body"); body.add_argument("--body-file")
    send.add_argument("--attach", nargs="*", default=[]); send.add_argument("--name", default=None)
    send.add_argument("--dry-run", action="store_true"); send.add_argument("--request-id")
    send.add_argument("--timeout", type=float, default=30)
    send.add_argument("--cap", type=int, help="Optional stricter rolling recipient cap")
    send.add_argument("--purpose", choices=["personal", "outreach"], default="personal")
    send.add_argument("--retry-rejected", action="store_true", help="Explicitly retry a definitively rejected request; never an ambiguous one")
    for name in ("status", "send-status"):
        status = command(name); status.add_argument("request_id")
    for name in ("sent", "ledger"):
        q = command(name); q.add_argument("--account", default="*"); q.add_argument("--hours", type=float, default=24)
    for name in ("mark", "flag", "move", "archive", "trash", "delete", "restore"):
        q = command(name); q.add_argument("ref")
        if name == "mark": q.add_argument("state", choices=["read", "unread"])
        if name == "flag": q.add_argument("state", choices=["on", "off"])
        if name == "move": q.add_argument("mailbox")
    for name in ("reply", "forward"):
        q = command(name); q.add_argument("ref"); q.add_argument("--from", dest="sender")
        body = q.add_mutually_exclusive_group(); body.add_argument("--body"); body.add_argument("--body-file")
        q.add_argument("--send", action="store_true")
        if name == "reply": q.add_argument("--all", action="store_true")
        else: q.add_argument("--to", nargs="+", required=True)
    draft = command("draft"); ds = draft.add_subparsers(dest="action", required=True)
    for name in ("create", "update", "show", "list", "send", "discard"):
        q = ds.add_parser(name, parents=[common])
        if name in {"update", "show", "send", "discard"}: q.add_argument("id")
        if name in {"create", "update"}: q.add_argument("file")
        if name == "send": q.add_argument("--dry-run", action="store_true")
    batch = command("batch"); bs = batch.add_subparsers(dest="action", required=True)
    for name in ("plan", "show", "run", "status", "pause", "resume", "cancel", "export"):
        q = bs.add_parser(name, parents=[common])
        if name == "plan":
            q.add_argument("file"); q.add_argument("--policy", choices=["outreach"], default="outreach")
        else: q.add_argument("id")
        if name == "export": q.add_argument("--out", required=True)
    policy = command("policy"); ps = policy.add_subparsers(dest="action", required=True)
    q = ps.add_parser("show", parents=[common]); q.add_argument("name", nargs="?", default="outreach", choices=["outreach", "gmail", "exchange", "imperial"])
    q = ps.add_parser("release", parents=[common]); q.add_argument("--account", required=True); q.add_argument("--reason", required=True)
    suppress = command("suppress"); ss = suppress.add_subparsers(dest="action", required=True)
    for name in ("add", "list", "remove"):
        q = ss.add_parser(name, parents=[common])
        if name != "list": q.add_argument("address")
        if name == "add": q.add_argument("--reason", required=True)
    return p


def conversation_draft(args, store, state, here):
    from workflows import draft_create, arguments
    from mail_sender import send
    from mail_operations import load_message
    source, parts = load_message(store,args.ref,state,here)
    account = store.accounts[source["account_uuid"]]
    sender = args.sender or (account.get("auth_address") or account["addresses"][0])
    own = set(store.resolve(sender)[0]["addresses"])
    def addresses(value): return [a.casefold() for _, a in email.utils.getaddresses([value]) if a]
    intro = args.body if args.body is not None else Path(args.body_file).read_text() if args.body_file else ""
    if args.command == "reply":
        recipients = addresses(source["reply_to"] or source["from"])
        if set(recipients).issubset(own): recipients = addresses(source["to"])
        cc = []
        if args.all:
            recipients += addresses(source["to"])
            cc = [a for a in addresses(source["cc"]) if a not in own]
        recipients = list(dict.fromkeys(a for a in recipients if a not in own))
        cc = list(dict.fromkeys(a for a in cc if a not in recipients))
        subject = source["subject"] if source["subject"].casefold().startswith("re:") else "Re: " + source["subject"]
        quote = "\n".join("> " + line for line in source["body"].splitlines())
        body = intro + f'\n\nOn {source["date"]}, {source["from"]} wrote:\n' + quote
        message = {"from": sender, "to": recipients, "cc": cc, "subject": subject, "body": body, "reply_to_ref": args.ref}
    else:
        attachments = []
        if parts:
            from mail_operations import fetch_attachments
            directory = state / "forward-attachments" / str(time.time_ns())
            if all(p["available"] for p in parts) and not args.ref.startswith("gmail:"): saved = store.extract(args.ref, directory)
            else: saved = fetch_attachments(store, args.ref, directory, state, here)
            attachments = [a["file"] for a in saved]
        body = intro + "\n\n---------- Forwarded message ----------\n" + "\n".join(
            f"{key}: {source[field]}" for key, field in [("From", "from"), ("To", "to"), ("Date", "date"), ("Subject", "subject")]) + "\n\n" + source["body"]
        message = {"from": sender, "to": args.to, "subject": "Fwd: " + source["subject"], "body": body, "attachments": attachments}
    draft = draft_create(store, state, message, Path.cwd())
    if args.send:
        return send(store, arguments(draft["message"], request_id=draft["id"] + "-1"), state, here)
    return draft


def run(args, store, state, here=HERE):
    command = args.command
    if command == "accounts":
        result = []
        with store.connect() as c:
            for account in store.accounts.values():
                ids = [b["id"] for b in store.mailboxes(account["uuid"])]
                slots = ",".join("?" for _ in ids)
                count = c.execute(f"select count(*) from messages where deleted=0 and mailbox in ({slots})", ids).fetchone()[0]
                result.append(dict(account, messages=count))
        return result
    if command == "mailboxes": return store.mailboxes(args.account)
    if command == "unread":
        return [{"account": a["name"], "unread": len(store.query(account=a["uuid"], unread=True, limit=100000))} for a in store.resolve(args.account)]
    if command in {"list", "search"}:
        fields = ("account", "mailbox", "limit", "sender", "recipient", "subject", "unread", "since_hours", "after", "before", "cursor")
        kwargs = {key: getattr(args, key) for key in fields}
        page = args.page or args.cursor is not None
        if page:
            if not 1 <= args.limit <= 10000: raise MailError("Page size must be between 1 and 10000")
            kwargs["limit"] += 1
        index = None
        if command == "search" and args.body:
            from body_index import BodyIndex
            ids, refs, index = BodyIndex(state / "bodies.sqlite").search(args.query)
            rows = [r for r in store.query(ids=ids, **kwargs) if refs.get(r["id"]) == store.ref(r)]
        else:
            rows = store.query(query=args.query if command == "search" else None, **kwargs)
        more = page and len(rows) > args.limit
        rows = rows[:args.limit] if page else rows
        messages = [store.item(r) for r in rows]
        if page or index:
            return {"messages": messages, "next_cursor": store.cursor(rows[-1]) if more else None,
                    "source": "local cache", "queried_at": time.time(), **({"index": index} if index else {})}
        return messages
    if command == "thread":
        from mail_operations import thread
        return thread(store,args.ref,state,here)
    if command in {"read", "attachments"}:
        if args.fetch or args.ref.startswith("gmail:"):
            from mail_operations import load_message, fetch_attachments
            if command == "attachments" and args.out:
                return fetch_attachments(store, args.ref, args.out, state, here)
            item, parts = load_message(store,args.ref,state,here,fetch=True)
        else:
            item, parts = store.read(args.ref)
        if command == "attachments":
            return store.extract(args.ref, args.out) if args.out else item["attachments"]
        if args.body_only:
            if not item["body_available"]: raise MailError("Body is not available; try --fetch")
            return item["body"]
        return item
    if command == "index":
        from body_index import BodyIndex
        return BodyIndex(state / "bodies.sqlite").refresh(store)
    if command in {"send", "status", "send-status", "sent", "ledger"}:
        from mail_sender import send, status, ledger, describe
        if command == "send": return send(store, args, state, here)
        from feedback import scan
        scan(store,state)
        if command in {"status", "send-status"}: return status(args.request_id, state, store)
        ids = [a["uuid"] for a in store.resolve(args.account)]
        with ledger(state) as c:
            rows = c.execute("select * from sends where created>=? and account in (" + ",".join("?" for _ in ids) + ") order by created desc", [time.time()-args.hours*3600] + ids).fetchall()
        return [describe(r,state,store) for r in rows]
    if command in {"mark", "flag", "move", "archive", "trash", "delete", "restore"}:
        from mail_operations import mutate
        action = args.state if command == "mark" else ("flag" if args.state == "on" else "unflag") if command == "flag" else "trash" if command == "delete" else command
        return mutate(store, args.ref, action, state, here, destination=getattr(args, "mailbox", None))
    if command in {"reply", "forward"}: return conversation_draft(args, store, state, here)
    if command == "draft":
        from workflows import draft_create, draft_get, draft_discard, arguments, initialise
        from mail_sender import send, ledger
        if args.action in {"create", "update"}:
            if args.action == "update": draft_get(state, args.id)
            file = Path(args.file).resolve()
            return draft_create(store, state, json.loads(file.read_text()), file.parent, getattr(args, "id", None))
        if args.action == "show": return draft_get(state, args.id)
        if args.action == "discard": return draft_discard(state, args.id)
        if args.action == "list":
            with ledger(state) as c:
                initialise(c)
                return [dict(r) for r in c.execute("select id,revision,created from drafts order by created desc")]
        draft = draft_get(state, args.id)
        return send(store, arguments(draft["message"], request_id=f'{args.id}-{draft["revision"]}', dry_run=args.dry_run), state, here)
    if command == "batch":
        from workflows import batch_plan, batch_show, batch_run, batch_control
        if args.action == "plan": return batch_plan(store, state, args.file)
        if args.action in {"show", "status"}: return batch_show(state, args.id, include_messages=args.action == "show")
        if args.action in {"run", "resume"}: return batch_run(store, state, here, args.id, resume=args.action == "resume")
        if args.action in {"pause", "cancel"}: return batch_control(state, args.id, args.action)
        plan = batch_show(state, args.id)
        with open(args.out, "x") as f: json.dump(plan, f, indent=2, ensure_ascii=False)
        return {"id": args.id, "file": str(Path(args.out).resolve())}
    if command in {"policy", "limits", "suppress"}:
        from policy import PROFILES, usage
        from mail_sender import ledger, address
        if command == "policy":
            if args.action == "show":
                profiles = getattr(store, "policy_profiles", PROFILES)
                return profiles if args.name == "outreach" else profiles["exchange" if args.name == "imperial" else args.name]
            accounts=store.resolve(args.account)
            with ledger(state) as c,c:
                c.execute("create table if not exists policy_reviews(account text,reason text,created real)")
                for account in accounts:
                    c.execute("delete from account_holds where account=?",(account["uuid"],))
                    c.execute("insert into policy_reviews values(?,?,?)",(account["uuid"],args.reason,time.time()))
            return {"released":[a["name"] for a in accounts],"reason":args.reason,"suppression_list_unchanged":True}
        if command == "limits":
            from feedback import scan
            feedback = scan(store, state)
        with ledger(state) as c:
            if command == "limits":
                from usage import observed_usage
                return {"accounts":[dict(usage(c,a),observed_usage=observed_usage(store,c,a)) for a in store.resolve(args.account)],"feedback_scan":feedback}
            if args.action == "list": return [dict(r) for r in c.execute("select * from suppressions order by address")]
            value = address(args.address)
            with c:
                if args.action == "add": c.execute("insert or replace into suppressions values(?,?,?)", (value, args.reason, time.time()))
                else: c.execute("delete from suppressions where address=?", (value,))
            return {"address": value, "suppressed": args.action == "add"}
    if command in {"doctor", "sync", "connect"}:
        from mail_operations import bridge
        from gmail_backend import Gmail
        accounts = store.resolve(args.account)
        result = []
        for account in accounts:
            if command == "connect":
                if account["kind"] == "com.apple.account.Google":
                    from configuration import credentials_path
                    if not credentials_path().is_file():
                        raise MailError("Register your own OAuth client first: amail setup --accounts ADDRESS --gmail-client-json FILE")
                    p = subprocess.run(["gog", "auth", "add", (account.get("auth_address") or account["addresses"][0]), "--client", "amail", "--services", "gmail", "--force-consent"], stdout=sys.stderr, stderr=sys.stderr, timeout=660)
                    if p.returncode: raise MailError("Gmail sign-in did not complete")
                    Gmail((account.get("auth_address") or account["addresses"][0]), state).token(force=True)
                    result.append({"account": account["name"], "connected": True, "provider": "gmail"})
                else:
                    result.append({"account": account["name"], **bridge(account["uuid"], "doctor", here, state),
                        "route": "existing authorised Apple Mail account"})
            elif command == "sync":
                result.append({"account": account["name"], **bridge(account["uuid"], "sync", here, state)})
            else:
                info = {"account": account["name"], "local_cache": "readable", "aliases": account["addresses"]}
                try:
                    if account["kind"] == "com.apple.account.Google":
                        profile = Gmail((account.get("auth_address") or account["addresses"][0]), state).profile()
                        info.update(send_route="gmail_api", authenticated_as=profile["emailAddress"])
                    else: info.update(send_route="apple_script", automation=bridge(account["uuid"], "doctor", here, state))
                    if account["kind"] == "com.apple.account.Exchange":
                        preferences=Path.home()/"Library/Group Containers/group.com.apple.mail/Library/Preferences/group.com.apple.mail.plist"
                        settings=plistlib.loads(preferences.read_bytes()) if preferences.exists() else {}
                        info["undo_send_delay_seconds"]=settings.get("UndoSendDelayTime",10)
                        if info["undo_send_delay_seconds"]:
                            info["send_delay_note"]="Mail holds outgoing messages for Undo Send; choose Off in Mail's Composing settings for immediate submission"
                except MailError as e: info["connection_error"] = str(e)
                result.append(info)
        return result
    raise MailError("Unsupported command")


def output(value, args):
    if isinstance(value, str): print(value); return
    if getattr(args, "tsv", False) or getattr(args, "plain", False):
        if isinstance(value, list):
            for row in value:
                print("\t".join(str(v).replace("\t", " ").replace("\n", " ") for v in row.values()))
        elif isinstance(value, dict):
            for key, item in value.items(): print(f"{key}: {item}")
    else: print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    args = parser().parse_args()
    from configuration import state_path, config_path, load, apply, setup
    state = state_path()
    try:
        if args.command == "setup":
            result = setup(args, state)
            if args.connect:
                config = load(state)
                store = apply(Store(enabled=config["accounts"]), config)
                result["connections"] = run(argparse.Namespace(command="connect", account="*"), store, state)
            output(result, args)
            return 0
        if args.command == "config":
            output({"path": str(config_path(state)), "config": load(state)}, args)
            return 0
        if args.command == "accounts" and args.available:
            from configuration import SUPPORTED
            output([a for a in Store().accounts.values() if a["kind"] in SUPPORTED], args)
            return 0
        config = load(state)
        store = apply(Store(enabled=config["accounts"]), config)
        if args.command == "send" and args.name is None: args.name = config["display_name"]
        result = run(args, store, state)
        output(result, args)
        if isinstance(result, dict) and (result.get("state") in {"outcome_unknown", "rejected", "provider_acceptance_unverified"}
                or result.get("state") == "paused" and args.command == "batch" and args.action in {"run", "resume"}):
            return 2
        return 0
    except KeyboardInterrupt:
        return 130
    except (MailError, OSError, sqlite3.Error, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        error = {"error": str(e)}
        if hasattr(e, "until"): error.update(retry_at=e.until, reason=e.reason)
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 3 if hasattr(e, "until") else 1
