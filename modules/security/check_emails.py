"""Read-only Git identity policy checks; never query account settings or rewrite commits."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
EMAIL = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+\Z")
NOREPLY = re.compile(
    r"(?:[1-9][0-9]*\+)?[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?"
    r"(?:\[bot\])?@users\.noreply\.github\.com\Z", re.IGNORECASE)
GITHUB_WEB_COMMITTER = "noreply@github.com"
IDENT = re.compile(rb"[^\r\n]* <([^<>\r\n]*)> -?\d+ [+-]\d{4}\Z")


class CheckError(Exception):
    pass


class EmailCheck:
    def __init__(self):
        # Inspect the stored objects, even if local replace refs mask their identity.
        self.env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
        self.violations = []
        self.commits = set()
        self.tags = set()
        self.git("rev-parse", "--show-toplevel")  # Require a working tree.
        values = self.git("config", "--local", "--null", "--get-all",
                          "harness.publicEmail", missing_ok=True)
        self.allowed = set(values.decode().split("\0")[:-1])
        if any(not EMAIL.fullmatch(value) for value in self.allowed):
            raise CheckError("harness.publicEmail must contain exact, nonempty public email addresses.")

    def git(self, *args, missing_ok=False):
        result = subprocess.run(["git", *args], env=self.env, capture_output=True)
        if result.returncode and not (missing_ok and result.returncode == 1):
            # Git diagnostics and configuration can contain the private identity.
            raise CheckError("Git metadata could not be read; check the repository and requested revisions.")
        return result.stdout

    def identity(self, value, role, oid=None):
        match = IDENT.fullmatch(value)
        if not match:
            raise CheckError("Malformed or missing Git identity metadata.")
        email = match[1].decode("utf-8")
        # GitHub web commits use this service address. This is an email policy
        # exception, not proof that the commit was created or signed by GitHub.
        web_committer = role == "committer" and email == GITHUB_WEB_COMMITTER
        if not NOREPLY.fullmatch(email) and not web_committer and email not in self.allowed:
            self.violations.append({"role": role, "object": oid,
                                    "reason": "Email is not permitted for this role by the public email policy."})

    def prospective(self):
        for role in ("AUTHOR", "COMMITTER"):
            self.identity(self.git("var", "GIT_" + role + "_IDENT").rstrip(b"\n"), role.lower())

    def headers(self, kind, oid):
        data = self.git("cat-file", kind, oid)
        return data.split(b"\n\n", 1)[0].split(b"\n")

    def stored_identity(self, headers, role, oid):
        values = [line[len(role) + 1:] for line in headers if line.startswith(role.encode() + b" ")]
        if len(values) != 1:
            raise CheckError("Malformed or missing Git identity metadata.")
        self.identity(values[0], role, oid)

    def commit(self, oid):
        if oid in self.commits:
            return
        headers = self.headers("commit", oid)
        self.stored_identity(headers, "author", oid)
        self.stored_identity(headers, "committer", oid)
        self.commits.add(oid)

    def complete_history(self):
        if self.git("rev-parse", "--is-shallow-repository").strip() != b"false":
            raise CheckError("Shallow history cannot establish the full outgoing range; fetch complete history first.")
        grafts = Path(os.fsdecode(self.git("rev-parse", "--git-path", "info/grafts")).rstrip("\n"))
        if grafts.is_file() and grafts.read_bytes().strip():
            raise CheckError("Local grafts prevent a reliable history check; use an unmodified history view.")

    def resolve(self, revision):
        value = self.git("rev-parse", "--verify", "--end-of-options", revision).decode().strip()
        if not OID.fullmatch(value):
            raise CheckError("Invalid Git object identifier.")
        return value

    def peel(self, oid, *, inspect_tags):
        seen = set()
        while True:
            if oid in seen:
                raise CheckError("Cyclic Git tag objects are unsupported.")
            seen.add(oid)
            kind = self.git("cat-file", "-t", oid).strip()
            if kind == b"commit":
                return oid
            if kind != b"tag":
                raise CheckError("Only commits and tags pointing to commits are supported.")
            headers = self.headers("tag", oid)
            if inspect_tags and oid not in self.tags:
                self.stored_identity(headers, "tagger", oid)
                self.tags.add(oid)
            targets = [line[7:].decode() for line in headers if line.startswith(b"object ")]
            if len(targets) != 1 or not OID.fullmatch(targets[0]):
                raise CheckError("Malformed tag target.")
            oid = targets[0]

    def history(self, head, base=None):
        self.complete_history()
        tip = self.peel(head, inspect_tags=True)
        args = ["rev-list", tip]
        if base is not None:
            # The actual remote OID is the boundary, never a possibly stale tracking ref.
            boundary = self.peel(base, inspect_tags=False)
            args += ["^" + boundary]
        for oid in self.git(*args, "--").decode().splitlines():
            self.commit(oid)

    def push(self, stream):
        updates = []
        for line in stream:
            parts = line.split()
            if len(parts) != 4 or not OID.fullmatch(parts[1]) or not OID.fullmatch(parts[3]):
                raise CheckError("Malformed pre-push input.")
            updates.append((parts[1], parts[3]))
        for local, remote in updates:
            if set(local) == {"0"} or local == remote:
                continue  # Deletion or no update publishes no new commit.
            try:
                self.history(local, None if set(remote) == {"0"} else remote)
            except CheckError as exc:
                raise CheckError(str(exc) + " Fetch the destination history if its objects are missing.") from exc

    def report(self):
        return {"email_policy": "blocked" if self.violations else "passed",
                "commits_checked": len(self.commits), "tags_checked": len(self.tags),
                "violations": self.violations,
                "github_account_settings": "not_checked",
                "email_ownership": "not_checked", "secret_scan": "not_run_by_this_check"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prospective", help="check the effective author and committer for a new commit")
    commands.add_parser("pre-push", help="check all ref updates supplied by Git on stdin")
    one = commands.add_parser("commit", help="inspect a stored commit without changing it")
    one.add_argument("revision", nargs="?", default="HEAD")
    history = commands.add_parser("range", help="inspect every commit reachable from head but not base")
    history.add_argument("--head", default="HEAD")
    history.add_argument("--base", help="omit to inspect the entire history")
    args = parser.parse_args(argv)
    try:
        check = EmailCheck()
        if args.command == "prospective":
            check.prospective()
        elif args.command == "pre-push":
            check.push(sys.stdin)
        elif args.command == "commit":
            check.commit(check.peel(check.resolve(args.revision), inspect_tags=True))
        else:
            check.history(check.resolve(args.head), check.resolve(args.base) if args.base else None)
        report = check.report()
        print(json.dumps(report))
        if check.violations:
            print("Email policy blocked this operation. For new commits, configure a public noreply identity. "
                  "Review any explicit public-address allowance in repository-local harness.publicEmail. "
                  "Publication status is not checked. For already published history, review the policy "
                  "and coordinate any history change with collaborators. Do not automatically rewrite "
                  "published commits or force-push. For unpublished commits, replacing stored identity "
                  "requires a separately approved rewrite.", file=sys.stderr)
            return 1
        return 0
    except (CheckError, OSError, UnicodeError) as exc:
        # Never echo raw Git stderr, object contents, or the private email.
        message = str(exc) if isinstance(exc, CheckError) else "Email policy could not read the required metadata."
        print(json.dumps({"email_policy": "error", "error": message,
                          "github_account_settings": "not_checked", "email_ownership": "not_checked",
                          "secret_scan": "not_run_by_this_check"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
