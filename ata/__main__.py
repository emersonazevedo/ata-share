"""Ponto de entrada:  python -m ata  [--host H] [--port P] [--no-browser]"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser

import uvicorn

from .config import load_config


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="ata", description="Transcrição local de reuniões + ata")
    parser.add_argument("--host", default=cfg.server.host)
    parser.add_argument("--port", type=int, default=cfg.server.port)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reload", action="store_true", help="dev only")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    if cfg.server.open_browser and not args.no_browser:
        def _open() -> None:
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    print(f"\n  ata v2  →  {url}\n  (Ctrl+C para encerrar)\n")
    uvicorn.run("ata.server:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
