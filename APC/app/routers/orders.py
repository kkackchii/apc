"""Order request workflow: a person submits an additional-order request for an N-code
(computed from Q4 Final Forecast minus what's already ordered — see dashboard_v2.html's
Forecast tab), someone else reviews and approves/rejects it, then marks it sent to the
factory once actually placed in SAP. No login system exists in this app, so there is no
access control here — anyone can submit or review (see conversation decision)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import create_order_request, get_db, update_order_request_status
from app.paths import BASE_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_VALID_TRANSITIONS = {
    "approved": {"pending"},
    "rejected": {"pending"},
    "sent": {"approved"},
}


@router.get("/order-requests", response_class=HTMLResponse)
async def order_requests_page(request: Request):
    return templates.TemplateResponse("order_requests.html", {"request": request})


@router.get("/api/order-requests")
async def list_order_requests(status: str = "", n_code: str = ""):
    query = "SELECT * FROM order_requests"
    clauses = []
    params: list = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if n_code:
        clauses.append("n_code=?")
        params.append(n_code)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY requested_at DESC"
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return {"requests": [dict(r) for r in rows]}


@router.post("/api/order-requests")
async def submit_order_request(request: Request):
    body = await request.json()
    category = (body.get("category") or "").strip()
    n_code = (body.get("n_code") or "").strip()
    covers_n_code = (body.get("covers_n_code") or "").strip()
    grade = (body.get("grade") or "").strip()
    requested_mt = body.get("requested_mt")
    requested_by = (body.get("requested_by") or "").strip()
    note = (body.get("note") or "").strip()

    if not category or not n_code:
        return JSONResponse({"status": "error", "error": "category and n_code are required"}, status_code=400)
    try:
        requested_mt = float(requested_mt)
    except (TypeError, ValueError):
        return JSONResponse({"status": "error", "error": "requested_mt must be a number"}, status_code=400)
    if requested_mt <= 0:
        return JSONResponse({"status": "error", "error": "requested_mt must be greater than 0"}, status_code=400)
    if not requested_by:
        return JSONResponse({"status": "error", "error": "requested_by is required"}, status_code=400)

    with get_db() as conn:
        request_id = create_order_request(
            conn, category, n_code, grade, requested_mt, requested_by, note, covers_n_code,
        )
    return {"status": "ok", "id": request_id}


@router.post("/api/order-requests/{request_id}/{action}")
async def review_order_request(request_id: int, action: str, request: Request):
    status_map = {"approve": "approved", "reject": "rejected", "mark-sent": "sent"}
    new_status = status_map.get(action)
    if not new_status:
        return JSONResponse({"status": "error", "error": f"unknown action: {action}"}, status_code=400)

    body = await request.json()
    reviewed_by = (body.get("reviewed_by") or "").strip()
    note = (body.get("note") or "").strip()
    if not reviewed_by:
        return JSONResponse({"status": "error", "error": "reviewed_by is required"}, status_code=400)

    with get_db() as conn:
        row = conn.execute("SELECT status FROM order_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return JSONResponse({"status": "error", "error": "request not found"}, status_code=404)
        if row["status"] not in _VALID_TRANSITIONS[new_status]:
            return JSONResponse(
                {"status": "error", "error": f"cannot move '{row['status']}' request to '{new_status}'"},
                status_code=400,
            )
        update_order_request_status(conn, request_id, new_status, reviewed_by, note)
    return {"status": "ok"}


@router.delete("/api/order-requests/{request_id}")
async def delete_order_request(request_id: int):
    """Only pending requests can be withdrawn — once reviewed, the record stays for the audit trail."""
    with get_db() as conn:
        row = conn.execute("SELECT status FROM order_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return JSONResponse({"status": "error", "error": "request not found"}, status_code=404)
        if row["status"] != "pending":
            return JSONResponse({"status": "error", "error": "only pending requests can be withdrawn"}, status_code=400)
        conn.execute("DELETE FROM order_requests WHERE id=?", (request_id,))
    return {"status": "ok"}
