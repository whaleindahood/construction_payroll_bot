from datetime import UTC, date, datetime

from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from alembic import command


def test_object_migration_preserves_history_and_backfills_memberships(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    monkeypatch.setenv("PAYROLL_DATABASE_URL", url)
    monkeypatch.setenv("PAYROLL_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("PAYROLL_OWNER_IDS", "1001")
    config = Config("alembic.ini")
    command.upgrade(config, "c9e27b6a0142")
    engine = create_engine(url)
    metadata = MetaData()
    metadata.reflect(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["employees"]
            .insert()
            .values(
                id="employee",
                name="Иван Петров",
                telegram_id=2001,
                payment_details="Банк, реквизиты",
                currency="RUB",
                start_date=date(2026, 1, 1),
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            metadata.tables["objects"].insert(),
            [
                {
                    "id": object_id,
                    "name": object_id,
                    "start_date": date(2026, 1, 1),
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
                for object_id in ("object-one", "object-two")
            ],
        )
        connection.execute(
            metadata.tables["attendance"]
            .insert()
            .values(
                id="old-shift",
                employee_id="employee",
                object_id="object-one",
                work_date=date(2026, 1, 2),
                status="worked",
                coefficient=1,
                rate_snapshot=2000,
                earned_amount=2000,
                currency="RUB",
                idempotency_key="old-shift",
                created_by=1001,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            metadata.tables["payments"]
            .insert()
            .values(
                id="old-payment",
                employee_id="employee",
                object_id="object-two",
                payment_date=date(2026, 1, 2),
                amount=1000,
                currency="RUB",
                method="cash",
                idempotency_key="old-payment",
                created_by=1001,
                created_at=now,
                updated_at=now,
            )
        )
    command.upgrade(config, "head")
    members = Table("object_employees", MetaData(), autoload_with=engine)
    columns = {
        table: {column["name"]: column for column in inspect(engine).get_columns(table)}
        for table in ("attendance", "payments")
    }
    assert {"status", "coefficient", "currency", "comment"}.isdisjoint(
        columns["attendance"]
    )
    assert "currency" not in columns["payments"]
    assert "method" not in columns["payments"]
    assert columns["payments"]["object_id"]["nullable"] is False
    with engine.connect() as connection:
        rows = connection.execute(select(members).order_by(members.c.object_id)).mappings().all()
        assert len(rows) == 2
        assert all(row["active"] for row in rows)
        assert rows[0]["shift_rate"] == 2000
        assert rows[1]["shift_rate"] is None
        assert (
            connection.execute(select(metadata.tables["attendance"].c.earned_amount)).scalar_one()
            == 2000
        )
        assert connection.execute(select(metadata.tables["payments"].c.amount)).scalar_one() == 1000
        assert (
            connection.execute(select(metadata.tables["employees"].c.payment_details)).scalar_one()
            == "Банк, реквизиты"
        )
    command.downgrade(config, "c9e27b6a0142")
    with engine.connect() as connection:
        assert (
            connection.execute(select(metadata.tables["attendance"].c.id)).scalar_one()
            == "old-shift"
        )
    engine.dispose()
