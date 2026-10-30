"""ISSMAKER text buffer editing primitives (pure Python, no bpy).

The committed model value is never modified by this module. A temporary empty
buffer, standalone sign or decimal point is allowed until explicit commit.
"""
from __future__ import annotations

NUMERIC_CHARS = frozenset("0123456789.,-+")


def edit(buffer, caret, command, *, insert="", selected=False, allowed=NUMERIC_CHARS):
    text = str(buffer)
    pos = max(0, min(len(text), int(caret)))
    if command == "SELECT_ALL":
        return text, len(text), True
    if command == "HOME":
        return text, 0, False
    if command == "END":
        return text, len(text), False
    if command == "LEFT":
        return text, max(0, pos - 1), False
    if command == "RIGHT":
        return text, min(len(text), pos + 1), False
    if command == "COPY":
        return text, pos, selected
    if command in ("DELETE", "BACKSPACE"):
        if selected:
            return "", 0, False
        if command == "DELETE":
            return text[:pos] + text[pos+1:], pos, False
        if pos == 0:
            return text, pos, False
        return text[:pos-1] + text[pos:], pos-1, False
    if command == "INSERT":
        if not insert or any(not ch.isprintable() or (allowed is not None and ch not in allowed)
                             for ch in insert):
            return text, pos, selected
        if selected:
            text, pos = "", 0
        return text[:pos] + insert + text[pos:], pos + len(insert), False
    raise ValueError(f"Unsupported edit command: {command}")


def caret_from_x(text, click_x, text_x, measure_width):
    """Find the nearest character boundary using actual renderer text widths."""
    value = str(text)
    relative = float(click_x) - float(text_x)
    widths = [float(measure_width(value[:index])) for index in range(len(value)+1)]
    return min(range(len(widths)), key=lambda index: (abs(widths[index]-relative), index))
