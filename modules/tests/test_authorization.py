"""Approval evidence must be verifiable before any model or preflight is run."""
import hashlib
import json

from modules.harness.authorization import resolve_source
from modules.harness.contracts import ContractError
from modules.save_dialog import SUB, build, content_id
from modules.tests.test_agent_calls import HarnessCase


class AuthorizationTests(HarnessCase):
    def rejected(self, registry):
        result = self.call(registry=registry, runner=lambda *a, **k: self.fail("spawned"))
        self.assertEqual(result.result["call_status"], "invalid_request")
        self.assertFalse((self.harness / "runs").exists())

    def test_source_required(self):
        registry = self.registry()
        del registry["user-1"]["source"]
        self.rejected(registry)

    def test_malformed_source_types_rejected(self):
        for source in (None, [], {}, {"kind": []}, {"kind": {}}):
            with self.subTest(source=source):
                registry = self.registry()
                registry["user-1"]["source"] = source
                self.rejected(registry)

    def test_changed_source_rejected(self):
        registry = self.registry()
        self.source.write_text(self.source.read_text() + "\n")
        self.rejected(registry)

    def test_message_role_identifier_and_instruction_must_match(self):
        original = json.loads(self.source.read_bytes())
        for field, value in (("role", "assistant"), ("message_id", "another-message"),
                             ("content", "Review only."), ("schema_version", True)):
            with self.subTest(field=field):
                self.source.write_text(json.dumps({**original, field: value}))
                self.rejected(self.registry())

    def test_target_owned_source_including_symlink_rejected(self):
        target = self.workspace / "source.json"
        target.write_bytes(self.source.read_bytes())
        link = self.harness / "linked-source.json"
        link.symlink_to(target)
        for path in (target, link):
            with self.subTest(path=path):
                registry = self.registry()
                registry["user-1"]["source"]["path"] = str(path)
                self.rejected(registry)

    def test_snapshot_is_exact_and_source_in_result(self):
        # Preserve non-ASCII and CRLF bytes, not a reserialized message.
        self.source.write_bytes(self.source.read_bytes().replace(b'"schema_version"', b'\r\n"schema_version"'))
        registry = self.registry()
        outcome = self.call(registry=registry)
        self.assertEqual(outcome.result["call_status"], "ok", outcome.result)
        self.assertEqual((outcome.call_dir / "authorization-source.txt").read_bytes(), self.source.read_bytes())
        self.assertEqual(outcome.result["authorization_source"], registry["user-1"]["source"])
        self.assertEqual(json.loads((outcome.call_dir / "authorization.json").read_bytes()), registry["user-1"])
        self.assertIn("message-1", (outcome.call_dir / "prompt.txt").read_text())

    def dialog_source(self, question, answer):
        meta = {"ai": "codex", "model": "unknown", "asked": "unknown", "saved": "unknown",
                "call_id": "approval-dialog", "content_id": content_id("codex", "unknown", None, question, answer)}
        self.source.write_text(build(question, answer, meta), encoding="utf-8")
        return {"kind": "dialog", "path": str(self.source), "call_id": "approval-dialog",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}

    def test_dialog_with_embedded_delimiters_is_unambiguous(self):
        question = "Proceed.\n" + SUB + "\n[ANSWER]\nThis is still the user question."
        answer = "Example:\n" + SUB + "\n[QUESTION]\nquoted."
        source = self.dialog_source(question, answer)
        self.assertEqual(resolve_source(source, question, self.workspace), self.source.read_text())
        with self.assertRaises(ContractError):
            resolve_source(source, "Proceed.", self.workspace)

    def test_dialog_identifier_and_body_integrity(self):
        question = "Implement and test M3."
        source = self.dialog_source(question, "Acknowledged.")
        with self.assertRaises(ContractError):
            resolve_source({**source, "call_id": "wrong"}, question, self.workspace)
        self.source.write_text(self.source.read_text().replace("Acknowledged.", "Modified answer."))
        source["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.assertRaises(ContractError):
            resolve_source(source, question, self.workspace)

    def test_missing_invalid_and_oversized_sources_rejected(self):
        for raw in (b"not-json", b"\xff", b" " * (2 * 1024 * 1024 + 1)):
            with self.subTest(length=len(raw)):
                self.source.write_bytes(raw)
                self.rejected(self.registry())
        registry = self.registry()
        self.source.unlink()
        self.rejected(registry)
