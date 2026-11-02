"""Add asset analysis without modifying existing projects, assets or renders."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("analyses", sa.Column("asset_id", sa.String, primary_key=True),
        sa.Column("version", sa.Integer), sa.Column("revision", sa.Integer),
        sa.Column("manifest_json", sa.Text), sa.Column("updated_at", sa.String))
    op.create_table("analysis_jobs", sa.Column("id", sa.String, primary_key=True),
        sa.Column("asset_id", sa.String), sa.Column("version", sa.Integer), sa.Column("mode", sa.String),
        sa.Column("status", sa.String), sa.Column("attempt", sa.Integer), sa.Column("progress", sa.Integer),
        sa.Column("cancel_requested", sa.Integer), sa.Column("error", sa.Text),
        sa.Column("result_json", sa.Text), sa.Column("created_at", sa.String), sa.Column("updated_at", sa.String))
    op.create_table("analysis_annotations", sa.Column("id", sa.String, primary_key=True),
        sa.Column("asset_id", sa.String), sa.Column("version", sa.Integer),
        sa.Column("anchor", sa.String), sa.Column("description", sa.Text), sa.Column("tags_json", sa.Text),
        sa.Column("updated_at", sa.String), sa.UniqueConstraint("asset_id", "version", "anchor"))


def downgrade():
    raise RuntimeError("不自动删除分析数据；恢复请使用经验证备份")
