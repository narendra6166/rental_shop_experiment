import re


def valid_phone(value):
    return re.fullmatch(r"[0-9]{10}", value) is not None
