"""Thread-safe via GUI event queue: progress in *completed operations*.

An operation counter is not a time or byte estimate. Unknown-length CDS/ARCO
tasks show activity instead of a fabricated percentage; NOAA uses bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class TaskProgress:
    total: int
    send: Callable[[dict[str, object]], None]
    done: int = 0

    def __post_init__(self):
        if self.total < 1:
            raise ValueError("Progress needs at least one operation")
        self._emit("処理を開始します", active=False)

    def _emit(self, label: str, *, active: bool,
              part: float | None = None) -> None:
        self.send({
            "done": self.done,
            "total": self.total,
            "label": label,
            "active": active,
            "part": part,
        })

    def start(self, label: str, part: float | None = None) -> None:
        self._emit(label, active=True, part=part)

    def fraction(self, label: str, fraction: float | None) -> None:
        if fraction is not None:
            fraction = max(0.0, min(float(fraction), 1.0))
        self._emit(label, active=True, part=fraction)

    def complete(self, label: str) -> None:
        if self.done >= self.total:
            raise ValueError("Progress completed more operations than planned")
        self.done += 1
        self._emit(label, active=False)

