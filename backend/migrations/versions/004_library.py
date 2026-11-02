"""Add project/episode metadata without rewriting any existing edit or media."""
import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("productions", sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False), sa.Column("description", sa.Text, nullable=False),
        sa.Column("language", sa.String, nullable=False), sa.Column("canvas_json", sa.Text, nullable=False),
        sa.Column("fps_json", sa.Text, nullable=False), sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("archived", sa.Integer, nullable=False), sa.Column("created_at", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False))
    op.create_table("episodes", sa.Column("project_id", sa.String, primary_key=True),
        sa.Column("production_id", sa.String, nullable=False), sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("archived", sa.Integer, nullable=False), sa.Column("created_at", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False))
    op.create_index("ix_episodes_production_id", "episodes", ["production_id"])
    op.create_table("production_assets", sa.Column("production_id", sa.String, primary_key=True),
        sa.Column("asset_id", sa.String, primary_key=True), sa.Column("created_at", sa.String, nullable=False))
    bind = op.get_bind()
    available = set(bind.execute(sa.text("SELECT id FROM assets")).scalars())
    for project in bind.execute(sa.text("SELECT id, document_json, updated_at FROM projects")).mappings():
        document = json.loads(project["document_json"])
        production_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"shortdrama:legacy:{project['id']}"))
        stamp = project["updated_at"] or "1970-01-01T00:00:00+00:00"
        dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        bind.execute(sa.text("""INSERT INTO productions
            (id,name,description,language,canvas_json,fps_json,revision,archived,created_at,updated_at)
            VALUES (:id,:name,'','zh',:canvas,:fps,1,0,:stamp,:stamp)"""),
            {"id": production_id, "name": document["name"], "canvas": dump(document["canvas"]),
             "fps": dump(document["fps"]), "stamp": stamp})
        bind.execute(sa.text("""INSERT INTO episodes
            (project_id,production_id,revision,archived,created_at,updated_at)
            VALUES (:project,:production,1,0,:stamp,:stamp)"""),
            {"project": project["id"], "production": production_id, "stamp": stamp})
        documents = [document]
        for table in ("snapshots", "jobs"):
            documents.extend(json.loads(value) for value in bind.execute(sa.text(
                f"SELECT document_json FROM {table} WHERE project_id=:id"), {"id": project["id"]}).scalars()
                if value)
        referenced = {clip["source"]["asset_id"] for value in documents for clip in value.get("clips", [])}
        for asset_id in sorted(referenced & available):
            bind.execute(sa.text("""INSERT INTO production_assets (production_id,asset_id,created_at)
                VALUES (:production,:asset,:stamp)"""),
                {"production": production_id, "asset": asset_id, "stamp": stamp})


def downgrade():
    raise RuntimeError("不自动删除项目归属与归档状态；恢复请使用经验证备份")
