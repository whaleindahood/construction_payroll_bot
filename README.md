# Construction Shift Bot

Telegram bot for tracking employee shifts separately on each construction object.

## Workflow

1. `/start` opens the objects list. The bottom menu contains **Объекты** and
   **База сотрудников**. Select an object or create one with a name, address
   and start date.
2. The object card immediately shows its active workers, effective rates and
   lifetime shift counts. Use **Добавить рабочих** to check several people from
   the shared database and attach them in one operation, or create a new card
   with full name and payment details. The new card is attached immediately.
   **Заполнить позже** skips the payment details. Phone and Telegram ID remain
   available under personal data editing, rather than in the creation dialog.
3. Set an optional shift rate in rubles for that employee on that object.
   A rate is not required to count shifts.
4. Each working day use **Отметить смены**, check the employees who attended,
   and confirm. Today's date is selected automatically; **Изменить дату** lets
   you choose another day and clears unsaved selections from the previous date.
   Only active employees assigned to that object appear.
   A new employee can be added during this flow; earlier selections are kept.
5. Select a worker directly on the object card to add one shift, record a
   payment, change the object rate or open history. The shared employee card
   remains a secondary directory and displays separate counts for each object.

One recorded date is one visit/shift on that object. A repeated confirmation or
another attempt to record the same employee/object/date cannot add a duplicate.
The same person can attend different objects on the same day. Future shifts are
not allowed. Every attendance record represents one full shift.

Use **object → employee → История → Смены** to review dates and
confirm cancellation of an erroneous entry. The count is derived from active
attendance records; cancelled entries remain in the database and audit log.
CSV/XLSX exports contain employee names, lifetime shift counts, membership status,
earnings, payments and balances for the selected object, including former members.

A shift rate belongs to an employee/object pair. Changing it applies to shifts
recorded afterwards, including dates entered retrospectively; old snapshots are
never recalculated. A rate can be omitted. If a shift has no rate,
the employee's object card shows how many shifts lack a rate, sums only known
earnings and does not present an incomplete balance as a final amount.

Use **Смены без ставки** on the employee's object card to select a date, enter its rate
and confirm the missing accrual. This fills only that previously unrated shift;
it cannot overwrite an existing calculation or price a cancelled record. The
current rate on the object is unchanged. Calculated shifts leave this task list
but remain in the complete shift history and lifetime counter. Payments also
never remove shifts from history or reduce the number of worked shifts.

## Payments and balances by object

Select an employee on the object card. The card shows earnings,
payments and the remaining debt for that employee on this object over all time.
A negative balance is displayed as an advance. Balances are never offset across
objects. Every payment belongs to one object.

Use **Записать выплату**, enter the amount and confirm. Today's date is selected
automatically. **Изменить дату** and **Комментарий** are optional actions on the
confirmation screen. This records an already made payment; it does not
transfer money. Advances use the same flow. Repeating a confirmation cannot
duplicate a payment. **История → Выплаты** shows payments for this employee/object;
select an erroneous entry and confirm cancellation to recalculate the balance.
Cancelled records remain stored. Future payment dates are rejected.

## Removing an employee from an object

In the employee's object card, **Убрать с объекта** asks for confirmation and
removes the employee from new shift selection only on this object. The shared
employee card, other objects, rate, shifts and payments remain intact. The former
member is available under **Бывшие сотрудники**; the balance is still visible,
payments can still be recorded, and erroneous entries can still be cancelled.

**Вернуть на объект** restores the same membership with its existing rate and
history. Selecting the former member through **Добавить рабочих** does the same.
Both the employee card and object must be active to restore membership. No
membership periods or partial shifts are required.

## Editing and deleting cards

**Настройки объекта** contains editing name, address, start date, description,
notes, completion and deletion. These actions are outside the daily work screen.
Employee cards support editing full name, phone, payment details, Telegram ID,
start date and notes. A start date cannot move beyond existing shifts, so editing
cannot invalidate recorded history.

**Удалить объект / Удалить сотрудника из базы** asks for confirmation. Employee
deletion is under **Личные данные → Изменить данные**. Confirmation permanently
deletes the selected card and its memberships, shifts and payments. Deleting an
object does not delete the global employee cards that worked there.

## Employee self-service

Owners use the IDs in `PAYROLL_OWNER_IDS`. All dialogs are private chats.
An employee can open the bot, create a global card with their full name and
payment details, then view and edit only that card. The owner attaches it to an
object and sets the object rate.

For a card created by the owner, select **Пригласить сотрудника** and send the
generated link personally. The link binds that existing card to the employee.
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
Regression checks cover old keyboards from another object, mismatched save and
cancellation confirmations, and preservation of shift counts after full payment.

## Backups

Back up before upgrades. Avoid piping binary dumps through older PowerShell:

```powershell
New-Item -ItemType Directory -Force backups
docker compose exec -T db pg_dump -U payroll -d payroll -Fc -f /tmp/payroll.dump
docker compose cp db:/tmp/payroll.dump ./backups/payroll.dump
```

Use `pg_restore` into an empty database for recovery. Backups and `.env` are
excluded from Git and Docker build contexts. Do not delete the database volume.
