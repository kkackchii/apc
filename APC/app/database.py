"""SQLite database connection and schema management."""
import json
import sqlite3
from contextlib import contextmanager

from app.paths import BASE_DIR

DB_PATH = BASE_DIR / "apc.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_type TEXT NOT NULL,
    filename TEXT,
    as_of_date TEXT,
    row_count INTEGER DEFAULT 0,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS target_quantities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    grade TEXT NOT NULL,
    target_qty_mt REAL NOT NULL DEFAULT 0,
    quarter TEXT DEFAULT '2025-Q4',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, grade, quarter)
);

CREATE TABLE IF NOT EXISTS ncode_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    grade_astm TEXT NOT NULL,
    grade_en TEXT,
    thickness_mm REAL,
    n_code TEXT,
    covers_n_code TEXT,
    width_mm REAL,
    customer_sold_to TEXT,
    customer_ship_to TEXT,
    order_balance_qty REAL,
    produced_qty REAL,
    production_balance_qty REAL,
    preparation_qty REAL,
    production_plan_qty REAL,
    as_of_date TEXT,
    upload_id INTEGER REFERENCES upload_history(id),
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monthly_quantities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ncode_item_id INTEGER NOT NULL REFERENCES ncode_items(id) ON DELETE CASCADE,
    year_month TEXT NOT NULL,
    quantity_kg REAL
);
CREATE INDEX IF NOT EXISTS idx_monthly_item ON monthly_quantities(ncode_item_id);
CREATE INDEX IF NOT EXISTS idx_monthly_ym ON monthly_quantities(year_month);

CREATE TABLE IF NOT EXISTS sap_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER REFERENCES upload_history(id),
    month TEXT,
    sdg_sold_to_party TEXT,
    ship_to TEXT,
    sdg_sold_to_party_name TEXT,
    ship_to_party TEXT,
    country_code TEXT,
    sdg_so_number TEXT,
    material TEXT,
    po_item TEXT,
    order_qty REAL,
    request_delivery_date TEXT,
    otx_date_created TEXT,
    order_type TEXT,
    surface TEXT,
    delivered_qty REAL,
    thickness REAL,
    created_by TEXT,
    in_production_qty REAL,
    final_qty REAL,
    status_description TEXT,
    reason TEXT,
    ncode TEXT,
    grade TEXT
);

CREATE TABLE IF NOT EXISTS sap_production (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER REFERENCES upload_history(id),
    month TEXT,
    batch TEXT,
    coil_no TEXT,
    grade TEXT,
    n_code TEXT,
    otx_order TEXT,
    posting_date TEXT,
    quantity REAL,
    rm_surface TEXT,
    sdg_order TEXT,
    ship_to TEXT,
    ship_to_party TEXT,
    so_item TEXT,
    sold_to TEXT,
    sold_to_party TEXT,
    supplier_code TEXT,
    thickness REAL,
    value REAL,
    width REAL
);

CREATE TABLE IF NOT EXISTS sap_apc_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER REFERENCES upload_history(id),
    sdg_so_number TEXT,
    ncode TEXT,
    customer_po_no TEXT,
    otx_item TEXT,
    surface TEXT,
    grade TEXT,
    thickness REAL,
    width REAL,
    material TEXT,
    description TEXT,
    order_qty REAL,
    in_production_qty REAL,
    produced_qty REAL,
    in_transit REAL,
    transfered_qty REAL,
    final_qty REAL,
    production_balance REAL,
    warehouse_stock_qty REAL,
    request_delivery_date TEXT,
    otx_sales_order TEXT,
    month TEXT
);

CREATE TABLE IF NOT EXISTS sap_pending_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id INTEGER REFERENCES upload_history(id),
    sold_to_code TEXT,
    sold_to_party TEXT,
    ship_to_code TEXT,
    ship_to_party TEXT,
    sdg_so_number TEXT,
    sdg_item_no TEXT,
    material_code TEXT,
    ncode TEXT,
    material_desc TEXT,
    pending_qty REAL,
    delivery_date TEXT,
    delivery_month TEXT,
    order_rcv_date TEXT,
    surface TEXT
);

CREATE TABLE IF NOT EXISTS weekly_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    grade TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    order_from_sdg REAL,
    production_order_sum REAL,
    order_balance REAL,
    produced_qty REAL,
    target_qty REAL,
    upload_id INTEGER REFERENCES upload_history(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grade_mapping (
    en_grade TEXT PRIMARY KEY,
    astm_grade TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ncode TEXT NOT NULL,
    thickness_mm REAL NOT NULL DEFAULT 0,
    width_mm REAL NOT NULL DEFAULT 0,
    ship_to TEXT NOT NULL DEFAULT '',
    year_month TEXT NOT NULL,
    forecast_mt REAL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ncode, thickness_mm, width_mm, ship_to, year_month)
);

CREATE TABLE IF NOT EXISTS consignment_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ncode TEXT NOT NULL,
    thickness_mm REAL NOT NULL DEFAULT 0,
    width_mm REAL NOT NULL DEFAULT 0,
    ship_to TEXT NOT NULL DEFAULT '',
    stock_mt REAL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ncode, thickness_mm, width_mm, ship_to)
);

CREATE TABLE IF NOT EXISTS v2_ncode_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT NOT NULL,
    category TEXT NOT NULL,
    n_code TEXT NOT NULL,
    q4_forecast_mt REAL DEFAULT 0,
    ordered_mt REAL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(as_of_date, category, n_code)
);

CREATE TABLE IF NOT EXISTS forecast_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    n_code TEXT NOT NULL,
    override_mt REAL NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, n_code)
);

CREATE TABLE IF NOT EXISTS grade_yield_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    grade TEXT NOT NULL,
    yield_rate_pct REAL NOT NULL DEFAULT 100,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, grade)
);

CREATE TABLE IF NOT EXISTS order_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT NOT NULL,
    grade TEXT NOT NULL,
    customer TEXT NOT NULL,
    order_qty_kg REAL NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_snap_date ON order_snapshots(as_of_date);

CREATE TABLE IF NOT EXISTS production_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT NOT NULL,
    grade TEXT NOT NULL,
    thickness_mm REAL,
    ncode TEXT NOT NULL,
    produced_mt REAL NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_prod_snap_date ON production_snapshots(as_of_date);

CREATE TABLE IF NOT EXISTS order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    n_code TEXT NOT NULL,
    covers_n_code TEXT,
    grade TEXT,
    requested_mt REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT,
    requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    reviewed_by TEXT,
    reviewed_at DATETIME,
    note TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_req_ncode ON order_requests(n_code);
CREATE INDEX IF NOT EXISTS idx_order_req_status ON order_requests(status);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT,
    content TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Migrations: add columns that may not exist in older DBs
        existing = {r[1] for r in conn.execute("PRAGMA table_info(ncode_items)").fetchall()}
        if "covers_n_code" not in existing:
            conn.execute("ALTER TABLE ncode_items ADD COLUMN covers_n_code TEXT")
        existing_req = {r[1] for r in conn.execute("PRAGMA table_info(order_requests)").fetchall()}
        if "covers_n_code" not in existing_req:
            conn.execute("ALTER TABLE order_requests ADD COLUMN covers_n_code TEXT")
    print(f"[DB] Initialized: {DB_PATH}")


def upsert_target(conn: sqlite3.Connection, category: str, grade: str, qty_mt: float, quarter: str = "2025-Q4"):
    conn.execute(
        """INSERT INTO target_quantities(category, grade, target_qty_mt, quarter)
           VALUES(?,?,?,?)
           ON CONFLICT(category, grade, quarter) DO UPDATE SET
               target_qty_mt=excluded.target_qty_mt,
               updated_at=CURRENT_TIMESTAMP""",
        (category, grade, qty_mt, quarter),
    )


def get_ncode_grade_map(conn: sqlite3.Connection) -> dict[str, str]:
    """n_code -> grade_astm across both categories (first match wins). Used to resolve
    a grade for sap_orders/sap_apc_orders rows, which only carry the n_code, not a
    category — mirrors dashboard._ncode_to_grade_map but spans Standard+Precision."""
    rows = conn.execute(
        "SELECT n_code, grade_astm FROM ncode_items WHERE n_code IS NOT NULL AND n_code != ''"
    ).fetchall()
    mapping: dict[str, str] = {}
    for r in rows:
        mapping.setdefault(r["n_code"], r["grade_astm"])
    return mapping


def get_ncode_category_map(conn: sqlite3.Connection) -> dict[str, str]:
    """n_code -> category (Standard/Precision), first match wins. Used to build
    /dashboard-v2 deep links (which need a category) from n_code-only data like
    sap_orders/sap_apc_orders — see app/routers/changes.py."""
    rows = conn.execute(
        "SELECT n_code, category FROM ncode_items WHERE n_code IS NOT NULL AND n_code != ''"
    ).fetchall()
    mapping: dict[str, str] = {}
    for r in rows:
        mapping.setdefault(r["n_code"], r["category"])
    return mapping


def snapshot_order_data(conn: sqlite3.Connection, as_of_date: str, ncode_grade_map: dict[str, str]):
    """Snapshot the current sap_orders (15.csv) totals by grade+customer (Ship-to Party)
    before they get replaced by a new upload. No-op if a snapshot for this as_of_date
    already exists (avoids duplicate snapshots when the same file is reloaded on restart)
    or if sap_orders is currently empty (nothing to snapshot yet)."""
    if conn.execute("SELECT 1 FROM order_snapshots WHERE as_of_date=? LIMIT 1", (as_of_date,)).fetchone():
        return
    rows = conn.execute(
        """SELECT ncode, ship_to_party, SUM(COALESCE(order_qty, 0)) as total_qty
           FROM sap_orders GROUP BY ncode, ship_to_party"""
    ).fetchall()
    if not rows:
        return
    agg: dict[tuple[str, str], float] = {}
    for r in rows:
        grade = ncode_grade_map.get(r["ncode"], "Unknown")
        customer = r["ship_to_party"] or "Unknown"
        key = (grade, customer)
        agg[key] = agg.get(key, 0) + (r["total_qty"] or 0)
    conn.executemany(
        "INSERT INTO order_snapshots(as_of_date, grade, customer, order_qty_kg) VALUES(?,?,?,?)",
        [(as_of_date, grade, customer, qty) for (grade, customer), qty in agg.items()],
    )


def snapshot_production_data(conn: sqlite3.Connection, as_of_date: str, ncode_grade_map: dict[str, str]):
    """Snapshot the current sap_apc_orders (15APC.csv) In Transit totals by n_code
    before they get replaced by a new upload. Same dedup/empty-guard as snapshot_order_data."""
    if conn.execute("SELECT 1 FROM production_snapshots WHERE as_of_date=? LIMIT 1", (as_of_date,)).fetchone():
        return
    rows = conn.execute(
        """SELECT ncode, thickness, SUM(COALESCE(in_transit, 0)) as total_mt
           FROM sap_apc_orders WHERE ncode IS NOT NULL AND ncode != '' GROUP BY ncode, thickness"""
    ).fetchall()
    if not rows:
        return
    conn.executemany(
        "INSERT INTO production_snapshots(as_of_date, grade, thickness_mm, ncode, produced_mt) VALUES(?,?,?,?,?)",
        [
            (as_of_date, ncode_grade_map.get(r["ncode"], "Unknown"), r["thickness"], r["ncode"], r["total_mt"] or 0)
            for r in rows
        ],
    )


def create_order_request(
    conn: sqlite3.Connection, category: str, n_code: str, grade: str,
    requested_mt: float, requested_by: str, note: str = "", covers_n_code: str = "",
) -> int:
    """covers_n_code is optional — blank means the request is at the Group N-code level;
    set it to request against one specific individual N-code within that group (see the
    N-code Dashboard's Forecast tab, which lists each covers_n_code with its own
    request input alongside the group-level one)."""
    cur = conn.execute(
        """INSERT INTO order_requests(category, n_code, covers_n_code, grade, requested_mt, requested_by, note)
           VALUES(?,?,?,?,?,?,?)""",
        (category, n_code, covers_n_code or "", grade, requested_mt, requested_by or "", note or ""),
    )
    return cur.lastrowid


def update_order_request_status(
    conn: sqlite3.Connection, request_id: int, status: str, reviewed_by: str, note: str = "",
):
    """status: 'approved' | 'rejected' | 'sent'. Only 'sent' requires the request to
    already be 'approved' — enforced by the caller (see app/routers/orders.py)."""
    conn.execute(
        """UPDATE order_requests SET status=?, reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP,
               note=CASE WHEN ?='' THEN note ELSE ? END, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (status, reviewed_by or "", note or "", note or "", request_id),
    )


def upsert_yield_rate(conn: sqlite3.Connection, category: str, grade: str, yield_rate_pct: float):
    conn.execute(
        """INSERT INTO grade_yield_rates(category, grade, yield_rate_pct)
           VALUES(?,?,?)
           ON CONFLICT(category, grade) DO UPDATE SET
               yield_rate_pct=excluded.yield_rate_pct,
               updated_at=CURRENT_TIMESTAMP""",
        (category, grade, yield_rate_pct),
    )


def upsert_customer_forecast(
    conn: sqlite3.Connection, ncode: str, thickness_mm: float, width_mm: float,
    ship_to: str, year_month: str, forecast_mt: float,
):
    conn.execute(
        """INSERT INTO customer_forecast(ncode, thickness_mm, width_mm, ship_to, year_month, forecast_mt)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(ncode, thickness_mm, width_mm, ship_to, year_month) DO UPDATE SET
               forecast_mt=excluded.forecast_mt,
               updated_at=CURRENT_TIMESTAMP""",
        (ncode, thickness_mm or 0, width_mm or 0, ship_to or "", year_month, forecast_mt),
    )


def upsert_consignment_stock(
    conn: sqlite3.Connection, ncode: str, thickness_mm: float, width_mm: float,
    ship_to: str, stock_mt: float,
):
    conn.execute(
        """INSERT INTO consignment_stock(ncode, thickness_mm, width_mm, ship_to, stock_mt)
           VALUES(?,?,?,?,?)
           ON CONFLICT(ncode, thickness_mm, width_mm, ship_to) DO UPDATE SET
               stock_mt=excluded.stock_mt,
               updated_at=CURRENT_TIMESTAMP""",
        (ncode, thickness_mm or 0, width_mm or 0, ship_to or "", stock_mt),
    )


def replace_ncode_snapshot(conn: sqlite3.Connection, category: str, as_of_date: str, rows: list[tuple]):
    """Store this week's per-N-code forecast/ordered figures for week-over-week deltas.
    Same as_of_date is replaced (re-uploading an unchanged file just refreshes it);
    comparisons read the latest *earlier* as_of_date via get_prev_ncode_snapshot."""
    conn.execute(
        "DELETE FROM v2_ncode_snapshots WHERE category=? AND as_of_date=?",
        (category, as_of_date),
    )
    conn.executemany(
        """INSERT INTO v2_ncode_snapshots(as_of_date, category, n_code, q4_forecast_mt, ordered_mt)
           VALUES(?,?,?,?,?)""",
        [(as_of_date, category, nc, q4, ordered) for nc, q4, ordered in rows],
    )


def get_prev_ncode_snapshot(conn: sqlite3.Connection, category: str, before_date: str):
    """Returns (prev_as_of_date, {n_code: {q4_mt, ordered_mt}}) for the latest snapshot
    strictly before before_date, or (None, {}) if there is none."""
    row = conn.execute(
        "SELECT MAX(as_of_date) AS d FROM v2_ncode_snapshots WHERE category=? AND as_of_date<?",
        (category, before_date),
    ).fetchone()
    prev_date = row["d"] if row else None
    if not prev_date:
        return None, {}
    rows = conn.execute(
        "SELECT n_code, q4_forecast_mt, ordered_mt FROM v2_ncode_snapshots WHERE category=? AND as_of_date=?",
        (category, prev_date),
    ).fetchall()
    return prev_date, {r["n_code"]: {"q4_mt": r["q4_forecast_mt"], "ordered_mt": r["ordered_mt"]} for r in rows}


def upsert_forecast_override(conn: sqlite3.Connection, category: str, n_code: str, override_mt: float):
    conn.execute(
        """INSERT INTO forecast_overrides(category, n_code, override_mt)
           VALUES(?,?,?)
           ON CONFLICT(category, n_code) DO UPDATE SET
               override_mt=excluded.override_mt,
               updated_at=CURRENT_TIMESTAMP""",
        (category, n_code, override_mt),
    )


def delete_forecast_override(conn: sqlite3.Connection, category: str, n_code: str):
    conn.execute(
        "DELETE FROM forecast_overrides WHERE category=? AND n_code=?",
        (category, n_code),
    )


def get_forecast_overrides(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT n_code, override_mt FROM forecast_overrides WHERE category=?",
        (category,),
    ).fetchall()
    return {r["n_code"]: r["override_mt"] for r in rows}


def insert_upload_history(conn: sqlite3.Connection, upload_type: str, filename: str, as_of_date: str, row_count: int) -> int:
    cur = conn.execute(
        "INSERT INTO upload_history(upload_type, filename, as_of_date, row_count) VALUES(?,?,?,?)",
        (upload_type, filename, as_of_date, row_count),
    )
    return cur.lastrowid


def bulk_insert_ncode_items(conn: sqlite3.Connection, records: list[dict], upload_id: int):
    """Insert ncode_items rows and their monthly_quantities. Replaces previous data from the same upload type."""
    for rec in records:
        cur = conn.execute(
            """INSERT INTO ncode_items(
                category, grade_astm, grade_en, thickness_mm, n_code, covers_n_code, width_mm,
                customer_sold_to, customer_ship_to,
                order_balance_qty, produced_qty, production_balance_qty,
                preparation_qty, production_plan_qty, as_of_date, upload_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec["category"], rec["grade_astm"], rec.get("grade_en"), rec.get("thickness_mm"),
                rec.get("n_code"), rec.get("covers_n_code"), rec.get("width_mm"),
                rec.get("customer_sold_to"), rec.get("customer_ship_to"),
                rec.get("order_balance_qty"), rec.get("produced_qty"),
                rec.get("production_balance_qty"), rec.get("preparation_qty"),
                rec.get("production_plan_qty"), rec.get("as_of_date"), upload_id,
            ),
        )
        item_id = cur.lastrowid
        monthly = rec.get("monthly_quantities", {})
        if monthly:
            conn.executemany(
                "INSERT INTO monthly_quantities(ncode_item_id, year_month, quantity_kg) VALUES(?,?,?)",
                [(item_id, ym, qty) for ym, qty in monthly.items()],
            )
        # Update grade mapping
        astm = rec["grade_astm"]
        en = rec.get("grade_en", "")
        if en and en != "nan":
            conn.execute(
                "INSERT OR IGNORE INTO grade_mapping(en_grade, astm_grade) VALUES(?,?)",
                (en, astm),
            )


def bulk_insert_sap_15(conn: sqlite3.Connection, records: list[dict], upload_id: int):
    for rec in records:
        conn.execute(
            """INSERT INTO sap_orders(upload_id,month,sdg_sold_to_party,ship_to,sdg_sold_to_party_name,
               ship_to_party,country_code,sdg_so_number,material,po_item,order_qty,
               request_delivery_date,otx_date_created,order_type,surface,delivered_qty,
               thickness,created_by,in_production_qty,final_qty,status_description,reason,ncode,grade)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (upload_id, rec.get("month"), rec.get("sdg_sold_to_party"), rec.get("ship_to"),
             rec.get("sdg_sold_to_party_name"), rec.get("ship_to_party"), rec.get("country_code"),
             rec.get("sdg_so_number"), rec.get("material"), rec.get("po_item"), rec.get("order_qty"),
             rec.get("request_delivery_date"), rec.get("otx_date_created"), rec.get("order_type"),
             rec.get("surface"), rec.get("delivered_qty"), rec.get("thickness"), rec.get("created_by"),
             rec.get("in_production_qty"), rec.get("final_qty"), rec.get("status_description"),
             rec.get("reason"), rec.get("ncode"), rec.get("grade")),
        )


def bulk_insert_sap_25(conn: sqlite3.Connection, records: list[dict], upload_id: int):
    for rec in records:
        conn.execute(
            """INSERT INTO sap_production(upload_id,month,batch,coil_no,grade,n_code,otx_order,
               posting_date,quantity,rm_surface,sdg_order,ship_to,ship_to_party,so_item,
               sold_to,sold_to_party,supplier_code,thickness,value,width)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (upload_id, rec.get("month"), rec.get("batch"), rec.get("coil_no"), rec.get("grade"),
             rec.get("n_code"), rec.get("otx_order"), rec.get("posting_date"), rec.get("quantity"),
             rec.get("rm_surface"), rec.get("sdg_order"), rec.get("ship_to"), rec.get("ship_to_party"),
             rec.get("so_item"), rec.get("sold_to"), rec.get("sold_to_party"), rec.get("supplier_code"),
             rec.get("thickness"), rec.get("value"), rec.get("width")),
        )


def bulk_insert_sap_104(conn: sqlite3.Connection, records: list[dict], upload_id: int):
    for rec in records:
        conn.execute(
            """INSERT INTO sap_pending_orders(upload_id,sold_to_code,sold_to_party,ship_to_code,
               ship_to_party,sdg_so_number,sdg_item_no,material_code,ncode,material_desc,
               pending_qty,delivery_date,delivery_month,order_rcv_date,surface)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (upload_id, rec.get("sold_to_code"), rec.get("sold_to_party"), rec.get("ship_to_code"),
             rec.get("ship_to_party"), rec.get("sdg_so_number"), rec.get("sdg_item_no"),
             rec.get("material_code"), rec.get("ncode"), rec.get("material_desc"),
             rec.get("pending_qty"), rec.get("delivery_date"), rec.get("delivery_month"),
             rec.get("order_rcv_date"), rec.get("surface")),
        )


def bulk_insert_sap_15apc(conn: sqlite3.Connection, records: list[dict], upload_id: int):
    for rec in records:
        conn.execute(
            """INSERT INTO sap_apc_orders(upload_id,sdg_so_number,ncode,customer_po_no,otx_item,
               surface,grade,thickness,width,material,description,order_qty,in_production_qty,
               produced_qty,in_transit,transfered_qty,final_qty,production_balance,
               warehouse_stock_qty,request_delivery_date,otx_sales_order,month)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (upload_id, rec.get("sdg_so_number"), rec.get("ncode"), rec.get("customer_po_no"),
             rec.get("otx_item"), rec.get("surface"), rec.get("grade"), rec.get("thickness"),
             rec.get("width"), rec.get("material"), rec.get("description"), rec.get("order_qty"),
             rec.get("in_production_qty"), rec.get("produced_qty"), rec.get("in_transit"),
             rec.get("transfered_qty"), rec.get("final_qty"), rec.get("production_balance"),
             rec.get("warehouse_stock_qty"), rec.get("request_delivery_date"),
             rec.get("otx_sales_order"), rec.get("month")),
        )
