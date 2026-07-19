"""Automatic data load from fixed file paths in data/ on server startup.

Drop the latest weekly export under these exact names in data/ and just restart the
server — no need to re-upload through the web UI:

    2B_APC.csv   Standard main sheet (historical monthly columns, Jan~Jun etc.)
    Pre_APC.csv  Precision main sheet
    15.csv       SDG sales orders (month = requested delivery month, Jul~Dec)
    25.csv       Production records
    15APC.csv    Pre-production orders (Production Input = Order Qty / Produced Qty = In Transit)
    104.csv      Pending orders — booked in SAP but not yet released to the factory
    Customer_Forecast.csv  N-Code, Thickness, Width, Ship-to, 2026-10, 2026-11, 2026-12
    Consignment_Stock.csv  N-Code, Thickness, Width, Ship-to, Consignment Stock

Any file that isn't present is skipped. 2B/Pre main sheets and 15/25/15APC/104 are
treated as full snapshot replaces (so restarting repeatedly with the same file doesn't
accumulate duplicates). Customer Forecast and Consignment Stock are upserted so manual
edits made via the dashboard are never wiped by a restart.
"""
from datetime import datetime
from pathlib import Path

from app.paths import BASE_DIR
from app.database import (
    bulk_insert_ncode_items,
    bulk_insert_sap_15,
    bulk_insert_sap_15apc,
    bulk_insert_sap_25,
    bulk_insert_sap_104,
    get_db,
    get_ncode_grade_map,
    insert_upload_history,
    snapshot_order_data,
    snapshot_production_data,
    upsert_consignment_stock,
    upsert_customer_forecast,
)
from parsers.consignment_stock_parser import parse_consignment_stock_csv
from parsers.customer_forecast_parser import parse_customer_forecast_csv
from parsers.main_sheet_parser import CATEGORY_PRECISION, CATEGORY_STANDARD, parse_main_sheet
from parsers.sap_upload_parser import (
    TABLE_15,
    TABLE_15APC,
    TABLE_25,
    TABLE_104,
    auto_detect_table_type,
    parse_single_file,
)

DATA_DIR = BASE_DIR / "data"
FILE_2B = DATA_DIR / "2B_APC.csv"
FILE_PRE = DATA_DIR / "Pre_APC.csv"
FILE_15 = DATA_DIR / "15.csv"
FILE_25 = DATA_DIR / "25.csv"
FILE_15APC = DATA_DIR / "15APC.csv"
FILE_104 = DATA_DIR / "104.csv"
FILE_FORECAST = DATA_DIR / "Customer_Forecast.csv"
FILE_CONSIGNMENT = DATA_DIR / "Consignment_Stock.csv"

_SAP_TABLES = [
    (FILE_15, TABLE_15, "sap_orders", bulk_insert_sap_15),
    (FILE_25, TABLE_25, "sap_production", bulk_insert_sap_25),
    (FILE_15APC, TABLE_15APC, "sap_apc_orders", bulk_insert_sap_15apc),
    (FILE_104, TABLE_104, "sap_pending_orders", bulk_insert_sap_104),
]


def _load_main_sheet(conn, path: Path, category: str, upload_type: str):
    records, as_of = parse_main_sheet(str(path), category)
    conn.execute("DELETE FROM ncode_items WHERE category=?", (category,))
    uid = insert_upload_history(conn, upload_type, path.name, as_of or datetime.now().strftime("%Y-%m-%d"), len(records))
    bulk_insert_ncode_items(conn, records, uid)
    print(f"[autoload] {path.name}: {len(records)} rows")


def _snapshot_before_replace(conn, detected: str, table_name: str):
    """Save a week-over-week snapshot of the data about to be deleted (see
    app/routers/changes.py). Only sap_orders (15.csv) and sap_apc_orders (15APC.csv)
    are tracked. Tagged with the *previous* upload's as_of_date so restarting with an
    unchanged file doesn't create a duplicate snapshot (snapshot_order_data/
    snapshot_production_data no-op if that as_of_date was already snapshotted)."""
    if table_name not in ("sap_orders", "sap_apc_orders"):
        return
    prev = conn.execute(
        "SELECT as_of_date FROM upload_history WHERE upload_type=? ORDER BY uploaded_at DESC, id DESC LIMIT 1",
        (detected,),
    ).fetchone()
    if not prev or not prev["as_of_date"]:
        return
    ncode_grade_map = get_ncode_grade_map(conn)
    if table_name == "sap_orders":
        snapshot_order_data(conn, prev["as_of_date"], ncode_grade_map)
    else:
        snapshot_production_data(conn, prev["as_of_date"], ncode_grade_map)


def _load_sap_file(conn, path: Path, expected_type: str, table_name: str, inserter):
    detected = auto_detect_table_type(str(path)) or expected_type
    records = parse_single_file(str(path), detected)
    _snapshot_before_replace(conn, detected, table_name)
    conn.execute(f"DELETE FROM {table_name}")
    as_of = datetime.now().strftime("%Y-%m-%d")
    uid = insert_upload_history(conn, detected, path.name, as_of, len(records))
    inserter(conn, records, uid)
    print(f"[autoload] {path.name}: {len(records)} rows")


def _load_customer_forecast(conn, path: Path):
    records = parse_customer_forecast_csv(str(path))
    cells = 0
    for rec in records:
        for ym, qty in rec["monthly"].items():
            upsert_customer_forecast(
                conn, rec["ncode"], rec.get("thickness_mm"), rec.get("width_mm"),
                rec.get("ship_to"), ym, qty,
            )
            cells += 1
    print(f"[autoload] {path.name}: {len(records)} rows, {cells} cells upserted")


def _load_consignment_stock(conn, path: Path):
    records = parse_consignment_stock_csv(str(path))
    for rec in records:
        upsert_consignment_stock(
            conn, rec["ncode"], rec.get("thickness_mm"), rec.get("width_mm"),
            rec.get("ship_to"), rec.get("stock_mt"),
        )
    print(f"[autoload] {path.name}: {len(records)} rows upserted")


def autoload_data_files():
    with get_db() as conn:
        if FILE_2B.exists():
            try:
                _load_main_sheet(conn, FILE_2B, CATEGORY_STANDARD, "main_std")
            except Exception as e:
                print(f"[autoload] Failed to load {FILE_2B.name}: {e}")

        if FILE_PRE.exists():
            try:
                _load_main_sheet(conn, FILE_PRE, CATEGORY_PRECISION, "main_pre")
            except Exception as e:
                print(f"[autoload] Failed to load {FILE_PRE.name}: {e}")

        for path, expected_type, table_name, inserter in _SAP_TABLES:
            if not path.exists():
                continue
            try:
                _load_sap_file(conn, path, expected_type, table_name, inserter)
            except Exception as e:
                print(f"[autoload] Failed to load {path.name}: {e}")

        if FILE_FORECAST.exists():
            try:
                _load_customer_forecast(conn, FILE_FORECAST)
            except Exception as e:
                print(f"[autoload] Failed to load {FILE_FORECAST.name}: {e}")

        if FILE_CONSIGNMENT.exists():
            try:
                _load_consignment_stock(conn, FILE_CONSIGNMENT)
            except Exception as e:
                print(f"[autoload] Failed to load {FILE_CONSIGNMENT.name}: {e}")

        # After everything is loaded, snapshot per-N-code forecast/ordered figures under
        # the current as_of_date so the dashboard can show week-over-week deltas.
        # Imported here (not at module top) to avoid app.routers <-> app.autoload cycles.
        from app.routers.dashboard import take_ncode_snapshot
        for category in ("Standard", "Precision"):
            try:
                take_ncode_snapshot(conn, category)
            except Exception as e:
                print(f"[autoload] Failed to snapshot {category}: {e}")
