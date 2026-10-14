"""Configurable tag population: how many tags, how often each is read, signal strength,
antennas, and dropouts."""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class TagConfig:
    count: int = 50
    prefix: str = ""  # hex digits every EPC starts with
    seed: int = 1
    antennas: int = 2
    read_rate: float = 1.0  # chance each tag appears in a given read
    rssi_min: int = -70
    rssi_max: int = -40
    rssi_dist: str = "uniform"  # uniform | normal | fixed
    dropout_prob: float = 0.0  # chance per read that a tag goes silent
    dropout_reads: int = 3  # how many reads a dropout lasts
    churn: bool = False  # replace about 5% of the EPCs on every read

    def validate(self) -> None:
        if self.count < 0:
            raise ValueError("count must be >= 0")
        if len(self.prefix) > 24 or any(c not in "0123456789abcdefABCDEF" for c in self.prefix):
            raise ValueError("prefix must be at most 24 hex digits")
        if self.antennas < 1:
            raise ValueError("antennas must be >= 1")
        for name in ("read_rate", "dropout_prob"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.rssi_min > self.rssi_max:
            raise ValueError("rssi_min must be <= rssi_max")
        if self.rssi_dist not in ("uniform", "normal", "fixed"):
            raise ValueError("rssi_dist must be uniform, normal or fixed")
        if self.dropout_reads < 1:
            raise ValueError("dropout_reads must be >= 1")


class TagPopulation:
    """Generates synthetic EPCs and the per-read inventory. Seeded, so equal input
    gives equal output."""

    def __init__(self, config: TagConfig) -> None:
        config.validate()
        self.config = config
        self._rng = random.Random(config.seed)
        self._lock = threading.Lock()
        prefix = config.prefix.lower()
        self._epcs = [self._new_epc(prefix) for _ in range(config.count)]
        self._silent_until = [0] * config.count
        self._read_no = 0

    def _new_epc(self, prefix: str = "") -> str:
        tail = "".join(self._rng.choice("0123456789abcdef") for _ in range(24 - len(prefix)))
        return prefix + tail

    def _rssi(self) -> int:
        c = self.config
        if c.rssi_dist == "fixed":
            return (c.rssi_min + c.rssi_max) // 2
        if c.rssi_dist == "normal":
            mean = (c.rssi_min + c.rssi_max) / 2
            sigma = max((c.rssi_max - c.rssi_min) / 6, 0.001)
            return round(min(max(self._rng.gauss(mean, sigma), c.rssi_min), c.rssi_max))
        return self._rng.randint(c.rssi_min, c.rssi_max)

    def epcs(self) -> list[str]:
        with self._lock:
            return list(self._epcs)

    def read(self, now_ms: int) -> list[dict]:
        """One inventory pass: the tags seen this time as dicts with epc, ts, port, rssi."""
        c = self.config
        with self._lock:
            self._read_no += 1
            if c.churn and self._epcs:
                for _ in range(max(1, len(self._epcs) // 20)):
                    self._epcs[self._rng.randrange(len(self._epcs))] = self._new_epc()
            out = []
            for i, epc in enumerate(self._epcs):
                if self._read_no <= self._silent_until[i]:
                    continue
                if c.dropout_prob and self._rng.random() < c.dropout_prob:
                    self._silent_until[i] = self._read_no + c.dropout_reads - 1
                    continue
                if c.read_rate < 1.0 and self._rng.random() >= c.read_rate:
                    continue
                out.append(
                    {
                        "epc": epc,
                        "ts": now_ms + i,
                        "port": (i % c.antennas) + 1,
                        "rssi": self._rssi(),
                    }
                )
            return out
