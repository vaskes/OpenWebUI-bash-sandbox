"""A minimal Prometheus exposition.

No client library: the proxy image installs nothing from the
distribution, and what has to be exposed is a handful of integers.
The format is the Prometheus text exposition format, which every
Prometheus (and every scraper that speaks it) understands.

The counters live in the process, so they reset when the container
restarts. That is what a counter is for; the rates Prometheus computes
from them are what a dashboard shows.
"""

from __future__ import annotations

from collections.abc import Callable


class Metrics:
    """A few counters and gauges, rendered for Prometheus.

    Counters only go up. Gauges are set to the current value every time
    they are rendered, because a gauge is a reading, not an event.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._help: dict[str, str] = {}
        self._gauge_sources: dict[str, Callable[[], float]] = {}

    def counter(self, name: str, help_text: str) -> None:
        """Declare a counter. Declaring twice is harmless."""
        self._help[name] = help_text
        self._counters.setdefault(name, 0.0)

    def gauge(self, name: str, help_text: str, source: Callable[[], float]) -> None:
        """Declare a gauge and the callable that reads it."""
        self._help[name] = help_text
        self._gauge_sources[name] = source

    def inc(self, name: str, value: float = 1.0) -> None:
        """Add to a counter."""
        self._counters[name] = self._counters.get(name, 0.0) + value

    def render(self) -> str:
        """Render the exposition format."""
        lines: list[str] = []
        for name, value in sorted(self._counters.items()):
            full = f"{self._prefix}_{name}"
            lines.append(f"# HELP {full} {self._help.get(name, name)}")
            lines.append(f"# TYPE {full} counter")
            lines.append(f"{full} {value:g}")
        for name, source in sorted(self._gauge_sources.items()):
            full = f"{self._prefix}_{name}"
            try:
                value = source()
            except Exception:  # noqa: BLE001 — a broken gauge must not break the scrape
                value = 0.0
            lines.append(f"# HELP {full} {self._help.get(name, name)}")
            lines.append(f"# TYPE {full} gauge")
            lines.append(f"{full} {value:g}")
        return "\n".join(lines) + "\n"
