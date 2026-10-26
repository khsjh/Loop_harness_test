"""Bounded, evidence-based choices after a cleaned-up test timeout."""
import copy

from .contracts import ContractError


class RecoveryStop(Exception):
    def __init__(self, status, reason, details):
        self.status, self.reason, self.details = status, reason, details
        super().__init__(details)


class TimeoutRecovery:
    def __init__(self, request):
        self.policy = request["timeout_policy"]
        self.timeouts = {i: command["timeout_seconds"] for i, command in enumerate(request["verification_commands"], 1)}
        self.history = []
        self.pending = None
        self.consecutive = {}

    def context(self, seconds, rounds):
        if self.pending is None:
            return None
        return {"timed_out_test": copy.deepcopy(self.pending), "history": copy.deepcopy(self.history),
                "available_seconds": max(0, int(seconds)), "available_rounds_including_this": rounds,
                "max_test_timeout_seconds": self.policy["max_test_timeout_seconds"],
                "max_consecutive_timeouts": self.policy["max_consecutive_timeouts"]}

    def record(self, check, code_state_id, round_number):
        entry = {**check, "code_state_id": code_state_id, "round": round_number}
        command_id = check["command_id"]
        self.consecutive[command_id] = self.consecutive.get(command_id, 0) + 1
        entry["consecutive_timeouts"] = self.consecutive[command_id]
        previous = next((item for item in reversed(self.history) if item["command_id"] == check["command_id"]), None)
        entry["unchanged_timeouts"] = (previous["unchanged_timeouts"] + 1
                                       if previous and previous["code_state_id"] == code_state_id else 1)
        self.history.append(entry)
        self.pending = entry
        return copy.deepcopy(entry)

    def repeated_without_progress(self):
        return self.pending is not None and self.pending["consecutive_timeouts"] >= self.policy["max_consecutive_timeouts"]

    def completed(self, command_id):
        """Only a completed, non-timeout attempt of this command resets its count."""
        self.consecutive[command_id] = 0

    def decide(self, response, code_state_id, seconds, rounds):
        decision = response.get("timeout_recovery")
        if self.pending is None:
            if decision is not None:
                raise ContractError("unexpected timeout decision without a pending test timeout")
            return None
        if response["status"] == "needs_user":
            return None
        if decision is None:
            raise ContractError("pending test timeout requires an explicit recovery decision")
        if decision["action"] == "stop":
            raise RecoveryStop("failed", "timeout_recovery_declined", decision["reason"] + "\n" + decision["plan"])
        requested = decision["test_timeout_seconds"]
        if requested > self.policy["max_test_timeout_seconds"]:
            raise ContractError("timeout decision exceeds the caller-approved test ceiling")
        if decision["estimated_seconds"] > seconds or decision["required_rounds"] > rounds:
            raise RecoveryStop("limit_reached", "recovery_budget_insufficient", "복구 계획에 필요한 시간·라운드가 남은 한도를 초과합니다.")
        if code_state_id == self.pending["code_state_id"] and requested <= self.pending["timeout_seconds"]:
            raise RecoveryStop("failed", "timeout_no_progress", "코드 변경이나 테스트 시간 증가 없이 같은 실행을 반복할 수 없습니다.")
        applied = {"command_id": self.pending["command_id"], "decision": copy.deepcopy(decision),
                   "code_state_id": code_state_id, "previous_timeout": copy.deepcopy(self.pending)}
        self.timeouts[self.pending["command_id"]] = requested
        self.pending = None
        return applied
