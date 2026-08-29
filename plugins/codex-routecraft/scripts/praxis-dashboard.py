#!/usr/bin/env python3
"""Start the independent, local-only, read-only Praxis Dashboard."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import threading
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from praxis_dashboard.server import create_server, query_for_directory  # noqa: E402

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="praxis-dashboard", description="Local-only Praxis Dashboard")
    p.add_argument("--data-dir", default=".", help="existing Praxis source directory (never created)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open-browser", action="store_true", help="open the loopback URL after startup")
    p.add_argument("--no-browser", action="store_true", help="compatibility option; browser is not opened by default")
    a = p.parse_args(argv)
    if not 0 <= a.port <= 65535:
        p.error("--port must be between 0 and 65535")
    server = create_server(query_for_directory(a.data_dir), port=a.port)
    print(server.url, flush=True)
    if a.open_browser and not a.no_browser:
        threading.Timer(0.1, lambda: webbrowser.open(server.url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0
if __name__ == "__main__": raise SystemExit(main())
