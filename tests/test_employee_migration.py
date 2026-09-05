from datetime import UTC, date, datetime

from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from alembic import command


def test_employee_migration_preserves_existing_cards(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("PAYROLL_DATABASE_URL", database_url)
    monkeypatch.setenv("PAYROLL_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDE")
    monkeypatch.setenv("PAYROLL_OWNER_IDS", "1001")
    config = Config("alembic.ini")
    command.upgrade(config, "5a4902dedba5")
    engine = create_engine(database_url)
    employees = Table("employees", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            employees.insert().values(
                id="existing-employee",
                name="Иван Петров",
                telegram_id=2001,
                currency="RUB",
                start_date=date(2026, 1, 1),
                status="active",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    command.upgrade(config, "head")
    upgraded = Table("employees", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        row = connection.execute(select(upgraded)).mappings().one()
        assert row["name"] == "Иван Петров"
        assert row["telegram_id"] == 2001
        assert row["payment_details"] is None
        assert row["invite_token_hash"] is None
    command.downgrade(config, "5a4902dedba5")
    assert "payment_details" not in {
        column["name"] for column in inspect(engine).get_columns("employees")
    }
    with engine.connect() as connection:
        assert connection.execute(select(employees.c.name)).scalar_one() == "Иван Петров"
    engine.dispose()
