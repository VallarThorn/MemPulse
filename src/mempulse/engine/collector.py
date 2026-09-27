"""Base collector: reads a data source periodically and survives read errors."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class Collector(ABC, Generic[T]):
    """Wraps a data source so the UI never crashes on a transient /proc error.

    Subclasses implement `read()`. `collect()` stores the latest good sample and
    the last error message, so a widget can show stale data with a warning
    instead of disappearing.
    """

    def __init__(self) -> None:
        self.latest: T | None = None
        self.error: str | None = None
        self.timestamp: float = 0.0

    @abstractmethod
    def read(self) -> T:
        """Read one sample. May raise OSError / ValueError."""

    def collect(self) -> T | None:
        try:
            self.latest = self.read()
            self.error = None
            self.timestamp = time.monotonic()
        except (OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        return self.latest


class RateCollector(Collector[dict[str, float]]):
    """Turns monotonically increasing kernel counters into per-second rates.

    Counters like `pgmajfault` in /proc/vmstat only ever go up; the useful
    number is how fast they grow. Keeps a history per key for sparklines.
    """

    def __init__(self, source: Collector[dict[str, int]], keys: list[str], history: int = 60):
        super().__init__()
        self.source = source
        self.keys = keys
        self.history: dict[str, deque[float]] = {k: deque(maxlen=history) for k in keys}
        self._prev: tuple[float, dict[str, int]] | None = None

    def read(self) -> dict[str, float]:
        counters = self.source.read()
        now = time.monotonic()
        rates: dict[str, float] = {}
        if self._prev is not None:
            prev_t, prev = self._prev
            dt = max(now - prev_t, 1e-6)
            for key in self.keys:
                # A counter can reset (e.g. after hibernation); clamp at zero.
                delta = counters.get(key, 0) - prev.get(key, 0)
                rates[key] = max(delta, 0) / dt
                self.history[key].append(rates[key])
        self._prev = (now, counters)
        return rates


class FuncCollector(Collector[T]):
    """A Collector around a plain reader function, e.g. FuncCollector(read_meminfo)."""

    def __init__(self, fn: Callable[[], T]) -> None:
        super().__init__()
        self._fn = fn

    def read(self) -> T:
        return self._fn()
