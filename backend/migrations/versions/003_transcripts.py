"""Keep local transcript generations separate from edit projects and corrections."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("transcripts", sa.Column("id", sa.String, primary_key=True),
        sa.Column("asset_id", sa.String, nullable=False), sa.Column("version", sa.Integer, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False), sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("job_id", sa.String, nullable=False, unique=True), sa.Column("model", sa.String, nullable=False),
        sa.Column("raw_json", sa.Text, nullable=False), sa.Column("corrections_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.String), sa.Column("updated_at", sa.String),
        sa.UniqueConstraint("asset_id", "version", "generation"))


def downgrade():
    raise RuntimeError("不自动删除逐字稿和人工修正；恢复请使用经验证备份")
