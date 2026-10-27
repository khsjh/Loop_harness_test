"""M5 contract, state, subprocess, loop and recording fault-injection tests."""
import copy
import json
import os
from pathlib import Path
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from modules.harness.contracts import ContractError, loads
from modules.harness.execution_budget import ExecutionBudget
from modules.harness.fixture_inventory import inventory
from modules.harness.inspect_run import inspect, recover, repair_recording
from modules.harness.intake import capture
from modules.harness.process_runner import ProcessResult, run_process
from modules.harness.recording import HARNESS_ROOT
from modules.harness.run_agent_once import run_once
from modules.harness.run_contracts import TEMPLATE_VERSION, approved_scope, validate_run
from modules.harness.run_loop import execute
from modules.harness.run_store import RunStore, commit, digest
from modules.harness.verification import VerificationRunner, process_ok
from modules.harness.workspace_guard import WorkspaceBusy, WorkspaceGuard, recover_guard
from modules.tests.runtime_fixture import python_runtime
from modules.harness.run_policy import DEFAULT_TIMEOUT_POLICY

FIXTURE = HARNESS_ROOT / "modules/tests/fixtures/fake_loop_agent.py"


class OfflineAgent:
    def __init__(self, mode="normal"):
        self.mode, self.calls = mode, []

    def __call__(self, request, registry, **kwargs):
        self.calls.append((request["role"], request["round"]))
        def runner(argv, **options):
            options["env"] = {**os.environ, **options.get("env", {}), "M5_FAKE_MODE": self.mode}
            return run_process([sys.executable, "-B", str(FIXTURE), *argv[1:]], **options)
        return run_once(request, registry, runner=runner, **kwargs)


class OfflineVerifier:
    """Unrestricted controlled fixture tests only; unavailable through the public CLI."""
    def __init__(self, workspace):
        self.workspace = workspace

    def probe(self, budget):
        return ProcessResult(returncode=0)

    def run(self, command, budget):
        return budget.wrap(run_process)(command["argv"], cwd=command["cwd"], timeout_seconds=command["timeout_seconds"])


class M5Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m5 한글 ")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root, self.workspace = base / "controller", base / "workspace"
        (self.root / "modules").mkdir(parents=True)
        shutil.copyfile(HARNESS_ROOT / "modules/save_dialog.py", self.root / "modules/save_dialog.py")
        (self.workspace / "modules/tests").mkdir(parents=True)
        (self.workspace / "AGENTS.md").write_text("Only calculator changes are permitted.")
        (self.workspace / "modules/calculator.py").write_text("def add(a,b): return a-b\n")
        (self.workspace / "modules/tests/test_calculator.py").write_text(
            "import unittest\nfrom modules.calculator import add\n"
            "class Tests(unittest.TestCase):\n def test_sum(self): self.assertEqual(add(2,3),5)\n")
        self.input_ref = capture("Implement addition.\r\nThen verify and review.\n", root=self.root)
        self.request = {"schema_version": 1, "run_id": "run-1", "parent_run_id": None,
                        "input_ref": self.input_ref, "authorization_ref": "approval-1",
                        "objective": "Fix modules/calculator.py to return the sum.", "workspace": str(self.workspace),
                        "allowed_paths": ["modules/calculator.py"], "instruction_files": [str(self.workspace / "AGENTS.md")],
                        "assignments": {"implementer": {"agent": "codex", "model": "fake-model", "reasoning_effort": "medium"},
                                        "reviewer": {"agent": "claude", "model": "fake-model", "reasoning_effort": "medium"}},
                        "verification_commands": [{"argv": [sys.executable, "-B", "-m", "unittest", "discover", "-s", "modules/tests"],
                                                   "cwd": str(self.workspace), "timeout_seconds": 5,
                                                   "protected_paths": ["modules/tests"], "runtime_paths": []}],
                        "limits": {"max_rounds": 2, "run_timeout_seconds": 30, "call_timeout_seconds": 21600},
                        "timeout_policy": dict(DEFAULT_TIMEOUT_POLICY),
                        "dialog_owner": "harness", "caller_dialog": None,
                        "report_identity": {"ai": "harness", "model": "none"}}
        self.approval = self.root / "approvals/approval-1/run-approval.json"
        self.approval.parent.mkdir()
        self.approve()

    def approve(self):
        rules = validate_run(self.request, self.root)
        intake = loads(Path(self.input_ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        self.approval.write_text(json.dumps({"schema_version": 1, "authorization_ref": self.request["authorization_ref"],
            "user_instruction": message["content"], "source": intake["source"], "scope": approved_scope(self.request),
            "instruction_hashes": {r["path"]: r["sha256"] for r in rules},
            "intake_sha256": digest(Path(self.input_ref).read_bytes()), "allow_iterations": True, "template_version": TEMPLATE_VERSION}))

    def run_loop(self, mode="normal", **kwargs):
        self.agent = OfflineAgent(mode)
        return execute(self.request, self.approval, root=self.root, agent_call=self.agent,
                       verifier_factory=kwargs.pop("verifier_factory", OfflineVerifier), **kwargs)


class RunContractTests(M5Case):
    def test_runtime_mounts_are_explicit_and_cannot_expose_controller(self):
        for path in ("/", str(Path.home()), str(self.root), str(self.root / "modules"), str(self.workspace), "/proc", "/etc"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                self.request["verification_commands"][0]["runtime_paths"] = [path]
                validate_run(self.request, self.root)
        del self.request["verification_commands"][0]["runtime_paths"]
        with self.assertRaises(ContractError):
            validate_run(self.request, self.root)

    def test_runtime_mount_scope_change_never_spawns(self):
        self.request["verification_commands"][0]["runtime_paths"] = ["/usr/bin/true"]
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertEqual(self.agent.calls, [])

    def test_missing_limits_and_wrong_roles_rejected(self):
        for variant in ({**self.request, "limits": {}}, {**self.request, "unknown": 1},
                        {**self.request, "limits": {**self.request["limits"], "max_rounds": True}},
                        {**self.request, "assignments": {"implementer": self.request["assignments"]["implementer"],
                                                        "reviewer": self.request["assignments"]["implementer"]}}):
            with self.subTest(variant=variant), self.assertRaises(ContractError):
                validate_run(variant, self.root)

    def test_scope_change_never_spawns(self):
        self.request["objective"] = "New unapproved objective"
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertEqual(self.agent.calls, [])
        self.assertFalse((self.root / "runs").exists())

    def test_changed_rule_or_source_never_spawns(self):
        (self.workspace / "AGENTS.md").write_text("changed")
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_iteration_authorization_is_required(self):
        approval = loads(self.approval.read_bytes())
        approval["allow_iterations"] = False
        self.approval.write_text(json.dumps(approval))
        with self.assertRaises(ContractError):
            self.run_loop()

    def test_intake_keeps_verbatim_and_does_not_approve(self):
        intake = loads(Path(self.input_ref).read_bytes())
        self.assertEqual(intake["source_kind"], "manual_import")
        message = loads(Path(intake["source"]["path"]).read_bytes())
        self.assertEqual(message["content"], "Implement addition.\r\nThen verify and review.\n")
        self.assertIsNone(message["user_asked_at"])
        self.assertNotIn("scope", intake)


class LoopTests(M5Case):
    def test_success_with_exact_dialog_and_current_evidence(self):
        result = self.run_loop()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.agent.calls, [("implementer", 1), ("reviewer", 1)])
        self.assertEqual(result["recording_status"], "saved")
        final = Path(result["run_path"]) / "final"
        self.assertEqual((final / "question.txt").read_bytes(), b"Implement addition.\r\nThen verify and review.\n")
        self.assertTrue(loads((final / "input-dialog-check.json").read_bytes())["matches"])
        self.assertEqual(inspect(self.root, "run-1")["state"]["status"], "success")
        call = loads((Path(result["run_path"]) / "calls/call-r1-implementer/request.json").read_bytes())
        self.assertEqual(call["timeout_seconds"], 21600)

    def test_reverse_assignment(self):
        self.request["assignments"]["implementer"]["agent"] = "claude"
        self.request["assignments"]["reviewer"]["agent"] = "codex"
        self.approve()
        self.assertEqual(self.run_loop()["status"], "success")

    def test_failed_test_is_fixed_in_next_round(self):
        result = self.run_loop("fix_second")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.agent.calls, [("implementer", 1), ("implementer", 2), ("reviewer", 2)])

    def test_review_rejection_repeats_test_and_review(self):
        result = self.run_loop("review_second")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.agent.calls, [("implementer", 1), ("reviewer", 1), ("implementer", 2), ("reviewer", 2)])
        directory = Path(result["run_path"]) / "calls"
        old = loads((directory / "call-r1-reviewer/result.json").read_bytes())
        for role in ("implementer", "reviewer"):
            call = loads((directory / f"call-r2-{role}/request.json").read_bytes())
            data = json.loads(call["task"].split("new authorization:\n", 1)[1])
            prior = data["previous_review"]
            self.assertEqual(prior["review"], old["response"])
            self.assertEqual(prior["round"], 1)
            self.assertEqual(prior["artifact_sha256"], digest(Path(prior["artifact_path"]).read_bytes()))
            self.assertTrue(prior["code_state_id"])

    def test_review_context_survives_intervening_verification_failure(self):
        self.request["limits"]["max_rounds"] = 3
        self.approve()
        result = self.run_loop("review_then_test_failure")
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.agent.calls, [("implementer", 1), ("reviewer", 1), ("implementer", 2),
                                           ("implementer", 3), ("reviewer", 3)])
        for role in ("implementer", "reviewer"):
            call = loads((Path(result["run_path"]) / f"calls/call-r3-{role}/request.json").read_bytes())
            data = json.loads(call["task"].split("new authorization:\n", 1)[1])
            self.assertEqual(data["previous_review"]["round"], 1)

    def test_round_limit_not_off_by_one(self):
        self.request["limits"]["max_rounds"] = 1
        self.approve()
        result = self.run_loop("review_reject")
        self.assertEqual(result["status"], "limit_reached", result)
        self.assertEqual(result["reason_code"], "round_limit")
        self.assertEqual(len(self.agent.calls), 2)

    def test_needs_user_is_terminal_without_reviewer(self):
        result = self.run_loop("needs_user")
        self.assertEqual(result["status"], "waiting_for_user", result)
        self.assertEqual(len(self.agent.calls), 1)

    def test_reviewer_needs_user(self):
        result = self.run_loop("review_needs_user")
        self.assertEqual(result["status"], "waiting_for_user", result)

    def test_rule_and_test_tampering_stop_before_verification(self):
        result = self.run_loop("test_tamper")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["reason_code"], "scope_violation")
        self.assertEqual(len(self.agent.calls), 1)
        self.assertFalse(list((self.root / "runs/run-1/rounds").glob("*/verification/*")))

    def test_reviewer_change_invalidates_approval(self):
        result = self.run_loop("review_write")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["reason_code"], "review_changed_workspace")

    def test_total_deadline_with_21600_second_request(self):
        self.request["limits"]["run_timeout_seconds"] = 1
        self.approve()
        started = time.monotonic()
        result = self.run_loop("hang")
        self.assertEqual(result["status"], "limit_reached", result)
        self.assertEqual(result["reason_code"], "run_deadline")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(self.agent.calls), 1)

    def test_cancel_before_launch(self):
        result = self.run_loop(cancel_check=lambda: True)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["reason_code"], "interrupted")
        self.assertFalse(self.agent.calls)

    def test_missing_sandbox_stops_before_model(self):
        class Unsupported(OfflineVerifier):
            def probe(self, budget):
                return ProcessResult(returncode=1, stderr=b"unsupported sandbox")
        result = self.run_loop(verifier_factory=Unsupported)
        self.assertEqual(result["reason_code"], "unsupported_configuration", result)
        self.assertFalse(self.agent.calls)

    def test_existing_run_never_reexecutes(self):
        self.run_loop()
        with self.assertRaises(FileExistsError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_dialog_failure_and_save_only_repair(self):
        result = self.run_loop(saver_runner=lambda *a, **k: ProcessResult(returncode=1, stderr=b"simulated disk full"))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["task_outcome"], "success")
        before = len(self.agent.calls)
        repaired = repair_recording(self.root, "run-1")
        self.assertTrue(repaired["dialog"]["saved"], repaired)
        self.assertEqual(repaired["state"]["status"], "failed")
        self.assertEqual(len(self.agent.calls), before)
        again = repair_recording(self.root, "run-1")
        self.assertEqual(again["dialog"]["path"], repaired["dialog"]["path"])
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)

    def test_workspace_is_leased_through_final_save(self):
        def saver(argv, **kwargs):
            with self.assertRaises(WorkspaceBusy):
                with WorkspaceGuard(self.root, self.workspace, "competitor"):
                    pass
            return run_process(argv, **kwargs)
        self.assertEqual(self.run_loop(saver_runner=saver)["status"], "success")

    def test_change_during_final_save_cannot_report_success(self):
        def saver(argv, **kwargs):
            (self.workspace / "modules/calculator.py").write_text("def add(a,b): return 0\n")
            return run_process(argv, **kwargs)
        result = self.run_loop(saver_runner=saver)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["reason_code"], "code_state_changed")

    def test_save_committed_but_response_lost_is_idempotent(self):
        def saver(argv, **kwargs):
            original = run_process(argv, **kwargs)
            self.assertEqual(original.returncode, 0)
            return ProcessResult(returncode=-15, timed_out=True)
        self.run_loop(saver_runner=saver)
        recovered = repair_recording(self.root, "run-1")
        self.assertTrue(recovered["dialog"]["saved"])
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)

    def test_frozen_answer_tampering_blocks_save_retry(self):
        self.run_loop(saver_runner=lambda *a, **k: ProcessResult(returncode=1))
        (self.root / "runs/run-1/final/answer.txt").write_text("tampered")
        with self.assertRaises(ContractError):
            repair_recording(self.root, "run-1")


class GuardAndStoreTests(M5Case):
    def test_controller_sigkill_keeps_guard_and_never_replays_call(self):
        request_file = self.root / "request.json"
        request_file.write_text(json.dumps(self.request))
        ready = self.root / "fake-ready"
        controller = subprocess.Popen([sys.executable, "-B", "-m", "modules.tests.fixtures.m5_crash_driver",
                                       str(self.root), str(request_file), str(self.approval)],
                                      cwd=HARNESS_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=dict(os.environ, M5_FAKE_READY=str(ready)))
        model_pid = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                files = list((self.root / "runs/.guards").glob("*.json"))
                if files:
                    guard = loads(files[0].read_bytes())
                    if guard["process"] is not None:
                        candidate = guard["process"]["pid"]
                        try:
                            cmdline = Path(f"/proc/{candidate}/cmdline").read_bytes()
                        except OSError:
                            continue
                        if (b"--output-last-message" in cmdline and ready.exists()
                                and ready.read_text() == str(candidate)):
                            model_pid = candidate
                            break
                time.sleep(0.02)
            self.assertIsNotNone(model_pid, "model did not start")
            controller.kill()
            controller.communicate(timeout=3)
            recovered = recover(self.root, "run-1")
            self.assertEqual(recovered["state"]["status"], "failed")
            self.assertTrue(recovered["state"]["recovery_required"])
            with self.assertRaises(WorkspaceBusy):
                with WorkspaceGuard(self.root, self.workspace, "new-run"):
                    pass
            os.killpg(model_pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            from modules.harness.process_runner import group_alive
            while group_alive(model_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(group_alive(model_pid))
            recovered = recover(self.root, "run-1")
            self.assertFalse(recovered["state"]["recovery_required"])
            self.assertFalse(recovered["model_reexecuted"])
            repaired = repair_recording(self.root, "run-1")
            self.assertTrue(repaired["dialog"]["saved"])
            self.assertEqual(len(list((self.root / "runs/run-1/calls").glob("call-*"))), 1)
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.communicate(timeout=3)
            if model_pid:
                try:
                    os.killpg(model_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_overlapping_workspaces_and_aliases_cannot_lease(self):
        alias = self.workspace.parent / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        with WorkspaceGuard(self.root, self.workspace, "first"):
            for path in (self.workspace, alias, self.workspace / "modules", self.workspace.parent):
                with self.subTest(path=path), self.assertRaises(WorkspaceBusy):
                    with WorkspaceGuard(self.root, path, "second"):
                        self.fail("acquired overlapping workspace")

    def test_uncertain_spawn_guard_blocks_reentry(self):
        with WorkspaceGuard(self.root, self.workspace, "first") as guard:
            guard.planned()
        self.assertFalse(recover_guard(self.root, self.workspace, "first"))
        with self.assertRaises(WorkspaceBusy):
            with WorkspaceGuard(self.root, self.workspace, "second"):
                pass

    def test_active_controller_cannot_be_recovered(self):
        store = RunStore(self.root, self.request)
        self.addCleanup(store.close)
        with self.assertRaises(ContractError):
            recover(self.root, "run-1")

    def test_state_cache_loss_recovers_without_calling_models(self):
        store = RunStore(self.root, self.request)
        store.event("start", {"phase": "implementing"})
        store.close()
        (store.path / "state.json").write_text("corrupt cache")
        result = recover(self.root, "run-1")
        self.assertEqual(result["state"]["status"], "failed")
        self.assertFalse(result["model_reexecuted"])
        self.assertEqual(loads((store.path / "state.json").read_bytes()), result["state"])

    def test_last_event_corruption_is_detected(self):
        store = RunStore(self.root, self.request)
        store.close()
        path = store.path / "events/000001.json"
        event = loads(path.read_bytes())
        event["state"]["rounds_started"] = 99
        path.write_text(json.dumps(event))
        with self.assertRaises(ContractError):
            inspect(self.root, "run-1")

    def test_event_commit_precedes_cache(self):
        store = RunStore(self.root, self.request)
        with patch("modules.harness.run_store.replace_json", side_effect=OSError("cache failed")):
            with self.assertRaises(OSError):
                store.event("progress", {"phase": "implementing"})
        store.close()
        self.assertEqual(inspect(self.root, "run-1")["state"]["phase"], "implementing")

    def test_legacy_records_are_read_only(self):
        path = self.root / "runs/legacy/calls/call-example"
        path.mkdir(parents=True)
        result = inspect(self.root, "legacy")
        self.assertEqual(result["kind"], "legacy_single_call")
        self.assertFalse((path.parent.parent / "state.json").exists())


class BudgetTests(unittest.TestCase):
    def test_no_spawn_when_already_expired(self):
        result = run_process(["/missing-executable"], cwd="/tmp", deadline_monotonic=time.monotonic() - 1)
        self.assertTrue(result.run_deadline_expired)
        self.assertIsNone(result.spawn_error)

    def test_deadline_includes_post_exit_drain(self):
        result = run_process([sys.executable, "-c", "import os,time;pid=os.fork();os._exit(0) if pid else time.sleep(30)"],
                             cwd="/tmp", timeout_seconds=3600, deadline_monotonic=time.monotonic() + 0.15,
                             termination_grace_seconds=0.05)
        self.assertTrue(result.run_deadline_expired)
        self.assertFalse(result.post_exit_drain_expired)
        self.assertTrue(result.cleanup_completed)
        self.assertLess(result.elapsed_seconds, 2)

    def test_preflight_uses_whole_run_budget(self):
        from modules.harness.adapters import codex
        from modules.harness.adapters.common import AdapterError
        from modules.harness.run_agent_once import preflight
        def sleeping_runner(argv, **kwargs):
            return run_process([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        budget = ExecutionBudget(0.1)
        with self.assertRaises(AdapterError) as error:
            preflight(codex, "fake", "/tmp", budget.wrap(sleeping_runner))
        self.assertEqual(error.exception.code, "run_deadline")


class SandboxTests(M5Case):
    def test_real_sandbox_denies_writes_and_host_network(self):
        verifier = VerificationRunner(self.workspace)
        probe = verifier.probe(ExecutionBudget(10))
        if not process_ok(probe):
            self.skipTest("bubblewrap unavailable in this caller sandbox: " + probe.stderr.decode())
        outside = self.root / "protected.txt"
        outside.write_text("original")
        host = socket.socket()
        self.addCleanup(host.close)
        host.bind(("127.0.0.1", 0))
        host.listen(1)
        port = host.getsockname()[1]
        script = ("from pathlib import Path\nimport socket\n"
                  f"paths = [{str(outside)!r}, {str(self.workspace / 'modules/calculator.py')!r}]\n"
                  "for p in paths:\n"
                  " try: Path(p).write_text('bad')\n"
                  " except OSError: pass\n"
                  " else: raise AssertionError('write escaped')\n"
                  "assert Path('/tmp').is_dir()\nPath('/tmp/private').write_text('ok')\n"
                  f"s=socket.socket();s.settimeout(1);assert s.connect_ex(('127.0.0.1',{port})) != 0\n")
        python, runtime = python_runtime()
        result = verifier.run({"argv": [python, "-B", "-S", "-c", script], "cwd": str(self.workspace),
                               "timeout_seconds": 5, "runtime_paths": list(runtime)}, ExecutionBudget(10))
        self.assertTrue(process_ok(result), result.stderr)
        self.assertEqual(outside.read_text(), "original")

    def test_real_sandbox_hides_host_secrets_and_allows_only_listed_dependency(self):
        verifier = VerificationRunner(self.workspace)
        probe = verifier.probe(ExecutionBudget(10))
        if not process_ok(probe):
            self.skipTest("bubblewrap unavailable in this caller sandbox: " + probe.stderr.decode())
        # Outside /tmp: the former whole-root mount exposed these paths.
        temporary = tempfile.TemporaryDirectory(prefix="read-boundary-", dir=HARNESS_ROOT / "modules/tests/fixtures")
        self.addCleanup(temporary.cleanup)
        host = Path(temporary.name)
        secrets = [host / "home/.codex/auth.json", host / "home/.claude/.credentials.json",
                   host / "other-project/private.txt", host / "controller/approval.json"]
        for path in secrets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("FAKE_TEST_SECRET_DO_NOT_FORWARD")
        (self.workspace / "escape").symlink_to(secrets[0])
        dependency = host / "deps/allowed.py"
        dependency.parent.mkdir()
        dependency.write_text("VALUE = 42\n")
        (dependency.parent / "unlisted.txt").write_text("FAKE_TEST_SECRET_DO_NOT_FORWARD")
        forbidden = [*secrets, dependency.parent / "unlisted.txt", self.workspace / "escape", Path("/etc/passwd")]
        script = ("from pathlib import Path\nimport sys\n"
                  f"sys.path.insert(0, {str(dependency.parent)!r})\nimport allowed\nassert allowed.VALUE == 42\n"
                  f"for name in {list(map(str, forbidden))!r}:\n"
                  " try: Path(name).read_bytes()\n"
                  " except OSError: pass\n"
                  " else: raise AssertionError('unapproved file was readable')\n"
                  "assert Path('modules/calculator.py').read_text()\nprint('READ_BOUNDARY_OK')\n")
        python, runtime = python_runtime()
        result = verifier.run({"argv": [python, "-B", "-S", "-c", script], "cwd": str(self.workspace),
                               "timeout_seconds": 5, "runtime_paths": [*runtime, str(dependency)]}, ExecutionBudget(10))
        self.assertTrue(process_ok(result), result.stderr)
        self.assertEqual(result.stdout.strip(), b"READ_BOUNDARY_OK")
        self.assertNotIn(b"FAKE_TEST_SECRET", result.stdout + result.stderr)


class RetentionTests(M5Case):
    def test_unknown_and_documented_fixtures_are_preserved(self):
        fixture = self.root / "modules/tests/fixtures/live-example"
        fixture.mkdir(parents=True)
        (fixture / "file").write_text("evidence")
        (self.root / "docs").mkdir()
        (self.root / "docs/evidence.md").write_text("Evidence is in live-example.")
        result = inventory(self.root)[0]
        self.assertFalse(result["candidate"])
        self.assertIn("unknown_legacy_fixture", result["keep_reasons"])
        self.assertIn("document_reference", result["keep_reasons"])
        self.assertTrue((fixture / "file").exists())


if __name__ == "__main__":
    unittest.main()
