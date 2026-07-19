"""Target quantity management endpoints."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db, upsert_target, upsert_yield_rate
from app.paths import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM target_quantities ORDER BY category, grade"
        ).fetchall()
        yield_rows = conn.execute(
            "SELECT * FROM grade_yield_rates ORDER BY category, grade"
        ).fetchall()
        grades_std = conn.execute(
            "SELECT DISTINCT grade_astm FROM ncode_items WHERE category='Standard' ORDER BY grade_astm"
        ).fetchall()
        grades_pre = conn.execute(
            "SELECT DISTINCT grade_astm FROM ncode_items WHERE category='Precision' ORDER BY grade_astm"
        ).fetchall()
    targets = [dict(r) for r in rows]
    yield_rates = [dict(r) for r in yield_rows]
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "targets": targets,
            "yield_rates": yield_rates,
            "grades_std": [r[0] for r in grades_std],
            "grades_pre": [r[0] for r in grades_pre],
        },
    )


@router.post("/api/settings/targets")
async def update_targets(request: Request):
    body = await request.json()
    items = body.get("items", [])
    with get_db() as conn:
        for item in items:
            try:
                upsert_target(
                    conn,
                    item["category"],
                    item["grade"],
                    float(item["target_qty_mt"]),
                    item.get("quarter", "2025-Q4"),
                )
            except Exception as e:
                return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    return {"status": "ok", "updated": len(items)}


@router.delete("/api/settings/targets/{target_id}")
async def delete_target(target_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM target_quantities WHERE id=?", (target_id,))
    return {"status": "ok"}


@router.post("/api/settings/yield-rates")
async def update_yield_rates(request: Request):
    body = await request.json()
    items = body.get("items", [])
    with get_db() as conn:
        for item in items:
            try:
                upsert_yield_rate(
                    conn,
                    item["category"],
                    item["grade"],
                    float(item["yield_rate_pct"]),
                )
            except Exception as e:
                return JSONResponse({"status": "error", "error": str(e)}, status_code=400)
    return {"status": "ok", "updated": len(items)}


@router.delete("/api/settings/yield-rates/{yield_id}")
async def delete_yield_rate(yield_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM grade_yield_rates WHERE id=?", (yield_id,))
    return {"status": "ok"}
