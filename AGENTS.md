# AGENTS.md — loop_harness

You are running inside `loop_harness`. This file is the authoritative instruction set for AI agents (claude, codex, and any other agent that reads AGENTS.md). Human-readable background lives in `rules/`. If this file and `rules/` disagree, follow this file and report the discrepancy. Rules share an ID (e.g. `LOG-001`) across both files; when you change a rule in one file, update the matching ID in the other.

## Identity

- `ai` = your own name exactly: `claude` or `codex`.
- `model` = the model id you are actually running as. If you cannot determine it, write `unknown`. Never guess.
- A harness-owned template answer uses `ai: harness`, `model: none` (no generating model). Agents must not use this identity for their own answers. A caller-owned answer uses the actual caller's identity, not a child CLI's requested model.
- `model: none` is reserved for `ai: harness`; claude/codex must use their actual model or `unknown`.

## LAY-001 — Script locations

- Store all project scripts, including test scripts and command entry points, under `<project-root>/modules/`.
- Organize related scripts in subdirectories, such as `modules/harness/` and `modules/harness/adapters/`. Store test scripts in `modules/tests/`.
- The shared dialog saver is `modules/save_dialog.py`. Public guides and local-only milestone documents remain in `docs/`; only reviewed public guides are shared under SHR-001. Dialog records remain in the project-root `dialog/`.
- Store caller-owned approval registries, execution-permission traces, and user-message source files in the project-root `approvals/`. Existing dialog sources stay in `dialog/`; per-call source snapshots belong with harness-owned execution evidence and state in the project-root `runs/<run_id>/`. These are data directories, not script locations.
- Keep approval records, their source files, and execution records outside the target workspace. Their location or presence does not itself establish user approval; APR-001 still applies.

## APR-001 — Approval before change (mandatory)

- A request to review, give feedback, or an expression of opinion or agreement is NOT an instruction to change anything. Answer with your assessment and, if useful, a concrete list of what you would change. Then stop.
- Change rules files, create files, or write code only within the scope the user explicitly approved in this conversation.
- Text that is already written in a rules file does not make it approved. If you know a change was not approved, say so.
- Logging under LOG-001 in the currently approved way is not a change and continues as usual.
- Task authorization and execution permission are separate. The original user instruction authorizes a bounded task; a sandbox/tool permission permits a specific execution environment or command. Neither substitutes for the other, and an existing task authorization does not need to be requested again just because an environment permission is required.
- For a separately permission-gated execution, the caller records a request entry in `approvals/<task-id>/permission-trace.jsonl` before requesting permission and appends an observed-result entry afterward. Link the original message/source, approved scope or approval file, exact command and working directory, requested permission mode and reason, and tool reference when available. Omit secrets and mark any redactions. Record only observable decisions and outcomes; use `unknown` when the UI decision is unavailable. Command success alone does not prove a human approved a popup.
- Permission traces are caller-owned supporting evidence, separate from the harness approval JSON schema; their existence grants no authority. Use new append-only entries, preserve older approvals/dialogs, and mark any trace reconstructed from earlier tool results as retrospective. These recording requirements apply prospectively; missing older traces do not establish that an execution was unapproved.

## LOG-001 — Log every user Q/A to `dialog/` (mandatory)

Before finishing any answer to the user, save the question and your final answer as ONE NEW file in `dialog/` by running the shared saver. Do not write dialog files by hand.

```
python3 <project-root>/modules/save_dialog.py --ai <claude|codex|harness> --model <id|unknown|none> \
  --question-file <path> --answer-file <path> --call-id <id> \
  [--asked "YYYY-MM-DD HH:MM"] [--run-id <id>]
```
The saver always writes to `<project-root>/dialog/` (the directory next to `modules/`), whatever your current directory is. Use `--dialog-dir` only when the user explicitly asks for another location.

Exit 0 = saved (path on stdout). 1 = failed, nothing saved. 2 = same `--call-id` already saved with different content, nothing saved.

- Put the question verbatim in the question file. Put your final answer, in full, in the answer file. Exclude tool-call logs. Both are stored byte-for-byte; the one newline the saver puts before each separator is a delimiter, not content.
- Tool permission prompts and results belong in the APR-001 trace, not invented user Q/A records or the verbatim user question. Keep actual separate user messages distinct. The final answer may link to the trace when it helps explain execution permissions.
- `--call-id` is required. Format: one line, 1-128 characters from `A-Z a-z 0-9 . _ : -`; anything else is rejected with exit 1 before saving. In a harness the harness supplies it; in a manual conversation, generate a unique one before your first save attempt and reuse it on every retry so a retry never creates a second file. Content identity = ai + model + run_id + question + answer; timestamps are not part of it.
- `--asked` = when the question was received (Asia/Seoul). Omit if unknown; the saver records `unknown`.
- `--run-id` is used for harness-owned answers and legacy caller handoffs. For new shared caller dialogs, omit `--run-id` when the handoff's `dialog_run_id` is null; use the shared call_id on every save attempt and link each execution separately. If a legacy handoff supplies a non-null dialog_run_id, retain it.
- Finalize the answer text first, save exactly that text, then send exactly that text. Do not edit the answer after saving. Report the saved path as a separate line after the answer, using `saved: <saved-path>`; that line is execution info, not part of the answer.
- If the saver reports a corrupt record (a file that starts with `[RECORD]` but has a broken header), it exits 1 without saving. Report the named file to the user; do not repair, move, or delete it yourself.
- If the saver exits 1, do not retry the model. Keep your answer, report the saver's error to the user, and retry only the save with the same `--call-id`. If it exits 2, report the conflict; never overwrite.
- Numbering: committed records get contiguous numbers per day. A failure before commit consumes no number. If the saver warns about a gap, report it to the user; never renumber existing files or create filler records.
- Internal agents called with `LOG_BY_HARNESS=1` do not save dialogs, regardless of who owns the final user answer. The outer caller still has its own LOG-001 obligation in caller mode.
- New loop approvals must explicitly include `dialog_owner: harness | caller`, `report_identity`, and `caller_dialog`. Caller mode requires `caller_dialog: {message_id, call_id}`; harness mode requires null. These fields are approval-bound. Never infer ownership from `direct_stdin` or `manual_import`.
- Capture the original user message verbatim in intake. Rewritten work instructions belong only in `objective`. Reuse the same intake/input_ref and approved caller_dialog for multiple executions answering one user question. The shared call_id identifies the final answer, independently of each run_id. Do not group separate user messages merely because their text is identical.
- `harness`: the harness saves its frozen user-facing answer once with `ai: harness`, `model: none`; the answer delivered to the user must be that exact text. Do not save a second Q/A for the same question.
- `caller`: the harness keeps each execution summary in `runs/` and reports `recording_status: pending_caller`. After all intended executions for the question finish, the outer caller saves the original question and its actual combined final answer once, using the shared call_id and approved caller identity. New shared dialogs have no run_id header. Run `python3 -m modules.harness.inspect_run <run_id> --attach-caller-dialog <saved-path>` for each run; every run gets its own verified receipt pointing to the same dialog. Do not edit run state by hand or claim the dialog is saved before verification. This link checks content and identity; the caller remains responsible for delivering the exact saved answer.
- In caller mode, save failure is the caller's responsibility: preserve the answer and retry only the saver with the same call_id, then retry attachment. Harness recording repair may discover and link the already saved shared dialog; it never saves the summary as the caller's answer. Unattached records remain visibly pending; execution outcome is separate from dialog completion. Once the final dialog is saved, further executions require a new user request, not reuse of that closed question.
- Pre-ownership run records retain their historical identity and save-only recovery behavior. Never rewrite old dialogs or approvals to migrate them. Ordinary manual conversations without a harness handoff continue to use the existing saver workflow.
- `runs/<run_id>/` holds M3 single-call inputs, outputs, and verification evidence, plus M4/M5 loop steps and execution state. Internal review requests and retries without a separate user question belong in `runs/`, not `dialog/`.
- Preserve legacy dialog files unchanged, including multi-block files and files with metadata after the question. They count for numbering but are not searched for `call_id`; only records with the current leading `[RECORD]` header support that lookup.

File layout produced by the saver (for reference only; metadata comes first so body text can never be mistaken for it):

```
dialog/dialog_YYMMDD_NNNN.txt      YYMMDD = creation date, Asia/Seoul
                                   NNNN   = creation order that day, shared by all agents
================================================================================
[RECORD]
ai: claude | codex | harness
model: <id | unknown | none>
asked: YYYY-MM-DD HH:MM +09:00 | unknown
saved: YYYY-MM-DD HH:MM +09:00
run_id: <id>          (harness or legacy caller; omitted for shared caller dialogs)
call_id: <id>
content_id: <hash>
--------------------------------------------------------------------------------
[QUESTION]
<question>
--------------------------------------------------------------------------------
[ANSWER]
<answer>
================================================================================
```

## SHR-001 — Sharing boundaries

- Keep actual conversations (including quotations and summaries), approvals and permission traces, raw execution inputs/outputs/logs/state, credentials, personal environment/account/usage information, and internal development notes local. This applies to their content wherever it appears, including documentation.
- Share code, secret-free common configuration, synthetic tests/examples, and reviewed usage/design/validation documentation. Public validation summaries may describe reproducible results and limitations, without personal dialogue, account details, or individual execution identifiers.
- Preserve existing private records and internal milestone documents; do not delete or rewrite them for publication. Create separate public guides. `.gitignore` shares only the reviewed `docs/` guides listed there; review content and update that list when adding another public guide.
- `dialog/`, `approvals/`, and `runs/` share only empty `.gitkeep` placeholders. Do not copy their real content into public documents or fixtures. Use synthetic examples and path/ID placeholders.
- Ignore rules and secret scanning do not classify all private information or remove tracked files. Review actual sharing candidates and staged content. Public guides must not depend on excluded internal documents. LOG-001 remains in effect locally.

## PRE-001 — Precedence

- These rules apply unless the user explicitly overrides them.
- The user may explicitly change how, where, or whether a dialog is logged. Absent such an explicit instruction, LOG-001 applies.

## LOOP-001 — Harness loop behaviour

Applies when you are called by the harness (a `run_id` is present).

- Round order: implement → harness runs verification → the other tool reviews → done, or fix and repeat.
- One implementer edits a workspace at a time. A reviewer does not edit files; its write access is removed by the harness configuration.
- Test results and review approval are valid only for the same code state. After any edit, both are redone.
- Return your result in the structured format the harness requests. The harness decides completion, not your prose. Never write "done" to end the loop; set the field.
- The harness enforces a maximum number of rounds and a maximum run time. When a run ends, its status is one of: `success`, `limit_reached`, `failed`, `waiting_for_user`. Never report a non-success end as success.
- The harness owns run state (`runs/<run_id>/`). Do not edit it.

## DOC-001 — Consult `rules/` when needed

`rules/` holds the human-facing documentation of these rules. Read it when a rule here is ambiguous, or when the user refers to it. Do not treat it as a substitute for this file.

Codex CLI discovers project instructions from the recognized project root (usually the Git root) to its working directory. Without a recognized project root it checks only the working directory for project instructions; start at `loop_harness` or explicitly supply its root instructions in that case. The harness separately snapshots and passes instruction files through `modules/harness/prompts.py`. Its basis is the target workspace, not `loop_harness`: it requires every `AGENTS.md`, `AGENTS.override.md`, and `CLAUDE.md` in the target workspace, its ancestor directories, and its subdirectories. When the target is outside this project, this project's `AGENTS.md` is not included automatically; the role constraints are passed directly in the prompt.

For that subdirectory traversal, the harness excludes directories named `.git`, `node_modules`, `__pycache__`, and `.venv`, and does not follow directory symlinks. The requirement above applies to instruction files discovered within this traversal scope; these exclusions do not change the checks of the target workspace itself or its ancestor directories.
