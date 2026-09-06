# Object-based shift tracking

Telegram private chat → owner object workflow / employee profile router →
services → SQLAlchemy → PostgreSQL (production) or SQLite (tests/local).

## Active user flows

- Owner: object → visible team → select attendees for today → confirm →
  per-employee counters for this object.
- Employee: single-use invitation → full name and payment details → confirmation.
- Owner payments: employee on an object → amount → confirmation. Date defaults
  to today; date and comment editing are optional. Payment history supports
  confirmed cancellation of errors.
- Navigation: two bottom buttons (objects and shared employee database); object
  cards show the roster and expose shifts, bulk attachment, payments, calculation
  and settings. Employee/object cards expose the common actions directly.
- Employee creation asks for full name, optional payment details and optional
  object rate. Phone and Telegram ID are edited later through personal data.
- The old payroll router, its keyboards and global report helpers have been
  removed. Common dialog helpers live in `app/bot/common.py`.

## Persistent records

- `employees`: shared identity, contacts, payment details, linked Telegram ID and
  invitation hash/expiry.
- `objects`: construction object, address, start date and lifecycle status.
- `object_employees`: unique employee/object membership, active flag and optional
  shift rate. Removal marks the membership inactive; restoration reuses the row.
- `attendance`: dated employee/object shift, optional rate/amount snapshot,
  idempotency key and soft-cancellation metadata.
- `payments`: employee/object payments and advances with soft cancellation.
- `audit_log`: durable record of creates, edits, assignments, rate changes and
  shift cancellations; not a transcript of Telegram messages.
- `telegram_updates`: duplicate update protection.

## Invariants

1. Active object membership is required before recording a shift.
2. One active attendance row per employee/object/date; different objects have
   independent attendance and counters, including on the same date.
3. Counters count active rows, never mutable stored totals. Each row is one full shift.
4. Confirmation is required for card creation, shifts and shift cancellation.
5. Per-object rates are optional. A missing rate produces a NULL snapshot rather
   than an invented zero. Updating a rate never changes old rows.
   An explicit, confirmed action may fill an unrated shift. A conditional UPDATE
   rejects already priced or cancelled rows and records the change in the audit.
6. Cancellation retains the original row and writes an audit record.
7. Employee access resolves only by Telegram ID in private chats; owner routes
   are gated independently. Employees cannot access another person's profile.
8. Migration backfills memberships from attendance and object-linked payments.
9. FSM dialogs use memory and per-user event isolation. Restarts discard only
   unfinished forms; saved business data persists in PostgreSQL.
10. Object balances use only that employee's noncancelled attendance and payments
    on that object. Removed members can still receive payments. An unrated shift
    makes the displayed balance incomplete; its earnings are never invented.
11. Saves and cancellations are bound to the specific form or record. Shift
    pickers use a per-screen token; card editing and exports carry the original
    entity ID. Old confirmation buttons cannot approve a different operation.
12. Payments affect balances only. All noncancelled shifts remain in lifetime
    counts and history, including after full settlement.

## Main modules

- `app/bot/object_workflow.py`: active owner dialogs and shift exports.
- `app/bot/employee.py`: invitation acceptance and personal profile editing.
- `app/bot/common.py`: service bundle, date parsing and long-message handling.
- `app/teams.py`: object membership, rates, shift counts and history queries.
- `app/services.py`: employee/object services, attendance and payment ledgers,
  object payroll summaries and audit.
- `app/models.py`: persistent schema; `alembic/versions/`: incremental migrations.
- `app/main.py`: role routing, duplicate protection and bot polling.
