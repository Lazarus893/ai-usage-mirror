"""SQLite canonical store + incremental ingest (ARCHITECTURE.md §3, §4). Read-only on sources."""
import os, sqlite3, hashlib, datetime

SCHEMA_VERSION = "1"

_DDL = """
CREATE TABLE IF NOT EXISTS session (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, file_path TEXT NOT NULL,
  file_mtime REAL NOT NULL, file_size INTEGER NOT NULL, content_hash TEXT,
  project TEXT, cwd_raw TEXT, model TEXT, originator TEXT,
  started_at TEXT, ended_at TEXT, n_user INTEGER, n_asst INTEGER,
  out_tokens INTEGER, kind TEXT NOT NULL, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, ts TEXT, role TEXT NOT NULL, text TEXT,
  is_task INTEGER DEFAULT 0, is_friction INTEGER DEFAULT 0, sig TEXT
);
CREATE TABLE IF NOT EXISTS tool_call (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  after_seq INTEGER, tool TEXT NOT NULL, cmd_key TEXT, file_ext TEXT, workdir TEXT
);
CREATE TABLE IF NOT EXISTS prompt_vec (
  message_id INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE, vec BLOB
);
CREATE TABLE IF NOT EXISTS ingest_state (
  file_path TEXT PRIMARY KEY, file_mtime REAL, file_size INTEGER, content_hash TEXT, session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON message(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_tc_session  ON tool_call(session_id);
CREATE INDEX IF NOT EXISTS idx_sess_kind   ON session(kind, source);
"""


def _now():
    # workflow/scripts forbid argless datetime.now(); this is a plain script, allowed.
    return datetime.datetime.now().isoformat(timespec='seconds')


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class Store:
    def __init__(self, db_path, version_path=None):
        self.path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.version_path = version_path or os.path.join(os.path.dirname(db_path), "VERSION")

    def init_schema(self):
        self.db.executescript(_DDL)
        self.db.commit()
        if not os.path.exists(self.version_path):      # stamp only when missing -> stale version stays detectable
            with open(self.version_path, 'w') as fh:
                fh.write(SCHEMA_VERSION)

    def stored_version(self):
        try:
            with open(self.version_path) as fh:
                return fh.read().strip()
        except OSError:
            return None

    def drop_all(self):
        for tbl in ('message_fts', 'prompt_vec', 'tool_call', 'message', 'session', 'ingest_state'):
            self.db.execute(f"DROP TABLE IF EXISTS {tbl}")
        self.db.commit()
        try:
            os.remove(self.version_path)                # so init_schema re-stamps current version
        except OSError:
            pass

    # ---- health ----
    def integrity_check(self):
        return self.db.execute("PRAGMA integrity_check").fetchone()[0]

    def session_count(self):
        return self.db.execute("SELECT COUNT(*) FROM session").fetchone()[0]

    def last_ingest(self):
        return self.db.execute("SELECT MAX(ingested_at) FROM session").fetchone()[0]

    def fts_status(self):
        has = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_fts'").fetchone()
        if not has:
            return {'built': False, 'in_sync': False}
        n_fts = self.db.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        n_msg = self.db.execute("SELECT COUNT(*) FROM message WHERE text IS NOT NULL").fetchone()[0]
        return {'built': True, 'in_sync': n_fts == n_msg, 'fts_rows': n_fts, 'msg_rows': n_msg}

    # ---- incremental ingest ----
    def _state(self, path):
        r = self.db.execute(
            "SELECT file_mtime, file_size, content_hash, session_id FROM ingest_state WHERE file_path=?",
            (path,)).fetchone()
        return r

    def ingest_file(self, source_name, path, parser):
        st = os.stat(path)
        mtime, size = st.st_mtime, st.st_size
        prev = self._state(path)
        if prev and abs(prev[0] - mtime) < 1e-6 and prev[1] == size:
            return 'skipped'                                   # unchanged (mtime+size match)
        h = _sha1(path)
        if prev and prev[2] == h:                              # content identical -> just refresh stamps
            self.db.execute("UPDATE ingest_state SET file_mtime=?, file_size=? WHERE file_path=?",
                            (mtime, size, path))
            self.db.commit()
            return 'unchanged'
        # delete any prior session for this file, then (re)parse
        if prev and prev[3]:
            self.db.execute("DELETE FROM session WHERE id=?", (prev[3],))
        sess = parser(path)
        if sess is None:
            self._save_state(path, mtime, size, h, None)
            return 'empty'
        self.db.execute("DELETE FROM session WHERE id=?", (sess['id'],))  # collision guard
        self._insert_session(sess, path, mtime, size, h)
        self._save_state(path, mtime, size, h, sess['id'])
        self.db.commit()
        return 'ingested'

    def _save_state(self, path, mtime, size, h, sid):
        self.db.execute(
            "INSERT INTO ingest_state(file_path,file_mtime,file_size,content_hash,session_id) "
            "VALUES(?,?,?,?,?) ON CONFLICT(file_path) DO UPDATE SET "
            "file_mtime=excluded.file_mtime, file_size=excluded.file_size, "
            "content_hash=excluded.content_hash, session_id=excluded.session_id",
            (path, mtime, size, h, sid))

    def _insert_session(self, s, path, mtime, size, h):
        self.db.execute(
            "INSERT INTO session(id,source,file_path,file_mtime,file_size,content_hash,project,cwd_raw,"
            "model,originator,started_at,ended_at,n_user,n_asst,out_tokens,kind,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s['id'], s['source'], path, mtime, size, h, s['project'], s['cwd_raw'], s['model'],
             s['originator'], s['started_at'], s['ended_at'], s['n_user'], s['n_asst'],
             s['out_tokens'], s['kind'], _now()))
        if s['messages']:
            self.db.executemany(
                "INSERT INTO message(session_id,seq,ts,role,text,is_task,is_friction,sig) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(s['id'], m['seq'], m['ts'], m['role'], m['text'], m['is_task'], m['is_friction'], m['sig'])
                 for m in s['messages']])
        if s['tool_calls']:
            self.db.executemany(
                "INSERT INTO tool_call(session_id,after_seq,tool,cmd_key,file_ext,workdir) "
                "VALUES(?,?,?,?,?,?)",
                [(s['id'], t['after_seq'], t['tool'], t['cmd_key'], t['file_ext'], t['workdir'])
                 for t in s['tool_calls']])

    # ---- FTS5 (derived asset, rebuilt from message; fail-open) ----
    def build_fts(self):
        self.db.execute("DROP TABLE IF EXISTS message_fts")
        try:
            self.db.execute("CREATE VIRTUAL TABLE message_fts USING fts5("
                            "text, content='message', content_rowid='id', tokenize='trigram')")
        except sqlite3.OperationalError:
            self.db.execute("CREATE VIRTUAL TABLE message_fts USING fts5("
                            "text, content='message', content_rowid='id')")   # fallback: unicode61
        self.db.execute("INSERT INTO message_fts(rowid, text) "
                        "SELECT id, text FROM message WHERE text IS NOT NULL")
        self.db.commit()

    def ensure_fts(self):
        has = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_fts'").fetchone()
        if not has:
            self.build_fts()
            return
        n_fts = self.db.execute("SELECT COUNT(*) FROM message_fts").fetchone()[0]
        n_msg = self.db.execute("SELECT COUNT(*) FROM message WHERE text IS NOT NULL").fetchone()[0]
        if n_fts != n_msg:                    # stale -> rebuild (derived-state, never manual repair)
            self.build_fts()

    def search(self, query, limit=20, kinds=('real', 'meta')):
        query = (query or '').strip()
        ph = ','.join('?' * len(kinds))
        if len(query) < 3:                                   # trigram needs >=3 chars -> LIKE fallback
            rows = self.db.execute(
                f"SELECT s.id, s.source, s.project, m.ts, m.text "
                f"FROM message m JOIN session s ON m.session_id=s.id "
                f"WHERE m.text LIKE ? AND s.kind IN ({ph}) ORDER BY m.ts DESC LIMIT ?",
                ('%' + query + '%', *kinds, limit)).fetchall()
        else:
            self.ensure_fts()
            q = '"' + query.replace('"', '""') + '"'          # trigram-safe phrase
            rows = self.db.execute(
                f"SELECT s.id, s.source, s.project, m.ts, m.text "
                f"FROM message_fts f JOIN message m ON f.rowid=m.id JOIN session s ON m.session_id=s.id "
                f"WHERE f.text MATCH ? AND s.kind IN ({ph}) ORDER BY rank LIMIT ?",
                (q, *kinds, limit)).fetchall()
        return [{'session': r[0], 'source': r[1], 'project': r[2], 'ts': r[3], 'text': r[4]}
                for r in rows]

    # ---- reporting ----
    def stats(self):
        rows = self.db.execute(
            "SELECT source, kind, COUNT(*) FROM session GROUP BY source, kind").fetchall()
        out = {}
        for source, kind, n in rows:
            out.setdefault(source, {})[kind] = n
        return out

    def close(self):
        self.db.close()
