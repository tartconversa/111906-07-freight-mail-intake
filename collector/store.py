"""采集程序自有持久状态:SQLite + 受限原件目录。

状态与来源服务完全分离;来源重启/清空不影响本库。原始 MIME 与附件
写入 0700 目录下的 0600 文件,普通查询只读 SQLite 中的脱敏视图。
"""
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(
  name TEXT PRIMARY KEY, uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, summary TEXT);
CREATE TABLE IF NOT EXISTS messages(
  hash TEXT PRIMARY KEY, message_id TEXT, sender TEXT, subject TEXT,
  raw_path TEXT NOT NULL, first_cycle INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS locations(
  msg_hash TEXT NOT NULL, folder TEXT NOT NULL, uid INTEGER NOT NULL,
  uidvalidity INTEGER NOT NULL, cycle INTEGER NOT NULL,
  PRIMARY KEY(msg_hash, folder, uid));
CREATE TABLE IF NOT EXISTS attachments(
  hash TEXT PRIMARY KEY, msg_hash TEXT NOT NULL, filename TEXT, content_type TEXT,
  path TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS documents(
  doc_key TEXT NOT NULL, content_hash TEXT NOT NULL, type TEXT NOT NULL,
  carrier TEXT NOT NULL, shipment TEXT NOT NULL, version INTEGER NOT NULL,
  boxes TEXT NOT NULL, status TEXT NOT NULL,
  first_msg TEXT NOT NULL, first_att TEXT NOT NULL, first_cycle INTEGER NOT NULL,
  PRIMARY KEY(doc_key, content_hash));
CREATE TABLE IF NOT EXISTS doc_sources(
  doc_key TEXT NOT NULL, content_hash TEXT NOT NULL, msg_hash TEXT NOT NULL,
  att_hash TEXT NOT NULL, PRIMARY KEY(doc_key, content_hash, msg_hash, att_hash));
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rkey TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, summary TEXT NOT NULL, detail TEXT,
  msg_hash TEXT, doc_key TEXT, created_cycle INTEGER NOT NULL,
  resolved_cycle INTEGER, resolution TEXT);
CREATE TABLE IF NOT EXISTS handoffs(
  reference TEXT NOT NULL, box TEXT NOT NULL, carrier TEXT NOT NULL,
  shipment TEXT NOT NULL, receipt TEXT, state TEXT NOT NULL, updated_cycle INTEGER,
  PRIMARY KEY(reference, box));
CREATE TABLE IF NOT EXISTS stops(
  reference TEXT PRIMARY KEY, handoff_reference TEXT, carrier TEXT, shipment TEXT,
  box TEXT NOT NULL, reason TEXT, state TEXT NOT NULL, updated_cycle INTEGER);
"""


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.store_dir = self.dir / 'store'
        self.store_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.store_dir, 0o700)
        self.db = sqlite3.connect(str(self.dir / 'collector.db'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 周期 ----
    def begin_cycle(self):
        cur = self.db.execute(
            "INSERT INTO cycles(started) VALUES(?)", (time.strftime('%Y-%m-%dT%H:%M:%S'),))
        self.db.commit()
        return cur.lastrowid

    def end_cycle(self, cycle, summary):
        self.db.execute("UPDATE cycles SET summary=? WHERE id=?",
                        (json.dumps(summary, ensure_ascii=False, sort_keys=True), cycle))
        self.db.commit()

    # ---- 文件夹 / UID 跟踪 ----
    def get_folder(self, name):
        row = self.db.execute("SELECT * FROM folders WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def set_folder(self, name, uidvalidity, last_uid):
        self.db.execute(
            "INSERT INTO folders(name,uidvalidity,last_uid) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid",
            (name, uidvalidity, last_uid))
        self.db.commit()

    # ---- 原件 ----
    def save_raw(self, digest, data, kind):
        path = self.store_dir / (kind + '-' + digest + ('.eml' if kind == 'msg' else '.bin'))
        if not path.exists():
            path.write_bytes(data)
            os.chmod(path, 0o600)
        return str(path)

    def has_message(self, msg_hash):
        return self.db.execute("SELECT 1 FROM messages WHERE hash=?", (msg_hash,)).fetchone() is not None

    def add_message(self, msg_hash, headers, raw_path, cycle):
        self.db.execute(
            "INSERT OR IGNORE INTO messages(hash,message_id,sender,subject,raw_path,first_cycle) "
            "VALUES(?,?,?,?,?,?)",
            (msg_hash, headers['message_id'], headers['sender'], headers['subject'], raw_path, cycle))
        self.db.commit()

    def add_location(self, msg_hash, folder, uid, uidvalidity, cycle):
        self.db.execute(
            "INSERT OR IGNORE INTO locations(msg_hash,folder,uid,uidvalidity,cycle) VALUES(?,?,?,?,?)",
            (msg_hash, folder, uid, uidvalidity, cycle))
        self.db.commit()

    def message_locations(self, msg_hash):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM locations WHERE msg_hash=? ORDER BY cycle", (msg_hash,))]

    def list_messages(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM messages ORDER BY first_cycle,hash")]

    def find_message(self, prefix):
        rows = self.list_messages()
        hit = [r for r in rows if r['hash'].startswith(prefix) or r['message_id'] == prefix]
        return hit[0] if len(hit) == 1 else None

    # ---- 附件与单证 ----
    def add_attachment(self, att_hash, msg_hash, filename, content_type, path, status, error=None):
        self.db.execute(
            "INSERT OR IGNORE INTO attachments(hash,msg_hash,filename,content_type,path,status,error) "
            "VALUES(?,?,?,?,?,?,?)",
            (att_hash, msg_hash, filename, content_type, path, status, error))
        self.db.commit()

    def attachments_of(self, msg_hash):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM attachments WHERE msg_hash=? ORDER BY hash", (msg_hash,))]

    def get_document_rows(self, doc_key):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM documents WHERE doc_key=? ORDER BY version", (doc_key,))]

    def add_document(self, doc, content_hash, status, msg_hash, att_hash, cycle):
        from .mime_parse import doc_key
        self.db.execute(
            "INSERT OR IGNORE INTO documents(doc_key,content_hash,type,carrier,shipment,version,"
            "boxes,status,first_msg,first_att,first_cycle) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (doc_key(doc), content_hash, doc['type'], doc['carrier'], doc['shipment'],
             doc['version'], json.dumps(doc['boxes'], ensure_ascii=False), status,
             msg_hash, att_hash, cycle))
        self.db.commit()

    def set_document_status(self, doc_key, content_hash, status):
        self.db.execute("UPDATE documents SET status=? WHERE doc_key=? AND content_hash=?",
                        (status, doc_key, content_hash))
        self.db.commit()

    def add_doc_source(self, doc_key, content_hash, msg_hash, att_hash):
        self.db.execute(
            "INSERT OR IGNORE INTO doc_sources(doc_key,content_hash,msg_hash,att_hash) VALUES(?,?,?,?)",
            (doc_key, content_hash, msg_hash, att_hash))
        self.db.commit()

    def documents(self, carrier=None, shipment=None):
        sql, args = "SELECT * FROM documents", ()
        if carrier is not None:
            sql, args = "SELECT * FROM documents WHERE carrier=? AND shipment=?", (carrier, shipment)
        rows = [dict(r) for r in self.db.execute(sql + " ORDER BY doc_key", args)]
        for r in rows:
            r['boxes'] = json.loads(r['boxes'])
        return rows

    def shipments(self):
        return [tuple(r) for r in self.db.execute(
            "SELECT DISTINCT carrier,shipment FROM documents ORDER BY carrier,shipment")]

    def doc_sources(self, doc_key, content_hash):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM doc_sources WHERE doc_key=? AND content_hash=?", (doc_key, content_hash))]

    # ---- 待人工核实 ----
    def open_review(self, rkey, kind, summary, detail, msg_hash, doc_key, cycle):
        if self.db.execute("SELECT 1 FROM reviews WHERE rkey=? AND status='open'", (rkey,)).fetchone():
            return None
        cur = self.db.execute(
            "INSERT INTO reviews(rkey,kind,status,summary,detail,msg_hash,doc_key,created_cycle) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (rkey, kind, 'open', summary, detail, msg_hash, doc_key, cycle))
        self.db.commit()
        return cur.lastrowid

    def resolve_review(self, review_id, resolution, cycle, auto=False):
        self.db.execute(
            "UPDATE reviews SET status=?,resolved_cycle=?,resolution=? WHERE id=? AND status='open'",
            ('auto' if auto else 'resolved', cycle, resolution, review_id))
        self.db.commit()
        return self.db.total_changes > 0

    def reviews(self, only_open=True):
        sql = "SELECT * FROM reviews" + (" WHERE status='open'" if only_open else "") + " ORDER BY id"
        return [dict(r) for r in self.db.execute(sql)]

    # ---- 交接与停交 ----
    def upsert_handoff(self, reference, box, carrier, shipment, receipt, state, cycle):
        self.db.execute(
            "INSERT INTO handoffs(reference,box,carrier,shipment,receipt,state,updated_cycle) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(reference,box) DO UPDATE SET "
            "receipt=excluded.receipt,state=excluded.state,updated_cycle=excluded.updated_cycle",
            (reference, box, carrier, shipment, receipt, state, cycle))
        self.db.commit()

    def handoffs(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM handoffs ORDER BY reference,box")]

    def get_handoff(self, reference, box):
        row = self.db.execute(
            "SELECT * FROM handoffs WHERE reference=? AND box=?", (reference, box)).fetchone()
        return dict(row) if row else None

    def upsert_stop(self, reference, handoff_reference, carrier, shipment, box, reason, state, cycle):
        self.db.execute(
            "INSERT INTO stops(reference,handoff_reference,carrier,shipment,box,reason,state,updated_cycle) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(reference) DO UPDATE SET "
            "state=excluded.state,updated_cycle=excluded.updated_cycle",
            (reference, handoff_reference, carrier, shipment, box, reason, state, cycle))
        self.db.commit()

    def stops(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM stops ORDER BY reference")]

    def get_stop(self, reference):
        row = self.db.execute("SELECT * FROM stops WHERE reference=?", (reference,)).fetchone()
        return dict(row) if row else None
