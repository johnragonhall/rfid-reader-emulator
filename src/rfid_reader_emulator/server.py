"""HTTP/XML server that behaves like an RFID reader with a REST-style API."""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree.ElementTree import ParseError  # noqa: S405  (exception type only)

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from rfid_reader_emulator.tags import TagConfig, TagPopulation

DEFAULT_PORT = 3161
REBOOT_PATH = "/system/os/Reboot"
STATIC_MODE_CANDIDATES = ["STATIC", "static", "Static"]
OK = "<response><status>OK</status></response>"
REJECTED = "<response><status>ERROR</status><msg>rejected by emulator</msg></response>"
BAD_BODY = "<response><status>ERROR</status><msg>bad body</msg></response>"
ERROR_BODY = "<response><status>ERROR</status><error>injected fault</error></response>"


_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
_IP_RE = re.compile(r"([0-9]{1,3}\.){3}[0-9]{1,3}")


def _checked(pattern: re.Pattern[str], what: str, allow_empty: bool = False):
    def parse(value: str) -> str:
        if (allow_empty and value == "") or pattern.fullmatch(value):
            return value
        raise argparse.ArgumentTypeError(f"invalid {what}: {value!r}")

    return parse


def log_line(msg: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"{stamp} {msg}", flush=True)


def xml_text(xml: str, tag: str) -> str | None:
    """Text of the first element named `tag`, or None when the body is not XML or lacks it."""
    try:
        root = ElementTree.fromstring(xml)
    except (ParseError, DefusedXmlException, ValueError):
        return None
    node = root if root.tag == tag else root.find(f".//{tag}")
    if node is None:
        return None
    return node.text or ""


def escape(value: str) -> str:
    return html.escape(value, quote=True)


@dataclass
class Faults:
    error_rate: float = 0.0  # fraction of requests answered with HTTP 500
    delay_ms: int = 0  # added to every response
    drop_rate: float = 0.0  # fraction of connections closed with no reply
    reboot_seconds: float = 30.0  # silence after GET REBOOT_PATH
    reject_network_put: bool = False
    accept_spelling: int = 1  # 1-based index into STATIC_MODE_CANDIDATES


@dataclass
class EmulatorState:
    tags: TagPopulation
    device_id: str = "EMU0001"
    faults: Faults = field(default_factory=Faults)
    net_mode: str = "STATIC"
    net_ip: str = "127.0.0.1"
    net_mac: str = "02:00:00:00:00:01"
    refused_local_ips: set[str] = field(default_factory=set)
    reboot_count: int = 0
    down_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _rng: random.Random = field(default_factory=lambda: random.Random(7))

    def begin_reboot(self) -> tuple[int, float]:
        with self._lock:
            self.reboot_count += 1
            self.down_until = time.monotonic() + self.faults.reboot_seconds
            return self.reboot_count, self.faults.reboot_seconds

    def rebooting(self) -> bool:
        with self._lock:
            return time.monotonic() < self.down_until

    def chance(self, p: float) -> bool:
        if p <= 0:
            return False
        with self._lock:
            return self._rng.random() < p

    def network_snapshot(self) -> tuple[str, str, str]:
        with self._lock:
            return self.net_mode, self.net_ip, self.net_mac

    def set_network(self, mode: str, ip: str) -> None:
        with self._lock:
            self.net_mode = mode
            self.net_ip = ip


def network_body(state: EmulatorState) -> str:
    mode, ip, mac = state.network_snapshot()
    if ip:
        gateway = ".".join([*ip.split(".")[:3], "1"])
        dns, subnet, prefix = "192.0.2.53", "255.255.255.0", "24"
    else:
        gateway = dns = subnet = prefix = ""
    return (
        "<response><type>response</type><status>OK</status><op>networkDevice.network</op><data>"
        f"<ip>{escape(ip)}</ip><dns>{dns}</dns><mode>{mode}</mode><subnet>{subnet}</subnet>"
        f"<subnet-prefix>{prefix}</subnet-prefix><gateway>{escape(gateway)}</gateway>"
        f"<mac>{escape(mac)}</mac></data></response>"
    )


def make_handler(state: EmulatorState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            print(f"{self.address_string()} - {format % args}", flush=True)

        def _send(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _network_put(self, request_body: str) -> None:
            ip = xml_text(request_body, "ip")
            mode = xml_text(request_body, "mode")
            if ip is None or mode is None:
                self._send(BAD_BODY, 400)
                return
            faults = state.faults
            accept = True
            if faults.reject_network_put:
                accept = False
            elif mode in STATIC_MODE_CANDIDATES:
                accept = mode == STATIC_MODE_CANDIDATES[faults.accept_spelling - 1]
            if not accept:
                log_line(f"PUT {self.path} mode={mode!r} -> rejected")
                self._send(REJECTED)
                return
            state.set_network("STATIC", ip)
            log_line(f"PUT {self.path} ip={ip!r} mode={mode!r} -> accepted")
            mac = escape(state.network_snapshot()[2])
            self._send(f"<response><status>OK</status><mac>{mac}</mac></response>")

        def _inventory(self) -> None:
            tags = state.tags.read(int(time.time() * 1000))
            self._send(
                "<response><status>OK</status><result>"
                + escape(json.dumps(tags))
                + "</result></response>"
            )

        def _route(self, request_body: str = "") -> None:
            p, cmd = self.path, self.command
            if p == REBOOT_PATH and cmd == "GET":
                count, seconds = state.begin_reboot()
                log_line(f"GET {p} -> OK (restart #{count}: silent for {seconds:g} s)")
                self._send(OK)
            elif state.chance(state.faults.error_rate):
                self._send(ERROR_BODY, 500)
            elif p == "/devices":
                self._send(
                    f"<devices><device><id>{escape(state.device_id)}</id></device></devices>"
                )
            elif p.endswith("/reader") and cmd == "GET":
                self._send("<reader><status>OK</status></reader>")
            elif p.endswith("/jsonMinLocation"):
                self._inventory()
            elif p.endswith("/networkDevice/network") and cmd == "GET":
                self._send(network_body(state))
            elif p.endswith("/networkDevice/network") and cmd == "PUT":
                self._network_put(request_body)
            else:
                # confAllSave, start, stop, connect, antennas, reader/parameter/* and any
                # unknown route all answer OK so a client never wedges on them.
                self._send(OK)

        def _delay(self) -> None:
            if state.faults.delay_ms > 0:
                time.sleep(state.faults.delay_ms / 1000)

        def do_GET(self) -> None:
            self._delay()
            self._route()

        def do_PUT(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(min(length, 65536)).decode("utf-8", "replace") if length else ""
            self._delay()
            self._route(body)

    return Handler


class EmulatorServer(ThreadingHTTPServer):
    """Closes a connection without replying while the emulator is restarting, when a fault
    drops it, or when the client dialed an address in `refused_local_ips`."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: EmulatorState) -> None:
        super().__init__(address, make_handler(state))
        self.state = state

    def verify_request(self, request, client_address) -> bool:
        if self.state.rebooting():
            log_line(f"dropping connection from {client_address[0]}: restarting")
            return False
        if self.state.chance(self.state.faults.drop_rate):
            log_line(f"dropping connection from {client_address[0]}: injected fault")
            return False
        if self.state.refused_local_ips:
            try:
                local_ip = request.getsockname()[0]
            except OSError:
                return True
            if local_ip in self.state.refused_local_ips:
                log_line(f"refusing connection to {local_ip} from {client_address[0]}")
                return False
        return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rfid-reader-emulator", description="Emulated RFID reader with an HTTP/XML API"
    )
    ap.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    ap.add_argument("--device-id", default="EMU0001", type=_checked(_ID_RE, "device id"))
    t = ap.add_argument_group("tag population")
    t.add_argument("--tags", type=int, default=50, help="number of tags (default 50)")
    t.add_argument("--prefix", default="", help="hex prefix for every EPC")
    t.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    t.add_argument("--antennas", type=int, default=2, help="antenna ports (default 2)")
    t.add_argument("--read-rate", type=float, default=1.0, help="per-tag chance of a read, 0-1")
    t.add_argument("--rssi-min", type=int, default=-70)
    t.add_argument("--rssi-max", type=int, default=-40)
    t.add_argument("--rssi-dist", choices=["uniform", "normal", "fixed"], default="uniform")
    t.add_argument("--dropout-prob", type=float, default=0.0, help="per-read chance of silence")
    t.add_argument("--dropout-reads", type=int, default=3, help="reads a dropout lasts")
    t.add_argument("--churn", action="store_true", help="replace about 5%% of EPCs on each read")
    n = ap.add_argument_group("network state reported by the API")
    n.add_argument("--net-mode", choices=["static", "dhcp"], default="static")
    n.add_argument(
        "--net-ip",
        default="127.0.0.1",
        type=_checked(_IP_RE, "IPv4 address", allow_empty=True),
        help="reported IP; empty for none",
    )
    n.add_argument("--mac", default="02:00:00:00:00:01", type=_checked(_MAC_RE, "MAC address"))
    f = ap.add_argument_group("fault injection")
    f.add_argument("--error-rate", type=float, default=0.0, help="fraction of requests given 500")
    f.add_argument("--drop-rate", type=float, default=0.0, help="fraction closed unanswered")
    f.add_argument("--delay-ms", type=int, default=0, help="delay added to every response")
    f.add_argument(
        "--reboot-seconds", type=float, default=30.0, help=f"silence after GET {REBOOT_PATH}"
    )
    f.add_argument("--reject-network-put", action="store_true")
    f.add_argument(
        "--accept-spelling",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="accept only the Nth of STATIC, static, Static for a DHCP-to-static PUT",
    )
    f.add_argument(
        "--refuse-local",
        action="append",
        default=[],
        metavar="IP",
        help="close connections dialed at this local address; repeatable",
    )
    return ap


def state_from_args(args: argparse.Namespace) -> EmulatorState:
    tags = TagPopulation(
        TagConfig(
            count=args.tags,
            prefix=args.prefix,
            seed=args.seed,
            antennas=args.antennas,
            read_rate=args.read_rate,
            rssi_min=args.rssi_min,
            rssi_max=args.rssi_max,
            rssi_dist=args.rssi_dist,
            dropout_prob=args.dropout_prob,
            dropout_reads=args.dropout_reads,
            churn=args.churn,
        )
    )
    faults = Faults(
        error_rate=args.error_rate,
        delay_ms=args.delay_ms,
        drop_rate=args.drop_rate,
        reboot_seconds=args.reboot_seconds,
        reject_network_put=args.reject_network_put,
        accept_spelling=args.accept_spelling,
    )
    return EmulatorState(
        tags=tags,
        device_id=args.device_id,
        faults=faults,
        net_mode="DHCP" if args.net_mode == "dhcp" else "STATIC",
        net_ip=args.net_ip,
        net_mac=args.mac,
        refused_local_ips=set(args.refuse_local),
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        state = state_from_args(args)
    except ValueError as exc:
        raise SystemExit(f"rfid-reader-emulator: {exc}") from exc
    srv = EmulatorServer((args.host, args.port), state)
    host, port = srv.server_address[:2]
    print(
        f"rfid-reader-emulator: http://{host}:{port}  device={state.device_id}  tags={args.tags}",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
