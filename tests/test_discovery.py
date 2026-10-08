import pytest

from lapclusters import config, discovery
from lapclusters.discovery import DiscoveryError, Host, find_hosts, resolve, start_beacon


@pytest.fixture(autouse=True)
def no_remembered_host(monkeypatch):
    # resolve() remembers the host it joined; start every test with none.
    monkeypatch.setattr(config, "CLUSTER_HOST", "")


ALPHA = Host(name="Alpha", address="10.0.0.1", redis_port=6379)
STRYKER = Host(name="Stryker", address="10.0.0.2", redis_port=6379)
ZETA = Host(name="Zeta", address="10.0.0.3", redis_port=6379)


def test_resolve_lets_the_user_choose_between_several_hosts(monkeypatch):
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: [ALPHA, STRYKER, ZETA])
    url = resolve("redis://:secret@auto:6379/0", ask=lambda hosts: hosts[2])
    assert url == "redis://:secret@10.0.0.3:6379/0"


def test_resolve_stays_with_the_chosen_host_without_asking_again(monkeypatch):
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: [ALPHA, STRYKER, ZETA])
    resolve("redis://auto:6379/0", ask=lambda hosts: hosts[1])
    again = resolve("redis://auto:6379/0", ask=lambda hosts: pytest.fail("asked twice"))
    assert again == "redis://10.0.0.2:6379/0"


def test_resolve_does_not_drift_to_another_host_when_its_own_disappears(monkeypatch):
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: [STRYKER])
    resolve("redis://auto:6379/0")
    monkeypatch.setattr(discovery, "find_hosts", lambda **kwargs: [ALPHA])
    with pytest.raises(DiscoveryError, match="'Stryker' is not on this network"):
        resolve("redis://auto:6379/0")


def test_terminal_prompt_repeats_until_a_valid_number(monkeypatch, capsys):
    answers = iter(["", "x", "9", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert discovery.ask_in_terminal([ALPHA, STRYKER, ZETA]) == STRYKER
    shown = capsys.readouterr().out
    assert "1. Alpha" in shown and "3. Zeta" in shown


def test_terminal_prompt_gives_up_cleanly_when_input_ends(monkeypatch):
    def no_input(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)
    with pytest.raises(DiscoveryError, match="No host was chosen"):
        discovery.ask_in_terminal([ALPHA, STRYKER])


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
