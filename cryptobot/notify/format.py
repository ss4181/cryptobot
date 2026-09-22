"""Number / date formatting for notification text (Turkish conventions).

Everything the notification layer prints goes through this module so the same
value always looks the same everywhere (phone, console log, audit file):

* **thousands separator** ``.``  and **decimal separator** ``,``  (``71.679,51``),
* a sensible number of decimals per magnitude (a 71k BTC price is not printed
  with 8 decimals, a 0,00063 BTC quantity is not printed with 2),
* PnL always carries an explicit sign (``+0,81`` / ``-1,24``),
* a missing value is ``n/a`` -- never ``0`` and never a placeholder that looks
  like a measurement.

Pure functions only: no state, no I/O, no imports from the rest of cryptobot.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

THOUSANDS = "."
DECIMAL = ","
NA = "n/a"


def to_float(value: Any) -> Optional[float]:
    """Coerce ``value`` to a finite float, else ``None`` (NaN/inf are not numbers)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _group(text: str) -> str:
    """Insert ``.`` between thousands of the integer part of ``text``."""
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if "." in text:
        integer_part, fraction = text.split(".", 1)
    else:
        integer_part, fraction = text, ""
    grouped = ""
    while len(integer_part) > 3:
        grouped = THOUSANDS + integer_part[-3:] + grouped
        integer_part = integer_part[:-3]
    grouped = integer_part + grouped
    result = grouped + (DECIMAL + fraction if fraction else "")
    return ("-" + result) if negative else result


def number(value: Any, digits: int = 2, *, signed: bool = False, grouping: bool = True) -> str:
    """Fixed-precision number in Turkish notation; ``signed`` adds ``+`` for positives."""
    parsed = to_float(value)
    if parsed is None:
        return NA
    if parsed == 0:
        parsed = 0.0  # collapse -0.0
    text = "{:.{digits}f}".format(parsed, digits=max(0, int(digits)))
    if grouping:
        text = _group(text)
    if signed and parsed > 0:
        text = "+" + text
    return text


def price_digits(value: Any) -> int:
    """Decimals that keep a price readable without lying about its magnitude."""
    parsed = to_float(value)
    if parsed is None:
        return 2
    magnitude = abs(parsed)
    if magnitude >= 1.0:
        return 2
    if magnitude >= 0.01:
        return 4
    if magnitude >= 0.0001:
        return 6
    return 8


def price(value: Any, digits: Optional[int] = None) -> str:
    """A price: ``71.679,51`` / ``0,50`` / ``n/a``."""
    parsed = to_float(value)
    if parsed is None:
        return NA
    return number(parsed, price_digits(parsed) if digits is None else digits)


def usdt(value: Any, digits: int = 2, *, signed: bool = False) -> str:
    """An amount in USDT (PnL amounts pass ``signed=True``)."""
    return number(value, digits, signed=signed)


def pct(value: Any, digits: int = 2, *, signed: bool = True) -> str:
    """A percentage *without* the ``%`` sign: ``+2,00`` / ``-2,50``."""
    return number(value, digits, signed=signed)


def pct_label(value: Any) -> str:
    """Compact percentage for a label: ``2`` / ``2,5`` (no trailing zeros)."""
    parsed = to_float(value)
    if parsed is None:
        return NA
    if float(parsed).is_integer():
        return str(int(parsed))
    return number(parsed, 1).rstrip("0").rstrip(DECIMAL)


def qty(value: Any) -> str:
    """A quantity: trailing zeros trimmed, up to 8 decimals (``0,00063``)."""
    parsed = to_float(value)
    if parsed is None:
        return NA
    digits = 8 if abs(parsed) < 1 else 4
    text = "{:.{digits}f}".format(parsed, digits=digits)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or text == "-0":
        text = "0"
    return _group(text)


def integer(value: Any) -> str:
    """A whole count: ``1.234``."""
    parsed = to_float(value)
    if parsed is None:
        return NA
    return _group(str(int(round(parsed))))


def duration(seconds: Any) -> str:
    """Human holding time: ``42sn`` / ``12dk`` / ``3s 12dk`` / ``2g 4s``."""
    parsed = to_float(seconds)
    if parsed is None:
        return NA
    total = max(0, int(round(parsed)))
    if total < 60:
        return "{}sn".format(total)
    if total < 3600:
        return "{}dk".format(total // 60)
    if total < 86400:
        hours, minutes = total // 3600, (total % 3600) // 60
        return "{}s {}dk".format(hours, minutes) if minutes else "{}s".format(hours)
    days, hours = total // 86400, (total % 86400) // 3600
    return "{}g {}s".format(days, hours) if hours else "{}g".format(days)


def utc_date(ms: Any) -> str:
    """``YYYY-MM-DD`` from an epoch-millisecond timestamp (UTC)."""
    parsed = to_float(ms)
    if parsed is None:
        return NA
    return datetime.fromtimestamp(parsed / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


def utc_stamp(ms: Any) -> str:
    """``YYYY-MM-DD HH:MM UTC`` from an epoch-millisecond timestamp."""
    parsed = to_float(ms)
    if parsed is None:
        return NA
    return datetime.fromtimestamp(parsed / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def pair_compact(pair: Any) -> str:
    """``BTC/USDT`` -> ``BTCUSDT`` (the shape phones read best)."""
    text = str(pair or "").strip()
    return text.replace("/", "").replace(" ", "")


def pairs_compact(pairs: Any) -> str:
    """``("BTC/USDT", "ETH/USDT")`` -> ``BTCUSDT/ETHUSDT``."""
    items: Tuple[Any, ...]
    if pairs is None:
        items = ()
    elif isinstance(pairs, str):
        items = (pairs,)
    else:
        try:
            items = tuple(pairs)
        except TypeError:
            items = (pairs,)
    return "/".join(pair_compact(item) for item in items if str(item or "").strip())


__all__ = [
    "THOUSANDS", "DECIMAL", "NA", "to_float", "number", "price", "price_digits",
    "usdt", "pct", "pct_label", "qty", "integer", "duration", "utc_date", "utc_stamp",
    "pair_compact", "pairs_compact",
]
