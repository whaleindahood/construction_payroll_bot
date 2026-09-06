"""Keep only object-based full-shift payroll."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6f0a1c2d3e4"
down_revision: str = "a4d719c8062e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    attendance = sa.Table("attendance", metadata, autoload_with=bind)
    payments = sa.Table("payments", metadata, autoload_with=bind)
    employees = sa.Table("employees", metadata, autoload_with=bind)
    rates = sa.Table("employee_rates", metadata, autoload_with=bind)
    members = sa.Table("object_employees", metadata, autoload_with=bind)

    if bind.scalar(sa.select(sa.func.count()).where(attendance.c.coefficient != 1)):
        raise RuntimeError("Cannot remove fractional shifts while such rows exist.")
    if bind.scalar(sa.select(sa.func.count()).where(payments.c.object_id.is_(None))):
        raise RuntimeError("Cannot require an object while unassigned payments exist.")
    for table in (employees, attendance, payments, rates):
        if bind.scalar(sa.select(sa.func.count()).where(table.c.currency != "RUB")):
            raise RuntimeError("Cannot remove currencies while non-RUB rows exist.")

    for member_id, employee_id in bind.execute(
        sa.select(members.c.id, members.c.employee_id).where(members.c.shift_rate.is_(None))
    ):
        latest_rate = bind.scalar(
            sa.select(rates.c.daily_rate)
            .where(rates.c.employee_id == employee_id)
            .order_by(rates.c.valid_from.desc())
            .limit(1)
        )
        if latest_rate is not None:
            bind.execute(
                members.update().where(members.c.id == member_id).values(shift_rate=latest_rate)
            )

    op.drop_table("employee_rates")
    with op.batch_alter_table("attendance") as batch:
        batch.drop_column("status")
        batch.drop_column("coefficient")
        batch.drop_column("currency")
        batch.drop_column("comment")
    with op.batch_alter_table("payments") as batch:
        batch.alter_column("object_id", existing_type=sa.String(36), nullable=False)
        batch.drop_column("currency")
        batch.drop_column("method")
    with op.batch_alter_table("employees") as batch:
        batch.drop_column("currency")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("role")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("role", sa.String(20), nullable=False, server_default="OWNER")
        )
    with op.batch_alter_table("employees") as batch:
        batch.add_column(
            sa.Column("currency", sa.String(3), nullable=False, server_default=sa.text("'RUB'"))
        )
        batch.add_column(
            sa.Column("method", sa.String(20), nullable=False, server_default="other")
        )
    op.create_table(
        "employee_rates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("employee_id", sa.String(36), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("daily_rate", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default=sa.text("'RUB'")),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("employee_id", "valid_from"),
        sa.CheckConstraint("daily_rate > 0"),
        sa.CheckConstraint("length(currency) = 3"),
    )
    op.create_index("ix_employee_rates_employee_id", "employee_rates", ["employee_id"])
    with op.batch_alter_table("payments") as batch:
        batch.alter_column("object_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(
            sa.Column("currency", sa.String(3), nullable=False, server_default=sa.text("'RUB'"))
        )
    with op.batch_alter_table("attendance") as batch:
        batch.add_column(
            sa.Column("status", sa.String(20), nullable=False, server_default="worked")
        )
        batch.add_column(
            sa.Column("coefficient", sa.Numeric(5, 2), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("currency", sa.String(3), nullable=False, server_default=sa.text("'RUB'"))
        )
        batch.add_column(sa.Column("comment", sa.Text(), nullable=True))
