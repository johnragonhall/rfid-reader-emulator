from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from defusedxml import ElementTree

from rfid_reader_emulator.server import (
    REBOOT_PATH,
    EmulatorServer,
    build_parser,
    state_from_args,
)
from rfid_reader_emulator.tags import TagConfig, TagPopulation

NET_PATH = "/devices/EMU0001/networkDevice/network"


@contextmanager
def running(*argv: str) -> Iterator[int]:
    args = build_parser().parse_args(["--port", "0", *argv])
    srv = EmulatorServer((args.host, args.port), state_from_args(args))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)


def call(port: int, method: str, path: str, body: str | None = None) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def inventory(port: int) -> list[dict]:
    _, text = call(port, "GET", "/devices/EMU0001/jsonMinLocation")
    return json.loads(ElementTree.fromstring(text).find("result").text)


def test_devices_lists_the_id():
    with running() as port:
        _, text = call(port, "GET", "/devices")
        assert ElementTree.fromstring(text).find(".//id").text == "EMU0001"


def test_inventory_defaults():
    with running("--tags", "10") as port:
        tags = inventory(port)
    assert len(tags) == 10
    assert all(len(t["epc"]) == 24 for t in tags)
    assert {t["port"] for t in tags} <= {1, 2}
    assert all(-70 <= t["rssi"] <= -40 for t in tags)


def test_same_seed_gives_same_epcs():
    a = TagPopulation(TagConfig(count=5, seed=3)).epcs()
    b = TagPopulation(TagConfig(count=5, seed=3)).epcs()
    c = TagPopulation(TagConfig(count=5, seed=4)).epcs()
    assert a == b != c


def test_prefix_and_antennas():
    pop = TagPopulation(TagConfig(count=8, prefix="ABCD", antennas=4))
    reads = pop.read(0)
    assert all(t["epc"].startswith("abcd") for t in reads)
    assert {t["port"] for t in reads} == {1, 2, 3, 4}


def test_read_rate_zero_reads_nothing():
    assert TagPopulation(TagConfig(count=20, read_rate=0.0)).read(0) == []


def test_dropout_silences_tags_for_the_configured_reads():
    pop = TagPopulation(TagConfig(count=5, dropout_prob=1.0, dropout_reads=2))
    assert pop.read(0) == []
    assert pop.read(0) == []


def test_rssi_distributions_stay_in_range():
    for dist in ("uniform", "normal", "fixed"):
        pop = TagPopulation(TagConfig(count=100, rssi_min=-80, rssi_max=-50, rssi_dist=dist))
        assert all(-80 <= t["rssi"] <= -50 for t in pop.read(0))


def test_churn_changes_epcs():
    pop = TagPopulation(TagConfig(count=40, churn=True))
    before = pop.epcs()
    pop.read(0)
    assert pop.epcs() != before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"count": -1},
        {"prefix": "zz"},
        {"antennas": 0},
        {"read_rate": 2.0},
        {"rssi_min": 0, "rssi_max": -5},
        {"rssi_dist": "x"},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        TagPopulation(TagConfig(**kwargs))


def test_network_get_and_put():
    with running("--net-ip", "192.0.2.10") as port:
        _, text = call(port, "GET", NET_PATH)
        assert ElementTree.fromstring(text).find(".//ip").text == "192.0.2.10"
        status, text = call(
            port, "PUT", NET_PATH, "<network><ip>192.0.2.20</ip><mode>STATIC</mode></network>"
        )
        assert status == 200
        assert "<status>OK</status>" in text
        _, text = call(port, "GET", NET_PATH)
        assert ElementTree.fromstring(text).find(".//ip").text == "192.0.2.20"


def test_network_put_rejects_wrong_spelling_and_bad_body():
    with running("--net-mode", "dhcp", "--net-ip", "", "--accept-spelling", "2") as port:
        _, text = call(port, "PUT", NET_PATH, "<n><ip>192.0.2.9</ip><mode>STATIC</mode></n>")
        assert "<status>ERROR</status>" in text
        _, text = call(port, "PUT", NET_PATH, "<n><ip>192.0.2.9</ip><mode>static</mode></n>")
        assert "<status>OK</status>" in text
        status, _ = call(port, "PUT", NET_PATH, "not xml")
        assert status == 400


def test_reject_network_put_flag():
    with running("--reject-network-put") as port:
        _, text = call(port, "PUT", NET_PATH, "<n><ip>192.0.2.9</ip><mode>STATIC</mode></n>")
        assert "<status>ERROR</status>" in text


def test_error_rate_one_returns_500():
    with running("--error-rate", "1") as port:
        status, _ = call(port, "GET", "/devices")
        assert status == 500


def test_drop_rate_one_closes_without_reply():
    with running("--drop-rate", "1") as port, pytest.raises((http.client.HTTPException, OSError)):
        call(port, "GET", "/devices")


def test_delay_is_applied():
    with running("--delay-ms", "200") as port:
        start = time.monotonic()
        call(port, "GET", "/devices")
        assert time.monotonic() - start >= 0.18


def test_reboot_goes_silent_then_returns():
    with running("--reboot-seconds", "1.0") as port:
        assert "<status>OK</status>" in call(port, "GET", REBOOT_PATH)[1]
        with pytest.raises((http.client.HTTPException, OSError)):
            call(port, "GET", "/devices")
        time.sleep(1.2)
        assert "<id>EMU0001</id>" in call(port, "GET", "/devices")[1]


def test_unknown_route_still_answers_ok():
    with running() as port:
        assert "<status>OK</status>" in call(port, "GET", "/anything")[1]
