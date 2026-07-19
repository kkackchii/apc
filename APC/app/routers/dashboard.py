"""Dashboard data API endpoints."""
import re
import sqlite3
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import forecast_engine
from app.database import (
    delete_forecast_override,
    get_db,
    get_forecast_overrides,
    get_prev_ncode_snapshot,
    replace_ncode_snapshot,
    upsert_consignment_stock,
    upsert_customer_forecast,
    upsert_forecast_override,
)
from app.paths import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _get_latest_as_of_date(conn: sqlite3.Connection, category: str) -> Optional[str]:
    row = conn.execute(
        "SELECT as_of_date FROM ncode_items WHERE category=? AND as_of_date IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (category,),
    ).fetchone()
    return row[0] if row else None


def _get_summary_by_grade(conn: sqlite3.Connection, category: str) -> list[dict]:
    """Aggregate ncode_items by grade for the summary table."""
    rows = conn.execute(
        """SELECT grade_astm,
               COALESCE(SUM(CASE WHEN order_balance_qty IS NOT NULL THEN order_balance_qty END), 0) as order_balance,
               COALESCE(SUM(CASE WHEN production_plan_qty IS NOT NULL THEN production_plan_qty END), 0) as plan_qty,
               COALESCE(SUM(CASE WHEN preparation_qty IS NOT NULL THEN preparation_qty END), 0) as prep_total,
               COUNT(*) as ncode_count
           FROM ncode_items
           WHERE category=?
           GROUP BY grade_astm
           ORDER BY grade_astm""",
        (category,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_targets(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT grade, target_qty_mt FROM target_quantities WHERE category=? ORDER BY grade",
        (category,),
    ).fetchall()
    return {r["grade"]: r["target_qty_mt"] for r in rows}


def _get_yield_rates(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT grade, yield_rate_pct FROM grade_yield_rates WHERE category=?",
        (category,),
    ).fetchall()
    return {r["grade"]: r["yield_rate_pct"] for r in rows}


def _get_apc_order_by_grade(conn: sqlite3.Connection, category: str) -> dict[str, dict[str, float]]:
    """Get OTX production orders from sap_apc_orders, mapped by ncode to grade.
    Returns {grade: {month: total_order_qty}}
    """
    grade_map = _ncode_to_grade_map(conn, category)
    rows = conn.execute(
        """SELECT ncode, month, SUM(order_qty) as total_qty
           FROM sap_apc_orders WHERE order_qty IS NOT NULL GROUP BY ncode, month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        month = r["month"] or "Unknown"
        bucket = result.setdefault(grade, {})
        bucket[month] = bucket.get(month, 0) + (r["total_qty"] or 0)
    return result


# 15APC의 Grade 컬럼(EN 강종 번호) → ASTM 계열 이름. N-code가 마스터 시트(2B_APC/
# Pre_APC)에 없는 행의 Grade별 생산량 배분에만 쓰는 보조 매칭 (N-code 매칭이 항상 우선).
_EN_TO_ASTM = {
    "1.4301": "304", "1.4306": "304L", "1.4307": "304L", "1.4303": "305",
    "1.4310": "301", "1.4318": "301LN", "1.4401": "316", "1.4404": "316L",
    "1.4435": "316L", "1.4406": "316L(N)", "1.4429": "316L(N)",
    "1.4541": "321", "1.4571": "316Ti", "1.4833": "309", "1.4845": "310",
}


def _resolve_apc_grade(raw_grade, master_labels: set) -> Optional[str]:
    """Map a 15APC Grade cell to one of this category's master-sheet grade labels.
    EN numbers translate first (1.4301→304, 1.4307→304L). Combined master labels win
    over exact ones: 1.4301/1.4307 both land on '304 / 304L' even though a standalone
    '304L' label exists (사용자 확정 규칙). Returns None when nothing matches — the row
    is then excluded from the per-grade rows and reported by _get_unresolved_apc_stats."""
    norm = re.sub(r"\s+", "", str(raw_grade or "")).upper()
    if not norm:
        return None
    astm = _EN_TO_ASTM.get(norm, norm).upper()
    combined: dict[str, str] = {}
    exact: dict[str, str] = {}
    for lbl in master_labels:
        if not lbl:
            continue
        lbl_norm = re.sub(r"\s+", "", lbl).upper()
        exact[lbl_norm] = lbl
        if "/" in lbl_norm:
            for part in lbl_norm.split("/"):
                combined.setdefault(part, lbl)
    return combined.get(astm) or exact.get(astm)


def _get_in_transit_by_grade(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    """생산량(Produced) per grade — 15APC In Transit (MT), layered matching:
    1) ncode in this category's master sheet → that master grade (most precise)
    2) ncode only in the other category's master → excluded (counted there instead)
    3) orphan ncode → category from Surface (2B=Standard, 그 외=Precision; blank
       excluded), grade from the 15APC Grade column via _resolve_apc_grade.
    Rows unresolvable by both (1) and (3) are excluded here and surfaced by
    _get_unresolved_apc_stats (Overview banner)."""
    grade_map = _ncode_to_grade_map(conn, category)
    other_map = _ncode_to_grade_map(conn, "Precision" if category == "Standard" else "Standard")
    master_labels = set(grade_map.values())
    rows = conn.execute(
        """SELECT ncode, grade, surface, SUM(COALESCE(in_transit, 0)) as total_in_transit
           FROM sap_apc_orders GROUP BY ncode, grade, surface"""
    ).fetchall()
    result: dict[str, float] = {}
    for r in rows:
        nc = r["ncode"]
        if nc in grade_map:
            grade = grade_map[nc]
        elif nc in other_map:
            continue
        else:
            surface = str(r["surface"] or "").strip().upper()
            if not surface or (surface == "2B") != (category == "Standard"):
                continue
            grade = _resolve_apc_grade(r["grade"], master_labels)
            if grade is None:
                continue
        result[grade] = round(result.get(grade, 0) + (r["total_in_transit"] or 0), 3)
    return result


def _get_in_transit_by_ncode(conn: sqlite3.Connection) -> dict[str, float]:
    """Sum in_transit from sap_apc_orders per n_code (already MT). Same source as
    _get_in_transit_by_grade, kept at n_code granularity for the detail-page N-code
    group rows."""
    rows = conn.execute(
        """SELECT ncode, SUM(COALESCE(in_transit, 0)) as total_in_transit
           FROM sap_apc_orders GROUP BY ncode"""
    ).fetchall()
    return {r["ncode"]: round(r["total_in_transit"] or 0, 3) for r in rows if r["ncode"]}


def _get_in_transit_total_by_surface(conn: sqlite3.Connection, category: str) -> float:
    """카테고리 전체 생산량 — Overview 그래프/KPI가 쓰는 기준 (사용자 확정 규칙):
    15APC In Transit 합계를 Surface 컬럼으로만 나눔 (Standard = Surface 2B,
    Precision = 그 외; blank Surface 행은 제외). N-code/Grade 매칭과 무관하게
    모든 행이 포함되므로 Grade별 행 합계(total_produced)보다 클 수 있다 — 그 차이는
    _get_unresolved_apc_stats 배너가 설명한다."""
    op = "=" if category == "Standard" else "!="
    row = conn.execute(
        f"""SELECT SUM(COALESCE(in_transit, 0)) FROM sap_apc_orders
            WHERE TRIM(COALESCE(surface, '')) != '' AND UPPER(TRIM(surface)) {op} '2B'"""
    ).fetchone()
    return round(row[0] or 0, 3)


def _get_in_production_by_grade(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    """Sum in_production_qty from sap_apc_orders (15APC.csv) per grade (mapped via ncode).
    Already MT (same convention as in_transit / produced_qty in this table)."""
    grade_map = _ncode_to_grade_map(conn, category)
    rows = conn.execute(
        """SELECT ncode, SUM(COALESCE(in_production_qty, 0)) as total_in_production
           FROM sap_apc_orders GROUP BY ncode"""
    ).fetchall()
    result: dict[str, float] = {}
    for r in rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        result[grade] = round(result.get(grade, 0) + (r["total_in_production"] or 0), 3)
    return result


def _get_ncode_match_stats(conn: sqlite3.Connection, category: str) -> dict:
    """Match rate between this category's master N-codes (ncode_items, i.e. the
    2B_APC/Pre_APC main sheet) and the 15APC upload (sap_apc_orders) that the
    Overview chart's 'Produced' total is built from (see _get_in_transit_by_grade).
    Master N-codes with no matching 15APC row contribute 0 to Produced even though
    they're part of the category's target."""
    master_ncodes = {
        r["n_code"] for r in conn.execute(
            "SELECT DISTINCT n_code FROM ncode_items WHERE category=? AND n_code IS NOT NULL AND n_code != ''",
            (category,),
        ).fetchall()
    }
    apc_ncodes = {
        r["ncode"] for r in conn.execute(
            "SELECT DISTINCT ncode FROM sap_apc_orders WHERE ncode IS NOT NULL AND ncode != ''"
        ).fetchall()
    }
    matched = master_ncodes & apc_ncodes
    unmatched = sorted(master_ncodes - apc_ncodes)
    total = len(master_ncodes)
    return {
        "total_ncodes": total,
        "matched_ncodes": len(matched),
        "match_pct": round(len(matched) / total * 100, 1) if total > 0 else None,
        "unmatched_ncodes": unmatched,
    }


def _get_unresolved_apc_stats(conn: sqlite3.Connection) -> dict:
    """15APC rows that can't be attributed to any grade row at all: ncode not in
    either category's master sheet AND (Surface blank OR the 15APC Grade column
    doesn't resolve via _resolve_apc_grade). 이 물량은 그래프/KPI의 Surface 기준
    카테고리 합계에는 포함되지만(blank Surface 제외) Grade별 표에서는 빠진다 —
    Overview 배너로 안내."""
    master_ncodes = {
        r["n_code"] for r in conn.execute(
            "SELECT DISTINCT n_code FROM ncode_items WHERE n_code IS NOT NULL AND n_code != ''"
        ).fetchall()
    }
    labels_by_cat = {
        cat: {
            r["grade_astm"] for r in conn.execute(
                "SELECT DISTINCT grade_astm FROM ncode_items WHERE category=? AND grade_astm IS NOT NULL",
                (cat,),
            ).fetchall()
        }
        for cat in ("Standard", "Precision")
    }
    rows = conn.execute(
        """SELECT ncode, grade, surface, SUM(COALESCE(in_transit, 0)) as total_in_transit
           FROM sap_apc_orders WHERE ncode IS NOT NULL AND ncode != ''
           GROUP BY ncode, grade, surface"""
    ).fetchall()
    unresolved_by_ncode: dict[str, float] = {}
    for r in rows:
        if r["ncode"] in master_ncodes:
            continue
        surface = str(r["surface"] or "").strip().upper()
        if surface:
            labels = labels_by_cat["Standard" if surface == "2B" else "Precision"]
            if _resolve_apc_grade(r["grade"], labels):
                continue
        mt = r["total_in_transit"] or 0
        unresolved_by_ncode[r["ncode"]] = round(unresolved_by_ncode.get(r["ncode"], 0) + mt, 3)
    unresolved = [{"ncode": nc, "mt": mt} for nc, mt in unresolved_by_ncode.items()]
    unresolved.sort(key=lambda o: -o["mt"])
    return {
        "count": len(unresolved),
        "total_mt": round(sum(o["mt"] for o in unresolved), 3),
        "ncodes": unresolved[:20],
    }


Q4_MONTHS = ["2026-10", "2026-11", "2026-12"]

# Standard 소량 grade — 대시보드 요약 테이블/차트에서 "Other"로 묶음
_STANDARD_OTHER_GRADES = {"316Ti", "305", "304L", "304(NI9%)"}


def _ncode_to_grade_map(conn: sqlite3.Connection, category: str) -> dict[str, str]:
    """One grade per n_code (first match). ncode_items has multiple rows per n_code
    (one per customer/spec combination), so joining sap_orders/sap_pending_orders/
    customer_forecast directly against ncode_items on n_code alone would fan out and
    multiply SUMs by however many ncode_items rows share that n_code. Resolving the
    grade in Python first avoids that duplication.
    """
    rows = conn.execute(
        "SELECT n_code, grade_astm FROM ncode_items WHERE category=? AND n_code IS NOT NULL AND n_code != ''",
        (category,),
    ).fetchall()
    mapping: dict[str, str] = {}
    for r in rows:
        mapping.setdefault(r["n_code"], r["grade_astm"])
    return mapping


def _get_released_order_by_grade_month(conn: sqlite3.Connection, category: str) -> dict[str, dict[str, float]]:
    """Released orders (sap_orders / '15') grouped by grade and delivery month.
    'month' on sap_orders is the SAP-extracted requested-delivery month.
    Returns {grade: {month: total_order_qty_kg}}
    """
    grade_map = _ncode_to_grade_map(conn, category)
    rows = conn.execute(
        """SELECT ncode, month, SUM(order_qty) as total_qty
           FROM sap_orders WHERE order_qty IS NOT NULL GROUP BY ncode, month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        month = r["month"] or "Unknown"
        bucket = result.setdefault(grade, {})
        bucket[month] = bucket.get(month, 0) + (r["total_qty"] or 0)
    return result


def _get_pending_order_by_grade_month(conn: sqlite3.Connection, category: str) -> dict[str, dict[str, float]]:
    """Pending orders (sap_pending_orders / '104') — booked in SAP but not yet released to
    the factory. Grouped by grade and delivery month (derived from 'Delivery Date').
    Returns {grade: {month: total_pending_qty_kg}}
    """
    grade_map = _ncode_to_grade_map(conn, category)
    rows = conn.execute(
        """SELECT ncode, delivery_month, SUM(pending_qty) as total_qty
           FROM sap_pending_orders WHERE pending_qty IS NOT NULL GROUP BY ncode, delivery_month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        month = r["delivery_month"] or "Unknown"
        bucket = result.setdefault(grade, {})
        bucket[month] = bucket.get(month, 0) + (r["total_qty"] or 0)
    return result


def _get_consignment_stock_by_grade(conn: sqlite3.Connection, category: str) -> dict[str, float]:
    """Warehouse stock already held for consignment customers, grouped by grade (MT).
    Not month-specific — it's a current stock level, not a per-month figure.
    Returns {grade: total_stock_mt}
    """
    grade_map = _ncode_to_grade_map(conn, category)
    rows = conn.execute(
        """SELECT ncode, SUM(stock_mt) as total_mt
           FROM consignment_stock WHERE stock_mt IS NOT NULL GROUP BY ncode"""
    ).fetchall()
    result: dict[str, float] = {}
    for r in rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        result[grade] = result.get(grade, 0) + (r["total_mt"] or 0)
    return result


def _get_released_order_by_ncode_month(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Same source as _get_released_order_by_grade_month, kept at ncode granularity
    (not rolled up to grade) for N-code level Q4 forecasting. kg."""
    rows = conn.execute(
        """SELECT ncode, month, SUM(order_qty) as total_qty
           FROM sap_orders WHERE order_qty IS NOT NULL GROUP BY ncode, month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        if not r["ncode"]:
            continue
        bucket = result.setdefault(r["ncode"], {})
        month = r["month"] or "Unknown"
        bucket[month] = bucket.get(month, 0) + (r["total_qty"] or 0)
    return result


def _get_pending_order_by_ncode_month(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Same source as _get_pending_order_by_grade_month, kept at ncode granularity. kg."""
    rows = conn.execute(
        """SELECT ncode, delivery_month, SUM(pending_qty) as total_qty
           FROM sap_pending_orders WHERE pending_qty IS NOT NULL GROUP BY ncode, delivery_month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        if not r["ncode"]:
            continue
        bucket = result.setdefault(r["ncode"], {})
        month = r["delivery_month"] or "Unknown"
        bucket[month] = bucket.get(month, 0) + (r["total_qty"] or 0)
    return result


def _get_customer_forecast_by_ncode_month(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Manually/CSV-entered Customer Forecast per n_code per month. Despite the column
    name, customer_forecast.forecast_mt is entered in kg (same convention as sap_orders
    .order_qty) — callers must divide by 1000, same as booked_mt elsewhere."""
    rows = conn.execute(
        """SELECT ncode, year_month, SUM(forecast_mt) as total_mt
           FROM customer_forecast WHERE forecast_mt IS NOT NULL GROUP BY ncode, year_month"""
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for r in rows:
        if not r["ncode"]:
            continue
        result.setdefault(r["ncode"], {})[r["year_month"] or "Unknown"] = r["total_mt"] or 0
    return result


_HISTORY_MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def _get_ncode_history_6m(conn: sqlite3.Connection, category: str) -> dict[str, list[float]]:
    """Jan~Jun 2026 actuals per n_code, in MT, from monthly_quantities (the monthly
    columns embedded in the 2B/Pre main sheet). This is the only historical demand
    source actually populated in practice — sap_production ('25.csv') has never been
    uploaded, so it isn't used here despite being the source TASK.md originally assumed.
    Summed across ncode_items rows sharing an n_code (one n_code can have several
    customer/spec rows).
    """
    placeholders = ",".join("?" * len(_HISTORY_MONTHS))
    rows = conn.execute(
        f"""SELECT n.n_code, m.year_month, SUM(m.quantity_kg) as total_kg
           FROM monthly_quantities m
           JOIN ncode_items n ON n.id = m.ncode_item_id
           WHERE n.category=? AND n.n_code IS NOT NULL AND n.n_code != ''
             AND m.year_month IN ({placeholders})
           GROUP BY n.n_code, m.year_month""",
        (category, *_HISTORY_MONTHS),
    ).fetchall()
    by_ncode: dict[str, dict[str, float]] = {}
    for r in rows:
        by_ncode.setdefault(r["n_code"], {})[r["year_month"]] = (r["total_kg"] or 0) / 1000
    return {nc: [months.get(m, 0.0) for m in _HISTORY_MONTHS] for nc, months in by_ncode.items()}


def compute_ncode_q4_forecast(conn: sqlite3.Connection, category: str) -> dict[str, dict]:
    """Full N-code level 26Q4 forecast — combines booked orders (Layer 1), customer
    forecast (Layer 2), and a statistical fallback (Layer 3, see app/forecast_engine.py)
    per TASK.md 11.2. Layer 4 (consignment) stays a separate grade-level adjustment
    against target (see _build_q4_forecast's net_need_mt), not folded into the
    per-N-code demand figure here.

    For each month: final_mt = max(booked_mt, cust_fcst_mt) if either is > 0,
    otherwise the Layer 3 statistical estimate (TASK.md 11.2's "①과 겹치면 max() 사용").

    A manual override (forecast_overrides, saved from the dashboard-v2 Forecast tab)
    replaces the Q4-total final_mt; monthly final_mt values are rescaled to sum to it
    (proportionally, or evenly when the computed total is 0). confidence and the
    booked/fcst/stat fields keep describing the *model* forecast — computed_final_mt
    preserves the pre-override total so the UI can show both.

    Returns {n_code: {stat_model, monthly: {month: {booked_mt, cust_fcst_mt, stat_mt,
    final_mt}}, booked_mt, cust_fcst_mt, stat_mt, final_mt, computed_final_mt,
    override_mt, confidence}} — the non-monthly fields are Q4 (Oct+Nov+Dec) totals in MT.
    """
    n_codes = sorted({
        r["n_code"] for r in conn.execute(
            "SELECT DISTINCT n_code FROM ncode_items WHERE category=? AND n_code IS NOT NULL AND n_code != ''",
            (category,),
        ).fetchall()
    })
    released = _get_released_order_by_ncode_month(conn)
    pending = _get_pending_order_by_ncode_month(conn)
    cust_fcst = _get_customer_forecast_by_ncode_month(conn)
    history = _get_ncode_history_6m(conn, category)
    overrides = get_forecast_overrides(conn, category)

    result: dict[str, dict] = {}
    for nc in n_codes:
        hist = history.get(nc, [0.0] * len(_HISTORY_MONTHS))
        stat_model, stat_month_mt = forecast_engine.stat_forecast(hist)

        monthly = {}
        q4_booked = q4_fcst = q4_stat_used = q4_final = 0.0
        for m in Q4_MONTHS:
            booked_mt = round((released.get(nc, {}).get(m, 0) + pending.get(nc, {}).get(m, 0)) / 1000, 3)
            fcst_mt = round(cust_fcst.get(nc, {}).get(m, 0) / 1000, 3)
            if booked_mt > 0 or fcst_mt > 0:
                final_mt = max(booked_mt, fcst_mt)
                stat_mt = 0.0
            else:
                final_mt = round(stat_month_mt, 3)
                stat_mt = final_mt
            monthly[m] = {
                "booked_mt": booked_mt,
                "cust_fcst_mt": fcst_mt,
                "stat_mt": stat_mt,
                "final_mt": final_mt,
            }
            q4_booked += booked_mt
            q4_fcst += fcst_mt
            q4_stat_used += stat_mt
            q4_final += final_mt

        computed_final = q4_final
        override_mt = overrides.get(nc)
        if override_mt is not None:
            if computed_final > 0:
                scale = override_mt / computed_final
                for m in Q4_MONTHS:
                    monthly[m]["final_mt"] = round(monthly[m]["final_mt"] * scale, 3)
            else:
                for m in Q4_MONTHS:
                    monthly[m]["final_mt"] = round(override_mt / 3, 3)
            q4_final = override_mt

        result[nc] = {
            "stat_model": stat_model,
            "monthly": monthly,
            "booked_mt": round(q4_booked, 3),
            "cust_fcst_mt": round(q4_fcst, 3),
            "stat_mt": round(q4_stat_used, 3),
            "final_mt": round(q4_final, 3),
            "computed_final_mt": round(computed_final, 3),
            "override_mt": override_mt,
            "confidence": forecast_engine.assign_confidence(q4_booked, q4_fcst, q4_stat_used, computed_final),
        }
    return result


def take_ncode_snapshot(conn: sqlite3.Connection, category: str):
    """Snapshot per-N-code Q4 forecast + ordered (James Prep) figures under the current
    as_of_date, for week-over-week deltas in the N-code dashboard. Called after every
    data load (autoload on startup, web upload). Same-date reloads just refresh the
    snapshot; the delta always compares against the latest *earlier* as_of_date."""
    as_of = _get_latest_as_of_date(conn, category)
    if not as_of:
        return
    forecast = compute_ncode_q4_forecast(conn, category)
    master = _get_ncode_master_info(conn, category)
    rows = [
        (nc, f["final_mt"], round(master.get(nc, {}).get("prep_mt", 0) or 0, 3))
        for nc, f in forecast.items()
    ]
    replace_ncode_snapshot(conn, category, as_of, rows)


def _get_ncode_master_info(conn: sqlite3.Connection, category: str) -> dict[str, dict]:
    """Per-n_code spec/customer/current-status info, aggregated across the several
    ncode_items rows that can share one n_code (one per customer/spec combination).
    Used by the /api/v2/ncodes sidebar+detail view (TASK2.md Step 6 prototype)."""
    rows = conn.execute(
        """SELECT n_code, grade_astm, thickness_mm, width_mm, customer_ship_to,
                  order_balance_qty, produced_qty, production_balance_qty,
                  preparation_qty, production_plan_qty
           FROM ncode_items WHERE category=? AND n_code IS NOT NULL AND n_code != ''""",
        (category,),
    ).fetchall()
    info: dict[str, dict] = {}
    for r in rows:
        nc = r["n_code"]
        entry = info.setdefault(nc, {
            "grade": r["grade_astm"], "thickness_mm": r["thickness_mm"], "width_mm": r["width_mm"],
            "customers": set(), "order_balance_mt": 0.0, "produced_mt": 0.0, "production_balance_mt": 0.0,
            "prep_mt": 0.0, "plan_mt": 0.0,
        })
        if r["customer_ship_to"]:
            entry["customers"].add(r["customer_ship_to"])
        entry["order_balance_mt"] += r["order_balance_qty"] or 0
        entry["produced_mt"] += r["produced_qty"] or 0
        entry["production_balance_mt"] += r["production_balance_qty"] or 0
        entry["prep_mt"] += r["preparation_qty"] or 0
        entry["plan_mt"] += r["production_plan_qty"] or 0
    return info


def _get_prev_snapshot(conn: sqlite3.Connection, category: str) -> dict[str, dict]:
    """Get the second-latest snapshot for delta comparison."""
    rows = conn.execute(
        """SELECT grade, as_of_date, order_from_sdg, production_order_sum, produced_qty
           FROM weekly_snapshots
           WHERE category=? AND as_of_date = (
               SELECT as_of_date FROM weekly_snapshots WHERE category=?
               GROUP BY as_of_date ORDER BY MAX(created_at) DESC LIMIT 1 OFFSET 1
           )""",
        (category, category),
    ).fetchall()
    return {r["grade"]: dict(r) for r in rows}


def build_summary_data(conn: sqlite3.Connection, category: str) -> dict:
    grades_data = _get_summary_by_grade(conn, category)
    targets = _get_targets(conn, category)
    apc_orders = _get_apc_order_by_grade(conn, category)
    in_transit = _get_in_transit_by_grade(conn, category)
    in_production = _get_in_production_by_grade(conn, category)
    prev_snap = _get_prev_snapshot(conn, category)
    as_of_date = _get_latest_as_of_date(conn, category)

    # Collect all months from APC orders for column headers
    all_months: set[str] = set()
    for mdict in apc_orders.values():
        all_months.update(mdict.keys())
    sorted_months = sorted(m for m in all_months if m and m != "Unknown")

    rows = []
    total_target = total_sdg_order = total_produced = total_plan_sum = total_in_transit = total_in_production = 0.0

    for gd in grades_data:
        grade = gd["grade_astm"]
        target_mt = targets.get(grade, 0)
        # order_balance_qty and preparation_qty are already in MT (CSV column labeled "(mt)")
        order_balance_mt = gd["order_balance"] or 0
        # SDG Order = order placed to the factory (James Preparation qty from 2B/Pre main sheet),
        # not future customer demand from 15.csv (sap_orders) — that's a different concept
        # (see the N-code Dashboard's 26Q4 forecast, which covers Released/Pending).
        sdg_order_mt = gd["prep_total"] or 0
        # in_transit / in_production are already in MT (from 15APC.csv). Produced = In Transit
        # — same figure, kept as two variables since they back two different columns below.
        in_transit_mt = in_transit.get(grade, 0)
        in_production_mt = in_production.get(grade, 0)
        produced_mt = in_transit_mt

        # Monthly APC production orders (from sap_apc_orders, in kg → MT)
        monthly = apc_orders.get(grade, {})
        plan_sum_kg = sum(monthly.get(m, 0) for m in sorted_months)
        plan_sum_mt = plan_sum_kg / 1000

        progress_pct = (produced_mt / target_mt * 100) if target_mt > 0 else None
        in_transit_pct = (in_transit_mt / target_mt * 100) if target_mt > 0 else None
        input_rate_pct = (plan_sum_mt / sdg_order_mt * 100) if sdg_order_mt > 0 else None

        prev = prev_snap.get(grade, {})
        delta_produced = (produced_mt - (prev.get("produced_qty", produced_mt) or produced_mt)) if prev else None
        delta_sdg = (sdg_order_mt - ((prev.get("order_from_sdg") or sdg_order_mt))) if prev else None

        rows.append({
            "grade": grade,
            "ncode_count": gd["ncode_count"],
            "target_mt": round(target_mt, 2),
            "sdg_order_mt": round(sdg_order_mt, 3),
            "sdg_order_pct": round(sdg_order_mt / target_mt * 100, 1) if target_mt > 0 else None,
            "monthly_apc": {m: round(monthly.get(m, 0) / 1000, 3) for m in sorted_months},
            "plan_sum_mt": round(plan_sum_mt, 3),
            "order_balance_mt": round(order_balance_mt, 3),
            "in_transit_mt": round(in_transit_mt, 3),
            "in_transit_pct": round(in_transit_pct, 1) if in_transit_pct is not None else None,
            "in_production_mt": round(in_production_mt, 3),
            "input_rate_pct": round(input_rate_pct, 1) if input_rate_pct is not None else None,
            "produced_mt": round(produced_mt, 3),
            "progress_pct": round(progress_pct, 1) if progress_pct is not None else None,
            "delta_produced": round(delta_produced, 3) if delta_produced is not None else None,
            "delta_sdg": round(delta_sdg, 3) if delta_sdg is not None else None,
        })

        total_target += target_mt
        total_sdg_order += sdg_order_mt
        total_produced += produced_mt
        total_plan_sum += plan_sum_mt
        total_in_transit += in_transit_mt
        total_in_production += in_production_mt

    # Fallback: if no per-grade targets matched, check for a category-level "Total" target
    if total_target == 0:
        row = conn.execute(
            "SELECT target_qty_mt FROM target_quantities WHERE category=? AND grade='Total'",
            (category,),
        ).fetchone()
        if row:
            total_target = row["target_qty_mt"]

    # Standard: roll up minor grades (see _STANDARD_OTHER_GRADES) into a single "Other"
    # row, same as the grade-chart page. Detail link (/detail/Standard/Other) is handled
    # specially in the detail() route below to expand back to the individual grades.
    if category == "Standard":
        other_rows = [r for r in rows if r["grade"] in _STANDARD_OTHER_GRADES]
        if other_rows:
            rows = [r for r in rows if r["grade"] not in _STANDARD_OTHER_GRADES]
            o_target = sum(r["target_mt"] for r in other_rows)
            o_sdg_order = sum(r["sdg_order_mt"] for r in other_rows)
            o_plan_sum = sum(r["plan_sum_mt"] for r in other_rows)
            o_in_transit = sum(r["in_transit_mt"] for r in other_rows)
            o_in_production = sum(r["in_production_mt"] for r in other_rows)
            o_produced = sum(r["produced_mt"] for r in other_rows)
            o_monthly: dict[str, float] = {}
            for r in other_rows:
                for m, v in r["monthly_apc"].items():
                    o_monthly[m] = round(o_monthly.get(m, 0) + v, 3)
            deltas_produced = [r["delta_produced"] for r in other_rows if r["delta_produced"] is not None]
            deltas_sdg = [r["delta_sdg"] for r in other_rows if r["delta_sdg"] is not None]
            rows.append({
                "grade": "Other",
                "ncode_count": sum(r["ncode_count"] for r in other_rows),
                "target_mt": round(o_target, 2),
                "sdg_order_mt": round(o_sdg_order, 3),
                "sdg_order_pct": round(o_sdg_order / o_target * 100, 1) if o_target > 0 else None,
                "monthly_apc": o_monthly,
                "plan_sum_mt": round(o_plan_sum, 3),
                "order_balance_mt": round(sum(r["order_balance_mt"] for r in other_rows), 3),
                "in_transit_mt": round(o_in_transit, 3),
                "in_transit_pct": round(o_in_transit / o_target * 100, 1) if o_target > 0 else None,
                "in_production_mt": round(o_in_production, 3),
                "input_rate_pct": round(o_plan_sum / o_sdg_order * 100, 1) if o_sdg_order > 0 else None,
                "produced_mt": round(o_produced, 3),
                "progress_pct": round(o_produced / o_target * 100, 1) if o_target > 0 else None,
                "delta_produced": round(sum(deltas_produced), 3) if deltas_produced else None,
                "delta_sdg": round(sum(deltas_sdg), 3) if deltas_sdg else None,
            })

    # 카테고리 전체 생산량(그래프·KPI·달성률): Surface 기준 15APC In Transit 합계.
    # Grade별 행은 N-code 매칭 우선 + 15APC Grade 컬럼 보조(_get_in_transit_by_grade)라
    # 양쪽 매칭이 모두 안 되는 행만큼 total_produced(표 합계)가 더 작을 수 있다.
    chart_produced_mt = _get_in_transit_total_by_surface(conn, category)

    return {
        "category": category,
        "as_of_date": as_of_date,
        "months": sorted_months,
        "rows": rows,
        "total_target": round(total_target, 2),
        "total_sdg_order": round(total_sdg_order, 3),
        "total_produced": round(total_produced, 3),
        "total_plan_sum": round(total_plan_sum, 3),
        "total_in_transit": round(total_in_transit, 3),
        "total_in_production": round(total_in_production, 3),
        "chart_produced_mt": chart_produced_mt,
        "total_progress_pct": round(chart_produced_mt / total_target * 100, 1) if total_target > 0 else None,
        "total_in_transit_pct": round(total_in_transit / total_target * 100, 1) if total_target > 0 else None,
        "ncode_match": _get_ncode_match_stats(conn, category),
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with get_db() as conn:
        std = build_summary_data(conn, "Standard")
        pre = build_summary_data(conn, "Precision")
        latest_uploads = conn.execute(
            "SELECT * FROM upload_history ORDER BY uploaded_at DESC LIMIT 5"
        ).fetchall()
        comments = conn.execute(
            "SELECT * FROM comments ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        pending_rows = conn.execute(
            """SELECT category, COUNT(*) as cnt, ROUND(SUM(requested_mt), 3) as total_mt
               FROM order_requests WHERE status='pending' GROUP BY category"""
        ).fetchall()
        pending_by_category = {r["category"]: {"count": r["cnt"], "mt": r["total_mt"] or 0} for r in pending_rows}
        unresolved_apc = _get_unresolved_apc_stats(conn)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "standard": std,
            "precision": pre,
            "latest_uploads": [dict(u) for u in latest_uploads],
            "comment": dict(comments) if comments else None,
            "pending_std": pending_by_category.get("Standard", {"count": 0, "mt": 0}),
            "pending_pre": pending_by_category.get("Precision", {"count": 0, "mt": 0}),
            "unresolved_apc": unresolved_apc,
        },
    )


# Months used for the N-code detail page's order/production ratio: the most recent
# closed month (actual, from the 2B/Pre main sheet) plus the next 2 months' booked
# orders (from 15.csv). Kept as separate constants from Q4_MONTHS/SDG_ORDER_CUTOVER_MONTH
# since this ratio looks 1 month back + 2 months forward, not at Oct/Nov/Dec.
NCODE_RATIO_ACTUAL_MONTH = "2026-06"
NCODE_RATIO_BOOKED_MONTHS = ["2026-07", "2026-08"]
# Target order qty must cover 3 future months (Oct/Nov/Dec) of production, but only
# 2.5x this ratio's 3-month-average is ordered at a time (per current ordering practice),
# then inflated by the grade's yield rate since not all raw material input becomes
# finished output.
NCODE_RATIO_TARGET_MULTIPLIER = 2.5


@router.get("/detail/{category}/{grade:path}", response_class=HTMLResponse)
async def detail(request: Request, category: str, grade: str):
    # "Other" is a virtual grade (see _STANDARD_OTHER_GRADES) rolled up on the dashboard —
    # expand it back to the individual grades it covers instead of an exact grade_astm match.
    if category == "Standard" and grade == "Other":
        placeholders = ",".join("?" * len(_STANDARD_OTHER_GRADES))
        target_clause = f"grade IN ({placeholders})"
        grade_params = tuple(_STANDARD_OTHER_GRADES)
        grade_set = set(_STANDARD_OTHER_GRADES)
    else:
        target_clause = "grade=?"
        grade_params = (grade,)
        grade_set = {grade}

    with get_db() as conn:
        target = conn.execute(
            f"SELECT SUM(target_qty_mt) as target_qty_mt FROM target_quantities WHERE category=? AND {target_clause}",
            (category, *grade_params),
        ).fetchone()
        # APC orders for this grade (IN-subquery, not a join, so a row can't be
        # duplicated by ncode_items having multiple rows for the same n_code)
        grade_clause = "grade_astm IN ({})".format(",".join("?" * len(grade_set)))
        apc_rows = conn.execute(
            f"""SELECT * FROM sap_apc_orders
               WHERE ncode IN (SELECT n_code FROM ncode_items WHERE category=? AND {grade_clause})
               ORDER BY ncode, month""",
            (category, *grade_set),
        ).fetchall()

        master = _get_ncode_master_info(conn, category)
        history = _get_ncode_history_6m(conn, category)
        released = _get_released_order_by_ncode_month(conn)
        in_transit_by_ncode = _get_in_transit_by_ncode(conn)
        yield_rates = _get_yield_rates(conn, category)

    ncode_groups = []
    for nc, info in master.items():
        if info["grade"] not in grade_set:
            continue
        hist = history.get(nc, [0.0] * len(_HISTORY_MONTHS))
        jun_mt = hist[_HISTORY_MONTHS.index(NCODE_RATIO_ACTUAL_MONTH)]
        booked = released.get(nc, {})
        booked_mts = [(booked.get(m, 0) or 0) / 1000 for m in NCODE_RATIO_BOOKED_MONTHS]
        avg3_mt = (jun_mt + sum(booked_mts)) / (1 + len(NCODE_RATIO_BOOKED_MONTHS))

        yield_pct = yield_rates.get(info["grade"], 100) or 100
        target_order_mt = (
            round(avg3_mt * NCODE_RATIO_TARGET_MULTIPLIER / (yield_pct / 100), 3)
            if yield_pct > 0 else None
        )

        order_mt = round(info["prep_mt"], 3)
        production_mt = in_transit_by_ncode.get(nc, 0)

        ncode_groups.append({
            "n_code": nc,
            "grade": info["grade"],
            "thickness_mm": info["thickness_mm"],
            "width_mm": info["width_mm"],
            "customers": ", ".join(sorted(info["customers"])) if info["customers"] else "—",
            "order_balance_mt": round(info["order_balance_mt"], 3),
            "produced_mt": round(info["produced_mt"], 3),
            "production_balance_mt": round(info["production_balance_mt"], 3),
            "plan_mt": round(info["plan_mt"], 3),
            "order_mt": order_mt,
            "production_mt": production_mt,
            "avg3_mt": round(avg3_mt, 3),
            "yield_pct": yield_pct,
            "target_order_mt": target_order_mt,
            "order_pct": round(order_mt / target_order_mt * 100, 1) if target_order_mt else None,
            "production_pct": round(production_mt / target_order_mt * 100, 1) if target_order_mt else None,
        })
    ncode_groups.sort(key=lambda g: (g["thickness_mm"] or 0, g["n_code"] or ""))

    return templates.TemplateResponse(
        "detail.html",
        {
            "request": request,
            "category": category,
            "grade": grade,
            "ncode_groups": ncode_groups,
            "target_mt": target["target_qty_mt"] if target else None,
            "apc_orders": [dict(a) for a in apc_rows],
        },
    )


@router.get("/api/summary/{category}")
async def api_summary(category: str):
    with get_db() as conn:
        return build_summary_data(conn, category)


@router.get("/api/chart/{category}")
async def api_chart(category: str):
    """
    Returns per-grade Target vs In Transit data for the overview grouped bar chart.
    in_transit comes from 15 APC 'In Transit' column (unit: MT).
    """
    with get_db() as conn:
        # Target quantities
        tgt_rows = conn.execute(
            "SELECT grade, target_qty_mt FROM target_quantities WHERE category=? ORDER BY grade",
            (category,),
        ).fetchall()
        targets = {r["grade"]: r["target_qty_mt"] for r in tgt_rows}

        # In Transit per grade (mapped via ncode)
        in_transit = _get_in_transit_by_grade(conn, category)

        # All grades present in either targets or ncode_items
        grade_rows = conn.execute(
            "SELECT DISTINCT grade_astm FROM ncode_items WHERE category=? ORDER BY grade_astm",
            (category,),
        ).fetchall()
        all_grades = sorted({r["grade_astm"] for r in grade_rows} | set(targets.keys()))

    chart_data = [
        {
            "grade": g,
            "target": targets.get(g, 0),
            "in_transit": in_transit.get(g, 0),
        }
        for g in all_grades
    ]
    return {
        "category": category,
        "grades": all_grades,
        "chart_data": chart_data,
        "total_target": round(sum(targets.values()), 2),
        "total_in_transit": round(sum(in_transit.values()), 3),
    }


def _count_months(start_month: str, end_month: str) -> int:
    """Count inclusive months between two YYYY-MM strings."""
    sy, sm = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    return max(1, (ey - sy) * 12 + (em - sm) + 1)


SDG_ORDER_CUTOVER_MONTH = "2026-07"  # months before this use 2B/Pre main-sheet monthly columns
                                      # (historical/near-term forecast); this month onward uses
                                      # actual SDG orders from 15.csv (sap_orders), which is the
                                      # more accurate source once orders are actually booked.


def _prev_month(year_month: str) -> str:
    y, m = int(year_month[:4]), int(year_month[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def _get_grade_chart_data(
    conn: sqlite3.Connection,
    category: str,
    start_month: str = "2026-01",
    end_month: str = "2026-12",
) -> dict:
    """
    Per-grade chart data — 4 metrics (all in MT):

      1. 주문량       preparation_qty        — latest James Preparation quantity, from 2B/Pre CSV (MT)
      2. 생산투입량   sap_apc_orders.order_qty   — SUM from 15APC.csv, kg → MT
      3. 생산량       sap_apc_orders.in_transit  — SUM from 15APC.csv, already MT (same
                      convention as _get_in_transit_by_grade elsewhere in this file)
      4. 6개월 평균   user-selectable period (MT/mo). Blended: months before
                      SDG_ORDER_CUTOVER_MONTH come from 2B/Pre monthly_quantities;
                      months from the cutover onward come from sap_orders (15.csv).
    """
    grade_map = _ncode_to_grade_map(conn, category)

    # ── 1. 주문량 (latest James Prep, MT) ────────────────────────────────
    prep_rows = conn.execute(
        """SELECT grade_astm, ROUND(COALESCE(SUM(preparation_qty), 0), 3) AS prep_mt
           FROM ncode_items WHERE category=? GROUP BY grade_astm""",
        (category,),
    ).fetchall()
    prep_by_grade = {r["grade_astm"]: r["prep_mt"] or 0 for r in prep_rows}

    # ── 2/3. 생산투입량 (Order Qty, kg→MT) / 생산량 (In Transit, already MT) ──
    # 생산량은 Overview 요약표와 동일한 계층 매칭(N-code 우선, 15APC Grade 컬럼 보조)을 사용
    prod_by_grade = _get_in_transit_by_grade(conn, category)
    apc_rows = conn.execute(
        """SELECT ncode, SUM(COALESCE(order_qty, 0)) AS order_kg
           FROM sap_apc_orders GROUP BY ncode"""
    ).fetchall()
    plan_by_grade: dict[str, float] = {}
    for r in apc_rows:
        grade = grade_map.get(r["ncode"], "Unknown")
        plan_by_grade[grade] = round(plan_by_grade.get(grade, 0) + (r["order_kg"] or 0) / 1000, 3)

    # ── 4. 주문잔량 (order_balance_qty, MT) ──────────────────────────────
    ob_rows = conn.execute(
        """SELECT grade_astm, ROUND(COALESCE(SUM(order_balance_qty), 0), 3) AS ob_mt
           FROM ncode_items WHERE category=? GROUP BY grade_astm""",
        (category,),
    ).fetchall()
    ob_by_grade = {r["grade_astm"]: r["ob_mt"] or 0 for r in ob_rows}

    # ── 5. 생산잔량 (production_balance_qty, MT) ─────────────────────────
    pb_rows = conn.execute(
        """SELECT grade_astm, ROUND(COALESCE(SUM(production_balance_qty), 0), 3) AS pb_mt
           FROM ncode_items WHERE category=? GROUP BY grade_astm""",
        (category,),
    ).fetchall()
    pb_by_grade = {r["grade_astm"]: r["pb_mt"] or 0 for r in pb_rows}

    # ── 6. 6개월 평균 — blended 2B/Pre(과거) + SDG order 15.csv(미래), kg → MT/mo ──
    n_months = _count_months(start_month, end_month)
    total_kg_by_grade: dict[str, float] = {}

    hist_end = min(end_month, _prev_month(SDG_ORDER_CUTOVER_MONTH))
    if start_month <= hist_end:
        hist_rows = conn.execute(
            """SELECT n.grade_astm, COALESCE(SUM(m.quantity_kg), 0) AS total_kg
               FROM ncode_items n
               LEFT JOIN monthly_quantities m
                   ON m.ncode_item_id = n.id
                   AND m.year_month >= ? AND m.year_month <= ?
               WHERE n.category=?
               GROUP BY n.grade_astm""",
            (start_month, hist_end, category),
        ).fetchall()
        for r in hist_rows:
            grade = r["grade_astm"]
            total_kg_by_grade[grade] = total_kg_by_grade.get(grade, 0) + (r["total_kg"] or 0)

    if end_month >= SDG_ORDER_CUTOVER_MONTH:
        future_start = max(start_month, SDG_ORDER_CUTOVER_MONTH)
        released_by_grade_month = _get_released_order_by_grade_month(conn, category)
        for grade, months in released_by_grade_month.items():
            future_kg = sum(qty for m, qty in months.items() if m and future_start <= m <= end_month)
            total_kg_by_grade[grade] = total_kg_by_grade.get(grade, 0) + future_kg

    avg_by_grade = {
        grade: round(total_kg / 1000 / n_months, 3)
        for grade, total_kg in total_kg_by_grade.items()
    }

    # ── Aggregate per grade ───────────────────────────────────────────────
    grade_rows = conn.execute(
        "SELECT DISTINCT grade_astm FROM ncode_items WHERE category=? ORDER BY grade_astm",
        (category,),
    ).fetchall()
    all_grades = [r["grade_astm"] for r in grade_rows]

    def _row(grade_key: str, source_grades: list[str]) -> dict:
        return {
            "grade":            grade_key,
            "order_qty_mt":     round(sum(prep_by_grade.get(g, 0) for g in source_grades), 3),
            "input_mt":         round(sum(plan_by_grade.get(g, 0) for g in source_grades), 3),
            "produced_mt":      round(sum(prod_by_grade.get(g, 0) for g in source_grades), 3),
            "avg_6m_mt":        round(sum(avg_by_grade.get(g, 0) for g in source_grades), 3),
            "order_balance_mt": round(sum(ob_by_grade.get(g, 0) for g in source_grades), 3),
            "prod_balance_mt":  round(sum(pb_by_grade.get(g, 0) for g in source_grades), 3),
        }

    if category == "Precision":
        # Precision: single aggregate "Total" row across all grades
        grades = [_row("Total", all_grades)]
    else:
        # Standard: show major grades individually, roll up minor grades into "Other"
        main = [g for g in all_grades if g not in _STANDARD_OTHER_GRADES]
        other = [g for g in all_grades if g in _STANDARD_OTHER_GRADES]
        grades = [_row(g, [g]) for g in main]
        if other:
            grades.append(_row("Other", other))

    all_months = [f"2026-{m:02d}" for m in range(1, 13)]

    return {
        "category":    category,
        "start_month": start_month,
        "end_month":   end_month,
        "n_months":    n_months,
        "all_months":  all_months,
        "grades":      grades,
    }


@router.get("/grade-chart/{category}", response_class=HTMLResponse)
async def grade_chart_page(
    request: Request,
    category: str,
    start_month: str = "2026-01",
    end_month: str = "2026-12",
):
    if category not in ("Standard", "Precision"):
        category = "Standard"
    with get_db() as conn:
        data = _get_grade_chart_data(conn, category, start_month, end_month)
    return templates.TemplateResponse(
        "grade_chart.html",
        {"request": request, "category": category, "data": data},
    )


@router.get("/api/grade-chart/{category}")
async def api_grade_chart(
    category: str,
    start_month: str = "2026-01",
    end_month: str = "2026-12",
):
    with get_db() as conn:
        return _get_grade_chart_data(conn, category, start_month, end_month)


@router.get("/api/q4-ncodes/{category}")
async def api_q4_ncodes(category: str, grade: str):
    """N-code list for a grade (from ncode_items) with Oct/Nov/Dec Customer Forecast values
    and current Consignment Stock, for the expandable drill-down under the 26Q4 forecast
    table. Any customer_forecast/consignment_stock rows that don't exactly match an
    ncode_items spec (ncode+thickness+width+ship-to) are still surfaced as unmatched rows
    so CSV-uploaded values are never silently hidden.

    grade="Unknown" is special-cased: those are N-codes not present anywhere in this
    category's main sheet yet (e.g. Customer Forecast/Consignment Stock entered ahead of
    a new N-code being added to 2B/Pre CSV), so there's no grade_astm to filter by — every
    customer_forecast/consignment_stock row whose ncode can't be resolved to a grade is
    shown instead.
    """
    with get_db() as conn:
        if grade == "Unknown":
            known_ncodes = set(_ncode_to_grade_map(conn, category).keys())
            item_rows = []
            ncodes = []
            # Layer 1/3 (booked orders, statistical fill-in) need a main-sheet grade/spec
            # to resolve, which these unmatched ncodes by definition don't have — so no
            # ncode_summary for this tab, same as the existing item_rows=[] treatment.
            ncode_forecast: dict[str, dict] = {}
            fcst_rows = [
                r for r in conn.execute(
                    "SELECT ncode, thickness_mm, width_mm, ship_to, year_month, forecast_mt FROM customer_forecast"
                ).fetchall()
                if r["ncode"] not in known_ncodes
            ]
            stock_rows = [
                r for r in conn.execute(
                    "SELECT ncode, thickness_mm, width_mm, ship_to, stock_mt FROM consignment_stock"
                ).fetchall()
                if r["ncode"] not in known_ncodes
            ]
        else:
            item_rows = conn.execute(
                """SELECT id as ncode_item_id, n_code, thickness_mm, width_mm, customer_ship_to
                   FROM ncode_items WHERE category=? AND grade_astm=?
                   ORDER BY n_code, thickness_mm""",
                (category, grade),
            ).fetchall()
            ncodes = sorted({r["n_code"] for r in item_rows if r["n_code"]})
            ncode_forecast = compute_ncode_q4_forecast(conn, category)
            fcst_rows = []
            stock_rows = []
            if ncodes:
                placeholders = ",".join("?" * len(ncodes))
                fcst_rows = conn.execute(
                    f"""SELECT ncode, thickness_mm, width_mm, ship_to, year_month, forecast_mt
                        FROM customer_forecast WHERE ncode IN ({placeholders})""",
                    tuple(ncodes),
                ).fetchall()
                stock_rows = conn.execute(
                    f"""SELECT ncode, thickness_mm, width_mm, ship_to, stock_mt
                        FROM consignment_stock WHERE ncode IN ({placeholders})""",
                    tuple(ncodes),
                ).fetchall()

    def _key(ncode, thickness, width, ship_to):
        return (ncode, round(thickness or 0, 2), round(width or 0, 2), ship_to or "")

    fcst_map: dict[tuple, dict[str, float]] = {}
    for f in fcst_rows:
        k = _key(f["ncode"], f["thickness_mm"], f["width_mm"], f["ship_to"])
        fcst_map.setdefault(k, {})[f["year_month"]] = f["forecast_mt"]

    stock_map: dict[tuple, float] = {}
    for s in stock_rows:
        k = _key(s["ncode"], s["thickness_mm"], s["width_mm"], s["ship_to"])
        stock_map[k] = s["stock_mt"]

    result_rows = []
    matched_keys = set()
    for r in item_rows:
        k = _key(r["n_code"], r["thickness_mm"], r["width_mm"], r["customer_ship_to"])
        matched_keys.add(k)
        fm = fcst_map.get(k, {})
        result_rows.append({
            "ncode_item_id": r["ncode_item_id"],
            "n_code": r["n_code"],
            "thickness_mm": r["thickness_mm"],
            "width_mm": r["width_mm"],
            "ship_to": r["customer_ship_to"],
            "fcst_oct": fm.get("2026-10"),
            "fcst_nov": fm.get("2026-11"),
            "fcst_dec": fm.get("2026-12"),
            "consignment_mt": stock_map.get(k),
            "matched": True,
        })

    for k in fcst_map.keys() | stock_map.keys():
        if k in matched_keys:
            continue
        ncode, thickness, width, ship_to = k
        fm = fcst_map.get(k, {})
        result_rows.append({
            "ncode_item_id": None,
            "n_code": ncode,
            "thickness_mm": thickness,
            "width_mm": width,
            "ship_to": ship_to,
            "fcst_oct": fm.get("2026-10"),
            "fcst_nov": fm.get("2026-11"),
            "fcst_dec": fm.get("2026-12"),
            "consignment_mt": stock_map.get(k),
            "matched": False,
        })

    ncodes_in_grade = set(ncodes)
    ncode_summary = [
        {
            "n_code": nc,
            "stat_model": f["stat_model"],
            "booked_mt": f["booked_mt"],
            "cust_fcst_mt": f["cust_fcst_mt"],
            "stat_mt": f["stat_mt"],
            "final_mt": f["final_mt"],
            "confidence": f["confidence"],
        }
        for nc, f in ncode_forecast.items()
        if nc in ncodes_in_grade
    ]
    ncode_summary.sort(key=lambda r: r["final_mt"], reverse=True)

    return {"category": category, "grade": grade, "ncodes": result_rows, "ncode_summary": ncode_summary}


@router.get("/dashboard-v2", response_class=HTMLResponse)
async def dashboard_v2(request: Request):
    """Standalone N-code sidebar+tabs dashboard (TASK2.md Step 6 prototype).
    Kept as a separate page from '/' — fetches its own data client-side."""
    return templates.TemplateResponse("dashboard_v2.html", {"request": request})


@router.get("/api/v2/ncodes/{category}")
async def api_v2_ncodes(category: str):
    """N-code list for the dashboard-v2 sidebar: spec/customer info + current status
    (ncode_items) + 26Q4 forecast layers/confidence (compute_ncode_q4_forecast) +
    6-month actuals history. SPLY is always null — no 2025 data exists yet to compute
    it from (see TASK.md 11.8)."""
    with get_db() as conn:
        master = _get_ncode_master_info(conn, category)
        forecast = compute_ncode_q4_forecast(conn, category)
        history = _get_ncode_history_6m(conn, category)
        as_of_date = _get_latest_as_of_date(conn, category)
        prev_date, prev_snap = get_prev_ncode_snapshot(conn, category, as_of_date) if as_of_date else (None, {})

    ncodes = []
    for nc in sorted(master.keys()):
        m = master[nc]
        f = forecast.get(nc, {})
        monthly = f.get("monthly", {})
        p = prev_snap.get(nc)
        delta = None
        if p is not None:
            delta = {
                "q4_mt": round(f.get("final_mt", 0) - p["q4_mt"], 3),
                "ordered_mt": round((m["prep_mt"] or 0) - p["ordered_mt"], 3),
            }
        ncodes.append({
            "code": nc,
            "grade": m["grade"],
            "thickness_mm": m["thickness_mm"],
            "width_mm": m["width_mm"],
            "customer": ", ".join(sorted(m["customers"])) if m["customers"] else "—",
            "current": {
                "order_balance_mt": round(m["order_balance_mt"], 3),
                "produced_mt": round(m["produced_mt"], 3),
                "production_balance_mt": round(m["production_balance_mt"], 3),
                "ordered_mt": round(m["prep_mt"], 3),
            },
            "q4": [round(monthly.get(mo, {}).get("final_mt", 0), 3) for mo in Q4_MONTHS],
            "layers": {
                "booked": [round(monthly.get(mo, {}).get("booked_mt", 0), 3) for mo in Q4_MONTHS],
                "cust_fcst": [round(monthly.get(mo, {}).get("cust_fcst_mt", 0), 3) for mo in Q4_MONTHS],
                "stat": [round(monthly.get(mo, {}).get("stat_mt", 0), 3) for mo in Q4_MONTHS],
            },
            "model": f.get("stat_model", "ZERO"),
            "confidence": f.get("confidence"),
            "override_mt": f.get("override_mt"),
            "computed_q4_mt": f.get("computed_final_mt"),
            "delta": delta,
            "sply": [None, None, None],
            "hist": history.get(nc, [0.0] * len(_HISTORY_MONTHS)),
        })
    grades = sorted({n["grade"] for n in ncodes if n["grade"]})
    return {
        "category": category,
        "as_of_date": as_of_date,
        "prev_as_of_date": prev_date,
        "grades": grades,
        "ncodes": ncodes,
        "history_months": _HISTORY_MONTHS,
        "q4_months": Q4_MONTHS,
    }


@router.get("/api/v2/ncode-items/{category}/{ncode}")
async def api_v2_ncode_items(category: str, ncode: str):
    """The individual ncode_items rows (one per customer/spec combination) that make up
    a Group N-code — each one carries its own 'covers_n_code' (the actual SAP-level
    N-code). Used by the N-code Dashboard's Forecast tab to let a person submit an order
    request against one specific covers_n_code, not just the group total. There's no
    per-covers_n_code Q4 forecast (booked/customer-forecast/stat are all only tracked at
    the group n_code level), so this only returns each row's current main-sheet figures
    for context — no forecast/"additional needed" pre-fill here."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, covers_n_code, thickness_mm, width_mm, customer_ship_to,
                      ROUND(COALESCE(preparation_qty, 0), 3) AS order_qty_mt,
                      ROUND(COALESCE(order_balance_qty, 0), 3) AS order_balance_mt,
                      ROUND(COALESCE(produced_qty, 0), 3) AS produced_mt,
                      ROUND(COALESCE(production_balance_qty, 0), 3) AS prod_balance_mt
               FROM ncode_items WHERE category=? AND n_code=?
               ORDER BY covers_n_code, customer_ship_to""",
            (category, ncode),
        ).fetchall()
    return {"category": category, "n_code": ncode, "items": [dict(r) for r in rows]}


@router.get("/api/v2/grade-summary/{category}")
async def api_v2_grade_summary(category: str):
    """Grade-level 26Q4 rollup for the dashboard-v2 'Grade 요약' tab. Monthly totals are
    summed from n_code-level final_mt (compute_ncode_q4_forecast), not re-derived from
    grade-level booked/cust_fcst separately, since final_mt already applies the
    max(booked,fcst)-or-stat-fallback logic per n_code (see compute_ncode_q4_forecast)."""
    with get_db() as conn:
        targets = _get_targets(conn, category)
        grades_data = _get_summary_by_grade(conn, category)
        grade_map = _ncode_to_grade_map(conn, category)
        ncode_forecast = compute_ncode_q4_forecast(conn, category)
        as_of_date = _get_latest_as_of_date(conn, category)
        consignment = _get_consignment_stock_by_grade(conn, category)
        # Produced = In Transit (15APC.csv), same definition as the Overview page's
        # "Produced" column — see build_summary_data.
        produced = _get_in_transit_by_grade(conn, category)

    # Distinct n_code count per grade — NOT the same as _get_summary_by_grade's
    # ncode_count, which counts ncode_items rows (one per customer/spec combo, so a
    # single n_code can contribute many). This page operates at the n_code-group level
    # (like compute_ncode_q4_forecast), so it needs the matching distinct count.
    ncode_count_by_grade: dict[str, int] = {}
    for grade in grade_map.values():
        ncode_count_by_grade[grade] = ncode_count_by_grade.get(grade, 0) + 1

    monthly_by_grade: dict[str, dict[str, float]] = {}
    conf_by_grade: dict[str, dict[str, int]] = {}
    for nc, f in ncode_forecast.items():
        grade = grade_map.get(nc, "Unknown")
        bucket = monthly_by_grade.setdefault(grade, {mo: 0.0 for mo in Q4_MONTHS})
        for mo in Q4_MONTHS:
            bucket[mo] += f["monthly"][mo]["final_mt"]
        counts = conf_by_grade.setdefault(grade, {"HIGH": 0, "MID": 0, "LOW": 0})
        if f["confidence"]:
            counts[f["confidence"]] += 1

    rows = []
    for gd in grades_data:
        grade = gd["grade_astm"]
        mb = monthly_by_grade.get(grade, {mo: 0.0 for mo in Q4_MONTHS})
        target_mt = targets.get(grade, 0)
        consignment_mt = consignment.get(grade, 0)
        produced_mt = produced.get(grade, 0)
        net_need_mt = max(target_mt - produced_mt - consignment_mt, 0) if target_mt > 0 else None
        rows.append({
            "grade": grade,
            "ncode_count": ncode_count_by_grade.get(grade, 0),
            "target_mt": round(target_mt, 2),
            "oct_mt": round(mb[Q4_MONTHS[0]], 3),
            "nov_mt": round(mb[Q4_MONTHS[1]], 3),
            "dec_mt": round(mb[Q4_MONTHS[2]], 3),
            "q4_total_mt": round(sum(mb.values()), 3),
            "sply_mt": None,
            "confidence_counts": conf_by_grade.get(grade, {"HIGH": 0, "MID": 0, "LOW": 0}),
            "consignment_mt": round(consignment_mt, 3),
            "net_need_mt": round(net_need_mt, 3) if net_need_mt is not None else None,
        })

    conf_totals = {"HIGH": 0, "MID": 0, "LOW": 0}
    for f in ncode_forecast.values():
        if f["confidence"]:
            conf_totals[f["confidence"]] += 1

    return {
        "category": category,
        "as_of_date": as_of_date,
        "rows": rows,
        "total_q4_mt": round(sum(r["q4_total_mt"] for r in rows), 3),
        "total_ncodes": len(ncode_forecast),
        "confidence_totals": conf_totals,
        "total_consignment_mt": round(sum(r["consignment_mt"] for r in rows), 3),
    }


@router.get("/api/v2/customer-summary/{category}")
async def api_v2_customer_summary(category: str):
    """Ship-to customer rollup for the N-code dashboard's Customers tab. Status figures
    (ordered/balance/produced) come straight from the per-customer ncode_items rows;
    the Q4 forecast column is the customer's own entered Customer FCST (customer_forecast
    table) — the model forecast is only tracked at group N-code level and can't be
    attributed to a single customer, so it is deliberately not shown here."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(NULLIF(TRIM(customer_ship_to), ''), '(Unspecified)') AS customer,
                      COUNT(DISTINCT n_code) AS ncode_count,
                      GROUP_CONCAT(DISTINCT n_code) AS ncodes,
                      ROUND(SUM(COALESCE(preparation_qty, 0)), 3) AS ordered_mt,
                      ROUND(SUM(COALESCE(order_balance_qty, 0)), 3) AS order_balance_mt,
                      ROUND(SUM(COALESCE(produced_qty, 0)), 3) AS produced_mt,
                      ROUND(SUM(COALESCE(production_balance_qty, 0)), 3) AS production_balance_mt
               FROM ncode_items
               WHERE category=? AND n_code IS NOT NULL AND n_code != ''
               GROUP BY customer
               ORDER BY ordered_mt DESC""",
            (category,),
        ).fetchall()
        fcst_rows = conn.execute(
            # forecast_mt is stored in kg despite the name (see
            # _get_customer_forecast_by_ncode_month) — convert to MT here
            f"""SELECT COALESCE(NULLIF(TRIM(ship_to), ''), '(Unspecified)') AS customer,
                       ROUND(SUM(COALESCE(forecast_mt, 0)) / 1000.0, 3) AS q4_fcst_mt
                FROM customer_forecast
                WHERE year_month IN ({",".join("?" * len(Q4_MONTHS))})
                  AND ncode IN (SELECT DISTINCT n_code FROM ncode_items WHERE category=?)
                GROUP BY customer""",
            (*Q4_MONTHS, category),
        ).fetchall()
        as_of_date = _get_latest_as_of_date(conn, category)

    fcst_by_customer = {r["customer"]: r["q4_fcst_mt"] for r in fcst_rows}
    customers = [{
        "customer": r["customer"],
        "ncode_count": r["ncode_count"],
        "ncodes": sorted((r["ncodes"] or "").split(",")),
        "ordered_mt": r["ordered_mt"] or 0,
        "order_balance_mt": r["order_balance_mt"] or 0,
        "produced_mt": r["produced_mt"] or 0,
        "production_balance_mt": r["production_balance_mt"] or 0,
        "q4_fcst_mt": fcst_by_customer.get(r["customer"], 0),
    } for r in rows]
    return {"category": category, "as_of_date": as_of_date, "customers": customers}


@router.post("/api/customer-forecast")
async def save_customer_forecast(request: Request):
    """Manual single-cell save from the Q4 forecast N-code drill-down."""
    body = await request.json()
    ncode = (body.get("ncode") or "").strip()
    year_month = body.get("year_month")
    if not ncode or year_month not in Q4_MONTHS:
        return JSONResponse({"status": "error", "error": "ncode and a valid year_month (2026-10/11/12) are required"}, status_code=400)
    with get_db() as conn:
        upsert_customer_forecast(
            conn, ncode, body.get("thickness_mm"), body.get("width_mm"),
            body.get("ship_to"), year_month, body.get("forecast_mt"),
        )
    return {"status": "ok"}


@router.post("/api/consignment-stock")
async def save_consignment_stock(request: Request):
    """Manual single-cell save from the Q4 forecast N-code drill-down."""
    body = await request.json()
    ncode = (body.get("ncode") or "").strip()
    if not ncode:
        return JSONResponse({"status": "error", "error": "ncode is required"}, status_code=400)
    with get_db() as conn:
        upsert_consignment_stock(
            conn, ncode, body.get("thickness_mm"), body.get("width_mm"),
            body.get("ship_to"), body.get("stock_mt"),
        )
    return {"status": "ok"}


@router.post("/api/forecast-override")
async def save_forecast_override(request: Request):
    """Q4-total manual override save/clear from the dashboard-v2 Forecast tab.
    override_mt null (or missing) clears the override back to the model forecast."""
    body = await request.json()
    category = (body.get("category") or "").strip()
    ncode = (body.get("ncode") or "").strip()
    override_mt = body.get("override_mt")
    if category not in ("Standard", "Precision") or not ncode:
        return JSONResponse(
            {"status": "error", "error": "category (Standard/Precision) and ncode are required"},
            status_code=400,
        )
    if override_mt is not None:
        try:
            override_mt = float(override_mt)
        except (TypeError, ValueError):
            return JSONResponse({"status": "error", "error": "override_mt must be a number"}, status_code=400)
        if override_mt < 0:
            return JSONResponse({"status": "error", "error": "override_mt must be 0 or greater"}, status_code=400)
    with get_db() as conn:
        if override_mt is None:
            delete_forecast_override(conn, category, ncode)
        else:
            upsert_forecast_override(conn, category, ncode, override_mt)
    return {"status": "ok", "override_mt": override_mt}


@router.get("/api/ncode-breakdown/{category}")
async def api_ncode_breakdown(category: str, grade: str, customer: str):
    with get_db() as conn:
        # All individual rows for this customer/grade
        all_rows = conn.execute(
            """SELECT
                   n_code, covers_n_code, thickness_mm, width_mm,
                   ROUND(COALESCE(preparation_qty,        0), 3) AS order_qty_mt,
                   ROUND(COALESCE(production_plan_qty,    0), 3) AS input_mt,
                   ROUND(COALESCE(produced_qty,           0), 3) AS produced_mt,
                   ROUND(COALESCE(order_balance_qty,      0), 3) AS order_balance_mt,
                   ROUND(COALESCE(production_balance_qty, 0), 3) AS prod_balance_mt
               FROM ncode_items
               WHERE category=? AND grade_astm=? AND customer_ship_to=?
               ORDER BY n_code, order_qty_mt DESC""",
            (category, grade, customer),
        ).fetchall()

    # Group by n_code in Python, preserving insertion order
    from collections import defaultdict
    groups: dict = {}
    for r in all_rows:
        key = r["n_code"]
        if key not in groups:
            groups[key] = {
                "group_n_code": key,
                "covers_n_codes": [],
                "row_count": 0,
                "order_qty_mt": 0.0,
                "input_mt": 0.0,
                "produced_mt": 0.0,
                "order_balance_mt": 0.0,
                "prod_balance_mt": 0.0,
                "items": [],
            }
        g = groups[key]
        if r["covers_n_code"]:
            g["covers_n_codes"].append(r["covers_n_code"])
        g["row_count"] += 1
        g["order_qty_mt"]     = round(g["order_qty_mt"]     + r["order_qty_mt"],     3)
        g["input_mt"]         = round(g["input_mt"]         + r["input_mt"],         3)
        g["produced_mt"]      = round(g["produced_mt"]      + r["produced_mt"],      3)
        g["order_balance_mt"] = round(g["order_balance_mt"] + r["order_balance_mt"], 3)
        g["prod_balance_mt"]  = round(g["prod_balance_mt"]  + r["prod_balance_mt"],  3)
        g["items"].append({
            "covers_n_code":   r["covers_n_code"] or "—",
            "thickness_mm":    r["thickness_mm"],
            "width_mm":        r["width_mm"],
            "order_qty_mt":    r["order_qty_mt"],
            "input_mt":        r["input_mt"],
            "produced_mt":     r["produced_mt"],
            "order_balance_mt":r["order_balance_mt"],
            "prod_balance_mt": r["prod_balance_mt"],
        })

    result = sorted(groups.values(), key=lambda g: g["order_qty_mt"], reverse=True)
    for g in result:
        g["covers_n_codes"] = ", ".join(g["covers_n_codes"])
    return {"grade": grade, "customer": customer, "ncodes": result}


def _pool_group_balances(balances: list[float]) -> list[float]:
    """Customers sharing one Group N-code share the same grade+thickness — literally the
    same coil, just slit to different widths — so a surplus (negative balance: over-
    produced/over-delivered) on one customer's line can offset another's shortfall
    (positive balance) before either is shown, rather than each being judged in
    isolation. Coverage is split proportionally across every shortfall/surplus in the
    group (no customer's balance is arbitrarily prioritized over another's). The group's
    total balance is unchanged — this only redistributes which customer it's shown against.
    """
    total_shortfall = sum(b for b in balances if b > 0)
    total_surplus = -sum(b for b in balances if b < 0)
    covered = min(total_shortfall, total_surplus)
    if covered <= 0:
        return balances
    shortfall_frac = covered / total_shortfall if total_shortfall > 0 else 0
    surplus_frac = covered / total_surplus if total_surplus > 0 else 0
    result = []
    for b in balances:
        if b > 0:
            result.append(round(b * (1 - shortfall_frac), 3))
        elif b < 0:
            result.append(round(b * (1 - surplus_frac), 3))
        else:
            result.append(0.0)
    return result


def _redistribute_by_forecast_share(values: list[float], forecasts: list[float]) -> list[float]:
    """Re-attribute a Group N-code's pooled Order Qty / Produced Qty across its
    customers by each customer's share of the *group's* forecast demand, instead of
    each row's raw own-recorded amount. Customers sharing a group only differ by
    width — same coil — so which customer's line a given unit of order/production
    happens to be booked against is a bookkeeping artifact, not a real constraint;
    the group's pooled total is real, its per-customer split isn't. Falls back to the
    raw values if no customer in the group has forecast data to split by."""
    total = sum(values)
    total_forecast = sum(forecasts)
    if total_forecast <= 0:
        return values
    return [round(total * (f / total_forecast), 3) for f in forecasts]


@router.get("/api/customer-breakdown/{category}")
async def api_customer_breakdown(category: str, grade: str):
    with get_db() as conn:
        # Balance rows kept at (n_code, customer) granularity — pooling below only makes
        # sense *within* one Group N-code, not across unrelated N-codes a customer
        # happens to also buy.
        rows = conn.execute(
            """SELECT n.n_code, n.customer_ship_to,
                   SUM(COALESCE(n.order_balance_qty,      0)) AS order_balance_mt,
                   SUM(COALESCE(n.production_balance_qty, 0)) AS prod_balance_mt,
                   SUM(COALESCE(n.preparation_qty,        0)) AS order_qty_mt,
                   SUM(COALESCE(n.production_plan_qty,    0)) AS input_mt,
                   SUM(COALESCE(n.produced_qty,           0)) AS produced_mt,
                   COUNT(DISTINCT n.id) AS ncode_count
               FROM ncode_items n
               WHERE n.category=? AND n.grade_astm=?
               GROUP BY n.n_code, n.customer_ship_to""",
            (category, grade),
        ).fetchall()
        # Forecast kept at (n_code, customer) granularity too — it's the basis used to
        # re-split a group's pooled Order Qty / Produced Qty below (see
        # _redistribute_by_forecast_share), so it needs to match that granularity, not
        # just be the customer's forecast averaged across every grade/group they buy.
        forecast_rows = conn.execute(
            """SELECT n.n_code, n.customer_ship_to,
                   ROUND(COALESCE(SUM(m.quantity_kg), 0) / 1000.0 / 6.0, 3) AS forecast_avg_mt
               FROM ncode_items n
               LEFT JOIN monthly_quantities m
                   ON m.ncode_item_id = n.id
                   AND m.year_month >= '2026-03' AND m.year_month <= '2026-08'
               WHERE n.category=? AND n.grade_astm=?
               GROUP BY n.n_code, n.customer_ship_to""",
            (category, grade),
        ).fetchall()
        overall_forecast_rows = conn.execute(
            """SELECT n.customer_ship_to,
                   ROUND(COALESCE(SUM(m.quantity_kg), 0) / 1000.0 / 6.0, 3) AS forecast_avg_mt
               FROM ncode_items n
               LEFT JOIN monthly_quantities m
                   ON m.ncode_item_id = n.id
                   AND m.year_month >= '2026-03' AND m.year_month <= '2026-08'
               WHERE n.category=? AND n.grade_astm=?
               GROUP BY n.customer_ship_to""",
            (category, grade),
        ).fetchall()

    group_forecast_map = {(r["n_code"], r["customer_ship_to"]): r["forecast_avg_mt"] or 0 for r in forecast_rows}

    by_group: dict[str, list[dict]] = {}
    for r in rows:
        by_group.setdefault(r["n_code"], []).append({
            "customer": r["customer_ship_to"],
            "order_balance": r["order_balance_mt"] or 0,
            "prod_balance": r["prod_balance_mt"] or 0,
            "order_qty_mt": r["order_qty_mt"] or 0,
            "input_mt": r["input_mt"] or 0,
            "produced_mt": r["produced_mt"] or 0,
            "ncode_count": r["ncode_count"],
        })

    customer_totals: dict[str, dict] = {}
    for n_code, entries in by_group.items():
        pooled_order = _pool_group_balances([e["order_balance"] for e in entries])
        pooled_prod = _pool_group_balances([e["prod_balance"] for e in entries])
        group_forecasts = [group_forecast_map.get((n_code, e["customer"]), 0) for e in entries]
        pooled_order_qty = _redistribute_by_forecast_share([e["order_qty_mt"] for e in entries], group_forecasts)
        pooled_produced = _redistribute_by_forecast_share([e["produced_mt"] for e in entries], group_forecasts)
        for e, ob, pb, oq, pd in zip(entries, pooled_order, pooled_prod, pooled_order_qty, pooled_produced):
            c = customer_totals.setdefault(e["customer"], {
                "order_balance_mt": 0.0, "prod_balance_mt": 0.0,
                "order_qty_mt": 0.0, "input_mt": 0.0, "produced_mt": 0.0, "ncode_count": 0,
            })
            c["order_balance_mt"] += ob
            c["prod_balance_mt"] += pb
            c["order_qty_mt"] += oq
            c["input_mt"] += e["input_mt"]
            c["produced_mt"] += pd
            c["ncode_count"] += e["ncode_count"]

    forecast_map = {r["customer_ship_to"]: r["forecast_avg_mt"] or 0 for r in overall_forecast_rows}
    customers = [
        {
            "customer_ship_to": cust,
            "forecast_avg_mt": forecast_map.get(cust, 0),
            "order_balance_mt": round(tot["order_balance_mt"], 3),
            "prod_balance_mt": round(tot["prod_balance_mt"], 3),
            "order_qty_mt": round(tot["order_qty_mt"], 3),
            "input_mt": round(tot["input_mt"], 3),
            "produced_mt": round(tot["produced_mt"], 3),
            "ncode_count": tot["ncode_count"],
        }
        for cust, tot in customer_totals.items()
    ]
    customers.sort(key=lambda c: c["forecast_avg_mt"], reverse=True)
    return {
        "grade": grade,
        "customers": customers,
    }
