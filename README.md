# Loop Harness

**English** | [한국어](README.ko.md)

Loop Harness coordinates Claude and Codex CLI tools through an implementation → independent verification → cross-review loop. One agent implements an approved task, the harness runs the specified tests, and the other agent reviews the same code state. Failed tests or review findings can trigger another round within the approved limits.

The project records task authorization, execution evidence, and the final question and answer separately. It supports single agent calls, bounded loops, static preflight checks, execution reports, and recording recovery.

## Current scope

Implemented features include single calls, bounded implementation/review loops, approval checks, isolated verification, execution reports, and recording recovery. The regression suite contains 248 tests (including 16 dependency-installation, 22 secret-hook and 27 email-policy checks), plus 18 dialog-saver checks. See [validation and limitations](docs/validation.md) for results and supported environments.

Default ceilings are 6 rounds, 24 hours per run, and 6 hours per model call. These are configured limits; a 6-hour model call and 24-hour continuous operation have not been validated.

## Environment and setup

- Linux x86_64 with Python 3.12 is the tested environment. Windows, macOS, and other architectures are not currently supported by the verification runtime.
- The controller and hooks require Python >=3.11,<3.13 (`tomllib` needs 3.11). The harness uses only the Python standard library.
- Actual agent loops require `codex`, `claude`, and bubblewrap (`bwrap`) on `PATH`, valid CLI authentication, and access to the requested models.
- Verification requires Linux namespace support and the runtime files checked by preflight. Tests run with a read-only target workspace, isolated networking, and private temporary directories. Builds that must write into the target are outside the current verification scope.
- Bash is needed for the dialog-saver test script. Gitleaks is used separately for secret scanning.

Clone or copy the project, then open a terminal in its root directory. The directory name does not need to be `loop_harness`.

Install the dependencies together on Debian/Ubuntu, Linux x86_64 with glibc >=2.35 and bootstrap Python >=3.10:

```sh
bash install.sh
source "$HOME/.local/share/loop-harness/deps/env.sh"
bash install.sh --check
```

[modules/setup/dependencies.json](modules/setup/dependencies.json) records system/CLI versions and download checksums. There are no PyPI dependencies. The root `install.sh` invokes the implementation under `modules/setup/`. The installer uses apt for missing system packages (sudo may be needed), verifies SHA256 before installing pinned native binaries, and installs a separate CPython runtime through pinned uv. Node.js/npm are not required. Existing CLI installations, shell startup files, login credentials, and Git settings are not changed. Git hooks are connected separately using the instructions below.

Use `--dry-run` to inspect the manifest and destination, or `--prefix '/path/with spaces/deps'` to choose a destination. Source that destination's `env.sh`. `--check` checks the **current PATH**, regardless of `--prefix`; it creates and removes temporary probes without downloading or changing settings. A failed check returns nonzero, including when namespaces are unavailable. An interrupted installation retains downloaded tools for retry; success is reported only after all checks pass. See [installation and version policy](docs/usage.md#의존성-일괄-설치와-호환성-검사).

Run these initial commands from the project root:

```sh
python3 --version
command -v codex claude bwrap
python3 -B -m modules.harness.run_loop --show-defaults
python3 -B -m modules.harness.preflight_run --help
```

The last two commands do not call a model. They show policy defaults and command usage, rather than validate a real execution request.

## Prepare and run a task

Read [AGENTS.md](AGENTS.md) before working with agents. It is the authoritative instruction set. The detailed [operating guide](docs/usage.md) describes request fields, approval records, runtime paths, and dialog ownership. The detailed guides are currently in Korean.

1. Prepare a target workspace and protected tests. Keep the controller's `approvals/`, `runs/`, and `dialog/` directories outside that workspace. For an initial trial, use a separate working copy without Git metadata: the current workspace snapshot includes `.git` if present.
2. Preserve the original user message through intake. Prepare a new request and approval record for the scope actually authorized by the user, following the operating guide. Creating JSON files does not itself grant approval.
3. Run static preflight, execute the approved request, and inspect the result using the commands below.

Replace the uppercase placeholders with your own files and new execution ID. Store user-message sources and approval records under `approvals/`.

```sh
python3 -B -m modules.harness.intake --question-file approvals/TASK_ID/user-message.txt
python3 -B -m modules.harness.preflight_run --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.harness.run_loop --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.harness.inspect_run RUN_ID --report
```

Use intake's returned `input_ref` when preparing the request. Request and approval fields are defined in [run_contracts.py](modules/harness/run_contracts.py). The ordinary `run_loop` command uses this project as its controller and expects a plain execution request, not a test fixture wrapper.

A preflight result of `ready` confirms static checks only; authentication, model availability, network access, and actual sandbox execution remain unchecked. In a report, check `execution.status` for the task result and `dialog.recording_status` for final-answer recording. A successful report command does not mean the task succeeded. In caller-owned dialog mode, the caller must save its final answer and attach the saved dialog to each execution as described in the operating guide.

## Paths and use on another PC

The default project root is derived from the source files' location. README links and internal project paths do not depend on a fixed installation directory or the name `loop_harness`.

Execution requests and approvals, however, bind to local absolute paths, instruction hashes, and user authorization. On another PC, prepare fresh requests and approvals using that machine's workspace and runtime paths. Existing execution records are historical evidence, not portable configuration. A unified configuration layer that generates every request from one root path has not been implemented.

## Project layout

| Path | Purpose |
|---|---|
| `modules/harness/` | Execution, verification, adapters, reporting, and recovery |
| `modules/save_dialog.py` | Shared question-and-answer saver |
| `modules/tests/` | Regression tests and synthetic fixtures |
| `docs/` | Public usage, architecture, validation, and sharing guides |
| `AGENTS.md`, `rules/` | Agent instructions and human-facing rule documentation |
| `approvals/` | Local user-message sources, approvals, and execution-permission traces |
| `runs/` | Local execution state and evidence |
| `dialog/` | Local question-and-answer records |

## Selective publication

Use `test` for development, commits, tests, and cross-review; keep `main` for stable versions selected for publication. Tags name specific commits. Local development and commits do not trigger publication: push only when the user requests it, and only to the specified destination repository and remote branch or tag. Record the exact candidate commit ID rather than relying on a moving branch tip.

Before publishing, check that test and review evidence applies to the candidate, inspect its public files and outgoing history, and run the email-policy and secret checks. The hooks do not run the full regression suite. Publishing a development branch or tag does not require updating `main`. When publication includes `main`, prefer a fast-forward to the validated commit when possible; a changed code state requires fresh validation and review. Push only the requested refs and verify the remote commit IDs. Branch and tag pushes include the required ancestor history, not just the candidate's current files. See the [publication procedure](docs/sharing.md#선택적-공개-절차).

## Local records and secret scanning

[.gitignore](.gitignore) excludes local dialog, approval, and execution records, generated live-test workspaces, environment files, and caches. The three record directories retain only their `.gitkeep` placeholders for sharing. Ignore rules do not remove files already tracked by Git; check the actual staged files before publishing.

Internal milestone notes and personal environment/account information stay local. Only the reviewed public guides listed in `.gitignore` are shared from `docs/`. Conversations, approval histories, and raw execution records remain private even when copied into documentation. Share synthetic examples and sanitized technical summaries instead; see the [sharing policy](docs/sharing.md).

[.gitleaks.toml](.gitleaks.toml) extends Gitleaks' built-in detection rules and was validated with Gitleaks v8.30.1. With Gitleaks available on `PATH`, scan the current directory from the project root:

```sh
gitleaks dir --config .gitleaks.toml --redact .
```

After initializing a Git repository, scan staged changes with:

```sh
gitleaks git --pre-commit --staged --config .gitleaks.toml --redact .
```

Before each push, after commits exist, scan the history reachable from all locally available refs:

```sh
gitleaks git --log-opts="--all" --config .gitleaks.toml --redact .
```

Resolve findings and scan errors before pushing. `pre-commit` and `pre-push` can be skipped with the respective command's `--no-verify`. Commit-time checking is not guaranteed when changes arrive through merge, rebase, or cherry-pick. The history scan supplements the hook; it does not remove the file-format limitations below.

To enable automatic checks in an existing Git checkout, install the project hook on each PC:

```sh
python3 modules/security/install_hooks.py --check
python3 modules/security/install_hooks.py
git config --local --get core.hooksPath
```

`--check` checks prerequisites and conflicts without changing settings. Installation sets the repository-local hook path to `modules/security`; it does not initialize Git or install Gitleaks. Both hooks must be executable, and Python 3 and Gitleaks must be on `PATH`. The installer refuses conflicting hook paths or existing default hook files instead of replacing them. Repeating the installation with the same configuration is safe.

The pre-commit hook checks the effective author and committer email addresses, then scans staged changes, including the first commit. Findings, a missing scanner/configuration, and scan errors block the commit. It masks detected secret values and does not modify or stage files. If a secret was staged, remove it and stage the corrected file before retrying. The scan uses the working tree's `.gitleaks.toml` and Git's selected index.

The secret scanner reads Git diffs and does not extract document text. A clean scan does not establish that binary files, Office documents (`.docx`, `.pptx`, `.xlsx`), or PDFs contain no secrets. Archive traversal is disabled by default (`--max-archive-depth=0`); the current commands do not enable it, so ZIP contents are not automatically unpacked either. Review such files and any separately extracted text before sharing them, and leave unnecessary generated documents out of the repository.

The email policy accepts GitHub noreply address formats by default, plus the exact `noreply@github.com` service address only in the committer role. This exception does not authenticate GitHub provenance. Other addresses require explicit repository-local `harness.publicEmail` entries after a public-use decision. The pre-push hook checks every outgoing commit's author and committer, plus annotated taggers. New remote refs trigger a full ancestry check; incomplete history fails closed. The checker does not print private email values, rewrite commits, or query GitHub account settings. A local policy pass does not verify address ownership or guarantee GitHub acceptance. History secret scanning remains a separate step. See the [email policy and existing-commit guidance](docs/sharing.md).

Hook source files are shared through Git; each checkout still needs installation. Inspect `modules/security/` before installing. Before integrating fetched changes through merge, rebase, or checkout, review changes to this directory, including new executable hook names. For updates, fetch and review first instead of applying an unreviewed pull: an executable hook such as `post-merge` added to this configured directory can run on the corresponding Git event. Keep credentials and actual conversation records local. See the [hook operation and sharing guidance](docs/sharing.md).

## Development checks

Run from the project root:

For the full regression suite or the OS isolation tests, keep the project's resolved path outside `/tmp` and `/var/tmp`. The read-isolation test creates a dependency inside the project, and the verifier rejects runtime paths under those private temporary namespaces. See [validation and limitations](docs/validation.md) for the distinction between path errors and environment-related skips, the command for the two isolation tests, and their results.

```sh
python3 -B -m unittest discover -s modules/tests -p 'test_*.py' -v
bash modules/tests/test_save_dialog.sh
```

These checks use synthetic fixtures and do not require live model calls. The secret-check suite uses temporary local Git repositories; its real-scanner tests require Gitleaks and are skipped if it is unavailable. OS isolation checks may be skipped when the host restricts the required namespace operations; a skipped check is not a pass. Live CLI validation is a separate workflow in the operating guide.

## License

This project is licensed under the [MIT License](LICENSE).

## Further documentation

- [Operating guide](docs/usage.md)
- [Validation and limitations](docs/validation.md)
- [Architecture and contracts](docs/architecture.md)
- [Human-facing rules](rules/default_rules.md)

Keep this file and [README.ko.md](README.ko.md) in sync when behavior or usage changes. The language links open separate documents; translations are maintained manually.
