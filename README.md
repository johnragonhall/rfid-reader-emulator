# rfid-reader-emulator

A small HTTP/XML server that behaves like a networked RFID reader. Use it to exercise client
code or device firmware on a bench without a physical reader: it serves a repeatable
population of fake tags and can inject network and protocol faults.

This project is independent. It is not affiliated with or endorsed by Keonn or any other
reader vendor. Endpoint names follow the style of a Keonn AdvanNet reader because that is the
device being imitated, and the XML shapes come from observed device behavior, not from vendor
documentation. Tags and EPCs are randomly generated.

## Quick start

```
uv sync
uv run rfid-reader-emulator --tags 100 --port 3161
curl http://127.0.0.1:3161/devices/EMU0001/jsonMinLocation
```

It binds to 127.0.0.1 by default. Use `--host` to listen on another address.

## Endpoints

Every response is XML. Paths are matched by suffix, so the device id segment is not checked.

| Method | Path | Response |
|--------|------|----------|
| GET | `/devices` | `<devices><device><id>` with the device id |
| GET | `/devices/{id}/reader` | `<reader><status>OK</status></reader>` |
| GET | `/devices/{id}/jsonMinLocation` | `<result>` holding a JSON array of `{epc, ts, port, rssi}` |
| GET | `/devices/{id}/networkDevice/network` | reported mode, IP, gateway, DNS, subnet, MAC |
| PUT | `/devices/{id}/networkDevice/network` | accepts or rejects a new address; 400 on a body that is not XML |
| GET | `/devices/{id}/confAllSave`, `/start`, `/stop`, `/connect` | OK |
| PUT | `.../antennas`, `.../reader/parameter/...` | OK |
| GET | `/system/os/Reboot` | OK, then silence for `--reboot-seconds` |
| any | anything else | OK |

## Tag population

| Option | Default | Meaning |
|--------|---------|---------|
| `--tags` | 50 | number of tags |
| `--prefix` | none | hex digits every EPC starts with |
| `--seed` | 1 | same seed, same EPCs and reads |
| `--antennas` | 2 | ports 1..N, assigned round robin |
| `--read-rate` | 1.0 | chance each tag appears in a read |
| `--rssi-min`, `--rssi-max`, `--rssi-dist` | -70, -40, uniform | signal strength range and `uniform`, `normal` or `fixed` |
| `--dropout-prob`, `--dropout-reads` | 0, 3 | chance per read a tag goes silent, and for how many reads |
| `--churn` | off | replace about 5% of EPCs on each read |

## Fault injection

| Option | Effect |
|--------|--------|
| `--error-rate P` | fraction of requests answered with HTTP 500 |
| `--drop-rate P` | fraction of connections closed with no reply |
| `--delay-ms N` | delay added to every response |
| `--reboot-seconds N` | after `GET /system/os/Reboot`, close every connection for N seconds |
| `--reject-network-put` | answer every network PUT with an error status |
| `--accept-spelling N` | for a PUT whose mode is `STATIC`, `static` or `Static`, accept only the Nth spelling |
| `--refuse-local IP` | close connections dialed at that local address |
| `--net-mode`, `--net-ip`, `--mac` | network state the GET reports; an empty `--net-ip` models a reader with no address |

## Limitations

- Behavior is inferred from observation and covers only the endpoints above.
- No authentication, TLS, or real RFID protocol. Do not expose it beyond a test network.
- Inventory is generated per request; there is no tag memory, writing, or locking.
- Fault injection uses a fixed random seed, so a run is repeatable but not tuned to match any
  real device.

## Development

```
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

MIT licensed. See `LICENSE`.
