"""Employee payment details and single-use invitations."""

import sqlalchemy as sa

from alembic import op

revision = "c9e27b6a0142"
down_revision = "5a4902dedba5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.add_column(sa.Column("payment_details", sa.Text(), nullable=True))
        batch.add_column(sa.Column("invite_token_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_employee_invite_token_hash", ["invite_token_hash"])


def downgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.drop_constraint("uq_employee_invite_token_hash", type_="unique")
        batch.drop_column("invite_expires_at")
        batch.drop_column("invite_token_hash")
        batch.drop_column("payment_details")
