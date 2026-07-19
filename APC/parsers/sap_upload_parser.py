"""Parser for SAP upload files: 15 (orders), 25 (production), 15 APC (pre-production), 104 (pending/unreleased orders)."""
import io
import re
from datetime import date, timedelta
from typing import Optional
import pandas as pd

TABLE_15 = "15"
TABLE_25 = "25"
TABLE_15APC = "15_apc"
TABLE_104 = "104"

_EXCEL_EPOCH = date(1899, 12, 30)


def _excel_serial_to_month(val) -> Optional[str]:
    """Convert an Excel/SAP serial date number (e.g. '46318') to a 'YYYY-MM' string."""
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return (_EXCEL_EPOCH + timedelta(days=float(s))).strftime("%Y-%m")
    except Exception:
        return None


def _safe_float(val) -> Optional[float]:
    try:
        s = str(val).replace(",", "").strip()
        if s in ("", "nan", "NaN", "-"):
            return None
        return float(s)
    except Exception:
        return None


def _detect_table_type(header_line: str) -> Optional[str]:
    """Identify table type from a single label line like '15;;', '25;;', '15 APC;;', '104,,,'."""
    first = re.split(r"[;,]", header_line, maxsplit=1)[0].strip()
    if first == "15 APC":
        return TABLE_15APC
    if first == "25":
        return TABLE_25
    if first == "15":
        return TABLE_15
    if first == "104":
        return TABLE_104
    return None


def _parse_table_15(df: pd.DataFrame) -> list[dict]:
    cols = [str(c).strip() for c in df.columns]
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "month": str(row.get("month", "")).strip(),
                "sdg_sold_to_party": str(row.get("SDG Sold To Party", "")).strip(),
                "ship_to": str(row.get("Ship To", "")).strip(),
                "sdg_sold_to_party_name": str(row.get("SDG Sold To Party Name", "")).strip(),
                "ship_to_party": str(row.get("Ship To Party", "")).strip(),
                "country_code": str(row.get("Country Code", "")).strip(),
                "sdg_so_number": str(row.get("SDG S/O Number", "")).strip(),
                "material": str(row.get("Material", "")).strip(),
                "po_item": str(row.get("POitem", "")).strip(),
                "order_qty": _safe_float(row.get("Order Qty", row.get("     Order Qty"))),
                "request_delivery_date": str(row.get("RequestDeliveryDate", "")).strip(),
                "otx_date_created": str(row.get("OTX Date Created", "")).strip(),
                "order_type": str(row.get("Order Type", "")).strip(),
                "surface": str(row.get("Surface", "")).strip(),
                "delivered_qty": _safe_float(row.get("Delivered Qty")),
                "thickness": _safe_float(row.get("Thickness")),
                "created_by": str(row.get("Created By", "")).strip(),
                "in_production_qty": _safe_float(row.get("In Production Qty")),
                "final_qty": _safe_float(row.get("Final Qty")),
                "status_description": str(row.get("Status Description", "")).strip(),
                "reason": str(row.get("Reason", "")).strip(),
                "ncode": str(row.get("Ncode", "")).strip(),
                "grade": str(row.get("Grade", "")).strip(),
            }
        )
    return records


def _parse_table_25(df: pd.DataFrame) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "month": str(row.get("month", "")).strip(),
                "batch": str(row.get("Batch", "")).strip(),
                "coil_no": str(row.get("Coil No", "")).strip(),
                "grade": str(row.get("Grade", "")).strip(),
                "n_code": str(row.get("N-code", "")).strip(),
                "otx_order": str(row.get("OTX Order", "")).strip(),
                "posting_date": str(row.get("Posting Date", "")).strip(),
                "quantity": _safe_float(row.get("Quantity")),
                "rm_surface": str(row.get("RM Surface", "")).strip(),
                "sdg_order": str(row.get("SDG Order", "")).strip(),
                "ship_to": str(row.get("Ship-to", "")).strip(),
                "ship_to_party": str(row.get("Ship-to party", "")).strip(),
                "so_item": str(row.get("SO Item", "")).strip(),
                "sold_to": str(row.get("Sold-to", "")).strip(),
                "sold_to_party": str(row.get("Sold-to party", "")).strip(),
                "supplier_code": str(row.get("Supplier Code", "")).strip(),
                "thickness": _safe_float(row.get("Thickness")),
                "value": _safe_float(row.get("Value")),
                "width": _safe_float(row.get("Width")),
            }
        )
    return records


def _parse_table_15apc(df: pd.DataFrame) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "sdg_so_number": str(row.get("SDG S/O Number", "")).strip(),
                "ncode": str(row.get("Ncode", "")).strip(),
                "customer_po_no": str(row.get("Customer PO No.", "")).strip(),
                "otx_item": str(row.get("Otx Item", "")).strip(),
                "surface": str(row.get("Surface", "")).strip(),
                "grade": str(row.get("Grade", "")).strip(),
                "thickness": _safe_float(row.get("Thickness")),
                "width": _safe_float(row.get("Width")),
                "material": str(row.get("Material", "")).strip(),
                "description": str(row.get("Description", "")).strip(),
                "order_qty": _safe_float(row.get("Order Qty")),
                "in_production_qty": _safe_float(row.get("In Production Qty")),
                "produced_qty": _safe_float(row.get("Produced Qty")),
                "in_transit": _safe_float(row.get("In Transit")),
                "transfered_qty": _safe_float(row.get("Transfered Qty")),
                "final_qty": _safe_float(row.get("Final Qty")),
                "production_balance": _safe_float(row.get("Production Balance")),
                "warehouse_stock_qty": _safe_float(row.get("Warehouse STOCK Qty")),
                "request_delivery_date": str(row.get("RequestDeliveryDate", "")).strip(),
                "otx_sales_order": str(row.get("OTX Sales Order", "")).strip(),
                "month": str(row.get("month", "")).strip(),
            }
        )
    return records


def _parse_table_104(df: pd.DataFrame) -> list[dict]:
    """104 = pending orders not yet released to the factory (unlike '15', which is released)."""
    records = []
    for _, row in df.iterrows():
        delivery_date_raw = str(row.get("Delivery Date", "")).strip()
        records.append(
            {
                "sold_to_code": str(row.get("Sold to Code", "")).strip(),
                "sold_to_party": str(row.get("Sold to Party", "")).strip(),
                "ship_to_code": str(row.get("Ship to Code", "")).strip(),
                "ship_to_party": str(row.get("Ship to Party", "")).strip(),
                "sdg_so_number": str(row.get("SDG S/O No.", "")).strip(),
                "sdg_item_no": str(row.get("SDG Item No.", "")).strip(),
                "material_code": str(row.get("Mat.Code SDG", "")).strip(),
                "ncode": str(row.get("N-Code", "")).strip(),
                "material_desc": str(row.get("Material Desc.SDG", "")).strip(),
                "pending_qty": _safe_float(row.get("Pending Qty.")),
                "delivery_date": delivery_date_raw,
                "delivery_month": _excel_serial_to_month(delivery_date_raw),
                "order_rcv_date": str(row.get("Order Rcv.Date", "")).strip(),
                "surface": str(row.get("Surface", "")).strip(),
            }
        )
    return records


def parse_combined_file(filepath: str) -> dict[str, list[dict]]:
    """
    Parse a single Up_APC file containing all 3 tables separated by label rows.
    Returns {'15': [...], '25': [...], '15_apc': [...]}.
    """
    with open(filepath, encoding="ascii", errors="replace") as f:
        content = f.read()

    lines = content.splitlines()

    # Split into sections
    sections: dict[str, list[str]] = {}
    current_type: Optional[str] = None
    current_lines: list[str] = []

    for line in lines:
        t = _detect_table_type(line)
        if t is not None:
            if current_type and current_lines:
                sections[current_type] = current_lines
            current_type = t
            current_lines = []
        else:
            if current_type is not None:
                current_lines.append(line)

    if current_type and current_lines:
        sections[current_type] = current_lines

    result: dict[str, list[dict]] = {TABLE_15: [], TABLE_25: [], TABLE_15APC: [], TABLE_104: []}
    parsers = {
        TABLE_15: _parse_table_15, TABLE_25: _parse_table_25,
        TABLE_15APC: _parse_table_15apc, TABLE_104: _parse_table_104,
    }

    for ttype, tlines in sections.items():
        if not tlines:
            continue
        raw_text = "\n".join(tlines)
        # 104 section is comma-delimited; 15/25/15APC are semicolon-delimited
        sep = "," if ttype == TABLE_104 else ";"
        try:
            df = pd.read_csv(
                io.StringIO(raw_text),
                sep=sep,
                dtype=str,
                on_bad_lines="skip",
            )
            # Strip whitespace from column names
            df.columns = [str(c).strip() for c in df.columns]
            # Drop fully empty rows
            df = df.dropna(how="all")
            result[ttype] = parsers[ttype](df)
        except Exception as e:
            print(f"[sap_upload_parser] Error parsing table {ttype}: {e}")

    return result


def parse_single_file(filepath: str, table_type: str) -> list[dict]:
    """
    Parse a single file that contains only one table (no label row).
    table_type: '15', '25', '15_apc', or '104'
    """
    pmap = {"15": TABLE_15, "25": TABLE_25, "15_apc": TABLE_15APC, "15 apc": TABLE_15APC, "104": TABLE_104}
    key = pmap.get(table_type.lower(), table_type.lower())
    sep = "," if key == TABLE_104 else ";"

    try:
        df = pd.read_csv(filepath, sep=sep, dtype=str, on_bad_lines="skip", encoding="ascii", errors="replace")
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")
    except Exception:
        df = pd.read_csv(filepath, sep=sep, dtype=str, on_bad_lines="skip")
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")

    parsers = {
        TABLE_15: _parse_table_15, TABLE_25: _parse_table_25,
        TABLE_15APC: _parse_table_15apc, TABLE_104: _parse_table_104,
    }
    return parsers[key](df)


def auto_detect_table_type(filepath: str) -> Optional[str]:
    """Detect table type from first non-empty line of the file."""
    try:
        with open(filepath, encoding="ascii", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = _detect_table_type(line)
                if t:
                    return t
                # Also check if it looks like a header row
                if "SDG S/O Number" in line and "Ncode" in line:
                    return TABLE_15APC
                if "Batch" in line and "Coil No" in line:
                    return TABLE_25
                if "SDG Sold To Party" in line:
                    return TABLE_15
                if "Pending Qty" in line and "SDG S/O No" in line:
                    return TABLE_104
    except Exception:
        pass
    return None
