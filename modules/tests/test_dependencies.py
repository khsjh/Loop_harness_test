"""Dependency installer failure boundaries; no network or model calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from modules.setup import dependencies as dep


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.spec = dep.load_manifest()
        self.temp = tempfile.TemporaryDirectory(prefix="dependency test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_manifest_pins_native_tools_and_python(self):
        self.assertEqual(set(self.spec["tools"]), {"codex", "claude", "gitleaks", "uv"})
        self.assertTrue(dep.python_supported(self.spec["python"]["install_version"], self.spec))

    def test_python_boundaries(self):
        for text, expected in [("Python 3.10.12", False), ("Python 3.11.0", True),
                               ("Python 3.12.13", True), ("Python 3.13.0", False)]:
            with self.subTest(text=text):
                self.assertEqual(dep.python_supported(text, self.spec), expected)

    def test_numeric_versions_not_lexicographic(self):
        self.assertGreater(dep.version("v8.30.1"), dep.version("v8.9.0"))
        self.assertEqual(dep.version("GNU bash, version 5.1.16(1)-release"), (5, 1, 16))
        with self.assertRaises(dep.SetupError):
            dep.version("unknown")

    def test_platform_rejects_other_architecture_and_old_libc(self):
        with patch.object(dep.platform, "system", return_value="Linux"), \
             patch.object(dep.platform, "machine", return_value="aarch64"):
            with self.assertRaises(dep.SetupError):
                dep.check_platform(self.spec)
        with patch.object(dep.platform, "system", return_value="Linux"), \
             patch.object(dep.platform, "machine", return_value="x86_64"), \
             patch.object(dep.platform, "libc_ver", return_value=("glibc", "2.31")):
            with self.assertRaises(dep.SetupError):
                dep.check_platform(self.spec)

    def test_malformed_manifest_rejected(self):
        for field, value in [("sha256", "bad"), ("version", "latest"), ("url", "http://example.invalid/a")]:
            spec = json.loads(json.dumps(self.spec))
            spec["tools"]["gitleaks"][field] = value
            with patch.object(Path, "read_text", return_value=json.dumps(spec)):
                with self.assertRaises(dep.SetupError):
                    dep.load_manifest()

    def test_dry_run_does_not_create_prefix(self):
        target = self.base / "absent destination"
        result = subprocess.run(["bash", str(dep.ROOT / "install.sh"),
                                 "--dry-run", "--prefix", str(target)],
                                cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["prefix"], str(target))
        self.assertFalse(target.exists())

    def test_subprocess_failure_is_not_accepted(self):
        with self.assertRaises(dep.SetupError):
            dep.run(["/bin/sh", "-c", "exit 2"])

    def test_uv_overrides_cannot_replace_pinned_downloads_or_destination(self):
        with patch.dict(os.environ, {"UV_PYTHON_INSTALL_DIR": "/outside",
                                     "UV_PYTHON_INSTALL_MIRROR": "https://example.invalid",
                                     "UV_PYTHON_DOWNLOADS_JSON_URL": "https://example.invalid/catalog"}):
            env = dep.python_install_env(self.base)
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(self.base / "python"))
        self.assertNotIn("UV_PYTHON_INSTALL_MIRROR", env)
        self.assertNotIn("UV_PYTHON_DOWNLOADS_JSON_URL", env)

    def test_checksum_mismatch_preserves_existing_binary(self):
        target = self.base / "tool"
        target.write_bytes(b"old binary")
        item = dict(self.spec["tools"]["gitleaks"], sha256="0" * 64)
        with patch.object(dep.urllib.request, "urlopen", return_value=io.BytesIO(b"bad download")):
            with self.assertRaisesRegex(dep.SetupError, "SHA256"):
                dep.download_binary(item, target)
        self.assertEqual(target.read_bytes(), b"old binary")
        self.assertEqual(list(self.base.iterdir()), [target])

    def archive(self, *, symlink=False):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            member = tarfile.TarInfo("tool")
            body = b"#!/bin/sh\necho 1.2.3\n"
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "/bin/sh"
            else:
                member.size = len(body)
            tar.addfile(member, None if symlink else io.BytesIO(body))
            # Even a malicious unrelated member is never extracted.
            other = tarfile.TarInfo("../escaped")
            other.size = 4
            tar.addfile(other, io.BytesIO(b"oops"))
        raw = data.getvalue()
        return raw, {"url": "https://example.invalid/test", "sha256": hashlib.sha256(raw).hexdigest(),
                     "member": "tool", "version": "1.2.3", "version_args": ["--version"]}

    def test_archive_extracts_only_requested_regular_member(self):
        raw, item = self.archive()
        target = self.base / "bin with spaces" / "tool"
        with patch.object(dep.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            dep.download_binary(item, target)
        self.assertEqual(dep.run([target, "--version"]).stdout.strip(), "1.2.3")
        self.assertFalse((self.base / "escaped").exists())
        self.assertTrue(os.access(target, os.X_OK))

    def test_archive_symlink_is_rejected(self):
        raw, item = self.archive(symlink=True)
        with patch.object(dep.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(dep.SetupError, "regular"):
                dep.download_binary(item, self.base / "tool")

    def test_downloaded_wrong_version_is_not_promoted(self):
        raw, item = self.archive()
        item["version"] = "1.2.4"
        target = self.base / "tool"
        target.write_text("original")
        with patch.object(dep.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(dep.SetupError, "wrong version"):
                dep.download_binary(item, target)
        self.assertEqual(target.read_text(), "original")

    def test_cli_version_mismatch_is_rejected(self):
        def fake_run(argv, **kwargs):
            name = Path(argv[0]).name
            item = {**self.spec["system"], **self.spec["tools"]}[name]
            v = "0.0.1" if name == "codex" else item.get("minimum", item.get("version"))
            return subprocess.CompletedProcess(argv, 0, v, "")
        with patch.object(dep, "executable", side_effect=lambda name, env: name), \
             patch.object(dep, "run", side_effect=fake_run):
            with self.assertRaisesRegex(dep.SetupError, "codex"):
                dep.check_versions(self.spec, {"PATH": "/bin"})

    def test_scanner_that_never_detects_is_rejected(self):
        scanner = self.base / "gitleaks"
        scanner.write_text("#!/bin/sh\nexit 0\n")
        scanner.chmod(0o755)
        with self.assertRaises(dep.SetupError):
            dep.scan_probe(dict(os.environ, PATH=str(self.base) + ":" + os.environ["PATH"]))

    def test_cli_flag_failure_propagates(self):
        from modules.harness.adapters.common import AdapterError
        with patch.object(dep, "check_platform"), patch.object(dep, "check_versions", return_value=[]), \
             patch.object(dep, "executable", side_effect=lambda name, env: name), \
             patch("modules.harness.run_agent_once.preflight", side_effect=AdapterError("unsupported_configuration", "missing_cli_flags", "flag missing")):
            with self.assertRaisesRegex(AdapterError, "flag missing"):
                dep.check(self.spec, dict(os.environ))

    def test_namespace_failure_is_not_skipped(self):
        from modules.harness.process_runner import run_process
        failed = run_process(["/bin/false"], cwd=self.base, timeout_seconds=5)
        with patch.object(dep, "check_platform"), patch.object(dep, "check_versions", return_value=[]), \
             patch.object(dep, "executable", side_effect=lambda name, env: name), \
             patch.object(dep, "scan_probe"), patch("modules.harness.run_agent_once.preflight"), \
             patch("modules.harness.verification.VerificationRunner.probe", return_value=failed):
            with self.assertRaisesRegex(dep.SetupError, "isolation probe failed"):
                dep.check(self.spec, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
