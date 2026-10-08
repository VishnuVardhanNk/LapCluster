import pytest

from lapclusters import config, discovery
from lapclusters.discovery import DiscoveryError, Host, find_hosts, resolve, start_beacon


def test_finder_gets_the_hosts_name_address_and_redis_port():
    stop, port = start_beacon("Stryker", redis_port=6379, port=0)
    try:
        hosts = find_hosts(timeout_s=1.0, port=port, targets=[("127.0.0.1", port)])
    finally:
        stop.set()
    assert hosts == [Host(name="Stryker", address="127.0.0.1", redis_port=6379)]


def test_finder_returns_nothing_when_no_host_answers():
    import time

    stop, port = start_beacon("Stryker", redis_port=6379, port=0)
    stop.set()
    time.sleep(0.5)  # let the beacon thread notice and close its socket
    assert find_hosts(timeout_s=0.3, port=port, targets=[("127.0.0.1", port)]) == []


def test_beacon_ignores_other_traffic_and_keeps_answering():
    import socket

    stop, port = start_beacon("Stryker", redis_port=6379, port=0)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as noise:
            noise.sendto(b"something else entirely", ("127.0.0.1", port))
        hosts = find_hosts(timeout_s=1.0, port=port, targets=[("127.0.0.1", port)])
    finally:
        stop.set()
    assert [h.name for h in hosts] == ["Stryker"]


def test_a_host_answering_on_several_adapters_is_listed_once():
    replies = [
        Host(name="Stryker", address="10.216.124.43", redis_port=6379),
        Host(name="stryker", address="172.18.112.1", redis_port=6379),
        Host(name="Alpha", address="10.216.124.50", redis_port=6379),
    ]
    assert discovery._one_per_host(replies) == [
        Host(name="Alpha", address="10.216.124.50", redis_port=6379),
        Host(name="Stryker", address="10.216.124.43", redis_port=6379),
    ]


def test_resolve_leaves_an_explicit_address_alone(monkeypatch):
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: pytest.fail("should not search"))
    url = "redis://:secret@10.0.0.5:6379/0"
    assert resolve(url) == url


def test_resolve_fills_in_the_discovered_host(monkeypatch):
    found = [Host(name="Stryker", address="10.216.124.43", redis_port=6379)]
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: found)
    assert resolve("redis://:secret@auto:6379/0") == "redis://:secret@10.216.124.43:6379/0"


def test_resolve_uses_the_port_the_host_announces(monkeypatch):
    found = [Host(name="Stryker", address="10.0.0.9", redis_port=6400)]
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: found)
    assert resolve("redis://auto/0") == "redis://10.0.0.9:6400/0"


def test_resolve_fails_clearly_when_no_host_is_found(monkeypatch):
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: [])
    with pytest.raises(DiscoveryError, match="No LapClusters host found"):
        resolve("redis://:secret@auto:6379/0")


def test_resolve_prefers_the_named_host_when_several_answer(monkeypatch):
    found = [
        Host(name="Alpha", address="10.0.0.1", redis_port=6379),
        Host(name="Stryker", address="10.0.0.2", redis_port=6379),
    ]
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: found)
    monkeypatch.setattr(config, "CLUSTER_HOST", "stryker")
    assert resolve("redis://auto:6379/0") == "redis://10.0.0.2:6379/0"


def test_resolve_refuses_to_guess_between_several_hosts(monkeypatch):
    found = [
        Host(name="Alpha", address="10.0.0.1", redis_port=6379),
        Host(name="Stryker", address="10.0.0.2", redis_port=6379),
    ]
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: found)
    monkeypatch.setattr(config, "CLUSTER_HOST", "")
    with pytest.raises(DiscoveryError, match="Alpha, Stryker"):
        resolve("redis://auto:6379/0")
