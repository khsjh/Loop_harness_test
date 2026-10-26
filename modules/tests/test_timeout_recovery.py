"""Budget ceilings and bounded recovery decisions after test timeouts."""
import copy
from pathlib import Path
import unittest

from modules.harness.contracts import ContractError, loads, request_scope, validate_authorization
from modules.harness.process_runner import ProcessResult
from modules.harness.run_contracts import call_request, validate_run
from modules.harness.run_policy import DEFAULT_LIMITS, DEFAULT_TIMEOUT_POLICY
from modules.harness.timeout_recovery import TimeoutRecovery
from modules.tests.test_m5 import M5Case, OfflineVerifier


def timeout(**changes):
    return ProcessResult(**{"returncode": -15, "timed_out": True, "cleanup_completed": True,
                            "stdout": b"test stalled at calculation", "elapsed_seconds": 5, **changes})


class LimitsTests(M5Case):
    def test_new_defaults_and_shorter_explicit_limits(self):
        self.assertEqual(DEFAULT_LIMITS, {"max_rounds": 6, "run_timeout_seconds": 86400, "call_timeout_seconds": 21600})
        self.request["limits"] = dict(DEFAULT_LIMITS)
        validate_run(self.request, self.root)
        for field, value in (("max_rounds", 7), ("run_timeout_seconds", 86401), ("call_timeout_seconds", 21601)):
            bad = copy.deepcopy(self.request)
            bad["limits"][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_run(bad, self.root)
        self.request["limits"]["call_timeout_seconds"] = 3600
        validate_run(self.request, self.root)

    def test_version_two_approval_cannot_be_downgraded(self):
        call = call_request(self.request, "implementer", 1, self.request["objective"])
        approval = loads(self.approval.read_bytes())
        registry = {call["authorization_ref"]: {"source": approval["source"], "user_instruction": approval["user_instruction"],
                                                "scope": request_scope(call)}}
        validate_authorization(call, registry)
        with self.assertRaises(ContractError):
            validate_authorization({**call, "schema_version": 1}, registry)

    def test_timeout_policy_change_requires_new_approval(self):
        self.request["timeout_policy"]["max_test_timeout_seconds"] = 1800
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)


class RecoveryTests(M5Case):
    def verifier(self, results):
        seen = self.seen = []
        class SequenceVerifier(OfflineVerifier):
            def run(inner, command, budget):
                seen.append(copy.deepcopy(command))
                if len(seen) <= len(results):
                    return results[len(seen) - 1]
                return super().run(command, budget)
        return SequenceVerifier

    def execute_sequence(self, results, mode="normal"):
        return self.run_loop(mode, verifier_factory=self.verifier(results))

    def test_timeout_is_feedback_and_retry_uses_selected_budget(self):
        result = self.execute_sequence([timeout()])
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.agent.calls, [("implementer", 1), ("implementer", 2), ("reviewer", 2)])
        self.assertEqual([c["timeout_seconds"] for c in self.seen], [5, 6])
        directory = Path(result["run_path"])
        call = loads((directory / "calls/call-r2-implementer/request.json").read_bytes())
        feedback = loads(call["task"].split("new authorization:\n", 1)[1])
        context = feedback["timeout_context"]
        self.assertTrue(context["timed_out_test"]["timed_out"])
        self.assertTrue(context["timed_out_test"]["cleanup_completed"])
        self.assertEqual(context["available_rounds_including_this"], 1)
        original = loads((directory / "rounds/0002/verification/0001/request.json").read_bytes())
        effective = loads((directory / "rounds/0002/verification/0001/effective-request.json").read_bytes())
        self.assertEqual(original["timeout_seconds"], 5)
        self.assertEqual(effective["timeout_seconds"], 6)
        self.assertEqual(self.request["verification_commands"][0]["timeout_seconds"], 5)
        self.assertEqual(call["timeout_seconds"], 21600)

    def test_implementer_can_stop_without_another_test(self):
        result = self.execute_sequence([timeout()], "timeout_stop")
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "timeout_recovery_declined"))
        self.assertEqual(len(self.seen), 1)
        self.assertTrue((Path(result["run_path"]) / "rounds/0002/timeout-decision.json").is_file())

    def test_identical_retry_without_change_or_more_time_stops(self):
        result = self.execute_sequence([timeout()], "timeout_no_change")
        self.assertEqual(result["reason_code"], "timeout_no_progress")
        self.assertEqual(len(self.seen), 1)

    def test_same_code_times_out_three_times_even_with_more_time_stops(self):
        self.request["limits"]["max_rounds"] = 6
        self.approve()
        result = self.execute_sequence([timeout(), timeout(), timeout()], "timeout_increase")
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "test_timeout_limit"))
        self.assertEqual(len(self.agent.calls), 3)

    def test_code_changes_do_not_reset_three_timeout_limit(self):
        self.request["limits"]["max_rounds"] = 6
        self.approve()
        result = self.execute_sequence([timeout() for _ in range(6)], "timeout_progress")
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "test_timeout_limit"))
        self.assertEqual(self.agent.calls, [("implementer", n) for n in range(1, 4)])
        feedback = loads((Path(result["run_path"]) / "rounds/0003/verification/0001/timeout-feedback.json").read_bytes())
        self.assertEqual(feedback["consecutive_timeouts"], 3)
        self.assertEqual(feedback["unchanged_timeouts"], 1)

    def test_changes_cannot_bypass_six_round_limit(self):
        self.request["limits"]["max_rounds"] = 6
        self.approve()
        result = self.execute_sequence([timeout(), ProcessResult(returncode=1)] * 3, "timeout_progress")
        self.assertEqual((result["status"], result["reason_code"]), ("limit_reached", "round_limit"))
        self.assertEqual(self.agent.calls, [("implementer", n) for n in range(1, 7)])

    def test_non_timeout_completion_resets_only_its_command(self):
        recovery = TimeoutRecovery(self.request)
        for index in (1, 2):
            recovery.record({"command_id": index, "timeout_seconds": 5}, "same-code", 1)
            recovery.record({"command_id": index, "timeout_seconds": 6}, "new-code", 2)
        recovery.completed(1)
        self.assertEqual(recovery.record({"command_id": 1}, "third-code", 3)["consecutive_timeouts"], 1)
        self.assertEqual(recovery.record({"command_id": 2}, "third-code", 3)["consecutive_timeouts"], 3)
        self.assertTrue(recovery.repeated_without_progress())

    def test_consecutive_limit_defaults_to_three_and_cannot_be_expanded(self):
        self.assertEqual(DEFAULT_TIMEOUT_POLICY["max_consecutive_timeouts"], 3)
        self.request["timeout_policy"]["max_consecutive_timeouts"] = 4
        with self.assertRaises(ContractError):
            validate_run(self.request, self.root)

    def test_timeout_in_final_round_does_not_launch_decision_call(self):
        self.request["limits"]["max_rounds"] = 1
        self.approve()
        result = self.execute_sequence([timeout()])
        self.assertEqual(result["reason_code"], "round_limit")
        self.assertEqual(self.agent.calls, [("implementer", 1)])

    def test_whole_deadline_is_not_a_retryable_test_timeout(self):
        result = self.execute_sequence([timeout(run_deadline_expired=True)])
        self.assertEqual((result["status"], result["reason_code"]), ("limit_reached", "run_deadline"))
        self.assertEqual(len(self.agent.calls), 1)

    def test_failed_cleanup_is_not_retried(self):
        result = self.execute_sequence([timeout(cleanup_completed=False)])
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "process_cleanup"))
        self.assertEqual(len(self.agent.calls), 1)

    def test_output_failure_takes_precedence_over_retry(self):
        result = self.execute_sequence([timeout(output_limit_exceeded=True)])
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "verification_process_error"))
        self.assertEqual(len(self.agent.calls), 1)

    def test_missing_decision_fails_closed(self):
        result = self.execute_sequence([timeout()], "timeout_missing_decision")
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "execution_error"))
        self.assertEqual(len(self.seen), 1)

    def test_model_cannot_expand_test_ceiling(self):
        result = self.execute_sequence([timeout()], "timeout_over_cap")
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "execution_error"))
        self.assertEqual(len(self.seen), 1)

    def test_unaffordable_plan_does_not_start_verification(self):
        result = self.execute_sequence([timeout()], "timeout_over_budget")
        self.assertEqual((result["status"], result["reason_code"]), ("limit_reached", "recovery_budget_insufficient"))
        self.assertEqual(len(self.seen), 1)

    def test_new_user_decision_stays_waiting_for_user(self):
        result = self.execute_sequence([timeout()], "timeout_needs_user")
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertEqual(len(self.seen), 1)


if __name__ == "__main__":
    unittest.main()
