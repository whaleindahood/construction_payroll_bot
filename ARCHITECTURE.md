# Object-based shift tracking

Telegram private chat → owner object workflow / employee profile router →
services → SQLAlchemy → PostgreSQL (production) or SQLite (tests/local).

## Active user flows

- Owner: object → create/select employee → object team → date → select attendees
  → confirm → per-employee counters for this object.
- Employee: single-use invitation → full name and payment details → confirmation.
- Owner payments: employee on an object → amount → date → optional comment →
  confirmation. Payment history supports confirmed cancellation of errors.
- Existing payroll dialogs in `app/bot/handlers.py` are no longer registered;
  their shared helpers and financial services remain for historical compatibility.

## Persistent records

- `employees`: shared identity, contacts, payment details, linked Telegram ID and
  invitation hash/expiry. A new card does not require a global rate.
- `objects`: construction object, address, start date and lifecycle status.
- `object_employees`: unique employee/object membership, active flag and optional
  shift rate. Removal marks the membership inactive; restoration reuses the row.
- `attendance`: dated employee/object visit, coefficient, optional rate/amount
  snapshot, idempotency key and soft-cancellation metadata.
- `payments`: employee/object payments and advances with soft cancellation.
  Historical payments without an object are retained but excluded from object
  balances. `employee_rates` retains legacy financial history.
- `audit_log`: durable record of creates, edits, assignments, rate changes and
  shift cancellations; not a transcript of Telegram messages.
- `telegram_updates`: duplicate update protection.

## Invariants

1. New object workflow requires active membership before recording a shift.
   Inactive memberships reject new shifts through both current and legacy APIs.
2. One active attendance row per employee/object/date; different objects have
   independent attendance and counters, including on the same date.
3. Counters count active rows, never mutable stored totals; legacy fractional
   rows each count as one visit. New UI records a full shift (coefficient 1).
4. Confirmation is required for card creation, shifts and shift cancellation.
5. Per-object rates are optional. Missing rates produce NULL snapshots, not an
   invented zero or placeholder salary. Updating a rate never changes old rows.
6. Cancellation retains the original row and writes an audit record.
7. Employee access resolves only by Telegram ID in private chats; owner routes
   are gated independently. Employees cannot access another person's profile.
8. Migration backfills memberships from attendance and object-linked payments
   and retains every historical card, rate, payment and attendance record.
9. FSM dialogs use memory and per-user event isolation. Restarts discard only
   unfinished forms; saved business data persists in PostgreSQL.
10. Object balances use only that employee's noncancelled attendance and payments
    on that object. Removed members can still receive payments. An unrated shift
    makes the displayed balance incomplete; its earnings are never invented.
11. Payment saves/cancellations and membership changes require a confirmation
    token bound to the current form. Payment saves also use a database unique
    idempotency key. Old confirmation buttons cannot approve a different form.

## Main modules

- `app/bot/object_workflow.py`: active owner dialogs and shift exports.
- `app/bot/employee.py`: invitation acceptance and personal profile editing.
- `app/teams.py`: object membership, rates, shift counts and history queries.
- `app/services.py`: employee/object services, attendance and payment ledgers,
  object-filtered payroll summaries and audit. Legacy global summaries remain
  available for historical integrations but are not exposed in the active UI.
- `app/models.py`: persistent schema; `alembic/versions/`: incremental migrations.
- `app/main.py`: role routing, duplicate protection and bot polling.
