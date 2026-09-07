"""An explicit, rebuildable index of decoded local message bodies."""
import contextlib
import datetime as dt
import os
from pathlib import Path
import sqlite3
import time
from local_store import MailError, parse_emlx, message_text, body_available


class BodyIndex:
    def __init__(self, path):
        self.path = Path(path)

    def refresh(self, store):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd = os.open(self.path,os.O_CREAT|os.O_WRONLY,0o600)
        os.close(fd)
        with contextlib.closing(sqlite3.connect(self.path,timeout=1)) as c:
            c.execute("pragma journal_mode=WAL")
            c.executescript("""create table if not exists entries(id integer primary key,ref text,signature text,body_available integer);
                             create virtual table if not exists bodies using fts5(body,tokenize='trigram');
                             create table if not exists metadata(key text primary key,value text);""")
            version = c.execute("select value from metadata where key='renderer_version'").fetchone()
            reset = not version or version[0] != '2'
            existing = {} if reset else {r[0]: r[1:] for r in c.execute("select id,ref,signature from entries")}
            seen = set()
            counts = {"indexed":0,"unchanged":0,"unavailable":0,"errors":0}
            started = time.perf_counter()
            with c:
                if reset:
                    c.execute("delete from entries")
                    c.execute("delete from bodies")
                    c.execute("insert or replace into metadata values('renderer_version','2')")
                for row in store.query(mailbox="*",limit=100000):
                    rid = row["id"]
                    seen.add(rid)
                    try:
                        path = store.path(row)
                        ref = store.ref(row)
                        if path is None:
                            signature, body, available = "missing", "", False
                        else:
                            stat = path.stat()
                            signature = f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"
                            if existing.get(rid) == (ref,signature):
                                counts["unchanged"] += 1
                                continue
                            msg = parse_emlx(path)
                            body = message_text(msg)
                            available = body_available(msg)
                        counts["indexed"] += 1
                        counts["unavailable"] += not available
                        c.execute("delete from bodies where rowid=?",(rid,))
                        c.execute("insert or replace into entries values(?,?,?,?)",(rid,ref,signature,available))
                        c.execute("insert into bodies(rowid,body) values(?,?)",(rid,body.casefold()))
                    except (MailError,OSError,ValueError):
                        counts["errors"] += 1
                        c.execute("delete from entries where id=?",(rid,))
                        c.execute("delete from bodies where rowid=?",(rid,))
                for rid in existing.keys()-seen:
                    c.execute("delete from entries where id=?",(rid,))
                    c.execute("delete from bodies where rowid=?",(rid,))
                c.execute("insert or replace into metadata values('updated_at',?)",(dt.datetime.now(dt.timezone.utc).isoformat(),))
            counts["seconds"] = round(time.perf_counter()-started,3)
            counts["unavailable"] = c.execute("select count(*) from entries where body_available=0").fetchone()[0]
            counts["total_entries"] = c.execute("select count(*) from entries").fetchone()[0]
            return counts

    def search(self, text):
        if not self.path.exists():
            raise MailError("Decoded body index does not exist; run `amail index` once")
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as c:
            stamp = c.execute("select value from metadata where key='updated_at'").fetchone()
            version = c.execute("select value from metadata where key='renderer_version'").fetchone()
            if not stamp or not version or version[0] != '2':
                raise MailError("Body index is not ready for this version; run amail index")
            needle = text.casefold()
            if len(needle)>=3:
                phrase = '"'+needle.replace('"','""')+'"'
                sql,params = "select rowid from bodies where bodies match ?",(phrase,)
            else:
                sql,params = "select rowid from bodies where instr(body,?)>0",(needle,)
            ids = [r[0] for r in c.execute(sql,params)]
            refs = {r[0]:r[1] for r in c.execute("select id,ref from entries")}
            unavailable = c.execute("select count(*) from entries where body_available=0").fetchone()[0]
            return ids, refs, {"updated_at":stamp[0] if stamp else None,"unavailable_bodies":unavailable,
                              "freshness":"snapshot; run amail index to refresh"}
