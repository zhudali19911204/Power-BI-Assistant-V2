"""Local SQLite repository. Connections never cross request threads."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, port INTEGER,
                    fingerprint TEXT NOT NULL, context TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL,
                    model_id TEXT NOT NULL REFERENCES models(id), query_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'complete', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS message_clarifications (
                    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS llm_generations (
                    id TEXT PRIMARY KEY,
                    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                    model_id TEXT REFERENCES models(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL, attempt INTEGER NOT NULL,
                    diagnostic TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS llm_generations_message ON llm_generations(message_id);
                CREATE TABLE IF NOT EXISTS message_images (
                    id TEXT PRIMARY KEY, message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    size INTEGER NOT NULL, data BLOB NOT NULL, created_at TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS message_images_message ON message_images(message_id);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL REFERENCES models(id), kind TEXT NOT NULL, name TEXT NOT NULL,
                    expression TEXT NOT NULL, home_table TEXT NOT NULL, validation TEXT NOT NULL,
                    applied_at TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id TEXT PRIMARY KEY, artifact_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analysis_queries (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL REFERENCES models(id), title TEXT NOT NULL, dax TEXT NOT NULL,
                    result TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS analysis_message ON analysis_queries(message_id);
                CREATE TABLE IF NOT EXISTS field_observations (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    result TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_semantics (
                    model_id TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_reports (
                    model_id TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_bindings (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    literal TEXT NOT NULL, table_name TEXT, column_name TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,literal));
                CREATE TABLE IF NOT EXISTS field_value_profiles (
                    model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                    table_name TEXT NOT NULL, column_name TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL, checked_at TEXT NOT NULL,
                    PRIMARY KEY(model_id,table_name,column_name));
                CREATE TABLE IF NOT EXISTS model_value_lookups (
                    model_id TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
                    literal TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    checked_at TEXT NOT NULL, PRIMARY KEY(model_id,literal));
                CREATE INDEX IF NOT EXISTS field_observations_message ON field_observations(message_id);
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,created_at);
                CREATE INDEX IF NOT EXISTS artifacts_message ON artifacts(message_id);
                PRAGMA user_version=1;
            """)
            if 'position' not in {r['name'] for r in db.execute('PRAGMA table_info(message_images)')}:
                db.execute('ALTER TABLE message_images ADD COLUMN position INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, params=()):
        with self.connection() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connection() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.connection() as db:
            return db.execute(sql, params).rowcount

    def audit(self, action, detail, artifact_id=None):
        self.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (uid(), artifact_id, action, encode(detail), now()))
