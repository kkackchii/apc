"""File upload API endpoints."""
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.database import (
    bulk_insert_ncode_items,
    bulk_insert_sap_15,
    bulk_insert_sap_15apc,
    bulk_insert_sap_25,
    bulk_insert_sap_104,
    get_db,
    insert_upload_history,
    upsert_consignment_stock,
    upsert_customer_forecast,
)
from app.paths import BASE_DIR
from parsers.consignment_stock_parser import parse_consignment_stock_csv
from parsers.customer_forecast_parser import parse_customer_forecast_csv
from parsers.main_sheet_parser import (
    CATEGORY_PRECISION,
    CATEGORY_STANDARD,
    parse_main_sheet,
)
from parsers.sap_upload_parser import (
    TABLE_15,
    TABLE_15APC,
    TABLE_25,
    TABLE_104,
    auto_detect_table_type,
    parse_combined_file,
    parse_single_file,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

RAW_DATA_DIR = BASE_DIR / "data" / "raw"
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)


async def _save_temp(upload: UploadFile) -> str:
    content = await upload.read()
    suffix = Path(upload.filename).suffix
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


def _snapshot_after_upload(categories):
    """Refresh the week-over-week per-N-code snapshots after a successful upload
    (see dashboard.take_ncode_snapshot). Imported inside the function to avoid an
    app.routers-internal import cycle."""
    from app.routers.dashboard import take_ncode_snapshot
    with get_db() as conn:
        for category in dict.fromkeys(categories):
            try:
                take_ncode_snapshot(conn, category)
            except Exception as e:
                print(f"[upload] snapshot failed for {category}: {e}")


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    with get_db() as conn:
        history = conn.execute(
            "SELECT * FROM upload_history ORDER BY uploaded_at DESC LIMIT 20"
        ).fetchall()
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        "upload.html",
        {"request": request, "history": [dict(h) for h in history], "today": today},
    )


@router.post("/api/upload/sap")
async def upload_sap(
    request: Request,
    file_15: Optional[UploadFile] = File(None),
    file_25: Optional[UploadFile] = File(None),
    file_15apc: Optional[UploadFile] = File(None),
    file_104: Optional[UploadFile] = File(None),
    file_combined: Optional[UploadFile] = File(None),
    as_of_date: Optional[str] = Form(None),
):
    """Accept SAP uploads: either separate files (15/25/15APC/104) or 1 combined file."""
    results = []
    errors = []
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")

    # --- Combined file ---
    if file_combined and file_combined.filename:
        tmp = await _save_temp(file_combined)
        try:
            tables = parse_combined_file(tmp)
            with get_db() as conn:
                for ttype, records in tables.items():
                    if not records:
                        continue
                    uid = insert_upload_history(conn, ttype, file_combined.filename, as_of, len(records))
                    if ttype == TABLE_15:
                        bulk_insert_sap_15(conn, records, uid)
                    elif ttype == TABLE_25:
                        bulk_insert_sap_25(conn, records, uid)
                    elif ttype == TABLE_15APC:
                        bulk_insert_sap_15apc(conn, records, uid)
                    elif ttype == TABLE_104:
                        bulk_insert_sap_104(conn, records, uid)
                    results.append({"type": ttype, "rows": len(records)})
        except Exception as e:
            errors.append({"file": file_combined.filename, "error": str(e)})
        finally:
            os.unlink(tmp)

    # --- Separate files ---
    for upload, forced_type in [
        (file_15, TABLE_15),
        (file_25, TABLE_25),
        (file_15apc, TABLE_15APC),
        (file_104, TABLE_104),
    ]:
        if not upload or not upload.filename:
            continue
        tmp = await _save_temp(upload)
        try:
            detected = auto_detect_table_type(tmp) or forced_type
            records = parse_single_file(tmp, detected)
            with get_db() as conn:
                uid = insert_upload_history(conn, detected, upload.filename, as_of, len(records))
                if detected == TABLE_15:
                    bulk_insert_sap_15(conn, records, uid)
                elif detected == TABLE_25:
                    bulk_insert_sap_25(conn, records, uid)
                elif detected == TABLE_15APC:
                    bulk_insert_sap_15apc(conn, records, uid)
                elif detected == TABLE_104:
                    bulk_insert_sap_104(conn, records, uid)
            results.append({"type": detected, "rows": len(records), "file": upload.filename})
        except Exception as e:
            errors.append({"file": upload.filename, "error": str(e)})
        finally:
            os.unlink(tmp)

    if results:
        # SAP tables feed the booked-order forecast layer for both categories
        _snapshot_after_upload([CATEGORY_STANDARD, CATEGORY_PRECISION])
    status = "ok" if not errors else ("partial" if results else "error")
    return JSONResponse({"status": status, "results": results, "errors": errors})


@router.post("/api/upload/main")
async def upload_main(
    request: Request,
    file_2b: Optional[UploadFile] = File(None),
    file_pre: Optional[UploadFile] = File(None),
    as_of_date: Optional[str] = Form(None),
):
    """Upload 2B_APC or Pre_APC main sheet CSV files."""
    results = []
    errors = []
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")

    for upload, category in [(file_2b, CATEGORY_STANDARD), (file_pre, CATEGORY_PRECISION)]:
        if not upload or not upload.filename:
            continue
        tmp = await _save_temp(upload)
        try:
            records, detected_date = parse_main_sheet(tmp, category)
            used_date = detected_date or as_of
            with get_db() as conn:
                # Remove old data for this category before inserting
                conn.execute("DELETE FROM ncode_items WHERE category=?", (category,))
                uid = insert_upload_history(conn, f"main_{category.lower()[:3]}", upload.filename, used_date, len(records))
                bulk_insert_ncode_items(conn, records, uid)
            results.append({"category": category, "rows": len(records), "as_of_date": used_date})
        except Exception as e:
            errors.append({"file": upload.filename, "category": category, "error": str(e)})
        finally:
            os.unlink(tmp)

    _snapshot_after_upload([r["category"] for r in results])
    status = "ok" if not errors else ("partial" if results else "error")
    return JSONResponse({"status": status, "results": results, "errors": errors})


@router.post("/api/upload/customer-forecast")
async def upload_customer_forecast(file: UploadFile = File(...)):
    """Upload a Customer Forecast CSV (N-Code, Thickness, Width, Ship-to, 2026-10/11/12)."""
    tmp = await _save_temp(file)
    try:
        records = parse_customer_forecast_csv(tmp)
        cell_count = 0
        with get_db() as conn:
            for rec in records:
                for ym, qty in rec["monthly"].items():
                    upsert_customer_forecast(
                        conn, rec["ncode"], rec.get("thickness_mm"), rec.get("width_mm"),
                        rec.get("ship_to"), ym, qty,
                    )
                    cell_count += 1
        return JSONResponse({
            "status": "ok",
            "results": [{"type": "customer_forecast", "rows": len(records), "cells": cell_count, "file": file.filename}],
            "errors": [],
        })
    except Exception as e:
        return JSONResponse({"status": "error", "results": [], "errors": [{"file": file.filename, "error": str(e)}]})
    finally:
        os.unlink(tmp)


@router.post("/api/upload/consignment-stock")
async def upload_consignment_stock(file: UploadFile = File(...)):
    """Upload a Consignment Stock CSV (N-Code, Thickness, Width, Ship-to, Consignment Stock)."""
    tmp = await _save_temp(file)
    try:
        records = parse_consignment_stock_csv(tmp)
        with get_db() as conn:
            for rec in records:
                upsert_consignment_stock(
                    conn, rec["ncode"], rec.get("thickness_mm"), rec.get("width_mm"),
                    rec.get("ship_to"), rec.get("stock_mt"),
                )
        return JSONResponse({
            "status": "ok",
            "results": [{"type": "consignment_stock", "rows": len(records), "file": file.filename}],
            "errors": [],
        })
    except Exception as e:
        return JSONResponse({"status": "error", "results": [], "errors": [{"file": file.filename, "error": str(e)}]})
    finally:
        os.unlink(tmp)


@router.post("/api/comment")
async def save_comment(request: Request):
    body = await request.json()
    content = body.get("content", "").strip()
    as_of = body.get("as_of_date", datetime.now().strftime("%Y-%m-%d"))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO comments(as_of_date, content) VALUES(?,?)", (as_of, content)
        )
    return {"status": "ok"}
