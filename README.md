# Event Hire Shop

Rental management for tents, cooking items, furniture, etc. (marriages, birthdays, parties).

Features: inventory, customers, bookings with date-based availability checks, advance/payments,
refundable deposits, status tracking (Booked/Delivered/Returned/Cancelled), overdue alerts, printable invoices.
Customer phone numbers must be exactly 10 digits; staff phone numbers are optional but follow the same rule when provided.
Enter customer details directly when booking. Customers are saved only after the booking passes validation;
identical name, phone and address details reuse a customer record. Editing details does not change other bookings.
Customers without bookings can be deleted from the Customers page; customers with booking history are protected.
Booking > Invoice shows a non-GST rental bill with payment history; use Print / Save PDF to save it
and attach it in WhatsApp. It reflects the current booking and is not an immutable issued tax invoice.
Bills and printed PDFs follow the selected English, Telugu or Hindi language, including dates and standard item names.
Customer details, shop address and custom item names remain as entered.
Set the shop name, address and phone below to your real business details before sharing bills.
Items are grouped on Inventory, Stock and Booking pages into Tents & Event Setup, Cooking & Catering,
and Pooja & Rituals. Add your actual hot boxes, plates, vessels, mandapams and homam equipment with their stock counts.
On Stock and New/Edit booking, categories are stacked expandable rows; tap a row to see its items.
Empty categories remain visible. Booking quantities are retained when switching categories.
On startup, old Tents, Furniture, Decoration and Lighting categories move to Tents & Event Setup;
Cooking moves to Cooking & Catering. Custom categories remain visible and can be reassigned by editing items.

## Run

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/flask --app app create-owner
./venv/bin/python app.py
```

Open http://127.0.0.1:5000. The SQLite database (`rental.db`) is created with sample items on first run.
Login is now required. Create the first Owner with the command above (interactive password prompt,
minimum 12 characters). There is no public signup and the old shared-login session is no longer accepted.
Back up an existing database before upgrading; its bookings, inventory, customers and payroll are preserved.
If you previously set a shorter `APP_PASSWORD`, replace it with a password of at least 12 characters
before the first start, or unset it and use `create-owner`.

## Team access

Owners manage individual logins under More > Team accounts. Logins are separate from payroll staff records.
Accounts can be deactivated instead of deleted; password resets, role changes and deactivation revoke old sessions.
The last active Owner cannot be disabled or demoted.

| Role | Access |
| --- | --- |
| Owner | All business data, account management, backups, inventory, attendance and work assignment |
| Partner | Read-only business operations, payroll and finance reports; no accounts or backups |
| Agent | Customers, bookings, delivery/returns and booking status; read stock; no bills, payments, payroll, expenses or finance reports |
| Accounts | Read bookings and customers; record payments, cancellation fees, expenses and salary payments; approve booking amendments and damage/loss settlements; view reports |
| Staff / Worker | Their assigned work and customer contact/location for those jobs only; update their task status |

On a booking, Owners assign a task to an active Staff/Worker login. The assignee sees it under My work.
Staff supports name/phone/role and active-status filters. Attendance > Attendance history filters recorded
entries by staff name/member, month, inclusive date range, status or all dates, including inactive staff.
History totals cover all matches; results are paged in batches of 50. Unmarked dates are not treated as absent.
Booking filters match rental periods overlapping the selected month/range and retain filters across status tabs.
The globe opens a language-only chooser, also available from More.

## Daily operations and payments

More > Daily operations lists pending deliveries, collections due (including overdue returns),
unfinished assigned work, and payment balances for roles that can view finance.
The date selects work due by that day using current records; it is not a historical snapshot.
Staff/Worker logins continue to see only their own assigned work.

On a booking, use Delivery and returns to record quantities sent or received for each item.
For example, returning 80 of 100 dispatched chairs leaves 20 outstanding.
Returned quantities immediately stop reserving stock. The deposit remains on the bill until
all booked quantities are returned or approved as lost; partial returns do not prorate rent or deposits.
Previously Delivered/WorkDone bookings are migrated as fully dispatched, and Returned bookings
as fully dispatched and returned. This initialization runs only once.
Bookings with dispatched items cannot be directly edited or deleted; use an approved amendment instead.
Delivery/return quantities, not manual status changes, set Delivered and Returned.
If only part of an order was supplied, return those items and request an amendment removing
the unsent remainder. Approval closes the booking if all remaining quantities are accounted for.

### Amendments and damage/loss approvals

On a booking, Request amendment proposes new dates, item quantities, daily rates, per-unit deposits,
discounts or additional charges. A reason is required. Existing quoted rates are prefilled, not replaced
by current inventory prices. New items default to their current inventory prices.
Agent requests retain existing quotes and cannot change prices; Owner and Accounts can propose prices.
Only Owner or Accounts can approve or reject requests, including their own requests.
No proposed change affects stock or the bill before approval.
Approval rechecks availability under a write lock, preserves delivery/return/loss counts and line IDs,
and prevents reducing quantities below the quantity already dispatched.
Rent is recalculated across the entire amended rental period at the approved rates; this is not
time-sliced billing. Confirm the full revised charge with the customer before approving.

Booking change history keeps the submitted before-state, proposal, reason, requester, reviewer,
review time and approved after-state. Approval refuses stale requests after the booking changes;
reject the stale request and submit a fresh one. More > Booking change requests lists pending work.
History begins with the first request after this upgrade, not earlier edits. Bookings with change
history cannot be deleted. Once an amendment is approved, subsequent edits also require amendments.
Agent history excludes financial snapshots; Staff/Worker roles cannot access this history.
These snapshots are application records, not tamper-proof external records or immutable issued invoices.

Damage / loss settlement supports:
- **Damaged return:** receives physically returned items and keeps them unavailable pending repair/write-off.
- **Lost:** settles missing items without increasing the physical returned count.
- **Already returned damaged:** flags damage found after receiving items; does not receive them twice.

Agents can submit a proposed total damage/loss charge; Owner or Accounts reviews the quantities and
sets the final total charge on approval. The approved charge becomes part of the booking bill.
Approval accounts for losses separately, releases their reservation, and keeps those units unavailable.
Lost items can be written off (reducing total stock), but cannot be marked repaired.
Damaged items return to available stock only after repair; writing them off reduces total stock instead.
Neither writing off nor repairing a record removes the approved customer charge.
The booking closes when all booked units are returned or approved lost, releasing the deposit charge
from the bill; any actual customer refund still follows the separate Owner-only refund approval flow.
Legacy damage records are preserved and are not automatically reconciled against dispatch counts.
Do not submit duplicate settlements for damage/loss already charged before this upgrade.
Use the general Damaged & lost page for warehouse stock; customer booking cases use settlement requests.

### Live team updates

Signed-in pages check for role-scoped change revisions about every five seconds while visible.
Idle pages refresh after a relevant committed change. Focused controls and unfinished forms are
not replaced: a localized banner offers Reload when ready, with confirmation before discarding edits.
Connection failures are visible and retried. Polling pauses on hidden tabs and does not prolong the
eight-hour login inactivity window. Worker notifications are limited to their assigned jobs.
Language-only screens, password screens and printable invoices/receipts do not poll.
All team devices must connect to the **same running server and database**. Separate Termux installs
do not synchronize automatically. Polling is near-real-time, not offline editing or a push notification service.

Regression checks:
```
python -W error::ResourceWarning -m unittest discover -s tests -v
node --test tests/test_live_updates.js
```

Record payments as Cash, UPI or Bank; UPI/Bank require a reference. Cash advances can be entered
while creating a booking; for other methods save the booking first, then add the payment.
Existing payments whose method was not recorded are explicitly marked Not recorded.
Each recorded payment has a printable receipt (Print / Save PDF), which can be attached in WhatsApp.
This is manual bookkeeping, not a payment gateway or bank-transfer verification.
Deposit refund creates a pending request, even for an Owner. Only an Owner can approve or reject it.
Approval records the actual refund and updates the balance; verify/send the funds before approving.
Refund limits are checked again under a database write lock, so repeated approvals cannot over-refund.
Pending requests do not affect balances.

Customers > Ledger shows each customer's current booking charges, payments, refunds and balances.
Amounts owed and refunds due are shown separately, without moving credit between bookings.
Owner, Partner and Accounts can access ledgers; Agent and worker roles cannot.
More > Activity history is Owner-only, with actor/date filters and 50 changes per page.
It records successful business mutations and delivery/return quantities in the same transaction.
History begins after this upgrade; it is not an immutable, externally secured audit ledger.
Times use the server timezone. Database files and downloaded backups include these records.

Automatic local backups are still local copies, not off-site disaster recovery. Hosting, a persistent
storage location, and a private off-site backup destination must be selected and configured separately.

Task completion does not automatically mark a booking returned or change stock/payments.
Assigning again to the same person replaces their task and resets its status to Assigned.
Permissions are enforced for direct requests as well as navigation. New forms use session-bound CSRF tokens.
Passwords are hashed; login attempts are rate-limited per client IP (10 failures in 15 minutes).
Sessions expire after 8 hours of inactivity. Password recovery from the server console:

```
./venv/bin/flask --app app reset-password
```

For production, set a persistent random `SECRET_KEY` for all processes, serve over HTTPS and set
`COOKIE_SECURE=1`. Without `SECRET_KEY`, a random per-process key is used and sessions end on restart.
Use a production WSGI server rather than Flask's development server, persistent database storage,
and protected off-site backups. Backups contain sensitive business data and account password hashes.

Optional environment variables:
- `APP_USER` - first Owner username (default `admin`), used only for initial environment-based setup.
- `APP_PASSWORD` - alternative to `create-owner`: creates the first Owner only if no accounts exist
  (minimum 12 characters). Changing this variable does not reset an existing account's password.
- `SHOP_NAME`, `SHOP_ADDRESS`, `SHOP_PHONE` - shown on the invoice.
- `BACKUP_DIR` - the app saves a copy of the database once a day in `backups/` (last 14 kept); More > Download backup gives you a copy any time.
