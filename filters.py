import calendar
import re
from datetime import date

from flask import abort

from db import parse_date
from i18n import _


def date_window(args, default="all"):
    period = args.get("period", default)
    month = args.get("month", date.today().strftime("%Y-%m"))
    start, end = args.get("from_date", ""), args.get("to_date", "")
    if period == "month":
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            abort(400, description=_("Select a valid month."))
        first = parse_date(month + "-01")
        if not first:
            abort(400, description=_("Select a valid month."))
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        start, end = first.isoformat(), last.isoformat()
    elif period == "range":
        first, last = parse_date(start), parse_date(end)
        if not first or not last or last < first:
            abort(400, description=_("Enter valid start and end dates (end on/after start)."))
        start, end = first.isoformat(), last.isoformat()
    elif period == "all":
        start = end = ""
    else:
        abort(400, description=_("Select a valid period."))
    return {"period": period, "month": month, "from_date": start, "to_date": end}
