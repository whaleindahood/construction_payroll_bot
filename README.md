# Construction Shift Bot

Telegram bot for tracking employee shifts separately on each construction object.

## Workflow

1. Open **Объекты**, select an existing object or create one with a name,
   address and start date.
2. In the object card choose **Добавить сотрудника**: select an employee from
   the shared database or create a card with full name, phone, payment details
   and optional Telegram ID. Optional fields can be skipped with `-`.
3. Set an optional shift rate in rubles for that employee on that object.
   No rate or currency choice is required to count shifts.
4. Each working day use **Отметить смену**, select the date, check the employees
   who attended, and confirm. Only employees assigned to that object appear.
   A new employee can be added during this flow; earlier selections are kept.
5. The object card displays each employee's shift count, including zero shifts.
   The shared employee card displays separate counts for each object.

One recorded date is one visit/shift on that object. A repeated confirmation or
another attempt to record the same employee/object/date cannot add a duplicate.
The same person can attend different objects on the same day. Future shifts are
not allowed. Historical fractional attendance remains stored as originally
entered; the new counters count recorded visits, not fractional coefficients.

Use **Состав и история смен → employee → История смен** to review dates and
confirm cancellation of an erroneous entry. The count is derived from active
attendance records; cancelled entries remain in the database and audit log.
CSV/XLSX exports contain employee names and lifetime shift counts for the object.

A shift rate belongs to an employee/object pair. Changing it applies to shifts
recorded afterwards, including dates entered retrospectively; old snapshots are
never recalculated. Rates can be omitted. The active interface displays no
currency totals, salary balances or payment-entry dialogs. Legacy financial
records and service code are retained; no historical payments are deleted.

## Editing and deleting cards

Object cards support editing name, address, start date, description and notes.
Employee cards support editing full name, phone, payment details, Telegram ID,
start date and notes. A start date cannot move beyond existing shifts (or legacy
employee rates), so editing cannot invalidate recorded history.

**Удалить объект / Удалить сотрудника** asks for confirmation. Deleted cards move
to **Удаленные объекты / Удаленные сотрудники**, where they can be restored.
Deleted objects reject new shifts; deleted employees cannot enter their profile
or be selected for a shift on any object. Existing shifts, payments and object
memberships remain stored, and their historical counts remain visible.

## Employee self-service

Owners use the IDs in `PAYROLL_OWNER_IDS`. All dialogs are private chats.
Employees can only view and edit their own full name and payment details.

Open the employee card (from the shared database or object team) and select
**Пригласить сотрудника**. Send the generated link personally to the employee.
They enter their full name and payment details and confirm. Reopen their card
to see the saved information. Later they can edit it with `/start` or `/profile`;
`/cancel` discards an unfinished form.

Links bind the Telegram account on opening, expire after seven days and are
single-use. Creating a new link invalidates the old one. Cards with an existing
Telegram ID already support `/start`. To switch accounts, clear the card's
Telegram ID with `-` and issue another invitation. Employees receive no access
to other cards, object rosters, rates or payroll controls.

Payment details are free text up to 1000 characters, not verified bank details.
Saved cards and shifts survive restarts; unfinished forms must be started again.

## Local run

Requires Python 3.12+ and uv. Create `.env` from `.env.example` only if it does
not already exist. Fill in the bot token and owner IDs. For local SQLite set
`PAYROLL_DATABASE_URL=sqlite:///payroll.db`.

```powershell
uv sync --dev
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.main
```

## Docker / PostgreSQL

Start Docker Desktop. In `.env`, use the same database password in
`POSTGRES_PASSWORD` and `PAYROLL_DATABASE_URL`. The hostname `db` works inside
Compose. Keep one bot instance for Telegram polling.

```powershell
docker compose up --build -d
docker compose logs -f bot
```

Migrations run automatically before polling. The `payroll-db` named volume
preserves the database. Existing employee/object associations are recovered from
historical attendance and object-linked payments during the upgrade. Employees
without such history remain in the shared database for manual assignment.

## Tests

```powershell
.\.venv\Scripts\pytest.exe -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
```

Tests cover the full object-first dialog, creation during attendance marking,
separate object rates/counts, duplicate prevention, cancellation, employee access
boundaries, and migration of existing cards and financial history.

## Backups

Back up before upgrades. Avoid piping binary dumps through older PowerShell:

```powershell
New-Item -ItemType Directory -Force backups
docker compose exec -T db pg_dump -U payroll -d payroll -Fc -f /tmp/payroll.dump
docker compose cp db:/tmp/payroll.dump ./backups/payroll.dump
```

Use `pg_restore` into an empty database for recovery. Backups and `.env` are
excluded from Git and Docker build contexts. Do not delete the database volume.
