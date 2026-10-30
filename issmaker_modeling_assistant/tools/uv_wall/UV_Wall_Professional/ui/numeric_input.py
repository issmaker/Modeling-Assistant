"""User-facing UV Wall 1.5.3 numeric contract.

Lengths are metres, angles degrees.  UI never displays unit suffixes.
No implicit integer->millimetre conversion; no silent quantization.
"""
import math
import re

_LITERAL = re.compile(r'^[+-]?(?:\d+(?:[\.,]\d*)?|[\.,]\d+)$')


def parse_value(text,current=0.0,angle=False):
    """Strict absolute numeric entry; numbers have one visible meaning.

    4 means 4 m for lengths, 4 degrees for angles; -0.05 is absolute.
    `current` remains for old callers but does not affect absolute values.
    """
    raw=str(text).strip().replace(' ', '').replace('\u00a0','')
    if not _LITERAL.fullmatch(raw):
        raise ValueError('Введите число, например 4.25 или -0.05')
    try:
        value=float(raw.replace(',','.'))
    except (ValueError, OverflowError):
        raise ValueError('Введите корректное число') from None
    if not math.isfinite(value):
        raise ValueError('Число должно быть конечным')
    return value


def format_value(value,digits=6):
    """Human-readable number without a unit; never rounds the stored value."""
    value=float(value)
    if not math.isfinite(value):return '—'
    if abs(value)<.5*10**-digits:value=0.0
    result=f'{value:.{digits}f}'.rstrip('0').rstrip('.')
    return '0' if result in ('','-0') else result
