"""Read-only adapter for the Mail cache. No Apple events, network, or cache writes."""
import contextlib
import base64
import datetime as dt
import email
import email.policy
import glob
import hashlib
import html
from html.parser import HTMLParser
import os
import json
from pathlib import Path
import plistlib
import re
import sqlite3
from urllib.parse import unquote, urlsplit


class MailError(Exception):
    pass


ALIASES = {
    "inbox": {"inbox"}, "sent": {"sent", "sent mail", "sent items", "sent messages"},
    "drafts": {"drafts"}, "trash": {"trash", "bin", "deleted items", "deleted messages"},
    "junk": {"junk", "junk email", "spam"}, "archive": {"archive", "all mail"},
}


def unarchive(data):
    """Decode only plist primitives used by account metadata; never instantiate classes."""
    p = plistlib.loads(data)
    if not isinstance(p, dict) or "$objects" not in p:
        return p
    objects = p["$objects"]
    def get(x, depth=0):
        if depth > 20:
            raise ValueError("nested account metadata")
        if isinstance(x, plistlib.UID):
            return get(objects[x.data], depth + 1) if x.data else None
        if isinstance(x, dict):
            if "NS.keys" in x:
                return {get(k, depth+1): get(v, depth+1) for k,v in zip(x["NS.keys"], x["NS.objects"])}
            if "NS.objects" in x:
                return [get(v, depth+1) for v in x["NS.objects"]]
            if "NS.string" in x:
                return x["NS.string"]
            if "NS.relative" in x:
                return get(x["NS.relative"], depth+1)
        return x
    return get(p["$top"]["root"])


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bits = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr", "td", "h1", "h2", "h3"}:
            self.bits.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr", "td"}:
            self.bits.append("\n")
    def handle_data(self, data):
        if not self.hidden:
            self.bits.append(data)


def parse_emlx(path):
    with open(path, "rb") as f:
        first = f.readline(40)
        if not first.endswith(b"\n") or not first.strip().isdigit():
            raise MailError("invalid emlx length header")
        n = int(first)
        if n > 100 * 1024 * 1024:
            raise MailError("message exceeds 100 MiB parsing limit")
        raw = f.read(n)
        if len(raw) != n:
            raise MailError("truncated emlx payload")
    return email.message_from_bytes(raw, policy=email.policy.default)


def decoded(part):
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def message_text(msg):
    # Mail's compose backend sometimes creates an alternative containing several
    # HTML fragments around inline attachments. Its generated text alternative
    # contains quote markers and object placeholders that are absent on screen.
    # Render that complete HTML alternative, rather than only its first fragment.
    fragments = [decoded(p) for _, p in mime_parts(msg)
                 if p.get_content_type() == "text/html" and p.get_content_disposition() != "attachment"]
    if any("Apple-Mail-URLShareWrapperClass" in text for text in fragments):
        parser = TextHTML()
        for text in fragments:
            parser.feed(text)
        return re.sub(r"\n{3,}", "\n\n", "".join(parser.bits)).strip()
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = decoded(part)
    if part.get_content_type() == "text/html":
        parser = TextHTML()
        parser.feed(text)
        text = "".join(parser.bits)
    return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()


def mime_parts(msg, prefix=""):
    if msg.is_multipart() and msg.get_content_type() != "message/rfc822":
        for i, child in enumerate(msg.iter_parts(), 1):
            yield from mime_parts(child, f"{prefix}.{i}" if prefix else str(i))
    else:
        yield prefix or "1", msg


def attachment_payload(part):
    payload = part.get_payload(decode=True)
    if payload is None and part.get_content_type() == "message/rfc822":
        children = part.get_payload()
        if isinstance(children, list) and len(children) == 1:
            child = children[0]
            if list(child.keys()) or child.get_payload():
                return child.as_bytes(policy=email.policy.SMTP), True
    return payload, False


def is_placeholder(part):
    length=part.get("X-Apple-Content-Length")
    if length is None or attachment_payload(part)[0]: return False
    try: return int(length)>0
    except (TypeError,ValueError): return True


def body_available(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    return part is not None and not is_placeholder(part)


def attachment_parts(msg, path):
    root = path.parent.parent / "Attachments" / path.name.split(".")[0]
    result = []
    for number, part in mime_parts(msg):
        if not part.get_filename() and part.get_content_disposition() != "attachment":
            continue
        name = part.get_filename() or f"attachment-{number}"
        payload, reconstructed = attachment_payload(part)
        placeholder = is_placeholder(part)
        sidecar = None
        if placeholder:
            directory = root / number
            exact = directory / Path(name.replace("\\", "/")).name
            if exact.is_file() and exact.resolve().is_relative_to(root.resolve()):
                sidecar = exact
        size = sidecar.stat().st_size if sidecar else (len(payload) if payload is not None and not placeholder else None)
        result.append({"part": number, "name": name, "type": part.get_content_type(),
                       "available": size is not None, "size": size,
                       "reconstructed_message": reconstructed and not sidecar,
                       "_payload": payload if not placeholder else None, "_path": sidecar})
    return result


class Store:
    def __init__(self, root=None, accounts_db=None, enabled=None):
        if root is None:
            candidates = [p for p in (Path.home()/"Library/Mail").glob("V*")
                          if re.fullmatch(r"V\d+", p.name) and (p/"MailData/Envelope Index").is_file()]
            if not candidates:
                raise MailError("Mail cache is unavailable; Full Disk Access may be required")
            root = max(candidates, key=lambda p: int(p.name[1:]))
        self.root = Path(root)
        self.envelope = self.root / "MailData/Envelope Index"
        self.accounts_db = Path(accounts_db or Path.home()/"Library/Accounts/Accounts4.sqlite")
        self.enabled = {s.casefold() for s in enabled} if enabled else None
        self.accounts = self._accounts()
        with self.connect() as c:
            self.boxes = [dict(r) for r in c.execute("select ROWID as id,url,total_count,unread_count from mailboxes")]
        for b in self.boxes:
            url = urlsplit(b["url"])
            b["account_uuid"] = url.netloc
            b["path"] = unquote(url.path.lstrip("/"))
            b["name"] = b["path"].rsplit("/",1)[-1]

    @contextlib.contextmanager
    def connect(self):
        try:
            c = sqlite3.connect(self.envelope.resolve().as_uri()+"?mode=ro", uri=True, timeout=1)
            c.row_factory = sqlite3.Row
            c.execute("pragma query_only=ON")
            c.create_function("casefold", 1, lambda x: (x or "").casefold(), deterministic=True)
            yield c
        except sqlite3.Error as e:
            raise MailError(f"Mail cache read failed: {e}") from e
        finally:
            if "c" in locals():
                c.close()

    def _accounts(self):
        try:
            with contextlib.closing(sqlite3.connect(self.accounts_db.resolve().as_uri()+"?mode=ro", uri=True)) as c:
                c.row_factory = sqlite3.Row
                records = {r["Z_PK"]: dict(r) for r in c.execute("select a.*,t.ZIDENTIFIER as kind from ZACCOUNT a join ZACCOUNTTYPE t on t.Z_PK=a.ZACCOUNTTYPE")}
                properties = {}
                for r in c.execute("select ZOWNER,ZKEY,ZVALUE from ZACCOUNTPROPERTY where ZKEY in ('EmailAliases','IdentityEmailAddress')"):
                    try:
                        properties.setdefault(r[0], {})[r[1]] = unarchive(r[2])
                    except (ValueError, TypeError, KeyError, IndexError, plistlib.InvalidFileException):
                        continue
        except sqlite3.Error as e:
            raise MailError(f"Account metadata unavailable: {e}") from e
        result = {}
        for r in records.values():
            uuid = r["ZIDENTIFIER"]
            if not (self.root/uuid).is_dir() or r["kind"] not in {"com.apple.account.IMAP", "com.apple.account.Exchange", "com.apple.account.POP"}:
                continue
            parent = records.get(r["ZPARENTACCOUNT"], r)
            addresses = set()
            for record in (r, parent):
                prop = properties.get(record["Z_PK"], {})
                if record["kind"] != "com.apple.account.AppleAccount":
                    for value in [record["ZUSERNAME"], prop.get("IdentityEmailAddress")]:
                        if isinstance(value,str) and "@" in value:
                            addresses.add(value.casefold())
                for alias in prop.get("EmailAliases", []):
                    if not alias.get("IsEnabled", True):
                        continue
                    for address in alias.get("EmailAddresses", []):
                        if address.get("IsEnabled", True) and address.get("EmailAddress"):
                            addresses.add(address["EmailAddress"].casefold())
            name = r["ZACCOUNTDESCRIPTION"] or parent["ZACCOUNTDESCRIPTION"] or r["ZUSERNAME"] or uuid
            if self.enabled and not (self.enabled & ({uuid.casefold(), name.casefold()} | addresses)):
                continue
            auth_address = r["ZUSERNAME"] or parent["ZUSERNAME"] or ""
            result[uuid] = {"uuid": uuid, "name":name,"addresses":sorted(addresses),"kind":parent["kind"],
                            "auth_address":auth_address.casefold() if isinstance(auth_address,str) else ""}
        if not result:
            raise MailError("No configured mail accounts found in the local cache")
        return result

    def resolve(self, key):
        if key == "*":
            return list(self.accounts.values())
        matches = [a for a in self.accounts.values() if key.casefold() in {a["uuid"].casefold(), a["name"].casefold(), *a["addresses"]}]
        if len(matches) != 1:
            raise MailError("Account is unknown or ambiguous; use an address from `amail accounts`")
        return matches

    def mailboxes(self, account, mailbox="*"):
        uuids = {a["uuid"] for a in self.resolve(account)}
        boxes = [b for b in self.boxes if b["account_uuid"] in uuids]
        if mailbox != "*":
            want = mailbox.casefold()
            full = [b for b in boxes if b["path"].casefold() == want]
            if full and want not in ALIASES:
                return full
            boxes = [b for b in boxes if b["name"].casefold() in ALIASES.get(want,{want})]
            if want not in ALIASES and len({b["path"] for b in boxes}) > 1:
                raise MailError("Ambiguous mailbox name; use the full mailbox path")
        return boxes

    def query(self, account="*", mailbox="inbox", limit=20, unread=False, subject=None, sender=None, query=None, since_hours=0, ids=None,
              recipient=None, after=None, before=None, cursor=None, conversation_id=None):
        if not 1 <= limit <= 100000:
            raise MailError("limit must be between 1 and 100000")
        if since_hours < 0:
            raise MailError("since-hours cannot be negative")
        boxes = self.mailboxes(account, mailbox)
        if not boxes:
            return []
        boxids = [b["id"] for b in boxes]
        slots = ",".join("?" for _ in boxids)
        conditions = [f"m.ROWID in (select ROWID from messages where mailbox in ({slots}) union select message_id from labels where mailbox_id in ({slots}))", "m.deleted=0"]
        params = boxids + boxids
        if unread:
            conditions.append("m.read=0")
        if since_hours:
            conditions.append("m.date_received>=?")
            params.append(dt.datetime.now().timestamp()-since_hours*3600)
        for value, operator in [(after, ">="), (before, "<")]:
            if value:
                try:
                    stamp = dt.datetime.fromisoformat(value).timestamp()
                except ValueError as e:
                    raise MailError("Dates must use ISO format, for example 2026-09-07 or 2026-09-07T12:00:00+01:00") from e
                conditions.append("m.date_received" + operator + "?")
                params.append(stamp)
        if recipient is not None:
            conditions.append("exists(select 1 from recipients rp join addresses ra on ra.ROWID=rp.address where rp.message=m.ROWID and rp.type=0 and (instr(casefold(ra.address),?)>0 or instr(casefold(ra.comment),?)>0))")
            params.extend([recipient.casefold()] * 2)
        if conversation_id is not None:
            conditions.append("m.conversation_id=?")
            params.append(conversation_id)
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                stamp, last_id = int(data["date"]), int(data["id"])
            except (ValueError, TypeError, KeyError) as e:
                raise MailError("Invalid pagination cursor") from e
            conditions.append("(m.date_received<? or (m.date_received=? and m.ROWID<?))")
            params.extend([stamp, stamp, last_id])
        for needle, fields in [(subject,["coalesce(m.subject_prefix,'')||s.subject"]),(sender,["a.address","a.comment"]),(query,["coalesce(m.subject_prefix,'')||s.subject","a.address","a.comment"])]:
            if needle is not None:
                conditions.append("("+" or ".join(f"instr(casefold({f}),?)>0" for f in fields)+")")
                params.extend([needle.casefold()]*len(fields))
        if ids is not None:
            if not ids:
                return []
            conditions.append("m.ROWID in (select value from json_each(?))")
            params.append(json.dumps(list(ids)))
        sql = """select m.ROWID as id,m.message_id,m.global_message_id,m.remote_id,m.mailbox,m.date_received,m.date_sent,m.read,m.flagged,m.conversation_id,
                 coalesce(m.subject_prefix,'')||coalesce(s.subject,'') as subject,a.address,a.comment,b.url
                 from messages m join mailboxes b on b.ROWID=m.mailbox left join subjects s on s.ROWID=m.subject
                 left join addresses a on a.ROWID=m.sender where """ + " and ".join(conditions) + " order by m.date_received desc,m.ROWID desc limit ?"
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql,params+[limit])]

    @staticmethod
    def cursor(row):
        return base64.urlsafe_b64encode(json.dumps({"date": row["date_received"], "id": row["id"]}).encode()).decode().rstrip("=")

    def thread(self, ref):
        row = self.locate(ref)
        if not row["conversation_id"]:
            return [self.item(row)]
        return [self.item(r) for r in reversed(self.query(account=urlsplit(row["url"]).netloc, mailbox="*",
                         conversation_id=row["conversation_id"], limit=10000))]

    def ref(self, row):
        uuid = urlsplit(row["url"]).netloc
        fingerprint = hashlib.sha256(f'{row["message_id"]}:{row["global_message_id"]}:{row["date_sent"]}'.encode()).hexdigest()[:12]
        return f'mail:{uuid}:{row["id"]}:{fingerprint}'

    def locate(self, ref):
        bits = ref.split(":")
        if len(bits)!=4 or bits[0]!="mail" or not bits[2].isdigit():
            raise MailError("Use a message ref returned by `amail list` or `amail search`")
        rows = self.query(account=bits[1],mailbox="*",ids=[int(bits[2])],limit=1)
        if not rows or self.ref(rows[0]) != ref:
            raise MailError("Message ref is stale or no longer available")
        return rows[0]

    def item(self, row):
        uuid = urlsplit(row["url"]).netloc
        date = row["date_received"] or row["date_sent"] or 0
        return {"ref":self.ref(row),"id":row["id"],"account":self.accounts[uuid]["name"],"account_uuid":uuid,
                "source":"local Mail cache","server_freshness":"unknown",
                "storage_mailbox":unquote(urlsplit(row["url"]).path.lstrip("/")),"date":dt.datetime.fromtimestamp(date).astimezone().isoformat(),
                "from":email.utils.formataddr((row["comment"] or "",row["address"] or "")),"subject":row["subject"],"read":bool(row["read"]),"flagged":bool(row["flagged"])}

    def path(self, row):
        url = urlsplit(row["url"])
        box = self.root / url.netloc
        for part in url.path.lstrip("/").split("/"):
            box /= unquote(part)+".mbox"
        if not box.resolve().is_relative_to((self.root/url.netloc).resolve()):
            raise MailError("Invalid mailbox storage path")
        rid = row["id"]
        digits = "/".join(reversed(str(rid//1000))) if rid>=1000 else ""
        names = (f"{rid}.emlx", f"{rid}.partial.emlx")
        for recursive in (False, True):
            for name in names:
                matches = glob.glob(os.path.join(glob.escape(str(box)),"*","Data","**" if recursive else digits,"Messages",name),recursive=recursive)
                if len(matches)>1:
                    raise MailError("Multiple storage files for message; refusing an ambiguous read")
                if matches:
                    return Path(matches[0])
        return None

    def read(self, ref):
        row = self.locate(ref)
        path = self.path(row)
        if path is None:
            raise MailError("Message body is not available locally; no slow fallback was attempted")
        msg = parse_emlx(path)
        parts = attachment_parts(msg,path)
        item = self.item(row)
        body = message_text(msg)
        available = body_available(msg)
        item.update({"to":str(msg.get("To","")),"cc":str(msg.get("Cc","")),"message_id":str(msg.get("Message-ID","")),
                     "reply_to":str(msg.get("Reply-To","")),"references":str(msg.get("References","")),"in_reply_to":str(msg.get("In-Reply-To","")),"body":body,
                     "body_available":available,"storage_partial":path.name.endswith(".partial.emlx"),"attachments_complete":all(p["available"] for p in parts),
                     "attachments":[{k:v for k,v in p.items() if not k.startswith("_")} for p in parts],"file":str(path),
                     "warnings":[type(d).__name__ for d in msg.defects]})
        return item, parts

    def extract(self, ref, output):
        item, parts = self.read(ref)
        missing = [p["part"] for p in parts if not p["available"]]
        if missing:
            raise MailError("Attachments are not downloaded locally: parts "+", ".join(missing))
        directory = Path(output).expanduser().resolve()
        directory.mkdir(parents=True,exist_ok=True)
        saved = []
        for part in parts:
            name = Path(part["name"].replace("\\","/")).name
            if name in {"", ".", ".."}:
                name = "attachment-"+part["part"]
            payload = part["_path"].read_bytes() if part["_path"] else part["_payload"]
            if payload is None:
                raise MailError("Attachment payload unavailable")
            for suffix in range(10000):
                dest = directory / (name if not suffix else f"{Path(name).stem}-{suffix}{Path(name).suffix}")
                try:
                    fd = os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                    break
                except FileExistsError:
                    continue
            else:
                raise MailError("Too many attachment filename collisions")
            with os.fdopen(fd,"wb") as f:
                f.write(payload)
            saved.append({"file":str(dest),"bytes":len(payload),"sha256":hashlib.sha256(payload).hexdigest()})
        return saved
