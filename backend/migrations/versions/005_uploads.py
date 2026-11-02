"""Persistent local upload sessions; originals remain immutable assets."""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("uploads", sa.Column("id", sa.String, primary_key=True),
        sa.Column("request_id", sa.String, nullable=False, unique=True),
        sa.Column("fingerprint", sa.String, nullable=False), sa.Column("filename", sa.String, nullable=False),
        sa.Column("size", sa.Integer, nullable=False), sa.Column("sha256", sa.String, nullable=False),
        sa.Column("last_modified", sa.Integer, nullable=False), sa.Column("production_id", sa.String),
        sa.Column("offset", sa.Integer, nullable=False), sa.Column("status", sa.String, nullable=False),
        sa.Column("chunks_json", sa.Text, nullable=False), sa.Column("asset_id", sa.String),
        sa.Column("duplicate", sa.Integer), sa.Column("error_json", sa.Text),
        sa.Column("created_at", sa.String, nullable=False), sa.Column("updated_at", sa.String, nullable=False))


def downgrade():
    raise RuntimeError("不自动删除导入记录；恢复请使用经验证备份")
