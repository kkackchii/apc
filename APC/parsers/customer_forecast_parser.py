"""Parser for Customer Forecast CSV uploads (26Q4: Oct/Nov/Dec forecast by N-code/spec/ship-to)."""
from typing import Optional
import pandas as pd

FORECAST_MONTHS = ["2026-10", "2026-11", "2026-12"]

# Accept a couple of header spellings per column, in case users export slightly differently.
_COL_ALIASES = {
    "ncode": ["N-Code", "N-code", "Ncode", "NCode"],
    "thickness": ["Thickness"],
    "width": ["Width"],
    "ship_to": ["Ship-to", "Ship To", "Ship-To"],
}


def _safe_float(val) -> Optional[float]:
    try:
        s = str(val).replace(",", "").strip()
        if s in ("", "nan", "NaN", "-"):
            return None
        return float(s)
    except Exception:
        return None


def _find_col(columns: list[str], aliases: list[str]) -> Optional[str]:
    for alias in aliases:
        if alias in columns:
            return alias
    return None


def parse_customer_forecast_csv(filepath: str) -> list[dict]:
    """
    Expected columns: N-Code, Thickness, Width, Ship-to, 2026-10, 2026-11, 2026-12
    Returns [{"ncode", "thickness_mm", "width_mm", "ship_to", "monthly": {"2026-10": qty, ...}}]
    """
    try:
        df = pd.read_csv(filepath, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(filepath, dtype=str, encoding="cp949")
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")

    ncode_col = _find_col(list(df.columns), _COL_ALIASES["ncode"])
    thickness_col = _find_col(list(df.columns), _COL_ALIASES["thickness"])
    width_col = _find_col(list(df.columns), _COL_ALIASES["width"])
    ship_to_col = _find_col(list(df.columns), _COL_ALIASES["ship_to"])

    records = []
    for _, row in df.iterrows():
        ncode = str(row.get(ncode_col, "")).strip() if ncode_col else ""
        if not ncode or ncode.lower() == "nan":
            continue
        monthly = {}
        for m in FORECAST_MONTHS:
            if m in df.columns:
                v = _safe_float(row.get(m))
                if v is not None:
                    monthly[m] = v
        records.append(
            {
                "ncode": ncode,
                "thickness_mm": _safe_float(row.get(thickness_col)) if thickness_col else None,
                "width_mm": _safe_float(row.get(width_col)) if width_col else None,
                "ship_to": str(row.get(ship_to_col, "")).strip() if ship_to_col else "",
                "monthly": monthly,
            }
        )
    return records
