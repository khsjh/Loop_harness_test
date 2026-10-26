"""M6 read-only operation, subprocess failure propagation and background cancellation."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from modules.harness.adapters.common import AdapterError
from modules.harness.contracts import ContractError, loads
from modules.harness.inspect_run import attach_caller_dialog, inspect, repair_recording, recover, main as inspect_main
from modules.harness.preflight_run import preflight, main as preflight_main
from modules.harness.finalize_run import prepare_final
from modules.harness.process_runner import ProcessResult, group_alive
from modules.harness.recording import HARNESS_ROOT, write_once
from modules.harness.run_contracts import validate_run
from modules.harness.run_loop import execute
from modules.harness.run_report import report
from modules.harness.run_store import RunStore, digest, commit
from modules.harness.workspace_guard import WorkspaceGuard, identity_alive
from modules.tests import background_m6_check as background
from modules.tests import live_m6_check
from modules.tests import test_dialog_owner as owner_tests
from modules.tests.test_m5 import M5Case, OfflineAgent, OfflineVerifier


def contents(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.readlink().as_posix() if p.is_symlink()
            else digest(p.read_bytes()) if p.is_file() else None) for p in root.rglob("*")}


def statuses(result):
    return {check["name"]: check["status"] for check in result["checks"]}


class PreflightTests(M5Case):
    def checked(self, approval=True, **kwargs):
        before = contents(self.root.parent)
        with patch("subprocess.Popen", side_effect=AssertionError("preflight must not spawn")):
            result = preflight(self.request, self.approval if approval else None, root=self.root,
                which=kwargs.get("which", lambda name, **kw: sys.executable))
        self.assertEqual(before, contents(self.root.parent))
        return result

    def test_ready_and_missing_approval_never_write_or_spawn(self):
        result = self.checked()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(self.checked(False)["status"], "incomplete")
        self.assertEqual(next(c["status"] for c in result["checks"] if c["name"] == "authentication"), "not_checked")

    def test_scope_source_and_rules_tampering_blocked(self):
        self.request["objective"] = "unapproved"
        self.assertEqual(self.checked()["status"], "blocked")
        self.request["objective"] = "Fix modules/calculator.py to return the sum."
        intake = loads(Path(self.input_ref).read_bytes())
        source = Path(intake["source"]["path"])
        original = source.read_bytes()
        source.write_bytes(original + b" ")
        self.assertEqual(self.checked()["status"], "blocked")
        source.write_bytes(original)
        (self.workspace / "AGENTS.md").write_text("changed")
        self.assertEqual(self.checked()["status"], "blocked")

    def test_executable_and_paths_blocked(self):
        self.assertEqual(self.checked(which=lambda *a, **k: None)["status"], "blocked")
        self.request["verification_commands"][0]["runtime_paths"] = ["/"]
        self.assertEqual(self.checked()["status"], "blocked")

    def test_existing_run_and_busy_workspace(self):
        with WorkspaceGuard(self.root, self.workspace, "busy"):
            result = self.checked()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(statuses(result)["workspace_guard"], "blocked")
            self.assertEqual(statuses(result)["run_id"], "passed")
        self.run_loop()
        # The completed run's controller (this process) is alive, but a terminal run frees the workspace.
        result = self.checked()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(statuses(result)["run_id"], "blocked")
        self.assertEqual(statuses(result)["workspace_guard"], "passed")

    def test_closed_caller_question_and_conflicting_binding(self):
        owner_tests.OwnershipTests.caller(self)
        result = self.run_loop()
        self.request["run_id"] = "run-2"
        self.request["caller_dialog"]["call_id"] = "another-answer"
        self.approve()
        self.assertEqual(self.checked()["status"], "blocked")
        self.request["caller_dialog"]["call_id"] = "shared-answer-1"
        self.approve()
        owner_tests.OwnershipTests.save_caller(self, result)
        self.assertEqual(self.checked()["status"], "blocked")

    def test_successful_check_does_not_authorize_later_edit(self):
        self.assertEqual(self.checked()["status"], "ready")
        self.request["limits"]["max_rounds"] = 3
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertEqual(self.agent.calls, [])


class ReportTests(M5Case):
    def checked(self):
        before = contents(self.root.parent)
        with patch("subprocess.Popen", side_effect=AssertionError("report must not spawn")):
            result = report(self.root, self.request["run_id"])
        self.assertEqual(before, contents(self.root.parent))
        return result

    def test_success_has_current_code_review_and_execution_source(self):
        self.run_loop()
        value = self.checked()
        self.assertEqual(value["report_status"], "ok", value)
        self.assertEqual(value["execution"]["status"], "success")
        self.assertTrue(value["source"]["at_execution"])
        self.assertTrue(all(v["matches_final_code"] for v in value["verification"]))
        self.assertTrue(all(v["passed"] for v in value["verification"]))
        self.assertEqual(value["final_code_reviews"][0]["verdict"], "approved")
        self.assertEqual(value["calls"][0]["actual_model"], "unknown")
        self.assertEqual(value["pending_call_recordings"], [])

    def test_failed_verification_is_marked_and_kept_apart_from_final_code(self):
        self.run_loop("fix_second")
        value = self.checked()
        self.assertEqual(value["execution"]["status"], "success")
        first, last = value["verification"][0], value["verification"][-1]
        self.assertNotEqual(first["process"]["returncode"], 0)
        self.assertFalse(first["passed"])
        self.assertFalse(first["matches_final_code"])
        self.assertTrue(last["passed"])
        self.assertTrue(last["matches_final_code"])

    def test_unsaved_call_record_needs_retry_even_when_dialog_is_saved(self):
        agent = OfflineAgent()
        def fail_stdout(path, data):
            if path.name == "stdout.txt":
                raise OSError("simulated disk full")
            write_once(path, data)
        def failing_call(call, registry, **kwargs):
            with patch("modules.harness.recording.write_once", side_effect=fail_stdout):
                return agent(call, registry, **kwargs)
        result = execute(self.request, self.approval, root=self.root, agent_call=failing_call,
                         verifier_factory=OfflineVerifier)
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "recording_failed"))
        value = self.checked()
        self.assertEqual(value["dialog"]["recording_status"], "saved")
        self.assertEqual([p["call_id"] for p in value["pending_call_recordings"]], ["r1-implementer"])
        self.assertEqual([a["argv"][-1] for a in value["next_actions"]], ["--retry-recording"])
        self.assertIn("call records", value["next_actions"][0]["reason"])
        calls = agent.calls[:]
        repair_recording(self.root, self.request["run_id"])
        value = self.checked()
        self.assertEqual(value["pending_call_recordings"], [])
        self.assertEqual(value["next_actions"], [])
        self.assertEqual(value["execution"]["status"], "failed")
        self.assertEqual(agent.calls, calls)

    def test_failure_limit_waiting_are_not_report_errors(self):
        for mode, expected in (("review_reject", "limit_reached"), ("needs_user", "waiting_for_user"),
                                ("test_tamper", "failed")):
            with self.subTest(mode=mode):
                self.request["run_id"] = mode
                self.approve()
                self.run_loop(mode)
                value = self.checked()
                self.assertEqual(value["report_status"], "ok", value)
                self.assertEqual(value["execution"]["status"], expected)

    def test_caller_pending_and_shared_record_attachment(self):
        owner_tests.OwnershipTests.caller(self)
        result = self.run_loop()
        self.assertEqual(self.checked()["dialog"]["recording_status"], "pending_caller")
        saved = owner_tests.OwnershipTests.save_caller(self, result)
        attach_caller_dialog(self.root, self.request["run_id"], saved)
        value = self.checked()
        self.assertEqual(value["dialog"]["recording_status"], "saved")
        self.assertEqual(value["dialog"]["receipt"]["path"], str(saved))
        saved.write_bytes(saved.read_bytes() + b"tamper")
        self.assertEqual(self.checked()["report_status"], "error")

    def test_save_only_repair_keeps_failed_status(self):
        self.run_loop(saver_runner=lambda *a, **k: ProcessResult(returncode=1))
        self.assertEqual(self.checked()["dialog"]["recording_status"], "error")
        calls = self.agent.calls[:]
        repair_recording(self.root, self.request["run_id"])
        value = self.checked()
        self.assertEqual(value["execution"]["status"], "failed")
        self.assertEqual(value["execution"]["task_outcome"], "success")
        self.assertEqual(value["dialog"]["recording_status"], "saved")
        self.assertEqual(self.agent.calls, calls)

    def test_corrupt_artifact_or_missing_request_never_looks_successful(self):
        self.run_loop()
        source = self.root / "runs/run-1/source-manifest.json"
        source.write_text("{}")
        value = self.checked()
        self.assertEqual(value["report_status"], "error")
        self.assertIsNone(value["execution"])
        (self.root / "runs/run-1/run-request.json").unlink()
        self.assertEqual(self.checked()["report_status"], "error")

    def test_partial_and_legacy_records_are_honest(self):
        store = RunStore(self.root, self.request)
        try:
            value = self.checked()
            self.assertEqual(value["report_status"], "incomplete")
            self.assertIsNone(value["execution"]["status"])
        finally:
            store.close()
        self.request["run_id"] = "legacy"
        (self.root / "runs/legacy/calls/call-1").mkdir(parents=True)
        value = self.checked()
        self.assertEqual(value["report_status"], "incomplete")
        self.assertEqual(value["integrity"]["status"], "not_checked")

    def test_evidence_bundle_points_to_controller_without_trusting_summary(self):
        self.request["run_id"] = "example-evidence"
        path = self.root / "runs/example-evidence"
        path.mkdir(parents=True)
        controller = str(self.root.parent / "separate controller")
        (path / "fixture-manifest.json").write_text(json.dumps({
            "run_id": "actual-run", "controller_root": controller}))
        (path / "summary.json").write_text(json.dumps({"execution": {"status": "success"},
            "integrity": {"status": "verified"}, "run_id": "actual-run",
            "run_path": str(Path(controller) / "runs/actual-run")}))
        value = self.checked()
        self.assertEqual((value["kind"], value["report_status"]), ("evidence_bundle", "ok"))
        self.assertIsNone(value["execution"])
        self.assertEqual(value["integrity"]["status"], "not_checked")
        self.assertFalse(value["evidence"]["references_verified"])
        self.assertEqual(value["next_actions"], [])
        refs = value["evidence"]["run_references"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["argv"], ["python3", "-B", "-m", "modules.harness.inspect_run",
                         "actual-run", "--controller", controller, "--report"])
        with patch("subprocess.Popen", side_effect=AssertionError("no followup execution")):
            self.assertEqual(inspect(self.root, "example-evidence"), value)
        self.assertFalse(Path(controller).exists())

    def test_aggregate_bundle_lists_multiple_runs_without_reading_run_contents(self):
        self.request["run_id"] = "aggregate-evidence"
        path = self.root / "runs/aggregate-evidence"
        path.mkdir(parents=True)
        for name in ("one", "two"):
            run = self.root / "runs" / name
            run.mkdir()
            (run / "run-request.json").write_text("corrupt: must not be read here")
        (path / "completion.json").write_text(json.dumps({"runs": [
            {"run_id": "one", "status": "success"}, {"run_id": "two"}, {"run_id": "one"}]}))
        value = self.checked()
        self.assertEqual([r["run_id"] for r in value["evidence"]["run_references"]], ["one", "two"])
        self.assertIsNone(value["execution"])
        self.assertEqual(value["evidence"]["reference_warnings"], [])

    def test_nested_outcome_uses_enclosing_controller_without_following_reference(self):
        self.request["run_id"] = "review-evidence"
        path = self.root / "runs/review-evidence"
        path.mkdir(parents=True)
        enclosing = str(self.root.parent / "review controller")
        override = str(self.root.parent / "other controller")
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                controller = override if explicit else enclosing
                outcome = {"run_id": "review-call", "call_status": "success"}
                if explicit:
                    outcome["controller_root"] = override
                (path / "summary.json").write_text(json.dumps({
                    "controller_root": enclosing, "outcome": outcome}))
                value = self.checked()
                refs = value["evidence"]["run_references"]
                self.assertEqual(len(refs), 1)
                self.assertEqual(refs[0], {"run_id": "review-call", "controller_root": controller,
                    "argv": ["python3", "-B", "-m", "modules.harness.inspect_run", "review-call",
                             "--controller", controller, "--report"]})
                self.assertEqual(value["evidence"]["reference_warnings"], [])
                self.assertFalse(value["evidence"]["references_verified"])
                self.assertIsNone(value["execution"])
                self.assertEqual(value["integrity"]["status"], "not_checked")
                self.assertEqual(value["next_actions"], [])
                self.assertFalse(Path(controller).exists())
                # The same reference in another supported source is still deduplicated.
                (path / "fixture-manifest.json").write_text(json.dumps({
                    "run_id": "review-call", "controller_root": controller}))
                self.assertEqual(self.checked()["evidence"]["run_references"], refs)
                (path / "fixture-manifest.json").unlink()

    def test_unresolved_controller_warns_and_keeps_other_references(self):
        self.request["run_id"] = "unresolved-evidence"
        path = self.root / "runs/unresolved-evidence"
        path.mkdir(parents=True)
        controller = str(self.root.parent / "external controller")
        valid = {"run_id": "valid", "controller_root": controller}
        cases = [
            ({"run_id": "missing", "runs": [valid]}, "$"),
            ({"controller_root": controller,
              "outcome": {"run_id": "missing", "controller_root": None},
              "runs": [valid]}, "$.outcome"),
            ({"controller_root": controller,
              "runs": [{"run_id": "missing"}, valid]}, "$.runs[0]"),
        ]
        for data, location in cases:
            with self.subTest(location=location):
                (path / "summary.json").write_text(json.dumps(data))
                value = self.checked()
                self.assertEqual(value["report_status"], "ok")
                self.assertEqual([r["run_id"] for r in value["evidence"]["run_references"]], ["valid"])
                warnings = value["evidence"]["reference_warnings"]
                self.assertEqual(len(warnings), 1)
                self.assertEqual((warnings[0]["file"], warnings[0]["record"]), ("summary.json", location))
                self.assertIn("controller", warnings[0]["error"])
                self.assertIn("missing", warnings[0]["error"])
                self.assertIsNone(value["execution"])
                self.assertEqual(value["integrity"]["status"], "not_checked")
                self.assertFalse(value["evidence"]["references_verified"])
                self.assertEqual(value["next_actions"], [])
                self.assertFalse(Path(controller).exists())

    def test_controller_fallbacks_do_not_emit_unresolved_warnings(self):
        self.request["run_id"] = "fallback-evidence"
        path = self.root / "runs/fallback-evidence"
        path.mkdir(parents=True)
        local = self.root / "runs/local"
        local.mkdir()
        (local / "run-request.json").write_text("must not be read")
        controller = str(self.root.parent / "external controller")
        (path / "summary.json").write_text(json.dumps({
            "controller_root": controller,
            "outcome": {"run_id": "local", "controller_root": None},
            "runs": [{"run_id": "remote", "run_path": str(Path(controller) / "runs/remote")},
                     {"status": "success"}],
        }))
        value = self.checked()
        self.assertEqual([(r["run_id"], r["controller_root"]) for r in value["evidence"]["run_references"]],
                         [("local", str(self.root)), ("remote", controller)])
        self.assertEqual(value["evidence"]["reference_warnings"], [])
        self.assertIsNone(value["execution"])
        self.assertEqual(value["integrity"]["status"], "not_checked")
        self.assertFalse(Path(controller).exists())

    def test_outcome_objects_and_errors_in_all_supported_bundle_sources(self):
        self.request["run_id"] = "outcome-sources-evidence"
        path = self.root / "runs/outcome-sources-evidence"
        path.mkdir(parents=True)
        controller = str(self.root.parent / "external controller")
        for name in ("fixture-manifest.json", "summary.json", "completion.json", "sample-report-final.json"):
            for outcome in ({"run_id": "nested"}, None, "success", []):
                with self.subTest(name=name, outcome=outcome):
                    file = path / name
                    file.write_text(json.dumps({"controller_root": controller, "outcome": outcome,
                        "runs": [{"run_id": "valid", "controller_root": controller}]}))
                    value = self.checked()
                    expected = ["nested", "valid"] if isinstance(outcome, dict) else ["valid"]
                    self.assertEqual([r["run_id"] for r in value["evidence"]["run_references"]], expected)
                    warnings = value["evidence"]["reference_warnings"]
                    if isinstance(outcome, dict):
                        self.assertEqual(warnings, [])
                    else:
                        self.assertEqual(len(warnings), 1)
                        self.assertEqual((warnings[0]["file"], warnings[0]["record"]), (name, "$.outcome"))
                    self.assertIsNone(value["execution"])
                    self.assertEqual(value["integrity"]["status"], "not_checked")
                    self.assertFalse(value["evidence"]["references_verified"])
                    self.assertEqual(value["next_actions"], [])
                    file.unlink()
        self.assertFalse(Path(controller).exists())

    def test_invalid_middle_reference_keeps_later_valid_references(self):
        self.request["run_id"] = "mixed-evidence"
        path = self.root / "runs/mixed-evidence"
        path.mkdir(parents=True)
        controller = str(self.root.parent / "separate controller")
        first = {"run_id": "first", "controller_root": controller}
        last = {"run_id": "last", "controller_root": controller}
        for invalid in ({"run_id": "../escape", "controller_root": controller},
                        {"run_id": "bad-controller", "controller_root": "../outside"},
                        {"run_id": 123}, "not an object"):
            with self.subTest(invalid=invalid):
                (path / "completion.json").write_text(json.dumps({"runs": [first, invalid, last, first]}))
                value = self.checked()
                self.assertEqual([r["run_id"] for r in value["evidence"]["run_references"]],
                                 ["first", "last"])
                warnings = value["evidence"]["reference_warnings"]
                self.assertEqual(len(warnings), 1)
                self.assertEqual((warnings[0]["file"], warnings[0]["record"]),
                                 ("completion.json", "$.runs[1]"))
                self.assertTrue(warnings[0]["error"])
                self.assertIsNone(value["execution"])
                self.assertEqual(value["next_actions"], [])

    def test_invalid_outcome_does_not_hide_other_records(self):
        self.request["run_id"] = "mixed-review-evidence"
        path = self.root / "runs/mixed-review-evidence"
        path.mkdir(parents=True)
        controller = str(self.root.parent / "separate controller")
        for outcome, enclosing in (([], controller),
                                   ({"run_id": "../escape"}, controller),
                                   ({"run_id": "review-call", "controller_root": "../outside"}, controller),
                                   ({"run_id": "review-call"}, "../outside")):
            with self.subTest(outcome=outcome, enclosing=enclosing):
                (path / "summary.json").write_text(json.dumps({"controller_root": enclosing,
                    "outcome": outcome, "runs": [{"run_id": "valid", "controller_root": controller}]}))
                value = self.checked()
                self.assertEqual([r["run_id"] for r in value["evidence"]["run_references"]], ["valid"])
                warnings = value["evidence"]["reference_warnings"]
                self.assertEqual(len(warnings), 1)
                self.assertEqual((warnings[0]["file"], warnings[0]["record"]),
                                 ("summary.json", "$.outcome"))
                self.assertIsNone(value["execution"])
                self.assertFalse(value["evidence"]["references_verified"])
                self.assertEqual(value["next_actions"], [])

    def test_invalid_bundle_hints_never_become_actions_or_verified_results(self):
        self.request["run_id"] = "broken-evidence"
        path = self.root / "runs/broken-evidence"
        path.mkdir(parents=True)
        for data in ("{", "[]", json.dumps({"run_id": "../escape", "controller_root": "/tmp"}),
                     json.dumps({"run_id": "safe", "controller_root": "../outside"})):
            with self.subTest(data=data):
                (path / "summary.json").write_text(data)
                value = self.checked()
                self.assertEqual(value["kind"], "evidence_bundle")
                self.assertEqual(value["evidence"]["run_references"], [])
                self.assertTrue(value["evidence"]["reference_warnings"])
                self.assertIsNone(value["execution"])
                self.assertEqual(value["next_actions"], [])
        (path / "summary.json").unlink()
        (path / "summary.json").symlink_to(self.root / "private.json")
        value = self.checked()
        self.assertIn("symlink", value["evidence"]["reference_warnings"][0]["error"])

    def test_evidence_suffix_never_hides_real_or_damaged_run(self):
        self.request["run_id"] = "real-evidence"
        self.approve()
        self.run_loop()
        self.assertEqual(self.checked()["kind"], "loop")
        (self.root / "runs/real-evidence/run-request.json").unlink()
        value = self.checked()
        self.assertEqual(value["report_status"], "error")
        self.assertIsNone(value["execution"])
        self.request["run_id"] = "legacy-evidence"
        (self.root / "runs/legacy-evidence/calls/call-1").mkdir(parents=True)
        self.assertEqual(self.checked()["kind"], "legacy_single_call")

    def test_unrecognized_directory_is_not_a_legacy_call(self):
        self.request["run_id"] = "unrecognized"
        (self.root / "runs/unrecognized").mkdir(parents=True)
        value = self.checked()
        self.assertEqual((value["kind"], value["report_status"]), ("unknown_record", "incomplete"))
        self.assertIsNone(value["execution"])

    def test_source_at_inspection_is_not_historical_source(self):
        self.run_loop()
        with patch("modules.harness.run_report.source_manifest", return_value={"new-source": "hash"}):
            value = self.checked()
        self.assertNotEqual(value["source"]["at_execution"], value["source"]["at_inspection"])

    def interrupted_store(self, request=None):
        request = request or self.request
        store = RunStore(self.root, request)
        intake = loads(Path(self.input_ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        commit(store.path / "input.json", message)
        store.event("approval_recorded", artifacts=[store.path / "run-request.json", store.path / "input.json"])
        return store, message

    def test_interrupted_report_recovery_and_save_only_action(self):
        store, _ = self.interrupted_store()
        store.close()
        value = self.checked()
        self.assertIsNone(value["execution"]["status"])
        self.assertTrue(any("not terminal" in a["reason"] for a in value["next_actions"]))
        with patch("modules.harness.inspect_run.recover_guard", return_value=False):
            recover(self.root, "run-1")
        value = self.checked()
        self.assertTrue(value["execution"]["recovery_required"])
        self.assertEqual(value["dialog"]["recording_status"], "pending")
        actions = [a["argv"][-1] for a in value["next_actions"]]
        self.assertIn("--recover-state", actions)
        self.assertIn("--retry-recording", actions)
        with patch("modules.harness.inspect_run.recover_guard", return_value=True):
            repair_recording(self.root, "run-1")
        value = self.checked()
        self.assertEqual(value["execution"]["status"], "failed")
        self.assertEqual(value["dialog"]["recording_status"], "saved")
        self.assertEqual(value["next_actions"], [])

    def test_preownership_loop_report_preserves_identity(self):
        legacy = copy.deepcopy(self.request)
        legacy.pop("dialog_owner")
        legacy.pop("caller_dialog")
        legacy["report_identity"] = {"ai": "codex", "model": "unknown"}
        store, message = self.interrupted_store(legacy)
        prepare_final(store, legacy, message, "failed", "interrupted_unconfirmed", "legacy test")
        store.finish("failed", "interrupted_unconfirmed")
        store.close()
        self.assertEqual(self.checked()["report_status"], "ok")
        repair_recording(self.root, "run-1")
        value = self.checked()
        self.assertEqual(value["report_status"], "ok", value)
        self.assertEqual(value["settings"]["report_identity"], legacy["report_identity"])
        self.assertEqual(value["dialog"]["recording_status"], "saved")
        self.assertIsNone(value["source"]["at_execution"])


class CliTests(M5Case):
    def test_preflight_exit_codes_and_no_side_effects(self):
        request = self.root / "request.json"
        request.write_text(json.dumps(self.request))
        argv = ["--request", str(request), "--controller", str(self.root)]
        before = contents(self.root.parent)
        def local_preflight(request, approval=None, **kwargs):
            return preflight(request, approval, which=lambda *a, **k: sys.executable, **kwargs)
        with patch("builtins.print"), patch("subprocess.Popen", side_effect=AssertionError("no spawn")), \
                patch("modules.harness.preflight_run.preflight", side_effect=local_preflight):
            self.assertEqual(preflight_main(argv + ["--approval", str(self.approval)]), 0)
            self.assertEqual(preflight_main(argv), 2)
            self.assertEqual(preflight_main(argv + ["--approval", str(self.root / "missing.json")]), 1)
        self.assertEqual(before, contents(self.root.parent))

    def test_report_exit_codes_and_conflicting_modes(self):
        self.run_loop("needs_user")
        (self.root / "runs/legacy/calls/call-1").mkdir(parents=True)
        (self.root / "runs/summary-evidence").mkdir()
        before = contents(self.root.parent)
        with patch("builtins.print"), patch("subprocess.Popen", side_effect=AssertionError("no spawn")):
            for run_id, code in (("run-1", 0), ("legacy", 2), ("summary-evidence", 0), ("unknown", 1)):
                self.assertEqual(inspect_main([run_id, "--controller", str(self.root), "--report"]), code)
            with patch("sys.stderr"), self.assertRaises(SystemExit) as exc:
                inspect_main(["run-1", "--report", "--recover-state"])
            self.assertEqual(exc.exception.code, 2)
        self.assertEqual(before, contents(self.root.parent))


class FailureIntegrationTests(M5Case):
    def test_actual_subprocess_failures_reach_terminal_record_without_reviewer(self):
        for agent in ("codex", "claude"):
            for mode in ("nonzero", "malformed", "invalid_response"):
                with self.subTest(agent=agent, mode=mode):
                    self.request["run_id"] = agent + "-" + mode
                    self.request["assignments"]["implementer"]["agent"] = agent
                    self.request["assignments"]["reviewer"]["agent"] = "claude" if agent == "codex" else "codex"
                    self.approve()
                    result = self.run_loop(mode)
                    self.assertEqual(result["status"], "failed", result)
                    expected = "nonzero_exit" if mode == "nonzero" else "response_validation"
                    self.assertEqual(result["reason_code"], expected)
                    self.assertEqual(self.agent.calls, [("implementer", 1)])
                    self.assertEqual(result["recording_status"], "saved")
                    value = report(self.root, self.request["run_id"])
                    self.assertEqual(value["report_status"], "ok", value)
                    self.assertEqual(value["final_code_reviews"], [])
                    call = loads(Path(value["calls"][0]["path"]).read_bytes())
                    self.assertEqual(call["error"]["code"], expected)


class BackgroundTests(M5Case):
    def test_launch_failure_and_dead_worker_status(self):
        with patch.object(background, "preflight", return_value={"status": "ready"}), \
                patch.object(background.subprocess, "Popen", side_effect=OSError("launch denied")):
            with self.assertRaises(OSError):
                background.start(self.root, self.request, self.approval)
        value = background.status(self.root, "run-1")
        self.assertEqual(value["status"], "launch_failed")
        self.assertEqual(value["launch_error"]["error"], "launch denied")
        self.assertFalse(background.stop(self.root, "run-1")["cancel_requested"])
        directory = background.job_path(self.root, "dead-worker")
        directory.mkdir()
        commit(directory / "job.json", {"created_at": "test"})
        commit(directory / "identity.json", {"pid": 999999999, "start_ticks": "0", "boot_id": "missing"})
        self.assertEqual(background.status(self.root, "dead-worker")["status"], "interrupted")
        self.assertFalse(background.stop(self.root, "dead-worker")["cancel_requested"])

    def test_worker_source_change_blocks_before_execution(self):
        with patch.object(background, "preflight", return_value={"status": "ready"}), \
                patch.object(background.subprocess, "Popen"):
            background.start(self.root, self.request, self.approval)
        job = loads((background.job_path(self.root, "run-1") / "job.json").read_bytes())
        self.assertIn("modules/tests/background_m6_check.py", job["source_manifest"])
        with patch.object(background, "worker_manifest", return_value={"changed": "hash"}), \
                patch.object(background, "execute") as execute, \
                patch.object(background.signal, "signal"), patch("builtins.print"):
            self.assertEqual(background.worker(self.root, "run-1"), 1)
            execute.assert_not_called()
        self.assertFalse((self.root / "runs/run-1").exists())
        self.assertIn("source changed", background.status(self.root, "run-1")["exit"]["error"])

    def test_worker_failure_cause_reaches_exit_record(self):
        for run_id, error, expected in (("adapter", AdapterError("process_error", "adapter_config",
                                                                 "adapter configuration drifted"),
                                         "adapter configuration drifted"),
                                        ("unexpected", RuntimeError("worker bug"), "RuntimeError: worker bug")):
            with self.subTest(run_id=run_id):
                self.request["run_id"] = run_id
                with patch.object(background, "preflight", return_value={"status": "ready"}), \
                        patch.object(background.subprocess, "Popen"):
                    background.start(self.root, self.request, self.approval)
                with patch.object(background, "execute", side_effect=error), \
                        patch.object(background.signal, "signal"), patch("builtins.print"):
                    if isinstance(error, AdapterError):
                        self.assertEqual(background.worker(self.root, run_id), 1)
                    else:
                        # Unexpected errors keep their traceback for controller.log.
                        with self.assertRaises(RuntimeError):
                            background.worker(self.root, run_id)
                value = background.status(self.root, run_id)
                self.assertEqual(value["status"], "finished")
                self.assertEqual((value["exit"]["status"], value["exit"]["reason_code"]), ("failed", "worker_error"))
                self.assertEqual(value["exit"]["error"], expected)

    def test_detached_worker_stop_cleanup_and_duplicate_rejection(self):
        real_popen = subprocess.Popen
        children = []
        def spawn(argv, **kwargs):
            child = real_popen([sys.executable, "-B", "-m", "modules.tests.fixtures.fake_background_worker",
                                *argv[4:]], **kwargs)
            children.append(child)
            return child
        with patch.object(background, "preflight", return_value={"status": "ready"}), patch.object(background.subprocess, "Popen", spawn):
            started = background.start(self.root, self.request, self.approval)
        child = children[0]
        def cleanup():
            if child.poll() is None:
                background.stop(self.root, self.request["run_id"])
                child.wait(timeout=15)
        self.addCleanup(cleanup)
        self.assertTrue(Path(started["log"]).exists())
        with patch.object(background, "preflight", return_value={"status": "ready"}), self.assertRaises(FileExistsError):
            background.start(self.root, self.request, self.approval)
        def model_process():
            # The guard also records the short CLI --version/--help probes; wait for the hanging model call.
            for guard in (self.root / "runs/.guards").glob("*.json"):
                identity = loads(guard.read_bytes())["process"]
                try:
                    argv = Path(f"/proc/{identity['pid']}/cmdline").read_bytes().split(b"\0") if identity else []
                except OSError:
                    continue
                if identity_alive(identity) and not {b"--version", b"--help"} & set(argv):
                    return identity
            return None
        deadline = time.monotonic() + 15
        model = None
        while time.monotonic() < deadline:
            value = background.status(self.root, self.request["run_id"])
            run = value.get("run") or {}
            if ((run.get("execution") or {}).get("active_operation") or {}).get("kind") == "model":
                model = model_process()
                if model is not None:
                    break
            if child.poll() is not None:
                self.fail(Path(started["log"]).read_text())
            time.sleep(.05)
        else:
            self.fail("worker did not start its model process")
        self.assertTrue(identity_alive(model))
        stopped = background.stop(self.root, self.request["run_id"])
        self.assertTrue(stopped["cancel_requested"])
        child.wait(timeout=15)
        # The recorded model process group itself is gone, not only the guard flag.
        self.assertFalse(identity_alive(model))
        self.assertFalse(group_alive(model["pid"]))
        value = background.status(self.root, self.request["run_id"])
        self.assertEqual(value["status"], "finished", value)
        self.assertEqual(value["exit"]["status"], "failed")
        self.assertEqual(value["exit"]["reason_code"], "interrupted")
        self.assertEqual(value["run"]["execution"]["status"], "failed")
        self.assertEqual(value["run"]["execution"]["reason_code"], "interrupted")
        self.assertFalse(value["exit"]["recovery_required"])
        guard = loads(next((self.root / "runs/.guards").glob("*.json")).read_bytes())
        self.assertFalse(guard["active"])
        self.assertFalse(background.stop(self.root, self.request["run_id"])["signal_sent"])
        with self.assertRaises(FileExistsError):
            background.worker(self.root, self.request["run_id"])

    def test_stop_during_startup_cannot_signal_a_reused_pid(self):
        directory = background.job_path(self.root, "run-1")
        directory.mkdir(parents=True)
        current = {"controller_alive": False, "status": "starting_or_unconfirmed", "controller": None}
        with patch.object(background, "status", return_value=current), patch.object(os, "kill") as send:
            self.assertTrue(background.stop(self.root, "run-1")["cancel_requested"])
            self.assertTrue(background.stop(self.root, "run-1")["cancel_requested"])
            send.assert_not_called()
        self.assertTrue((directory / "stop-request.json").exists())


class LiveM6CheckTests(unittest.TestCase):
    """Offline checks of the live entry point: no model, no approval, isolated controllers only."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m6 live ")
        self.addCleanup(temporary.cleanup)
        self.harness = Path(temporary.name)
        (self.harness / "modules/tests/fixtures").mkdir(parents=True)
        for name in ("AGENTS.md", "modules/save_dialog.py", "modules/tests/fixtures/m6_ledger_checks.py"):
            shutil.copyfile(HARNESS_ROOT / name, self.harness / name)
        for patcher in (patch.object(live_m6_check, "HARNESS_ROOT", self.harness),
                        patch.object(live_m6_check, "execute", side_effect=AssertionError("model execution"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.plan = self.harness / "plans/plan.json"

    def main(self, argv):
        with patch("builtins.print"):
            return live_m6_check.main(argv)

    def prepare(self):
        return self.main(["--prepare", str(self.plan), "--implementer", "claude", "--codex-model", "codex-test",
                          "--codex-effort", "xhigh", "--claude-model", "claude-test", "--claude-effort", "medium",
                          "--stress-seconds", "0"])

    def test_prepare_writes_a_valid_request_without_approval_or_run(self):
        self.assertEqual(self.prepare(), 0)
        plan = loads(self.plan.read_bytes())
        root = Path(plan["controller_root"])
        self.assertEqual(root.parent.parent, self.harness / "modules/tests/fixtures")
        validate_run(plan["request"], root)
        roles = plan["request"]["assignments"]
        self.assertEqual((roles["implementer"]["agent"], roles["reviewer"]["agent"]), ("claude", "codex"))
        self.assertEqual(loads((root / "request.json").read_bytes()), plan["request"])
        self.assertFalse((root / "runs").exists())
        # Only the synthetic intake exists; preparing never authors an approval.
        self.assertEqual(sorted(p.name for p in (root / "approvals").glob("*/*")), ["intake.json", "user-message.json"])
        self.assertFalse([p for p in root.rglob("*approval*") if p.is_file()])
        self.assertEqual(self.prepare(), 1)  # An existing plan is never overwritten.

    def test_execution_requires_approval_isolation_and_unchanged_saver(self):
        self.assertEqual(self.prepare(), 0)
        approval = self.harness / "approval.json"
        approval.write_text("{}")
        with patch("sys.stderr"), self.assertRaises(SystemExit) as exc:
            self.main(["--request", str(self.plan)])
        self.assertEqual(exc.exception.code, 2)
        plan = loads(self.plan.read_bytes())
        root = Path(plan["controller_root"])
        (root / "modules/save_dialog.py").write_text("changed")
        self.assertEqual(self.main(["--request", str(self.plan), "--approval", str(approval)]), 1)
        outside = self.harness / "plans/outside.json"
        commit(outside, {**plan, "controller_root": str(self.harness / "elsewhere/controller")})
        self.assertEqual(self.main(["--request", str(outside), "--approval", str(approval)]), 1)
        self.assertFalse((self.harness / "runs").exists())
