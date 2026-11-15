# Event Hire Shop

Rental management for tents, cooking items, furniture, etc. (marriages, birthdays, parties).

Features: inventory, customers, bookings with date-based availability checks, advance/payments,
refundable deposits, status tracking (Booked/Delivered/Returned/Cancelled), overdue alerts, printable invoices.

## Run

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Open http://127.0.0.1:5000. The SQLite database (`rental.db`) is created with sample items on first run.
Set `SECRET_KEY` in the environment for production. Change `SHOP_NAME` in `app.py` to your shop's name.
