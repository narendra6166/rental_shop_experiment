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
| Agent | Customers, bookings and booking status; read stock; no payments, payroll, expenses or finance reports |
| Accounts | Read bookings and customers; record payments, cancellation fees, expenses and salary payments; view reports |
| Staff / Worker | Their assigned work and customer contact/location for those jobs only; update their task status |

On a booking, Owners assign a task to an active Staff/Worker login. The assignee sees it under My work.
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
