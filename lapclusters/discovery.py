"""Finding the host laptop on the local network, so nobody types an IP address.

The host runs a beacon that answers a broadcast question. A worker whose
REDIS_URL has the host name `auto` asks that question and connects to whoever
answers. The Redis password is never sent; each laptop keeps it in its own .env.
"""

from __future__ import annotations

import json
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from lapclusters import config

PORT = 47600
QUESTION = b"lapclusters?"
SERVICE = "lapclusters"
AUTO = "auto"


class DiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    redis_port: int


# --- host side -------------------------------------------------------------


def start_beacon(name: str, redis_port: int, port: int = PORT) -> tuple[threading.Event, int]:
    """Answer discovery questions in a background thread.

    Returns an event that stops the beacon when set, and the UDP port in use
    (useful when `port` is 0, which lets the system choose one).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))
    sock.settimeout(0.2)
    answer = json.dumps({"service": SERVICE, "name": name, "redis_port": redis_port}).encode()
    stop = threading.Event()

    def serve() -> None:
        with sock:
            while not stop.is_set():
                try:
                    data, sender = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    # Windows reports an earlier unreachable reply here; keep serving.
                    continue
                if data == QUESTION:
                    try:
                        sock.sendto(answer, sender)
                    except OSError:
                        pass

    threading.Thread(target=serve, daemon=True).start()
    return stop, sock.getsockname()[1]


# --- worker side -----------------------------------------------------------


def _local_addresses() -> list[str]:
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        addresses = []
    # "" lets the system pick an interface, as a fallback.
    return [a for a in addresses if not a.startswith("127.")] + [""]


def _broadcast_targets(port: int) -> list[tuple[str, tuple[str, int]]]:
    """(local address to send from, destination) pairs covering every network.

    Sending from each local address matters on Windows, where a plain broadcast
    only leaves through one adapter and may pick a virtual one.
    """
    targets = []
    for local in _local_addresses():
        targets.append((local, ("255.255.255.255", port)))
        if local:
            # Most hotspots and home networks are /24, so try that broadcast too.
            targets.append((local, (local.rsplit(".", 1)[0] + ".255", port)))
    return targets


def find_hosts(
    timeout_s: float = 2.0, port: int = PORT, targets: list[tuple[str, int]] | None = None
) -> list[Host]:
    """Ask the network who is hosting, and return every host that answers."""
    if targets is None:
        plan = _broadcast_targets(port)
    else:
        plan = [("", target) for target in targets]

    sockets: dict[str, socket.socket] = {}
    try:
        for local, destination in plan:
            if local not in sockets:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                try:
                    sock.bind((local, 0))
                except OSError:
                    sock.close()
                    continue
                sockets[local] = sock
            try:
                sockets[local].sendto(QUESTION, destination)
            except OSError:
                pass

        found: list[Host] = []
        deadline = time.monotonic() + timeout_s
        while sockets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(list(sockets.values()), [], [], remaining)
            for sock in ready:
                try:
                    data, sender = sock.recvfrom(1024)
                    reply = json.loads(data)
                    if reply.get("service") != SERVICE:
                        continue
                    host = Host(str(reply["name"]), sender[0], int(reply["redis_port"]))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
                found.append(host)
        return _one_per_host(found)
    finally:
        for sock in sockets.values():
            sock.close()


def _one_per_host(replies: list[Host]) -> list[Host]:
    # A laptop with several network adapters answers once per adapter. Keep the
    # first answer from each name, which came over the quickest route.
    by_name: dict[str, Host] = {}
    for host in replies:
        by_name.setdefault(host.name.lower(), host)
    return sorted(by_name.values(), key=lambda h: h.name.lower())


def is_auto(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == AUTO


def resolve(url: str) -> str:
    """Replace the host name `auto` in a Redis URL with the discovered host."""
    if not is_auto(url):
        return url
    hosts = find_hosts()
    if not hosts:
        raise DiscoveryError(
            "No LapClusters host found on this network. Check that the host laptop is "
            "running 'python -m lapclusters.host' and that both laptops are on the same Wi-Fi."
        )
    if len(hosts) > 1:
        wanted = config.CLUSTER_HOST.lower()
        matching = [h for h in hosts if h.name.lower() == wanted]
        if not matching:
            names = ", ".join(h.name for h in hosts)
            raise DiscoveryError(
                f"Several LapClusters hosts answered: {names}. "
                "Set CLUSTER_HOST in .env to the one you want."
            )
        hosts = matching
    host = hosts[0]
    parts = urlsplit(url)
    credentials = parts.netloc.rpartition("@")[0]
    netloc = f"{host.address}:{host.redis_port}"
    if credentials:
        netloc = f"{credentials}@{netloc}"
    return parts._replace(netloc=netloc).geturl()


def main() -> int:
    hosts = find_hosts()
    if not hosts:
        print("No LapClusters host found on this network.")
        return 1
    print("LapClusters hosts on this network:")
    for host in hosts:
        print(f"  {host.name}  {host.address}  (Redis port {host.redis_port})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
