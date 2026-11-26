"""A shared CPU slot budget for trajectory runs and meteorology conversion."""
from __future__ import annotations

from contextlib import contextmanager
from threading import Condition
from typing import Iterator


class WorkBudget:
    """Limit active HYSPLIT groups plus one ERA5 converter to one CPU budget.

    Downloads do not take a slot, so network waits can overlap trajectory
    runs. A waiting converter gets priority over newly queued HYSPLIT groups;
    it occupies one slot while conversion runs, preventing CPU oversubscription.
    """

    def __init__(self, slots: int):
        if slots < 1:
            raise ValueError("Work budget must have at least one slot")
        self.slots = slots
        self._condition = Condition()
        self._running_trajectories = 0
        self._converter_waiting = False
        self._converter_active = False

    @contextmanager
    def trajectory_slot(self) -> Iterator[None]:
        with self._condition:
            while (self._converter_waiting
                   or self._running_trajectories + int(self._converter_active)
                   >= self.slots):
                self._condition.wait()
            self._running_trajectories += 1
        try:
            yield
        finally:
            with self._condition:
                self._running_trajectories -= 1
                self._condition.notify_all()

    @contextmanager
    def conversion_slot(self) -> Iterator[None]:
        with self._condition:
            self._converter_waiting = True
            self._condition.notify_all()
            try:
                while (self._converter_active
                       or self._running_trajectories + 1 > self.slots):
                    self._condition.wait()
                self._converter_active = True
            finally:
                self._converter_waiting = False
                self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._converter_active = False
                self._condition.notify_all()
