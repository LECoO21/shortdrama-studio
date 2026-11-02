"""Single local workspace persistence. A queue is never the project truth."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (Column, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, event)

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("STUDIO_DATA_DIR", ROOT.parent / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
for dirname in ("originals", "renders", "work"):
    (DATA / dirname).mkdir(exist_ok=True)

engine = create_engine(f"sqlite:///{DATA / 'studio.db'}", connect_args={"timeout": 30})


@event.listens_for(engine, "connect")
def sqlite_options(dbapi, _):
    dbapi.execute("PRAGMA journal_mode=WAL")
    dbapi.execute("PRAGMA foreign_keys=ON")
    dbapi.execute("PRAGMA busy_timeout=30000")


metadata = MetaData()
assets = Table("assets", metadata, Column("id", String, primary_key=True),
               Column("version", Integer, nullable=False), Column("sha256", String, unique=True),
               Column("filename", String), Column("path", String), Column("metadata_json", Text),
               Column("created_at", String))
projects = Table("projects", metadata, Column("id", String, primary_key=True),
                 Column("revision", Integer, nullable=False), Column("document_json", Text),
                 Column("updated_at", String))
snapshots = Table("snapshots", metadata, Column("id", String, primary_key=True),
                  Column("project_id", String), Column("revision", Integer),
                  Column("document_json", Text), Column("sha256", String),
                  Column("created_at", String), UniqueConstraint("project_id", "revision"))
receipts = Table("receipts", metadata, Column("id", String, primary_key=True),
                 Column("fingerprint", String), Column("response_json", Text))
jobs = Table("jobs", metadata, Column("id", String, primary_key=True),
             Column("project_id", String), Column("revision", Integer),
             Column("document_json", Text), Column("status", String),
             Column("attempt", Integer, default=0), Column("progress", Integer, default=0),
             Column("cancel_requested", Integer, default=0), Column("error", Text),
             Column("output_path", String), Column("created_at", String),
             Column("updated_at", String))
analyses = Table("analyses", metadata, Column("asset_id", String, primary_key=True),
                 Column("version", Integer), Column("revision", Integer),
                 Column("manifest_json", Text), Column("updated_at", String))
analysis_jobs = Table("analysis_jobs", metadata, Column("id", String, primary_key=True),
                      Column("asset_id", String), Column("version", Integer), Column("mode", String),
                      Column("status", String), Column("attempt", Integer), Column("progress", Integer),
                      Column("cancel_requested", Integer), Column("error", Text),
                      Column("result_json", Text), Column("created_at", String), Column("updated_at", String))
analysis_annotations = Table("analysis_annotations", metadata, Column("id", String, primary_key=True),
                             Column("asset_id", String), Column("version", Integer),
                             Column("anchor", String), Column("description", Text), Column("tags_json", Text),
                             Column("updated_at", String), UniqueConstraint("asset_id", "version", "anchor"))
transcripts = Table("transcripts", metadata, Column("id", String, primary_key=True),
                    Column("asset_id", String, nullable=False), Column("version", Integer, nullable=False),
                    Column("generation", Integer, nullable=False), Column("revision", Integer, nullable=False),
                    Column("job_id", String, nullable=False, unique=True), Column("model", String, nullable=False),
                    Column("raw_json", Text, nullable=False), Column("corrections_json", Text, nullable=False),
                    Column("created_at", String), Column("updated_at", String),
                    UniqueConstraint("asset_id", "version", "generation"))


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def migrate():
    import fcntl
    from alembic import command
    from alembic.config import Config
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
    # Alembic's ConfigParser interpolates %, including URL-encoded Chinese paths.
    config.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))
    # API and worker may boot together; serialize schema migration locally.
    with (DATA / "migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        command.upgrade(config, "head")


def asset_map(connection):
    return {r.id: {"id": r.id, "version": r.version, "sha256": r.sha256,
                   "filename": r.filename, "path": r.path,
                   "created_at": r.created_at,
                   "metadata": json.loads(r.metadata_json)}
            for r in connection.execute(assets.select())}
