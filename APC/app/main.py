"""FastAPI application entry point."""
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.autoload import autoload_data_files
from app.database import init_db
from app.paths import BASE_DIR
from app.routers import changes, dashboard, orders, settings, upload

app = FastAPI(title="APC Pre-Production Inventory Dashboard", version="1.0.0")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(dashboard.router)
app.include_router(upload.router)
app.include_router(settings.router)
app.include_router(changes.router)
app.include_router(orders.router)


@app.on_event("startup")
async def startup():
    init_db()
    autoload_data_files()
    print("[APC] Server started.")
