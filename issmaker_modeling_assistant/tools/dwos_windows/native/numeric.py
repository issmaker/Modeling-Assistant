from __future__ import annotations
import math


def parse_meter_text(text: str, *, allow_negative: bool = False) -> float:
    s = (text or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        raise ValueError("Пустое значение")
    try:
        value = float(s)
    except ValueError as exc:
        raise ValueError("Используйте число, например 0,08 или 0.08") from exc
    if not math.isfinite(value):
        raise ValueError("Размер должен быть конечным числом")
    if not allow_negative and value < 0.0:
        raise ValueError("Отрицательное значение здесь недопустимо")
    return value


def format_meter(value: float, digits: int = 6) -> str:
    if not math.isfinite(float(value)):
        raise ValueError("Нельзя показывать NaN или бесконечность как размер")
    s = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return s if s not in {"", "-0"} else "0"
