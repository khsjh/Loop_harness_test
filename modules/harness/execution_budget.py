"""One monotonic deadline shared by preflight, model calls and verification."""
import time


class ExecutionBudget:
    def __init__(self, seconds, *, cancel_check=None, guard=None):
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.cancel_check = cancel_check or (lambda: False)
        self.guard = guard

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def wrap(self, runner):
        def execute(argv, **kwargs):
            if self.guard:
                self.guard.planned()
            result = runner(argv, deadline_monotonic=self.deadline,
                            cancel_check=self.cancel_check,
                            on_start=self.guard.started if self.guard else None, **kwargs)
            if self.guard and result.cleanup_completed:
                self.guard.cleared()
            return result
        return execute
