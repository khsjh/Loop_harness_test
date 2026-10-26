"""Secret hook and installer checks in isolated local Git repositories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
REAL_GITLEAKS = shutil.which("gitleaks")


class HookCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hook-check-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repository with spaces"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITLEAKS_"))}
        self.env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Hook Test", GIT_AUTHOR_EMAIL="hook@example.invalid",
                        GIT_COMMITTER_NAME="Hook Test", GIT_COMMITTER_EMAIL="hook@example.invalid")
        self.git("init", "--quiet", "--template=")
        for name in ("modules/security/pre-commit", "modules/security/install_hooks.py", ".gitleaks.toml"):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.hook = self.repo / "modules/security/pre-commit"
        self.installer = self.repo / "modules/security/install_hooks.py"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "git").symlink_to(shutil.which("git"))
        self.fake = self.bin / "gitleaks"
        self.fake.write_text('#!/bin/sh\nif [ "$1" = version ]; then echo v8.30.1; exit 0; fi\nexit 0\n')
        self.fake.chmod(0o755)
        self.fake_env = dict(self.env, PATH=str(self.bin))

    def run_cmd(self, argv, *, cwd=None, env=None):
        return subprocess.run(argv, cwd=cwd or self.repo, env=env or self.env,
                              capture_output=True, text=True, timeout=30)

    def git(self, *args, env=None, check=True):
        result = self.run_cmd([shutil.which("git"), "-c", "commit.gpgsign=false", *args], env=env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def install(self, *args, env=None, cwd=None):
        return self.run_cmd([sys.executable, "-B", str(self.installer), *args],
                            env=env or self.fake_env, cwd=cwd)

    def configure(self):
        self.git("config", "--local", "core.hooksPath", "modules/security")

    def commit(self, *, env=None):
        return self.git("commit", "--quiet", "--allow-empty", "-m", "Synthetic check", env=env, check=False)


class InstallerTests(HookCase):
    def test_check_is_read_only_and_install_is_idempotent(self):
        config = self.repo / ".git/config"
        original = config.read_bytes()
        checked = self.install("--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Ready to install", checked.stdout)
        self.assertEqual(config.read_bytes(), original)
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath").stdout.strip(), "modules/security")
        after = config.read_bytes()
        again = self.install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Already installed", again.stdout)
        self.assertEqual(config.read_bytes(), after)

    def test_default_hook_conflict_preserved(self):
        hooks = self.repo / ".git/hooks"
        hooks.mkdir()
        old = hooks / "pre-commit"
        old.write_text("#!/bin/sh\nexit 0\n")
        old.chmod(0o755)
        original = (self.repo / ".git/config").read_bytes()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing files", result.stderr)
        self.assertEqual((self.repo / ".git/config").read_bytes(), original)
        self.assertEqual(old.read_text(), "#!/bin/sh\nexit 0\n")

    def test_sample_hooks_do_not_block_installation(self):
        hooks = self.repo / ".git/hooks"
        hooks.mkdir()
        (hooks / "pre-commit.sample").write_text("sample")
        self.assertEqual(self.install().returncode, 0)

    def test_local_and_global_hook_path_conflicts_preserved(self):
        for scope in ("local", "global"):
            with self.subTest(scope=scope):
                global_file = self.base / "global-config"
                env = dict(self.fake_env, GIT_CONFIG_GLOBAL=str(global_file))
                self.git("config", "--" + scope, "core.hooksPath", "existing hooks", env=env)
                original = (self.repo / ".git/config").read_bytes()
                result = self.install(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Existing core.hooksPath", result.stderr)
                self.assertEqual((self.repo / ".git/config").read_bytes(), original)
                self.git("config", "--" + scope, "--unset", "core.hooksPath", env=env)

    def test_missing_gitleaks_does_not_modify_config(self):
        self.fake.unlink()
        original = (self.repo / ".git/config").read_bytes()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Gitleaks is required", result.stderr)
        self.assertEqual((self.repo / ".git/config").read_bytes(), original)

    def test_non_executable_hook_and_invalid_config_rejected(self):
        original = (self.repo / ".git/config").read_bytes()
        self.hook.chmod(0o644)
        self.assertNotEqual(self.install().returncode, 0)
        self.hook.chmod(0o755)
        (self.repo / ".gitleaks.toml").write_text("invalid = [\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid TOML", result.stderr)
        self.assertEqual((self.repo / ".git/config").read_bytes(), original)

    def test_missing_repository_is_not_initialized(self):
        shutil.rmtree(self.repo / ".git")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an accessible Git", result.stderr)
        self.assertFalse((self.repo / ".git").exists())

    def test_install_from_subdirectory_and_move_checkout(self):
        result = self.install(cwd=self.repo / "modules")
        self.assertEqual(result.returncode, 0, result.stderr)
        renamed = self.base / "renamed checkout"
        self.repo.rename(renamed)
        self.repo = renamed
        self.installer = self.repo / "modules/security/install_hooks.py"
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.commit(env=self.fake_env).returncode, 0)

    def test_parent_repository_not_mistaken_for_project(self):
        shutil.rmtree(self.repo / ".git")
        self.run_cmd([shutil.which("git"), "init", "--quiet", "--template="], cwd=self.base)
        original = (self.base / ".git/config").read_bytes()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("working-tree root", result.stderr)
        self.assertEqual((self.base / ".git/config").read_bytes(), original)


class HookFailureTests(HookCase):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_missing_gitleaks_blocks_actual_commit(self):
        self.fake.unlink()
        result = self.commit(env=self.fake_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Gitleaks is required", result.stderr)

    def test_missing_config_blocks_actual_commit(self):
        (self.repo / ".gitleaks.toml").unlink()
        result = self.commit(env=self.fake_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unreadable", result.stderr)

    def test_scanner_errors_block_actual_commit(self):
        for code in (1, 2, 127):
            with self.subTest(code=code):
                self.fake.write_text(f"#!/bin/sh\nexit {code}\n")
                result = self.commit(env=self.fake_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Secret scan failed", result.stderr)

    def test_not_a_repository_fails(self):
        result = self.run_cmd([str(self.hook)], cwd=self.base, env=self.fake_env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot find", result.stderr)

    def test_corrupt_index_blocks_even_if_scanner_would_succeed(self):
        index = self.base / "bad-index"
        index.write_bytes(b"corrupt")
        env = dict(self.fake_env, GIT_INDEX_FILE=str(index))
        result = self.run_cmd([str(self.hook)], env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read the staged changes", result.stderr)
        self.assertEqual(index.read_bytes(), b"corrupt")


@unittest.skipUnless(REAL_GITLEAKS, "Gitleaks required for real scanner integration")
class GitleaksIntegrationTests(HookCase):
    def setUp(self):
        super().setUp()
        self.fake.unlink()
        self.fake.symlink_to(REAL_GITLEAKS)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.sample = self.repo / "sample.txt"
        # Generated synthetic fixture, never a credential obtained from a service.
        self.token = "ghp_" + uuid.uuid4().hex + "A1b2"

    def stage(self, secret=False):
        self.sample.write_text('synthetic_token = "' + self.token + '"\n' if secret else "safe content\n")
        self.git("add", "--", "sample.txt")

    def assert_blocked(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Secret scan failed", result.stderr)
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_clean_initial_commit_and_no_changes_pass(self):
        self.stage()
        self.assertEqual(self.commit(env=self.fake_env).returncode, 0)
        self.assertEqual(self.commit(env=self.fake_env).returncode, 0)

    def test_secret_blocks_initial_commit(self):
        self.stage(secret=True)
        self.assert_blocked(self.commit(env=self.fake_env))
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_partial_staging_blocks_after_working_file_cleaned(self):
        self.stage(secret=True)
        self.sample.write_text("clean working file\n")
        before_index = (self.repo / ".git/index").read_bytes()
        before_file = self.sample.read_bytes()
        result = self.run_cmd([str(self.hook)], env=self.fake_env, cwd=self.repo / "modules")
        self.assert_blocked(result)
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual(self.sample.read_bytes(), before_file)
        self.assert_blocked(self.commit(env=self.fake_env))

    def test_unstaged_secret_does_not_block_clean_index(self):
        self.stage()
        self.sample.write_text('synthetic_token = "' + self.token + '"\n')
        result = self.commit(env=self.fake_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "HEAD:sample.txt").stdout, "safe content\n")

    def test_secret_after_initial_commit_blocks(self):
        self.stage()
        self.assertEqual(self.commit(env=self.fake_env).returncode, 0)
        original_head = self.git("rev-parse", "HEAD").stdout
        self.stage(secret=True)
        self.assert_blocked(self.commit(env=self.fake_env))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, original_head)

    def test_invalid_configuration_blocks_commit(self):
        self.stage()
        (self.repo / ".gitleaks.toml").write_text("invalid = [\n")
        self.assert_blocked(self.commit(env=self.fake_env))

    def test_alternate_relative_index_preserved_from_subdirectory(self):
        self.stage()
        original_index = (self.repo / ".git/index").read_bytes()
        alternate = self.base / "alternate index"
        shutil.copy2(self.repo / ".git/index", alternate)
        self.sample.write_text('synthetic_token = "' + self.token + '"\n')
        self.git("add", "--", "sample.txt", env=dict(self.env, GIT_INDEX_FILE=str(alternate)))
        before = alternate.read_bytes()
        cwd = self.repo / "modules"
        env = dict(self.fake_env, GIT_INDEX_FILE=os.path.relpath(alternate, cwd))
        self.assert_blocked(self.run_cmd([str(self.hook)], cwd=cwd, env=env))
        self.assertEqual(alternate.read_bytes(), before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), original_index)

    def test_git_commit_path_uses_git_temporary_index(self):
        self.stage()
        self.assertEqual(self.commit(env=self.fake_env).returncode, 0)
        # Only the named file enters this commit, from a temporary Git index.
        self.sample.write_text('synthetic_token = "' + self.token + '"\n')
        result = self.git("commit", "--quiet", "-m", "Synthetic path commit", "--", "sample.txt",
                          env=self.fake_env, check=False)
        self.assert_blocked(result)


if __name__ == "__main__":
    unittest.main()
