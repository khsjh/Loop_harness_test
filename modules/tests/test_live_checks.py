"""Offline regression tests for live-check acceptance, no CLI/model calls."""
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from modules.harness.contracts import ContractError, SCOPE_FIELDS
from modules.harness.process_runner import ProcessResult
from modules.tests import live_agent_check, live_permissions_check
from modules.tests.live_agent_check import assess, snapshot


class LiveCheckTests(unittest.TestCase):
    def test_test_failure_is_not_success(self):
        result = {"call_status": "ok", "response": {"status": "complete"}}
        self.assertFalse(assess(result, {}, {}, ProcessResult(returncode=1), "implementer")["passed"])

    def test_scope_violation_is_not_success(self):
        result = {"call_status": "ok", "response": {"status": "complete"}}
        for before, after in (({}, {"other.py": "x"}), ({"other.py": "x"}, {}),
                              ({"other.py": "x"}, {"other.py": "y"})):
            assessment = assess(result, before, after, ProcessResult(returncode=0), "implementer")
            self.assertFalse(assessment["passed"])
            self.assertEqual(assessment["scope_violations"], ["other.py"])

    def test_reviewer_cannot_change_calculator(self):
        result = {"call_status": "ok", "response": {"verdict": "approved"}}
        self.assertFalse(assess(result, {}, {"modules/calculator.py": "x"},
                                ProcessResult(returncode=0), "reviewer")["passed"])

    def test_incomplete_or_failed_call_is_not_success(self):
        for result in ({"call_status": "ok", "response": {"status": "incomplete"}},
                       {"call_status": "process_error", "response": None}):
            self.assertFalse(assess(result, {}, {}, ProcessResult(returncode=0), "implementer")["passed"])

    def test_normal_success_and_timeout(self):
        result = {"call_status": "ok", "response": {"status": "complete"}}
        self.assertTrue(assess(result, {}, {"modules/calculator.py": "x"},
                               ProcessResult(returncode=0), "implementer")["passed"])
        self.assertFalse(assess(result, {}, {}, ProcessResult(returncode=0, timed_out=True),
                                "implementer")["passed"])

    def test_snapshot_detects_broken_symlink_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to("missing")
            (root / "file").write_text("x")
            before = snapshot(root)
            (root / "file").chmod(0o700)
            self.assertNotEqual(before, snapshot(root))
            self.assertEqual(before["link"]["link"], "missing")

    def test_expired_output_drain_is_not_success(self):
        result = {"call_status": "ok", "response": {"status": "complete"}}
        self.assertFalse(assess(result, {}, {}, ProcessResult(returncode=0, post_exit_drain_expired=True),
                                "implementer")["passed"])


class LiveApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "modules/tests/fixtures").mkdir(parents=True)
        (self.root / "AGENTS.md").write_text("Only scoped test operations are authorized.")
        for module in (live_agent_check, live_permissions_check):
            context = patch.object(module, "HARNESS_ROOT", self.root)
            context.start()
            self.addCleanup(context.stop)

    def prepare(self, module):
        path = self.root / (module.__name__.split(".")[-1] + ".json")
        with redirect_stdout(io.StringIO()), patch.object(module, "run_process", side_effect=AssertionError("spawned")):
            self.assertEqual(module.main(["--prepare", str(path), "--agent", "codex", "--role", "implementer",
                                          "--model", "explicit-model", "--effort", "medium",
                                          "--authorization-ref", "user-approval"]), 0)
        return path, json.loads(path.read_bytes())

    def approval(self, request):
        source = self.root / "user-message.json"
        source.write_text(json.dumps({"schema_version": 1, "message_id": "msg-1", "role": "user",
                                      "content": "Run the controlled regression tests.", "user_asked_at": None}))
        registry = {request["authorization_ref"]: {
            "user_instruction": "Run the controlled regression tests.",
            "source": {"kind": "user_message", "path": str(source), "message_id": "msg-1",
                       "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
            "scope": {key: request[key] for key in SCOPE_FIELDS}}}
        path = self.root / "authorizations.json"
        path.write_text(json.dumps(registry))
        return path

    def test_prepare_creates_requests_without_approval_or_model_call(self):
        for module in (live_agent_check, live_permissions_check):
            with self.subTest(module=module.__name__):
                _, request = self.prepare(module)
                self.assertEqual(request["timeout_seconds"], 3600)
                self.assertEqual(request["authorization_ref"], "user-approval")
                self.assertNotIn("user_instruction", request)
                self.assertFalse((self.root / "runs").exists())

    def test_execution_requires_external_approval_and_rejects_overrides(self):
        for module in (live_agent_check, live_permissions_check):
            with self.subTest(module=module.__name__):
                path, _ = self.prepare(module)
                for extra in ([], ["--authorizations", "absent.json", "--timeout-seconds", "1"]):
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                        module.main(["--request", str(path), *extra])
                    self.assertEqual(error.exception.code, 2)

    def test_mismatched_source_never_calls_model(self):
        for module in (live_agent_check, live_permissions_check):
            with self.subTest(module=module.__name__):
                path, request = self.prepare(module)
                registry = self.approval(request)
                (self.root / "user-message.json").write_text("tampered")
                with redirect_stdout(io.StringIO()), patch.object(module, "run_process", side_effect=AssertionError("spawned")):
                    if module is live_permissions_check:
                        with self.assertRaises(ContractError):
                            module.main(["--request", str(path), "--authorizations", str(registry)])
                    else:
                        self.assertEqual(module.main(["--request", str(path), "--authorizations", str(registry)]), 1)
                self.assertFalse((self.root / "runs").exists())

    def test_permission_execution_records_external_source(self):
        path, request = self.prepare(live_permissions_check)
        registry = self.approval(request)
        workspace = Path(request["workspace"])
        def probe(*args, **kwargs):
            (workspace / "modules/canary.txt").write_text("changed\n")
            (workspace / "modules/created.txt").write_text("new\n")
            (workspace / "modules/victim.txt").unlink()
            results = {name: "allowed" for name in ("inside_create", "inside_modify", "inside_delete")}
            results.update({name: "denied" for name in ("outside_create", "outside_modify", "outside_delete", "symlink_modify")})
            event = {"type": "item.completed", "item": {"type": "command_execution",
                     "aggregated_output": "M3_PERMISSION_RESULTS=" + json.dumps(results)}}
            return ProcessResult(returncode=0, stdout=json.dumps(event).encode())
        with patch.object(live_permissions_check, "run_process", side_effect=probe) as runner, redirect_stdout(io.StringIO()):
            self.assertEqual(live_permissions_check.main(["--request", str(path), "--authorizations", str(registry)]), 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 3600)
        record = self.root / "runs" / request["run_id"] / "calls" / ("call-" + request["call_id"])
        self.assertEqual((record / "authorization-source.txt").read_bytes(), (self.root / "user-message.json").read_bytes())
        self.assertIn("msg-1", (record / "prompt.txt").read_text())


if __name__ == "__main__":
    unittest.main()
