import threading
import unittest

from hysplit_tool.work_budget import WorkBudget


class TestWorkBudget(unittest.TestCase):
    def test_converter_waits_for_running_trajectory_and_blocks_queued_one(self):
        budget = WorkBudget(1)
        trajectory_started = threading.Event()
        release_trajectory = threading.Event()
        converter_started = threading.Event()
        release_converter = threading.Event()
        queued_trajectory_started = threading.Event()

        def first_trajectory():
            with budget.trajectory_slot():
                trajectory_started.set()
                release_trajectory.wait(2)

        def conversion():
            with budget.conversion_slot():
                converter_started.set()
                release_converter.wait(2)

        def queued_trajectory():
            with budget.trajectory_slot():
                queued_trajectory_started.set()

        first = threading.Thread(target=first_trajectory)
        first.start()
        self.assertTrue(trajectory_started.wait(1))

        converter = threading.Thread(target=conversion)
        converter.start()
        # Wait until the converter has announced priority while the first run
        # still occupies the only slot.
        for _ in range(100):
            with budget._condition:
                waiting = budget._converter_waiting
            if waiting:
                break
            threading.Event().wait(0.01)
        self.assertTrue(waiting)

        queued = threading.Thread(target=queued_trajectory)
        queued.start()
        release_trajectory.set()
        self.assertTrue(converter_started.wait(1))
        self.assertFalse(queued_trajectory_started.wait(0.05))

        release_converter.set()
        self.assertTrue(queued_trajectory_started.wait(1))
        first.join(1)
        converter.join(1)
        queued.join(1)
        self.assertFalse(first.is_alive() or converter.is_alive() or queued.is_alive())


if __name__ == "__main__":
    unittest.main()
