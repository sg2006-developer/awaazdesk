import json
import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, text
from .config import ROOT

class ConflictError(Exception):
    """Optimistic-concurrency conflict: the record changed since the client read it."""


def now():
    return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self, url=None):
        url = url or os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'awaazdesk.db'}")
        (ROOT / "data").mkdir(exist_ok=True)
        if url.startswith("sqlite:///./"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})

    def initialize(self):
        with self.engine.begin() as db:
            for statement in [
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS draft (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, config TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, config TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, version INTEGER NOT NULL REFERENCES versions(id), revision INTEGER NOT NULL, messages TEXT NOT NULL, ticket TEXT NOT NULL, ended INTEGER NOT NULL, created_at TEXT NOT NULL)",
            ]:
                db.execute(text(statement))
            if not db.execute(text("SELECT version FROM schema_version")).first():
                db.execute(text("INSERT INTO schema_version VALUES (1)"))
            if not db.execute(text("SELECT id FROM draft WHERE id=1")).first():
                seed = json.loads((ROOT / "sample_business.json").read_text())
                db.execute(text("INSERT INTO draft VALUES (1,1,:c)"), {"c": json.dumps(seed)})

    def draft(self):
        with self.engine.connect() as db:
            row = db.execute(text("SELECT * FROM draft WHERE id=1")).mappings().one()
            return {"revision": row["revision"], "config": json.loads(row["config"])}

    def save_draft(self, config, revision):
        with self.engine.begin() as db:
            result = db.execute(text("UPDATE draft SET config=:c, revision=revision+1 WHERE id=1 AND revision=:r"), {"c": json.dumps(config), "r": revision})
            if result.rowcount != 1:
                raise ConflictError("Draft changed in another tab. Reload before saving.")
        return self.draft()

    def publish(self, revision):
        with self.engine.begin() as db:
            # Acquire a write lock and validate the expected revision atomically.
            updated = db.execute(text("UPDATE draft SET revision=revision+1 WHERE id=1 AND revision=:r"), {"r": revision})
            if updated.rowcount != 1:
                raise ConflictError("Draft changed. Reload before publishing.")
            config = db.execute(text("SELECT config FROM draft WHERE id=1")).scalar_one()
            version = db.execute(text("SELECT COALESCE(MAX(id),0)+1 FROM versions")).scalar_one()
            db.execute(text("INSERT INTO versions VALUES (:id,:c,:t)"), {"id": version, "c": config, "t": now()})
        return version

    def versions(self):
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(text("SELECT id,created_at FROM versions ORDER BY id DESC")).mappings()]

    def version(self, version=None):
        with self.engine.connect() as db:
            row = db.execute(text("SELECT * FROM versions WHERE id=:v") if version else text("SELECT * FROM versions ORDER BY id DESC LIMIT 1"), {"v": version} if version else {}).mappings().first()
            return {"id": row["id"], "config": json.loads(row["config"])} if row else None

    def create_call(self, call_id, version, greeting):
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO calls VALUES (:id,:v,0,:m,:t,0,:at)"), {"id": call_id, "v": version, "m": json.dumps([{"role": "assistant", "content": greeting}]), "t": json.dumps({"category": "Unclassified", "status": "open", "summary": "Awaiting customer", "fields": {}, "sources": [], "engine": "pending"}), "at": now()})
        return self.call(call_id)

    @staticmethod
    def _call_row(row):
        return {**dict(row), "messages": json.loads(row["messages"]), "ticket": json.loads(row["ticket"]), "ended": bool(row["ended"])}

    def call(self, call_id):
        with self.engine.connect() as db:
            row = db.execute(text("SELECT * FROM calls WHERE id=:id"), {"id": call_id}).mappings().first()
            return self._call_row(row) if row else None

    def update_call(self, call_id, revision, messages, ticket):
        with self.engine.begin() as db:
            result = db.execute(text("UPDATE calls SET revision=revision+1,messages=:m,ticket=:t WHERE id=:id AND revision=:r AND ended=0"), {"m": json.dumps(messages), "t": json.dumps(ticket), "id": call_id, "r": revision})
            if result.rowcount != 1:
                raise ConflictError("Conversation changed or ended. Reload before retrying.")
        return self.call(call_id)

    def end(self, call_id):
        with self.engine.begin() as db:
            db.execute(text("UPDATE calls SET ended=1,revision=revision+1 WHERE id=:id AND ended=0"), {"id": call_id})
        return self.call(call_id)

    def calls(self):
        with self.engine.connect() as db:
            rows = db.execute(text("SELECT * FROM calls ORDER BY created_at DESC LIMIT 100")).mappings().all()
        return [self._call_row(r) for r in rows]
