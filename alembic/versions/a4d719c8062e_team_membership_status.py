"""Keep former object members and their financial history."""

import sqlalchemy as sa

from alembic import op

revision = "a4d719c8062e"
down_revision = "f2a617d8053b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "object_employees",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade():
    with op.batch_alter_table("object_employees") as batch:
        batch.drop_column("active")
