"""Run on the host laptop: announce this cluster so other laptops can find it."""

from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

from lapclusters import config, discovery


def main() -> int:
    redis_port = urlsplit(config.REDIS_URL).port or 6379
    try:
        discovery.start_beacon(config.WORKER_NAME, redis_port)
    except OSError as exc:
        print(f"Could not start announcing on UDP port {discovery.PORT}: {exc}", file=sys.stderr)
        print("Is 'python -m lapclusters.host' already running?", file=sys.stderr)
        return 1
    print(f"Announcing cluster '{config.WORKER_NAME}' on this network (Redis port {redis_port}).")
    print("Other laptops join by setting, in their .env:")
    print(f"  REDIS_URL=redis://:YOUR_REDIS_PASSWORD@{discovery.AUTO}:{redis_port}/0")
    print("and running: python -m lapclusters.worker")
    print("Leave this window open. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Stopped announcing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
