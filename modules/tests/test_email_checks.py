"""Synthetic identity and outgoing-history checks using real local Git hooks."""
import json
import os
from pathlib import Path
import subprocess
import sys

from modules.tests.test_secret_checks import HookCase


PUBLIC = "123+hook-test@users.noreply.github.com"
PRIVATE = "private-person@example.invalid"
WEB_COMMITTER = "noreply@github.com"


class EmailPolicyTests(HookCase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.checker = self.repo / "modules/security/check_emails.py"
        self.remote = self.base / "remote.git"
        self.git("init", "--quiet", "--bare", "--template=", str(self.remote))
        self.number = 0

    def check(self, *args, env=None, input=None):
        return subprocess.run([sys.executable, "-B", str(self.checker), *args], cwd=self.repo,
                              env=env or self.fake_env, input=input, text=True,
                              capture_output=True, timeout=30)

    def make_commit(self, *, author=PUBLIC, committer=PUBLIC, filename="sample.txt"):
        self.number += 1
        (self.repo / filename).write_text(str(self.number))
        self.git("add", "--", filename)
        env = dict(self.env, GIT_AUTHOR_EMAIL=author, GIT_COMMITTER_EMAIL=committer)
        # Deliberately seed legacy commits to verify that pre-push catches them.
        self.git("-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Synthetic history", env=env)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def push(self, *refspecs):
        return self.git("push", "--porcelain", str(self.remote),
                        *(refspecs or ("HEAD:refs/heads/main",)), env=self.fake_env, check=False)

    def assert_private_blocked(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('"email_policy": "blocked"', result.stdout + result.stderr)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_real_commit_blocks_private_author_and_committer(self):
        for role in ("AUTHOR", "COMMITTER"):
            with self.subTest(role=role):
                result = self.commit(env=dict(self.fake_env, **{"GIT_" + role + "_EMAIL": PRIVATE}))
                self.assert_private_blocked(result)
                self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_effective_environment_overrides_config_and_author_option(self):
        self.git("config", "user.email", PUBLIC)
        self.assert_private_blocked(self.commit(env=dict(self.fake_env, GIT_AUTHOR_EMAIL=PRIVATE)))
        result = self.git("commit", "--allow-empty", "--author", "Synthetic <" + PRIVATE + ">",
                          "-m", "Synthetic author override", env=self.fake_env, check=False)
        self.assert_private_blocked(result)

    def test_noreply_formats_pass_without_claiming_github_verification(self):
        for email in (PUBLIC, "hook-test@users.noreply.github.com",
                      "123+synthetic[bot]@users.noreply.github.com"):
            with self.subTest(email=email):
                result = self.check("prospective", env=dict(self.fake_env, GIT_AUTHOR_EMAIL=email))
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["github_account_settings"], "not_checked")
                self.assertEqual(report["email_ownership"], "not_checked")
                self.assertEqual(report["secret_scan"], "not_run_by_this_check")

    def test_deceptive_noreply_domains_and_empty_emails_fail(self):
        for email in ("test@users.noreply.github.com.example.invalid", "test@noreply.github.com", ""):
            with self.subTest(email=email):
                result = self.check("prospective", env=dict(self.fake_env, GIT_AUTHOR_EMAIL=email))
                self.assertNotEqual(result.returncode, 0)
                if email:
                    self.assertNotIn(email, result.stdout + result.stderr)

    def test_explicit_repository_allowance_and_global_allowance_ignored(self):
        global_file = self.base / "global-config"
        env = dict(self.fake_env, GIT_CONFIG_GLOBAL=str(global_file), GIT_AUTHOR_EMAIL=PRIVATE)
        self.git("config", "--global", "harness.publicEmail", PRIVATE, env=env)
        self.assert_private_blocked(self.check("prospective", env=env))
        self.git("config", "--local", "--add", "harness.publicEmail", PRIVATE)
        result = self.check("prospective", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_invalid_allowance_fails_closed(self):
        self.git("config", "--local", "harness.publicEmail", "*")
        result = self.check("prospective")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["email_policy"], "error")

    def test_amend_checks_retained_author_and_reset_author_can_replace_it(self):
        before = self.make_commit(author=PRIVATE)
        result = self.git("commit", "--amend", "--no-edit", env=self.fake_env, check=False)
        self.assert_private_blocked(result)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)
        result = self.git("commit", "--amend", "--no-edit", "--reset-author", env=self.fake_env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "-s", "--format=%ae%n%ce", "HEAD").stdout.splitlines(), [PUBLIC, PUBLIC])

    def test_existing_range_blocks_older_private_commit_beneath_clean_tip(self):
        base = self.make_commit()
        self.assertEqual(self.push().returncode, 0)
        self.make_commit(author=PRIVATE)
        self.make_commit()
        self.assert_private_blocked(self.push())
        remote_head = self.git("--git-dir=" + str(self.remote), "rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(remote_head, base)

    def test_new_remote_branch_checks_full_ancestry_despite_tracking_refs(self):
        self.make_commit(committer=PRIVATE)
        head = self.make_commit()
        self.git("update-ref", "refs/remotes/elsewhere/main", head)
        self.assert_private_blocked(self.push())
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show-ref", check=False).returncode, 1)

    def test_safe_history_pushes_and_read_only_range_is_unchanged(self):
        base = self.make_commit()
        self.assertEqual(self.push().returncode, 0)
        tip = self.make_commit()
        index = (self.repo / ".git/index").read_bytes()
        result = self.check("range", "--base", base, "--head", tip)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["commits_checked"], 1)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), tip)
        self.assertEqual(self.push().returncode, 0)

    def test_all_refs_and_deletions(self):
        safe = self.make_commit()
        unsafe = self.make_commit(author=PRIVATE)
        zero = "0" * 40
        lines = f"refs/heads/a {safe} refs/heads/a {zero}\nrefs/heads/b {unsafe} refs/heads/b {zero}\n"
        self.assert_private_blocked(self.check("pre-push", input=lines))
        deleted = self.check("pre-push", input=f"(delete) {zero} refs/heads/main {unsafe}\n")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(deleted.stdout)["commits_checked"], 0)

    def test_merge_includes_side_branch_commits(self):
        self.make_commit()
        self.assertEqual(self.push().returncode, 0)
        branch = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()
        self.git("checkout", "-q", "-b", "feature")
        self.make_commit(author=PRIVATE, filename="feature.txt")
        self.git("checkout", "-q", branch)
        self.make_commit(filename="main.txt")
        self.git("merge", "--no-ff", "feature", "-m", "Synthetic merge", env=self.fake_env)
        self.assert_private_blocked(self.push())

    def test_annotated_tag_private_tagger_is_blocked(self):
        self.make_commit()
        self.git("-c", "tag.gpgsign=false", "tag", "-a", "v1", "-m", "Synthetic tag",
                 env=dict(self.env, GIT_COMMITTER_EMAIL=PRIVATE))
        result = self.push("refs/tags/v1:refs/tags/v1")
        self.assert_private_blocked(result)
        self.assertIn('"role": "tagger"', result.stdout)

    def test_lightweight_tag_cannot_bypass_commit_checks(self):
        self.make_commit(author=PRIVATE)
        self.git("tag", "v1")
        self.assert_private_blocked(self.push("refs/tags/v1:refs/tags/v1"))

    def test_unknown_remote_object_and_malformed_input_fail_closed(self):
        head = self.make_commit()
        for line in (f"refs/heads/main {head} refs/heads/main {'1' * 40}\n", "malformed\n"):
            result = self.check("pre-push", input=line)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["email_policy"], "error")

    def test_shallow_history_cannot_hide_older_commits(self):
        self.make_commit(author=PRIVATE)
        head = self.make_commit()
        (self.repo / ".git/shallow").write_text(head + "\n")
        result = self.check("range")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Shallow history", result.stdout)

    def test_replace_refs_cannot_hide_private_identity(self):
        safe = self.make_commit()
        unsafe = self.make_commit(author=PRIVATE)
        self.git("replace", unsafe, safe)
        self.assert_private_blocked(self.check("commit", unsafe))

    def test_missing_checker_and_python_block_commits_and_installation(self):
        self.checker.rename(self.checker.with_suffix(".saved"))
        self.assertNotEqual(self.commit(env=self.fake_env).returncode, 0)
        self.assertNotEqual(self.install().returncode, 0)
        self.checker.with_suffix(".saved").rename(self.checker)
        (self.bin / "python3").unlink()
        self.assertNotEqual(self.commit(env=self.fake_env).returncode, 0)
        self.assertNotEqual(self.install().returncode, 0)

    def test_installer_requires_executable_pre_push_hook(self):
        hook = self.repo / "modules/security/pre-push"
        hook.chmod(0o644)
        self.assertNotEqual(self.install().returncode, 0)
        hook.chmod(0o755)
        self.assertEqual(self.install().returncode, 0)

    def test_commit_check_does_not_change_or_print_existing_private_identity(self):
        head = self.make_commit(author=PRIVATE, committer=PRIVATE)
        index = (self.repo / ".git/index").read_bytes()
        self.assert_private_blocked(self.check("commit", head))
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_web_committer_allows_new_branch_with_full_history(self):
        self.make_commit(committer=WEB_COMMITTER)
        tip = self.make_commit()
        checked = self.check("range")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["commits_checked"], 2)
        self.assertEqual(self.push().returncode, 0)
        pushed = self.push("HEAD:refs/heads/new-branch")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse",
                                  "refs/heads/new-branch").stdout.strip(), tip)

    def test_web_committer_allows_new_lightweight_and_annotated_tags(self):
        self.make_commit(committer=WEB_COMMITTER)
        tip = self.make_commit()
        self.git("tag", "lightweight")
        self.git("-c", "tag.gpgsign=false", "tag", "-a", "annotated", "-m", "Synthetic public tag")
        for name in ("lightweight", "annotated"):
            with self.subTest(tag=name):
                pushed = self.push(f"refs/tags/{name}:refs/tags/{name}")
                self.assertEqual(pushed.returncode, 0, pushed.stderr)
                self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse",
                                          f"refs/tags/{name}^{{commit}}").stdout.strip(), tip)

    def test_web_committer_is_allowed_by_actual_pre_commit(self):
        result = self.commit(env=dict(self.fake_env, GIT_COMMITTER_EMAIL=WEB_COMMITTER))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "-s", "--format=%ae%n%ce", "HEAD").stdout.splitlines(),
                         [PUBLIC, WEB_COMMITTER])

    def test_web_address_exception_does_not_allow_author_or_tagger(self):
        self.make_commit()
        author = self.commit(env=dict(self.fake_env, GIT_AUTHOR_EMAIL=WEB_COMMITTER))
        self.assert_private_blocked(author)
        self.assertIn('"role": "author"', author.stdout + author.stderr)
        self.git("-c", "tag.gpgsign=false", "tag", "-a", "service-tagger", "-m", "Synthetic tag",
                 env=dict(self.env, GIT_COMMITTER_EMAIL=WEB_COMMITTER))
        tagger = self.push("refs/tags/service-tagger:refs/tags/service-tagger")
        self.assert_private_blocked(tagger)
        self.assertIn('"role": "tagger"', tagger.stdout)
        self.assertNotIn(WEB_COMMITTER, author.stdout + author.stderr + tagger.stdout + tagger.stderr)

    def test_web_committer_exception_does_not_allow_other_addresses(self):
        for email in ("test@github.com", "noreply@github.com.example.invalid",
                      "noreply@github.org", "NOREPLY@github.com", "test@noreply.github.com"):
            with self.subTest(email=email):
                result = self.check("prospective", env=dict(self.fake_env, GIT_COMMITTER_EMAIL=email))
                self.assert_private_blocked(result)
                self.assertNotIn(email, result.stdout + result.stderr)

    def test_web_committer_does_not_hide_a_private_author_in_history(self):
        self.make_commit(author=PRIVATE, committer=WEB_COMMITTER)
        self.make_commit(committer=WEB_COMMITTER)
        result = self.push()
        self.assert_private_blocked(result)
        self.assertIn('"role": "author"', result.stdout)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "show-ref", check=False).returncode, 1)

    def test_block_guidance_distinguishes_publication_states_without_claiming_detection(self):
        head = self.make_commit(author=PRIVATE)
        result = self.check("range")
        self.assert_private_blocked(result)
        self.assertIn("Publication status is not checked", result.stderr)
        self.assertIn("For already published history", result.stderr)
        self.assertIn("Do not automatically rewrite", result.stderr)
        self.assertIn("For unpublished commits", result.stderr)
        self.assertNotIn("Existing commits require a separately approved rewrite", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head)


if __name__ == "__main__":
    import unittest
    unittest.main()
