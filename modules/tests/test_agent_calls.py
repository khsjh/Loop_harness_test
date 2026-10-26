"""M3 offline tests: real subprocesses, no network, no model calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from modules.harness.adapters import claude, codex
from modules.harness.adapters.common import AdapterError
from modules.harness.contracts import (ContractError, RESPONSE_SCHEMAS, SCOPE_FIELDS, loads,
                                      structure, validate_request, validate_response)
from modules.harness.process_runner import run_process
from modules.harness.prompts import snapshot_instructions
from modules.harness.recording import HARNESS_ROOT, retry_recording, write_once
from modules.harness.run_agent_once import run_once

FIXTURE = Path(__file__).parent / "fixtures/fake_agent.py"


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness 한글 ; ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("Only the requested target change is allowed.\n")
        self.harness = self.root / "controller"
        self.harness.mkdir()
        self.source = self.harness / "user-message.json"
        self.source.write_text(json.dumps({"schema_version": 1, "message_id": "message-1",
                                          "role": "user", "content": "Implement and test M3.",
                                          "user_asked_at": None}))
        self.request = {
            "schema_version": 1, "run_id": "test-1", "call_id": "call-1", "round": None,
            "agent": "codex", "role": "implementer", "model": "requested-alias",
            "reasoning_effort": "medium", "authorization_ref": "user-1", "task": "normal",
            "workspace": str(self.workspace), "allowed_paths": ["modules/calculator.py"],
            "instruction_files": [str(self.workspace / "AGENTS.md")],
            "verification_commands": [["python3", "-m", "unittest"]], "timeout_seconds": 5,
        }

    def registry(self):
        return {"user-1": {"user_instruction": "Implement and test M3.",
                           "source": {"kind": "user_message", "path": str(self.source),
                                      "message_id": "message-1",
                                      "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()},
                           "scope": {k: copy.deepcopy(self.request[k]) for k in SCOPE_FIELDS}}}

    def runner(self, argv, **kwargs):
        return run_process([sys.executable, str(FIXTURE), *argv[1:]], **kwargs)

    def call(self, **kwargs):
        registry = kwargs.pop("registry") if "registry" in kwargs else self.registry()
        return run_once(self.request, registry,
                        harness_root=self.harness, runner=kwargs.pop("runner", self.runner), **kwargs)

    def response(self):
        return {"schema_version": 1, "status": "complete", "summary": "fixed",
                "changed_files": ["modules/calculator.py"], "remaining_work": [], "questions": []}


class ContractTests(HarnessCase):
    def test_valid_request_and_role_responses(self):
        self.assertEqual(validate_request(self.request), self.workspace)
        validate_response(self.response(), self.request)
        self.request.update(role="reviewer", allowed_paths=[])
        validate_response({"schema_version": 1, "verdict": "approved", "summary": "ok",
                           "findings": [], "questions": []}, self.request)

    def test_strict_json(self):
        for text in ('{"a":1,"a":2}', '{"a":{"x":1,"x":2}}', '{"a":NaN}',
                     '{"a":Infinity}', '{"a":1e9999}', '```json\n{}\n```', '{} {}'):
            with self.subTest(text=text), self.assertRaises(ContractError):
                loads(text)

    def test_structure_and_values(self):
        for key, value in (("schema_version", True), ("schema_version", 3), ("round", 0),
                           ("round", False), ("timeout_seconds", 0), ("timeout_seconds", True),
                           ("model", None), ("model", " "), ("agent", "other"),
                           ("reasoning_effort", ""), ("task", "\0"), ("run_id", ".."),
                           ("call_id", "foo/bar"), ("verification_commands", [[]])):
            with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                validate_request({**self.request, key: value})
        for request in ({**self.request, "extra": 1}, {k: v for k, v in self.request.items() if k != "model"}):
            with self.assertRaises(ContractError):
                validate_request(request)

    def test_path_escape(self):
        (self.workspace / "link").symlink_to(self.root)
        for path in ("/tmp/outside", "../outside", "link/outside", ".", "a/../../x"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                validate_request({**self.request, "allowed_paths": [path]})

    def test_role_scope(self):
        for request in ({**self.request, "allowed_paths": []}, {**self.request, "role": "reviewer"}):
            with self.assertRaises(ContractError):
                validate_request(request)

    def test_response_semantics(self):
        variants = [{"summary": " "}, {"schema_version": True}, {"remaining_work": ["fix"]},
                    {"questions": ["why?"]}, {"status": "incomplete"}, {"status": "needs_user"},
                    {"changed_files": ["../x"]}, {"changed_files": ["unapproved.py"]}, {"extra": 1}]
        for change in variants:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_response({**self.response(), **change}, self.request)

    def test_review_findings(self):
        self.request.update(role="reviewer", allowed_paths=[])
        finding = {"severity": "medium", "path": "a.py", "line": 1, "message": "bug"}
        response = {"schema_version": 1, "verdict": "changes_requested", "summary": "fix",
                    "findings": [finding], "questions": []}
        validate_response(response, self.request)
        for change in ({"line": 0}, {"line": True}, {"path": None}, {"message": ""}, {"path": "../x"}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_response({**response, "findings": [{**finding, **change}]}, self.request)
        for change in ({"verdict": "approved"}, {"findings": []}):
            with self.assertRaises(ContractError):
                validate_response({**response, **change}, self.request)

    def test_schema_has_only_structural_constraints(self):
        def visit(schema):
            self.assertFalse(set(schema) & {"minimum", "minLength", "pattern"})
            if schema["type"] == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for value in schema["properties"].values():
                    visit(value)
            if schema["type"] == "array":
                visit(schema["items"])
        for schema in RESPONSE_SCHEMAS.values():
            visit(schema)
        response = {**self.response(), "summary": ""}
        structure(response, RESPONSE_SCHEMAS["implementer"])
        with self.assertRaises(ContractError):
            validate_response(response, self.request)

    def test_authorization_missing_or_scope_changed_never_spawns(self):
        registry = self.registry()
        self.request["task"] = "unapproved task"
        for approvals in ({}, registry):
            with self.subTest(approvals=approvals):
                outcome = self.call(registry=approvals, runner=lambda *a, **k: self.fail("spawned"))
                self.assertEqual(outcome.result["call_status"], "invalid_request")
        self.assertFalse((self.harness / "runs").exists())

    def test_instructions_missing_unrelated_and_nested(self):
        self.request["instruction_files"] = []
        self.assertEqual(self.call().result["call_status"], "invalid_request")
        other = self.root / "other"
        other.mkdir()
        (other / "AGENTS.md").write_text("wrong")
        self.request["instruction_files"] = [str(other / "AGENTS.md")]
        self.assertEqual(self.call().result["call_status"], "invalid_request")
        self.request["instruction_files"] = [str(self.workspace / "AGENTS.md")]
        nested = self.workspace / "modules"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("nested")
        with self.assertRaises(ContractError):
            snapshot_instructions(self.request)


class ProcessTests(unittest.TestCase):
    def run_python(self, script, **kwargs):
        return run_process([sys.executable, "-c", script], cwd="/tmp", **kwargs)

    def test_concurrent_streams_and_large_stdin(self):
        data = b"a" * 300000
        result = self.run_python("import os,sys; os.write(2,b'e'*200000); "
                                 "data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)",
                                 stdin=data, timeout_seconds=4)
        self.assertEqual(result.stdout, data)
        self.assertEqual(result.stderr, b"e" * 200000)
        self.assertEqual(result.returncode, 0)

    def test_output_limit_each_stream(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                result = self.run_python(f"import os;os.write({fd},b'x'*1000000)", output_limit=4096)
                self.assertTrue(result.output_limit_exceeded)
                self.assertEqual(len(result.stdout if fd == 1 else result.stderr), 4096)
                self.assertTrue(result.cleanup_completed)

    def test_timeout_kills_term_ignoring_child(self):
        result = self.run_python(
            "import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "pid=os.fork();print(os.getpid(),flush=True);time.sleep(30)",
            timeout_seconds=0.2, termination_grace_seconds=0.1)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.cleanup_completed)
        self.assertLess(result.elapsed_seconds, 3)
        self.assertTrue(result.stdout)
        for pid in result.stdout.decode().split():
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists():
                self.assertEqual(stat.read_text().rsplit(")", 1)[1].split()[0], "Z")

    def test_parent_exits_child_holds_pipes(self):
        result = self.run_python("import os,time;pid=os.fork();"
                                 "time.sleep(30) if pid == 0 else os._exit(0)",
                                 timeout_seconds=3600, termination_grace_seconds=0.1,
                                 post_exit_drain_seconds=0.1)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.post_exit_drain_expired)
        self.assertTrue(result.descendants_terminated)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.cleanup_completed)
        self.assertLess(result.elapsed_seconds, 3)

    def test_child_output_within_drain_grace_is_preserved(self):
        result = self.run_python("import os,time;pid=os.fork();"
                                 "os._exit(0) if pid else None;time.sleep(0.05);"
                                 "os.write(1,b'trailing output');os._exit(0)",
                                 timeout_seconds=3600, post_exit_drain_seconds=1)
        self.assertEqual(result.stdout, b"trailing output")
        self.assertFalse(result.post_exit_drain_expired)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)

    def test_closed_pipes_do_not_skip_timeout(self):
        result = self.run_python("import os,time;os.close(1);os.close(2);time.sleep(30)",
                                 timeout_seconds=0.2, termination_grace_seconds=0.1)
        self.assertTrue(result.timed_out)

    def test_exited_parent_nonzero_and_stderr_only_orphan(self):
        result = self.run_python(
            "import os,signal,time;os.close(1);pid=os.fork();"
            "os._exit(7) if pid else None;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "os.write(2,b'child stderr');time.sleep(30)",
            timeout_seconds=3600, post_exit_drain_seconds=0.1, termination_grace_seconds=0.1)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr, b"child stderr")
        self.assertTrue(result.post_exit_drain_expired)
        self.assertTrue(result.cleanup_completed)
        self.assertFalse(result.timed_out)
        self.assertLess(result.elapsed_seconds, 3)

    def test_spawn_error(self):
        result = run_process(["/missing-harness-executable"], cwd="/tmp")
        self.assertIsNotNone(result.spawn_error)
        self.assertIsNone(result.returncode)

    def test_stdin_is_literal(self):
        value = "한글 ; $(touch /tmp/not-executed-by-harness) `echo test`"
        result = self.run_python("import sys;print(sys.stdin.read(),end='')", stdin=value.encode())
        self.assertEqual(result.stdout.decode(), value)


class CallTests(HarnessCase):
    def test_valid_response_with_orphan_is_process_error(self):
        self.request.update(task="orphan_after_response", timeout_seconds=3600)
        def runner(argv, **kwargs):
            return self.runner(argv, post_exit_drain_seconds=0.1,
                               termination_grace_seconds=0.1, **kwargs)
        outcome = self.call(runner=runner)
        self.assertEqual(outcome.result["call_status"], "process_error")
        self.assertEqual(outcome.result["error"]["code"], "post_exit_drain_expired")
        self.assertFalse(outcome.result["response_valid"])
        self.assertTrue((outcome.call_dir / "stdout.txt").read_bytes())
        self.assertFalse((outcome.call_dir / "response.json").exists())

    def test_both_agents_and_roles(self):
        for agent in ("codex", "claude"):
            for role in ("implementer", "reviewer"):
                with self.subTest(agent=agent, role=role):
                    self.request.update(agent=agent, role=role, call_id=f"{agent}-{role}",
                                        allowed_paths=["modules/calculator.py"] if role == "implementer" else [])
                    outcome = self.call()
                    self.assertEqual(outcome.result["call_status"], "ok", outcome.result)
                    self.assertEqual(outcome.result["actual_model"], "actual-test-model")
                    self.assertEqual(outcome.result["execution"]["requested_model"], "requested-alias")
                    self.assertEqual(outcome.result["execution"]["cli_version"], "fake-cli 1.0")
                    self.assertEqual(loads((outcome.call_dir / "result.json").read_bytes()), outcome.result)
                    self.assertIn("Only the requested", (outcome.call_dir / "prompt.txt").read_text())
                    self.assertFalse((self.workspace / "runs").exists())

    def test_failure_matrix(self):
        for agent in ("codex", "claude"):
            for case, status in (("invalid_response", "invalid_response"), ("missing_final", "invalid_response"),
                                 ("nonzero", "process_error"), ("cli_error", "process_error")):
                with self.subTest(agent=agent, case=case):
                    self.request.update(agent=agent, task=case, call_id=f"{agent}-{case}")
                    outcome = self.call()
                    self.assertEqual(outcome.result["call_status"], status, outcome.result)
                    self.assertFalse(outcome.result["response_valid"])
                    self.assertFalse((outcome.call_dir / "response.json").exists())
                    self.assertTrue((outcome.call_dir / "stdout.txt").exists())

    def test_codex_final_mismatch(self):
        self.request["task"] = "mismatch"
        self.assertEqual(self.call().result["call_status"], "invalid_response")

    def test_no_reuse_or_stale_final(self):
        first = self.call()
        before = (first.call_dir / "result.json").read_bytes()
        second = self.call(runner=lambda *a, **k: self.fail("recalled model"))
        self.assertEqual(second.result["call_status"], "recording_error")
        self.assertEqual((first.call_dir / "result.json").read_bytes(), before)

    def test_recording_start_failure_does_not_spawn(self):
        (self.harness / "runs").write_text("not a directory")
        outcome = self.call(runner=lambda *a, **k: self.fail("spawned"))
        self.assertEqual(outcome.result["call_status"], "recording_error")

    def test_recording_failure_retries_only_save(self):
        def fail_stdout(path, data):
            if path.name == "stdout.txt":
                raise OSError("simulated disk full")
            write_once(path, data)
        with patch("modules.harness.recording.write_once", side_effect=fail_stdout):
            outcome = self.call()
        self.assertEqual(outcome.result["call_status"], "recording_error")
        self.assertEqual(outcome.result["underlying_call_status"], "ok")
        self.assertIsNotNone(outcome.result["response"])
        receipt = loads(json.dumps(outcome.as_dict()))
        saved = retry_recording(receipt, self.harness)
        self.assertEqual(saved.result["call_status"], "ok")
        self.assertEqual(retry_recording(receipt, self.harness).result, saved.result)

    def test_recording_error_preserves_original_process_error(self):
        self.request["task"] = "nonzero"
        with patch("modules.harness.recording.write_once", side_effect=OSError("disk full")):
            outcome = self.call()
        # The initial allocation also uses recording.write_once, so test post-call failure separately.
        self.assertEqual(outcome.result["call_status"], "recording_error")
        self.request["call_id"] = "second"
        def fail_result(path, data):
            if path.name == "result.json":
                raise OSError("disk full")
            write_once(path, data)
        with patch("modules.harness.recording.write_once", side_effect=fail_result):
            outcome = self.call()
        self.assertEqual(outcome.result["underlying_call_status"], "process_error")
        self.assertEqual(outcome.result["underlying_error"]["code"], "nonzero_exit")

    def test_symlink_record_directory_rejected(self):
        (self.harness / "runs").symlink_to(self.workspace, target_is_directory=True)
        self.assertEqual(self.call().result["call_status"], "recording_error")

    def test_runs_cannot_be_inside_workspace(self):
        outcome = run_once(self.request, self.registry(), harness_root=self.workspace,
                           runner=lambda *a, **k: self.fail("spawned"))
        self.assertEqual(outcome.result["call_status"], "recording_error")

    def test_unsupported_effort(self):
        self.request["reasoning_effort"] = "mystery"
        self.assertEqual(self.call().result["call_status"], "unsupported_configuration")

    def test_codex_project_config_rejected(self):
        config = self.workspace / ".codex"
        config.mkdir()
        (config / "config.toml").write_text("[mcp_servers]\n")
        self.assertEqual(self.call().result["call_status"], "unsupported_configuration")

    def test_command_permissions_and_explicit_models(self):
        path = self.root / "call"
        for adapter in (codex, claude):
            for role in ("implementer", "reviewer"):
                self.request["role"] = role
                cmd = adapter.command(self.request, path)
                self.assertEqual(cmd[cmd.index("--model") + 1], self.request["model"])
                self.assertFalse(any("bypass" in word for word in cmd))
                self.assertNotIn(self.request["task"], cmd)
                if adapter is codex:
                    self.assertIn("--ignore-user-config", cmd)
                    self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only" if role == "reviewer" else "workspace-write")
                    self.assertIn('model_reasoning_effort="medium"', cmd)
                else:
                    self.assertIn("--restricted", cmd)
                    self.assertIn("--safe-mode", cmd)
                    self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep" + (",Edit,Write" if role == "implementer" else ""))

    def test_actual_model_missing_and_multiple(self):
        response = self.response()
        outer = {"type": "result", "subtype": "success", "is_error": False, "structured_output": response}
        self.assertEqual(claude.extract(json.dumps(outer), self.root)[1]["actual_model"], "unknown")
        outer["modelUsage"] = {"one": {}, "two": {}}
        meta = claude.extract(json.dumps(outer), self.root)[1]
        self.assertEqual(meta["actual_model"], "unknown")
        self.assertEqual(len(meta["reported_models"]), 2)

    def test_cli_error_not_replaced_with_natural_language(self):
        outer = {"type": "result", "is_error": True, "subtype": "error", "structured_output": self.response()}
        with self.assertRaises(AdapterError):
            claude.extract(json.dumps(outer), self.root)
        outer = {"type": "result", "is_error": False, "subtype": "success", "result": json.dumps(self.response())}
        with self.assertRaises(ContractError):
            claude.extract(json.dumps(outer), self.root)

    def test_final_symlink_not_read(self):
        (self.root / "agent-final.txt").symlink_to(self.workspace / "AGENTS.md")
        events = ["{\"type\":\"turn.completed\"}"]
        with self.assertRaises(ContractError):
            codex.extract("\n".join(events).encode(), self.root)

    def test_cli_rejects_approval_file_inside_target(self):
        request = self.root / "request.json"
        request.write_text(json.dumps(self.request))
        registry = self.workspace / "approvals.json"
        registry.write_text(json.dumps(self.registry()))
        result = subprocess.run([sys.executable, "-m", "modules.harness.run_agent_once",
                                 "--request", str(request), "--authorizations", str(registry)],
                                cwd=HARNESS_ROOT, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(loads(result.stdout)["call_status"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
