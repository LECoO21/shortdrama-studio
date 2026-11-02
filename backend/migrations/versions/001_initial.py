"""First local persistence schema, immutable migration."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assets", sa.Column("id", sa.String, primary_key=True),
        sa.Column("version", sa.Integer, nullable=False), sa.Column("sha256", sa.String, unique=True),
        sa.Column("filename", sa.String), sa.Column("path", sa.String),
        sa.Column("metadata_json", sa.Text), sa.Column("created_at", sa.String))
    op.create_table("projects", sa.Column("id", sa.String, primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False), sa.Column("document_json", sa.Text),
        sa.Column("updated_at", sa.String))
    op.create_table("snapshots", sa.Column("id", sa.String, primary_key=True),
        sa.Column("project_id", sa.String), sa.Column("revision", sa.Integer),
        sa.Column("document_json", sa.Text), sa.Column("sha256", sa.String),
        sa.Column("created_at", sa.String), sa.UniqueConstraint("project_id", "revision"))
    op.create_table("receipts", sa.Column("id", sa.String, primary_key=True),
        sa.Column("fingerprint", sa.String), sa.Column("response_json", sa.Text))
    op.create_table("jobs", sa.Column("id", sa.String, primary_key=True),
        sa.Column("project_id", sa.String), sa.Column("revision", sa.Integer),
        sa.Column("document_json", sa.Text), sa.Column("status", sa.String),
        sa.Column("attempt", sa.Integer), sa.Column("progress", sa.Integer),
        sa.Column("cancel_requested", sa.Integer), sa.Column("error", sa.Text),
        sa.Column("output_path", sa.String), sa.Column("created_at", sa.String),
        sa.Column("updated_at", sa.String))


def downgrade():
    raise RuntimeError("本地阶段不自动破坏性删除数据；恢复请使用经验证备份")
