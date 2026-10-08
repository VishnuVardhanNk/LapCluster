"""`python -m lapclusters` starts the app and opens it in the browser."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from lapclusters.app.node import Node
from lapclusters.app.server import create_app

FIRST_PORT = 8470


def _free_port(first: int, tries: int = 20) -> int:
    for port in range(first, first + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port between {first} and {first + tries - 1}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the LapClusters app on this laptop.")
    parser.add_argument("--port", type=int, default=FIRST_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args()

    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"LapClusters is running at {url}")
    print("Leave this window open. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    # Remembered next to the project, and listed in .gitignore: it holds the
    # cluster password, as .env does.
    node = Node(memory=Path(__file__).resolve().parent.parent / ".lapclusters.json")
    threading.Thread(target=node.restore, daemon=True).start()
    uvicorn.run(create_app(node), host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
