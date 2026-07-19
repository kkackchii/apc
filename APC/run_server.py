"""Entry point for running the dashboard — from source or as a packaged (PyInstaller)
executable. Prints the LAN address so it can be shared with coworkers on the same
network directly from the console, without needing to look up the IP separately.
"""
import socket
import threading
import time
import webbrowser

import uvicorn

from app.main import app

HOST = "0.0.0.0"
PORT = 8000


def _get_lan_ip() -> str:
    """Best-effort local network IP (no packets actually sent; UDP connect() just
    triggers a routing-table lookup for the interface that would be used)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    lan_ip = _get_lan_ip()
    print("===========================")
    print("APC Pre-Production Dashboard")
    print("===========================")
    print(f"On this PC:        http://localhost:{PORT}")
    print(f"From other PCs:    http://{lan_ip}:{PORT}")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
