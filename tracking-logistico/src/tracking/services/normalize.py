"""Normalización de valores provenientes de Excel (texto, fechas, números, cédulas, teléfonos)."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

EXCEL_ERRORS = {"#N/A", "#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!"}
_EXCEL_EPOCH = datetime(1899, 12, 30)
_WS = re.compile(r"\s+")
_Q4 = Decimal("0.0001")


def is_excel_error(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() in EXCEL_ERRORS


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or is_excel_error(value)
    return False


def norm_key(value: Any) -> str:
    """Clave de comparación: minúsculas, sin acentos, sin puntuación, espacios colapsados."""
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9#%]+", " ", text)
    return _WS.sub(" ", text).strip()


def clean_text(value: Any, max_len: int | None = None) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = _WS.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:max_len] if max_len else text


def clean_code(value: Any, max_len: int = 40) -> str | None:
    """Códigos (factura, pedido, vendedor, placa): mayúsculas y sin espacios internos."""
    text = clean_text(value)
    if text is None:
        return None
    text = text.upper().replace(" ", "")
    return text[:max_len] or None


def clean_cedula(value: Any) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    digits = re.sub(r"\D", "", str(value))
    return digits or None


def clean_phone(value: Any) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        digits = str(int(value))
    else:
        digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    # Celulares venezolanos guardados como número pierden el 0 inicial (4127889338 -> 04127889338)
    if len(digits) == 10 and digits[0] in "24":
        digits = "0" + digits
    return digits


MIN_YEAR = 1990  # fechas anteriores son errores de captura (p.ej. "39" leído como 08/02/1900)


def to_date(value: Any) -> date | None:
    d = _to_date(value)
    return d if d is not None and d.year >= MIN_YEAR else None


def _to_date(value: Any) -> date | None:
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 < value < 80000:  # rango razonable de seriales Excel (1954-2119)
            return (_EXCEL_EPOCH + timedelta(days=float(value))).date()
        return None
    text = str(value).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_decimal(value: Any) -> Decimal | None:
    if is_blank(value) or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            dec = Decimal(repr(value))
        else:
            text = str(value).strip().replace(" ", "")
            if "," in text and "." in text:
                text = text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", ".")
            dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    return dec.quantize(_Q4, rounding=ROUND_HALF_UP)


def to_int(value: Any) -> int | None:
    dec = to_decimal(value)
    if dec is None:
        return None
    return int(dec) if dec == dec.to_integral_value() else None
