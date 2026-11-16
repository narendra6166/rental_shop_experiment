# Event Hire Shop

Rental management for tents, cooking items, furniture, etc. (marriages, birthdays, parties).

Features: inventory, customers, bookings with date-based availability checks, advance/payments,
refundable deposits, status tracking (Booked/Delivered/Returned/Cancelled), overdue alerts, printable invoices.
Customer phone numbers must be exactly 10 digits; staff phone numbers are optional but follow the same rule when provided.
Enter customer details directly when booking. Customers are saved only after the booking passes validation;
identical name, phone and address details reuse a customer record. Editing details does not change other bookings.
Customers without bookings can be deleted from the Customers page; customers with booking history are protected.

## Run

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Open http://127.0.0.1:5000. The SQLite database (`rental.db`) is created with sample items on first run.
Set `SECRET_KEY` in the environment for production. 

Optional environment variables:
- `APP_USER` - login ID (default `admin`).
- `APP_PASSWORD` - turns on a login page (recommended if the app is reachable by anyone else).
- `SHOP_NAME`, `SHOP_ADDRESS`, `SHOP_PHONE` - shown on the invoice.
- `BACKUP_DIR` - the app saves a copy of the database once a day in `backups/` (last 14 kept); More > Download backup gives you a copy any time.
