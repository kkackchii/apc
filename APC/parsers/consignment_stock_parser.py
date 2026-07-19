"""Parser for Consignment Stock CSV uploads (warehouse stock held for consignment customers)."""
from typing import Optional
import pandas as pd

_COL_ALIASES = {
    "ncode": ["N-Code", "N-code", "Ncode", "NCode"],
    "thickness": ["Thickness"],
    "width": ["Width"],
    "ship_to": ["Ship-to", "Ship To", "Ship-To"],
    "stock": ["Consignment Stock", "Stock", "Stock Qty", "Stock (MT)"],
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


def parse_consignment_stock_csv(filepath: str) -> list[dict]:
    """
    Expected columns: N-Code, Thickness, Width, Ship-to, Consignment Stock
    Returns [{"ncode", "thickness_mm", "width_mm", "ship_to", "stock_mt"}]
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
    stock_col = _find_col(list(df.columns), _COL_ALIASES["stock"])

    records = []
    for _, row in df.iterrows():
        ncode = str(row.get(ncode_col, "")).strip() if ncode_col else ""
        if not ncode or ncode.lower() == "nan":
            continue
        stock_mt = _safe_float(row.get(stock_col)) if stock_col else None
        if stock_mt is None:
            continue
        records.append(
            {
                "ncode": ncode,
                "thickness_mm": _safe_float(row.get(thickness_col)) if thickness_col else None,
                "width_mm": _safe_float(row.get(width_col)) if width_col else None,
                "ship_to": str(row.get(ship_to_col, "")).strip() if ship_to_col else "",
                "stock_mt": stock_mt,
            }
        )
    return records
