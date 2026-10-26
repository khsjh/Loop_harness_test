"""Approved dialog ownership, real saver handoff and save-only recovery."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

from modules.save_dialog import record_block
from modules.harness import finalize_run, inspect_run
from modules.harness.authorization import resolve_source
from modules.harness.caller_dialog import bind_caller_dialog
from modules.harness.contracts import ContractError, loads
from modules.harness.finalize_run import prepare_final
from modules.harness.inspect_run import attach_caller_dialog, recover, repair_recording
from modules.harness.intake import capture
from modules.harness.process_runner import run_process
from modules.harness.run_contracts import validate_run
from modules.harness.run_store import RunStore, commit, digest
from modules.tests.test_m5 import M5Case


class OwnershipTests(M5Case):
    def caller(self):
        intake = loads(Path(self.input_ref).read_bytes())
        self.request.update(dialog_owner="caller", report_identity={"ai": "codex", "model": "unknown"},
                            caller_dialog={"message_id": intake["source"]["message_id"], "call_id": "shared-answer-1"})
        self.approve()

    def save_caller(self, result, *, answer="실제 사용자 답변입니다.\n", **overrides):
        handoff = result["dialog"]
        params = {key: handoff[key] for key in ("ai", "model", "run_id", "call_id")}
        params["run_id"] = handoff.get("dialog_run_id", handoff["run_id"])
        params.update(overrides)
        answer_file = self.root / "caller-answer.txt"
        answer_file.write_bytes(answer.encode())
        argv = [sys.executable, "-B", str(self.root / "modules/save_dialog.py"),
                "--question-file", handoff["question_file"], "--answer-file", str(answer_file)]
        for key, value in params.items():
            if value is not None:
                argv += ["--" + key.replace("_", "-"), value]
        process = run_process(argv, cwd=self.root, timeout_seconds=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        return Path(process.stdout.decode().strip())

    def test_explicit_owner_and_matching_identity_required(self):
        for owner, identity in ((None, {"ai": "harness", "model": "none"}),
                                ("automatic", {"ai": "harness", "model": "none"}),
                                ("harness", {"ai": "codex", "model": "unknown"}),
                                ("harness", {"ai": "harness", "model": "fake-model"}),
                                ("caller", {"ai": "harness", "model": "none"})):
            request = {**self.request, "dialog_owner": owner, "report_identity": identity}
            with self.subTest(owner=owner, identity=identity), self.assertRaises(ContractError):
                validate_run(request, self.root)
        del self.request["dialog_owner"]
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)
        self.assertFalse((self.root / "runs").exists())

    def test_ownership_cannot_change_after_approval(self):
        self.request.update(dialog_owner="caller", report_identity={"ai": "codex", "model": "unknown"})
        self.request["caller_dialog"] = {
            "message_id": loads(Path(self.input_ref).read_bytes())["source"]["message_id"], "call_id": "shared-answer-1"}
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_caller_identity_cannot_change_after_approval(self):
        self.caller()
        self.request["report_identity"]["ai"] = "claude"
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_old_template_cannot_authorize_new_owner(self):
        approval = loads(self.approval.read_bytes())
        approval["template_version"] = "m5-v3"
        self.approval.write_bytes(json.dumps(approval).encode())
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_harness_identity_exact_answer_and_idempotent_repair(self):
        result = self.run_loop()
        path = Path(result["dialog"]["path"])
        meta = record_block(path)
        self.assertEqual((meta["ai"], meta["model"]), ("harness", "none"))
        self.assertEqual(result["answer_kind"], "user_answer")
        before = path.read_bytes()
        for _ in range(2):
            repaired = repair_recording(self.root, "run-1")
            self.assertTrue(repaired["dialog"]["saved"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)
        source = {"kind": "dialog", "path": str(path), "sha256": digest(before), "call_id": meta["call_id"]}
        question = (Path(result["run_path"]) / "final/question.txt").read_bytes().decode()
        self.assertIn(result["answer"], resolve_source(source, question, self.workspace))

    def test_caller_does_not_save_summary_even_during_repair(self):
        self.caller()
        with patch.object(finalize_run, "run_process", side_effect=AssertionError("saver called")):
            result = self.run_loop(saver_runner=lambda *a, **k: self.fail("saver called"))
            repaired = repair_recording(self.root, "run-1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["answer_kind"], "execution_summary")
        self.assertEqual(result["recording_status"], "pending_caller")
        self.assertFalse(result["dialog"]["saved"])
        self.assertEqual(repaired["state"]["recording_status"], "pending_caller")
        self.assertFalse((self.root / "dialog").exists())
        self.assertFalse(list((Path(result["run_path"]) / "final").glob("save-attempt-*.json")))
        with patch.object(inspect_run, "HARNESS_ROOT", self.root), redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_run.main(["run-1", "--retry-recording"]), 0)

    def test_caller_final_answer_saved_once_and_linked(self):
        self.caller()
        result = self.run_loop()
        answer = "사용자에게 전달할 최종 답변\r\n실행: run-1\n"
        path = self.save_caller(result, answer=answer)
        # A lost save response is retried with the same handed-off call_id.
        self.assertEqual(self.save_caller(result, answer=answer), path)
        calls = list(self.agent.calls)
        for _ in range(2):
            linked = attach_caller_dialog(self.root, "run-1", path)
            self.assertEqual(linked["state"]["recording_status"], "saved")
            self.assertEqual(linked["state"]["status"], "success")
        check = loads((Path(result["run_path"]) / "final/input-dialog-check.json").read_bytes())
        self.assertEqual(check["answer_sha256"], digest(answer.encode()))
        self.assertNotEqual(check["answer_sha256"], digest(result["answer"].encode()))
        self.assertTrue(repair_recording(self.root, "run-1")["dialog"]["saved"])
        self.assertEqual(self.agent.calls, calls)
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)

    def test_wrong_call_run_or_identity_cannot_be_attached(self):
        self.caller()
        result = self.run_loop()
        for key, value in (("call_id", "wrong-call"), ("run_id", "wrong-run"),
                           ("ai", "claude"), ("model", "wrong-model")):
            path = self.save_caller(result, **{key: value})
            with self.subTest(key=key), self.assertRaises(ContractError):
                attach_caller_dialog(self.root, "run-1", path)
            path.unlink()  # Isolated temporary malformed fixture, not a project record.
        self.assertEqual(inspect_run.inspect(self.root, "run-1")["state"]["recording_status"], "pending_caller")

    def test_changed_question_or_content_hash_cannot_be_attached(self):
        self.caller()
        result = self.run_loop()
        path = self.save_caller(result)
        original = path.read_bytes()
        for old, new in ((b"Implement addition.", b"Different question."),
                         ("실제 사용자 답변".encode(), "변조된 사용자 답변".encode())):
            path.write_bytes(original.replace(old, new))
            with self.assertRaises(ContractError):
                attach_caller_dialog(self.root, "run-1", path)
        path.write_bytes(original)
        self.assertTrue(attach_caller_dialog(self.root, "run-1", path)["dialog"]["saved"])

    def test_external_or_symlink_dialog_is_rejected(self):
        self.caller()
        result = self.run_loop()
        path = self.save_caller(result)
        outside = self.root / "external.txt"
        outside.write_bytes(path.read_bytes())
        link = self.root / "dialog/link.txt"
        link.symlink_to(path)
        for candidate in (outside, link):
            with self.assertRaises(ContractError):
                attach_caller_dialog(self.root, "run-1", candidate)

    def test_harness_mode_rejects_caller_attachment(self):
        result = self.run_loop()
        with self.assertRaises(ContractError):
            attach_caller_dialog(self.root, "run-1", result["dialog"]["path"])

    def test_active_run_cannot_attach_a_caller_dialog(self):
        self.caller()
        store = RunStore(self.root, self.request)
        store.close()
        with self.assertRaises(ContractError):
            attach_caller_dialog(self.root, "run-1", self.root / "dialog/absent.txt")

    def test_cli_reports_corrupt_record_and_attaches_valid_record(self):
        self.caller()
        result = self.run_loop()
        path = self.save_caller(result)
        original = path.read_bytes()
        path.write_bytes(original.split(b"model: ")[0])
        with patch.object(inspect_run, "HARNESS_ROOT", self.root), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(inspect_run.main(["run-1", "--attach-caller-dialog", str(path)]), 1)
        self.assertIn(str(path), json.loads(output.getvalue())["error"])
        path.write_bytes(original)
        with patch.object(inspect_run, "HARNESS_ROOT", self.root), redirect_stdout(io.StringIO()):
            self.assertEqual(inspect_run.main(["run-1", "--attach-caller-dialog", str(path)]), 0)

    def test_caller_failed_or_waiting_outcome_is_not_hidden(self):
        self.caller()
        for index, mode, expected in ((1, "scope_violation", "failed"), (2, "needs_user", "waiting_for_user")):
            self.request["run_id"] = f"run-{index}"
            self.approve()
            result = self.run_loop(mode, saver_runner=lambda *a, **k: self.fail("saver called"))
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["recording_status"], "pending_caller")
        self.assertFalse((self.root / "dialog").exists())

    def test_source_kind_does_not_choose_owner(self):
        for index, (kind, owner) in enumerate((("manual_import", "harness"), ("direct_stdin", "caller")), 1):
            self.request["run_id"] = f"run-{index}"
            self.input_ref = capture("Explicit owner is independent of input transport.", root=self.root, source_kind=kind)
            self.request["input_ref"] = self.input_ref
            self.request.update(dialog_owner=owner, report_identity={"ai": "harness", "model": "none"} if owner == "harness"
                                else {"ai": "codex", "model": "unknown"},
                                caller_dialog=None if owner == "harness" else {
                                    "message_id": loads(Path(self.input_ref).read_bytes())["source"]["message_id"],
                                    "call_id": "source-kind-caller"})
            self.approve()
            result = self.run_loop()
            self.assertEqual(result["dialog"]["saved"], owner == "harness")

    def test_caller_crash_recovery_preserves_handoff_without_saver(self):
        self.caller()
        intake = loads(Path(self.input_ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        bind_caller_dialog(self.root, self.request, message)
        store = RunStore(self.root, self.request)
        commit(store.path / "input.json", message)
        store.close()
        recovered = recover(self.root, "run-1")
        self.assertEqual(recovered["state"]["status"], "failed")
        result = repair_recording(self.root, "run-1")
        self.assertEqual(result["state"]["recording_status"], "pending_caller")
        self.assertFalse(result["model_reexecuted"])
        path = self.save_caller(result)
        attached = attach_caller_dialog(self.root, "run-1", path)
        self.assertEqual(attached["state"]["status"], "failed")

    def test_partial_attachment_is_repaired_without_model_or_save(self):
        self.caller()
        result = self.run_loop()
        path = self.save_caller(result)
        def fail_check(path, value, **kwargs):
            if path.name == "input-dialog-check.json":
                raise OSError("injected crash after receipt")
            return commit(path, value, **kwargs)
        with patch.object(finalize_run, "commit", side_effect=fail_check), self.assertRaises(OSError):
            attach_caller_dialog(self.root, "run-1", path)
        calls = list(self.agent.calls)
        repaired = repair_recording(self.root, "run-1")
        self.assertTrue(repaired["dialog"]["saved"])
        self.assertEqual(self.agent.calls, calls)
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)

    def test_frozen_owner_cannot_be_downgraded_to_legacy(self):
        self.caller()
        result = self.run_loop()
        path = Path(result["run_path"]) / "final/dialog-request.json"
        receipt = loads(path.read_bytes())
        receipt.pop("dialog_owner")
        receipt["schema_version"] = 1
        path.write_bytes(json.dumps(receipt).encode())
        with self.assertRaises(ContractError):
            repair_recording(self.root, "run-1")
        self.assertFalse((self.root / "dialog").exists())

    def test_legacy_run_retains_original_save_identity(self):
        legacy = copy.deepcopy(self.request)
        legacy.pop("dialog_owner")
        legacy.pop("caller_dialog")
        legacy["report_identity"] = {"ai": "codex", "model": "unknown"}
        store = RunStore(self.root, legacy)
        intake = loads(Path(self.input_ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        prepare_final(store, legacy, message, "failed", "interrupted_unconfirmed", "legacy test")
        store.finish("failed", "interrupted_unconfirmed")
        store.close()
        result = repair_recording(self.root, "run-1")
        meta = record_block(result["dialog"]["path"])
        self.assertEqual((meta["ai"], meta["model"]), ("codex", "unknown"))

    def test_saver_rejects_harness_with_model_or_missing_run(self):
        q, a = self.root / "q.txt", self.root / "a.txt"
        q.write_text("question"); a.write_text("answer")
        base = [sys.executable, "-B", str(self.root / "modules/save_dialog.py"), "--ai", "harness",
                "--question-file", str(q), "--answer-file", str(a), "--call-id", "invalid-harness"]
        for args in (("--model", "a-model", "--run-id", "run-1"), ("--model", "none")):
            process = run_process([*base, *args], cwd=self.root, timeout_seconds=10)
            self.assertEqual(process.returncode, 1)
        self.assertFalse((self.root / "dialog").exists())

    def test_two_runs_share_one_final_answer_and_repair_discovers_it(self):
        self.caller()
        first = self.run_loop()
        self.request.update(run_id="run-2", parent_run_id="run-1")
        self.approve()
        second = self.run_loop()
        self.assertEqual(first["dialog"]["call_id"], second["dialog"]["call_id"])
        self.assertIsNone(first["dialog"]["dialog_run_id"])
        answer = "한 질문에 대한 두 실행의 결과를 종합한 최종 답변입니다.\n"
        path = self.save_caller(first, answer=answer)
        self.assertNotIn("run_id", record_block(path))
        self.assertTrue(attach_caller_dialog(self.root, "run-1", path)["dialog"]["saved"])
        # A caller interrupted before the second attachment can repair the link only.
        linked = repair_recording(self.root, "run-2")
        self.assertEqual(linked["dialog"]["path"], str(path))
        self.assertEqual(linked["state"]["recording_status"], "saved")
        self.assertFalse(linked["model_reexecuted"])
        self.assertEqual(self.save_caller(second, answer=answer), path)
        self.assertEqual(len(list((self.root / "dialog").glob("dialog_*.txt"))), 1)
        for run_id in ("run-1", "run-2"):
            self.assertEqual(inspect_run.inspect(self.root, run_id)["state"]["recording_status"], "saved")
            check = loads((self.root / "runs" / run_id / "final/input-dialog-check.json").read_bytes())
            self.assertEqual(check["answer_sha256"], digest(answer.encode()))

    def test_shared_dialog_reference_requires_approval(self):
        self.caller()
        self.request["caller_dialog"]["call_id"] = "unapproved-answer"
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)
        self.assertFalse((self.root / "runs").exists())

    def test_dialog_reference_must_match_original_message(self):
        self.caller()
        self.request["caller_dialog"]["message_id"] = "different-message"
        self.approve()
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_one_input_cannot_be_bound_to_two_answer_ids(self):
        self.caller()
        self.run_loop()
        self.request["run_id"] = "run-2"
        self.request["caller_dialog"]["call_id"] = "another-answer"
        self.approve()
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)
        self.assertFalse((self.root / "runs/run-2").exists())

    def test_one_answer_cannot_be_reused_for_distinct_intakes_with_equal_text(self):
        self.caller()
        first = self.run_loop()
        original = (Path(first["run_path"]) / "final/question.txt").read_bytes().decode()
        self.input_ref = capture(original, root=self.root)
        self.request.update(run_id="run-2", input_ref=self.input_ref)
        self.request["caller_dialog"]["message_id"] = loads(Path(self.input_ref).read_bytes())["source"]["message_id"]
        self.approve()
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_shared_dialog_identity_cannot_change_between_runs(self):
        self.caller()
        self.run_loop()
        self.request["run_id"] = "run-2"
        self.request["report_identity"]["ai"] = "claude"
        self.approve()
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_closed_dialog_cannot_start_another_run(self):
        self.caller()
        first = self.run_loop()
        self.save_caller(first)
        self.request["run_id"] = "run-2"
        self.approve()
        with self.assertRaises(ContractError):
            self.run_loop()
        self.assertFalse(self.agent.calls)

    def test_legacy_caller_recovery_still_uses_its_run_id(self):
        self.caller()
        legacy = copy.deepcopy(self.request)
        legacy.pop("caller_dialog")
        store = RunStore(self.root, legacy)
        intake = loads(Path(self.input_ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        prepare_final(store, legacy, message, "failed", "interrupted_unconfirmed", "legacy caller")
        store.finish("failed", "interrupted_unconfirmed")
        store.close()
        result = repair_recording(self.root, "run-1")
        self.assertEqual(result["dialog"]["dialog_run_id"], "run-1")
        path = self.save_caller(result)
        self.assertEqual(record_block(path)["run_id"], "run-1")
        self.assertTrue(attach_caller_dialog(self.root, "run-1", path)["dialog"]["saved"])

    def test_model_none_is_rejected_for_callers_and_agent_assignments(self):
        self.caller()
        self.request["report_identity"]["model"] = "none"
        with self.assertRaises(ContractError):
            validate_run(self.request, self.root)
        self.request["report_identity"]["model"] = "unknown"
        self.request["assignments"]["implementer"]["model"] = "none"
        with self.assertRaises(ContractError):
            validate_run(self.request, self.root)

    def test_saver_rejects_model_none_for_both_agents(self):
        q, a = self.root / "q.txt", self.root / "a.txt"
        q.write_text("question"); a.write_text("answer")
        for ai in ("claude", "codex"):
            argv = [sys.executable, "-B", str(self.root / "modules/save_dialog.py"), "--ai", ai,
                    "--model", "none", "--question-file", str(q), "--answer-file", str(a), "--call-id", "bad-none"]
            result = run_process(argv, cwd=self.root, timeout_seconds=10)
            self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / "dialog").exists())
