"""Parser for 2B_APC.csv and Pre_APC.csv main sheets."""
import io
import re
from typing import Optional
import pandas as pd

CATEGORY_STANDARD = "Standard"
CATEGORY_PRECISION = "Precision"


def _safe_float(val) -> Optional[float]:
    try:
        s = str(val).replace(",", "").strip()
        if s in ("", "nan", "NaN", "-"):
            return None
        return float(s)
    except Exception:
        return None


def _get_group_n_code(row, last: str) -> str:
    """col[3] Group n-code: forward-fill merged Excel cells."""
    val = str(row.iloc[3]).strip().split("\n")[0].strip()
    if val and val != "nan":
        return val
    return last


def _normalize_ym(raw: str) -> str:
    """Convert '2025-1' → '2025-01', '2026-10' stays."""
    m = re.match(r"^(\d{4})-(\d{1,2})$", raw.strip())
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return raw.strip()


def parse_main_sheet(filepath: str, category: str) -> tuple[list[dict], str | None]:
    """
    Parse a 2B_APC or Pre_APC CSV file.

    Returns (records, as_of_date) where records is a list of dicts with:
    - category, grade_astm, grade_en, thickness_mm, n_code, width_mm
    - customer_sold_to, customer_ship_to
    - monthly_quantities: dict {YYYY-MM: kg}
    - order_balance_qty, produced_qty, production_balance_qty
    - preparation_qty, production_plan_qty, as_of_date
    """
    with open(filepath, encoding="cp949", errors="replace") as f:
        content = f.read()

    df = pd.read_csv(
        io.StringIO(content),
        sep=";",
        header=None,
        dtype=str,
        on_bad_lines="skip",
    )

    # Locate header row: first row where col 0 == "Grade (ASTM)"
    header_idx = None
    for i in range(min(15, len(df))):
        val = str(df.iloc[i, 0]).strip()
        if val == "Grade (ASTM)":
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(f"Could not find 'Grade (ASTM)' header in {filepath}")

    raw_names = df.iloc[header_idx].tolist()
    col_names = [
        str(c).replace("\n", " ").replace("\r", "").strip()
        if str(c) not in ("nan", "")
        else ""
        for c in raw_names
    ]

    # Data starts 2 rows after header (header + formula/annotation row)
    data_start = header_idx + 2
    data_df = df.iloc[data_start:].reset_index(drop=True)

    # ----- Column detection -----
    # 2B: width at col 9; Pre: width at col 9
    # Customer sold-to: col 13 in both
    # Customer ship-to: col 14 in both
    # For Pre: cols 7,8 = TS min/max → customer cols shift out by 0 (still 13,14)
    width_col = 9
    sold_to_col = 13
    ship_to_col = 14

    # Monthly columns: first occurrence of each YYYY-MM pattern
    monthly_cols: dict[str, int] = {}
    for i, name in enumerate(col_names):
        m = re.match(r"^(\d{4}-\d{1,2})$", name)
        if m:
            ym = _normalize_ym(m.group(1))
            if ym not in monthly_cols:
                monthly_cols[ym] = i

    # Snapshot columns (Order Balance, Produced, Production Balance)
    as_of_date: str | None = None
    order_balance_col: int | None = None
    produced_col: int | None = None
    prod_balance_col: int | None = None
    # Multiple James Preparation columns (oldest→newest order)
    james_prep_cols: list[int] = []
    # Multiple Irinel Production Plan columns (DEC→JUL order)
    irinel_plan_cols: list[int] = []

    for i, name in enumerate(col_names):
        if not name:
            continue
        name_upper = name.upper()
        if ("ORER BALANCE" in name_upper or "ORDER BALANCE" in name_upper) and (
            "QUANTITY" in name_upper or "QTY" in name_upper
        ):
            order_balance_col = i
            # Extract day/month for as_of_date (case-insensitive)
            dm = re.search(r"(\d+)(?:th|st|nd|rd)?\s+([A-Za-z]{3})", name, re.IGNORECASE)
            if dm:
                day, mon = dm.group(1), dm.group(2).capitalize()
                try:
                    from datetime import datetime

                    dt = datetime.strptime(f"{day} {mon} 2026", "%d %b %Y")
                    as_of_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    as_of_date = "2026-06-24"
        elif "PRODUCED QUANTITY" in name_upper:
            produced_col = i
        elif "PRODUCTION BALANCE QUANTITY" in name_upper:
            prod_balance_col = i
        elif "JAMES" in name_upper and "PREPARATION" in name_upper:
            james_prep_cols.append(i)
        elif "IRINEL" in name_upper and "PLAN" in name_upper:
            irinel_plan_cols.append(i)

    # ----- Parse rows -----
    records: list[dict] = []
    last_group_n_code: str = ""  # forward-fill merged Group n-code cells
    for _, row in data_df.iterrows():
        grade = str(row.iloc[0]).strip()
        if not grade or grade == "nan":
            continue  # subtotal row

        # Build monthly dict
        monthly: dict[str, float] = {}
        for ym, ci in monthly_cols.items():
            v = _safe_float(row.iloc[ci])
            if v is not None:
                monthly[ym] = v

        # SUM of all James Preparation quantity columns
        james_total = sum((_safe_float(row.iloc[ci]) or 0) for ci in james_prep_cols)
        prep_qty = james_total if james_total > 0 else None

        # SUM of all Irinel Production Plan quantities (each column = one month's plan)
        irinel_total = sum(
            (_safe_float(row.iloc[ci]) or 0) for ci in irinel_plan_cols
        )

        # forward-fill: update last seen group n_code if this row has one
        raw_n = str(row.iloc[3]).strip().split("\n")[0].strip()
        if raw_n and raw_n != "nan":
            last_group_n_code = raw_n

        records.append(
            {
                "category": category,
                "grade_astm": grade,
                "grade_en": str(row.iloc[1]).strip() if str(row.iloc[1]) != "nan" else "",
                "thickness_mm": _safe_float(row.iloc[2]),
                "n_code": _get_group_n_code(row, last_group_n_code),
                "covers_n_code": (
                    str(row.iloc[6]).strip()
                    if str(row.iloc[6]) not in ("nan", "")
                    else ""
                ),
                "width_mm": _safe_float(row.iloc[width_col]),
                "customer_sold_to": (
                    str(row.iloc[sold_to_col]).strip()
                    if str(row.iloc[sold_to_col]) != "nan"
                    else ""
                ),
                "customer_ship_to": (
                    str(row.iloc[ship_to_col]).strip()
                    if str(row.iloc[ship_to_col]) != "nan"
                    else ""
                ),
                "monthly_quantities": monthly,
                "order_balance_qty": (
                    _safe_float(row.iloc[order_balance_col])
                    if order_balance_col is not None
                    else None
                ),
                "produced_qty": (
                    _safe_float(row.iloc[produced_col])
                    if produced_col is not None
                    else None
                ),
                "production_balance_qty": (
                    _safe_float(row.iloc[prod_balance_col])
                    if prod_balance_col is not None
                    else None
                ),
                "preparation_qty": prep_qty,
                "production_plan_qty": irinel_total if irinel_total > 0 else None,
                "as_of_date": as_of_date,
            }
        )

    return records, as_of_date
