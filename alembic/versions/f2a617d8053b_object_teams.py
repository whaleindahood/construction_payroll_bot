"""Object teams and shifts without a mandatory rate."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "f2a617d8053b"
down_revision = "c9e27b6a0142"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "object_employees",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("object_id", sa.String(36), sa.ForeignKey("objects.id"), nullable=False),
        sa.Column("employee_id", sa.String(36), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("shift_rate", sa.Numeric(14, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("object_id", "employee_id"),
        sa.CheckConstraint("shift_rate > 0"),
    )
    op.create_index("ix_object_employees_object_id", "object_employees", ["object_id"])
    op.create_index("ix_object_employees_employee_id", "object_employees", ["employee_id"])
    bind = op.get_bind()
    metadata = sa.MetaData()
    attendance = sa.Table("attendance", metadata, autoload_with=bind)
    payments = sa.Table("payments", metadata, autoload_with=bind)
    members = sa.Table("object_employees", metadata, autoload_with=bind)
    pairs = bind.execute(
        sa.union(
            sa.select(attendance.c.object_id, attendance.c.employee_id),
            sa.select(payments.c.object_id, payments.c.employee_id).where(
                payments.c.object_id.is_not(None)
            ),
        )
    ).all()
    for object_id, employee_id in pairs:
        rate = bind.execute(
            sa.select(attendance.c.rate_snapshot)
            .where(
                attendance.c.object_id == object_id,
                attendance.c.employee_id == employee_id,
                attendance.c.voided_at.is_(None),
            )
            .order_by(attendance.c.work_date.desc(), attendance.c.created_at.desc())
            .limit(1)
        ).scalar()
        bind.execute(
            members.insert().values(
                id=str(uuid.uuid4()),
                object_id=object_id,
                employee_id=employee_id,
                shift_rate=rate,
                created_at=datetime.now(UTC),
            )
        )
    with op.batch_alter_table("attendance") as batch:
        batch.alter_column("rate_snapshot", existing_type=sa.Numeric(14, 2), nullable=True)
        batch.alter_column("earned_amount", existing_type=sa.Numeric(14, 2), nullable=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM attendance WHERE rate_snapshot IS NULL OR earned_amount IS NULL"
        )
    ).scalar():
        raise RuntimeError(
            "Cannot downgrade while shifts without a rate exist; restore a backup instead."
        )
    with op.batch_alter_table("attendance") as batch:
        batch.alter_column("rate_snapshot", existing_type=sa.Numeric(14, 2), nullable=False)
        batch.alter_column("earned_amount", existing_type=sa.Numeric(14, 2), nullable=False)
    op.drop_table("object_employees")
