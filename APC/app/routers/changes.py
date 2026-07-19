"""Week-over-week change tracking: order (15.csv) and production (15APC.csv) deltas
vs the most recent snapshot taken right before the last reload (see app/autoload.py's
_snapshot_before_replace and app/database.py's snapshot_order_data/snapshot_production_data)."""
import sqlite3
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db, get_ncode_category_map, get_ncode_grade_map
from app.paths import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _row_status(current: float, previous: float) -> str:
    """Classify a delta row: 'new' = wasn't there last snapshot (a brand-new order/
    production line, not just a quantity change on an existing one), 'completed' =
    was there last snapshot but isn't anymore, 'changed'/'unchanged' = existed in both."""
    if previous == 0 and current > 0:
        return "new"
    if previous > 0 and current == 0:
        return "completed"
    return "changed" if current != previous else "unchanged"


def _current_order_totals(
    conn: sqlite3.Connection, ncode_grade_map: dict[str, str],
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], list[str]]]:
    """Returns (totals, ncodes_by_key) — ncodes_by_key lets the UI link a Grade/Customer
    row to the underlying N-code(s) in the N-code Dashboard (see /dashboard-v2 deep link)."""
    rows = conn.execute(
        """SELECT ncode, ship_to_party, SUM(COALESCE(order_qty, 0)) as total_qty
           FROM sap_orders GROUP BY ncode, ship_to_party"""
    ).fetchall()
    agg: dict[tuple[str, str], float] = {}
    ncodes_by_key: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        grade = ncode_grade_map.get(r["ncode"], "Unknown")
        customer = r["ship_to_party"] or "Unknown"
        key = (grade, customer)
        agg[key] = agg.get(key, 0) + (r["total_qty"] or 0)
        if r["ncode"]:
            ncodes_by_key.setdefault(key, []).append(r["ncode"])
    return agg, ncodes_by_key


def _prev_order_totals(conn: sqlite3.Connection, as_of_date: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT grade, customer, order_qty_kg FROM order_snapshots WHERE as_of_date=?",
        (as_of_date,),
    ).fetchall()
    return {(r["grade"], r["customer"]): r["order_qty_kg"] for r in rows}


@router.get("/api/changes/orders")
async def api_changes_orders():
    with get_db() as conn:
        latest = conn.execute("SELECT MAX(as_of_date) as d FROM order_snapshots").fetchone()
        prev_as_of = latest["d"] if latest else None
        cur_upload = conn.execute(
            "SELECT as_of_date FROM upload_history WHERE upload_type='15' ORDER BY uploaded_at DESC, id DESC LIMIT 1"
        ).fetchone()

        ncode_grade_map = get_ncode_grade_map(conn)
        ncode_category_map = get_ncode_category_map(conn)
        current, ncodes_by_key = _current_order_totals(conn, ncode_grade_map)
        prev = _prev_order_totals(conn, prev_as_of) if prev_as_of else {}

    rows = []
    for key in set(current) | set(prev):
        cur_kg = current.get(key, 0)
        prev_kg = prev.get(key, 0)
        if cur_kg == 0 and prev_kg == 0:
            continue
        grade, customer = key
        current_mt = round(cur_kg / 1000, 3)
        previous_mt = round(prev_kg / 1000, 3)
        rows.append({
            "grade": grade,
            "customer": customer,
            "current_mt": current_mt,
            "previous_mt": previous_mt,
            "delta_mt": round((cur_kg - prev_kg) / 1000, 3),
            "status": _row_status(current_mt, previous_mt),
            "ncodes": [
                {"ncode": nc, "category": ncode_category_map.get(nc)}
                for nc in sorted(set(ncodes_by_key.get(key, [])))
            ],
        })
    rows.sort(key=lambda r: r["delta_mt"], reverse=True)

    return {
        "current_as_of": cur_upload["as_of_date"] if cur_upload else None,
        "previous_as_of": prev_as_of,
        "rows": rows,
        "new_count": sum(1 for r in rows if r["status"] == "new"),
    }


@router.get("/api/changes/production")
async def api_changes_production():
    with get_db() as conn:
        latest = conn.execute("SELECT MAX(as_of_date) as d FROM production_snapshots").fetchone()
        prev_as_of = latest["d"] if latest else None
        cur_upload = conn.execute(
            "SELECT as_of_date FROM upload_history WHERE upload_type='15_apc' ORDER BY uploaded_at DESC, id DESC LIMIT 1"
        ).fetchone()

        ncode_grade_map = get_ncode_grade_map(conn)
        ncode_category_map = get_ncode_category_map(conn)
        cur_rows = conn.execute(
            """SELECT ncode, thickness, SUM(COALESCE(in_transit, 0)) as total_mt
               FROM sap_apc_orders WHERE ncode IS NOT NULL AND ncode != '' GROUP BY ncode, thickness"""
        ).fetchall()
        prev_rows = (
            conn.execute(
                "SELECT ncode, grade, thickness_mm, produced_mt FROM production_snapshots WHERE as_of_date=?",
                (prev_as_of,),
            ).fetchall()
            if prev_as_of else []
        )

    current: dict[str, float] = {}
    current_thickness: dict[str, float] = {}
    for r in cur_rows:
        current[r["ncode"]] = current.get(r["ncode"], 0) + (r["total_mt"] or 0)
        current_thickness[r["ncode"]] = r["thickness"]

    prev: dict[str, float] = {}
    prev_meta: dict[str, tuple[str, float]] = {}
    for r in prev_rows:
        prev[r["ncode"]] = r["produced_mt"]
        prev_meta[r["ncode"]] = (r["grade"], r["thickness_mm"])

    ncode_rows = []
    for nc in set(current) | set(prev):
        cur_mt = current.get(nc, 0)
        prev_mt = prev.get(nc, 0)
        if cur_mt == 0 and prev_mt == 0:
            continue
        meta_grade, meta_thickness = prev_meta.get(nc, (None, None))
        grade = ncode_grade_map.get(nc, meta_grade or "Unknown")
        thickness = current_thickness.get(nc, meta_thickness)
        current_mt = round(cur_mt, 3)
        previous_mt = round(prev_mt, 3)
        ncode_rows.append({
            "ncode": nc,
            "grade": grade,
            "category": ncode_category_map.get(nc),
            "thickness_mm": thickness,
            "current_mt": current_mt,
            "previous_mt": previous_mt,
            "delta_mt": round(cur_mt - prev_mt, 3),
            "status": _row_status(current_mt, previous_mt),
        })
    ncode_rows.sort(key=lambda r: r["delta_mt"], reverse=True)

    rollup_map: dict[tuple[str, float], dict] = {}
    for r in ncode_rows:
        key = (r["grade"], r["thickness_mm"])
        b = rollup_map.setdefault(key, {
            "grade": r["grade"], "thickness_mm": r["thickness_mm"],
            "current_mt": 0.0, "previous_mt": 0.0, "delta_mt": 0.0,
            "ncode_count": 0, "new_ncode_count": 0,
        })
        b["current_mt"] += r["current_mt"]
        b["previous_mt"] += r["previous_mt"]
        b["delta_mt"] += r["delta_mt"]
        b["ncode_count"] += 1
        if r["status"] == "new":
            b["new_ncode_count"] += 1
    rollup = sorted(rollup_map.values(), key=lambda r: r["delta_mt"], reverse=True)
    for r in rollup:
        r["current_mt"] = round(r["current_mt"], 3)
        r["previous_mt"] = round(r["previous_mt"], 3)
        r["delta_mt"] = round(r["delta_mt"], 3)

    return {
        "current_as_of": cur_upload["as_of_date"] if cur_upload else None,
        "previous_as_of": prev_as_of,
        "rollup": rollup,
        "new_count": sum(1 for r in ncode_rows if r["status"] == "new"),
        "ncodes": ncode_rows,
    }


@router.get("/changes", response_class=HTMLResponse)
async def changes_page(request: Request):
    return templates.TemplateResponse("changes.html", {"request": request})
