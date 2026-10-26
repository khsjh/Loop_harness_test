#!/usr/bin/env python3
"""Shared dialog saver for loop_harness (LOG-001).

Creates exactly one new file <root>/dialog/dialog_YYMMDD_NNNN.txt per record.
<root> is the project root (the parent of this script's directory) regardless
of the current working directory; override only with --dialog-dir.

File layout (metadata first, so body text can never be mistaken for it):

    ================================================================================
    [RECORD]
    ai: claude | codex | harness
    model: <id | unknown>
    asked: YYYY-MM-DD HH:MM +09:00 | unknown
    saved: YYYY-MM-DD HH:MM +09:00
    run_id: <id>            (only when given)
    call_id: <id>
    content_id: <hash>
    --------------------------------------------------------------------------------
    [QUESTION]
    <question, byte-for-byte>
    --------------------------------------------------------------------------------
    [ANSWER]
    <answer, byte-for-byte>
    ================================================================================

The single newline before each separator is a delimiter added by the saver;
question and answer bytes are stored and compared exactly as given.

Guarantees
- Date and timestamps are Asia/Seoul.
- NNNN is the next number for today, shared by all agents, assigned under an
  exclusive lock (dialog/.lock): committed records are contiguous; a failure
  before commit consumes no number. An existing gap is warned about, never
  filled or renumbered.
- Never overwrites: temp file, fsync, then os.link into place.
- --call-id is required: one line, 1-128 chars of [A-Za-z0-9._:-]; anything
  else is rejected before the lock with exit 1.
- --call-id is idempotent across retries and midnight. It is looked up only in
  the [RECORD] block of files that have one. Same id + same content_id
  (ai, model, run_id, question, answer) -> existing path, exit 0. Same id +
  different content_id -> exit 2, nothing written. If any dialog file cannot be
  read during that lookup -> exit 1, nothing written.
- After the final link, the record is committed: temp cleanup failure is a
  warning, exit stays 0.
- Files whose second line is not [RECORD] are older
  formats; they count for numbering but are skipped for call_id lookup. A file
  that starts with [RECORD] but has a truncated header or a missing required
  field is a corrupt record: the lookup fails with exit 1 and names the file.

Exit codes: 0 saved (path on stdout) / 1 failure / 2 call-id content conflict.
"""
import argparse, datetime, fcntl, hashlib, os, re, sys, uuid, zoneinfo

TZ = zoneinfo.ZoneInfo("Asia/Seoul")
SEP = "=" * 80
SUB = "-" * 80
NAME_RE = re.compile(r"dialog_(\d{6})_(\d{4})\.txt")
CALL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
REQUIRED_META = ("ai", "model", "asked", "saved", "call_id", "content_id")


class CorruptRecord(Exception):
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "dialog")


def fmt(dt):
    z = dt.strftime("%z")
    return dt.strftime("%Y-%m-%d %H:%M ") + z[:3] + ":" + z[3:]


def parse_time(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def content_id(ai, model, run_id, q, a):
    h = hashlib.sha256()
    for part in (ai, model, run_id or "", q, a):
        h.update(part.encode("utf-8")); h.update(b"\0")
    return h.hexdigest()[:16]


def build(q, a, meta):
    head = [f"{k}: {v}" for k, v in meta.items() if v is not None]
    return "".join([
        SEP, "\n[RECORD]\n", "\n".join(head), "\n", SUB, "\n[QUESTION]\n",
        q, "\n", SUB, "\n[ANSWER]\n", a, "\n", SEP, "\n",
    ])


def record_block(path):
    """Metadata of a [RECORD]-format file, or None for older formats.
    Raises OSError on read failure and CorruptRecord if the file starts with
    [RECORD] but its header is truncated or lacks a required field."""
    meta = {}
    with open(path, encoding="utf-8") as fh:
        if fh.readline().rstrip("\n") != SEP or fh.readline().rstrip("\n") != "[RECORD]":
            return None
        for line in fh:
            line = line.rstrip("\n")
            if line == SUB:
                missing = [k for k in REQUIRED_META if k not in meta]
                if missing:
                    raise CorruptRecord(f"{path}: missing {missing}")
                return meta
            k, sep, v = line.partition(": ")
            if not sep:
                raise CorruptRecord(f"{path}: malformed header line {line!r}")
            meta[k] = v
    raise CorruptRecord(f"{path}: header not terminated")


def find_call(dialog_dir, call_id):
    """Return (path, content_id) of the record with this call_id, or (None, None).
    Raises OSError if any dialog file cannot be read (unknown != absent)."""
    for f in sorted(os.listdir(dialog_dir)):
        if NAME_RE.fullmatch(f):
            p = os.path.join(dialog_dir, f)
            m = record_block(p)
            if m and m.get("call_id") == call_id:
                return p, m.get("content_id")
    return None, None


def todays_numbers(dialog_dir, day):
    return sorted(int(m.group(2)) for f in os.listdir(dialog_dir)
                  if (m := NAME_RE.fullmatch(f)) and m.group(1) == day)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ai", required=True, choices=["claude", "codex", "harness"])
    p.add_argument("--model", required=True)
    p.add_argument("--question-file", required=True)
    p.add_argument("--answer-file", required=True)
    p.add_argument("--asked", help='question received time, "YYYY-MM-DD HH:MM" Asia/Seoul')
    p.add_argument("--run-id", help="harness run identifier (loop only)")
    p.add_argument("--call-id", help="record identifier (required): [A-Za-z0-9._:-]{1,128}; reuse on retries")
    p.add_argument("--dialog-dir", default=DEFAULT_DIR, help=f"default: {DEFAULT_DIR}")
    p.add_argument("--now", help="override current time (tests only)")
    p.add_argument("--_fail-cleanup", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.ai == "harness" and (args.model != "none" or not args.run_id or not args.run_id.strip()):
        print("save_dialog: harness records require --model none and --run-id", file=sys.stderr)
        return 1
    if args.ai != "harness" and args.model == "none":
        print("save_dialog: model none is reserved for ai harness", file=sys.stderr)
        return 1

    try:
        with open(args.question_file, encoding="utf-8", newline="") as f:
            q = f.read()
        with open(args.answer_file, encoding="utf-8", newline="") as f:
            a = f.read()
    except OSError as e:
        print(f"save_dialog: cannot read input: {e}", file=sys.stderr)
        return 1
    if not q.strip() or not a.strip():
        print("save_dialog: question and answer must be non-empty", file=sys.stderr)
        return 1
    if args.call_id is None or not CALL_ID_RE.fullmatch(args.call_id):
        print("save_dialog: --call-id is required and must be a single line of 1-128 chars "
              "from [A-Za-z0-9._:-]", file=sys.stderr)
        return 1
    try:
        now = parse_time(args.now) if args.now else datetime.datetime.now(TZ)
        asked = fmt(parse_time(args.asked)) if args.asked else "unknown"
    except ValueError as e:
        print(f"save_dialog: bad time: {e}", file=sys.stderr)
        return 1
    day = now.strftime("%y%m%d")
    cid = content_id(args.ai, args.model, args.run_id, q, a)

    tmp = None
    try:
        os.makedirs(args.dialog_dir, exist_ok=True)
        with open(os.path.join(args.dialog_dir, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)

            try:
                existing, existing_cid = find_call(args.dialog_dir, args.call_id)
            except OSError as e:
                print(f"save_dialog: cannot verify whether call_id {args.call_id} is already "
                      f"saved ({e}); nothing written", file=sys.stderr)
                return 1
            except CorruptRecord as e:
                print(f"save_dialog: corrupt record found while checking call_id ({e}); "
                      f"nothing written, please repair or move the file", file=sys.stderr)
                return 1
            if existing:
                if existing_cid == cid:
                    print(existing)
                    return 0
                print(f"save_dialog: call_id {args.call_id} already saved at {existing} "
                      f"with different content; nothing written", file=sys.stderr)
                return 2

            nums = todays_numbers(args.dialog_dir, day)
            if nums and nums != list(range(1, nums[-1] + 1)):
                missing = sorted(set(range(1, nums[-1] + 1)) - set(nums))
                print(f"save_dialog: warning: gap in today's numbering, missing {missing}; "
                      f"not filling, please investigate", file=sys.stderr)
            n = (nums[-1] if nums else 0) + 1

            meta = {"ai": args.ai, "model": args.model, "asked": asked, "saved": fmt(now),
                    "run_id": args.run_id, "call_id": args.call_id, "content_id": cid}
            tmp = os.path.join(args.dialog_dir, f".tmp-{uuid.uuid4().hex}")
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(build(q, a, meta)); f.flush(); os.fsync(f.fileno())

            target = os.path.join(args.dialog_dir, f"dialog_{day}_{n:04d}.txt")
            os.link(tmp, target)  # commit point
            committed = target
    except OSError as e:
        print(f"save_dialog: write failed: {e}", file=sys.stderr)
        _cleanup(tmp)
        return 1

    if args._fail_cleanup:
        print("save_dialog: warning: temp cleanup failed (simulated); record is committed", file=sys.stderr)
    else:
        try:
            os.unlink(tmp)
        except OSError as e:
            print(f"save_dialog: warning: temp cleanup failed ({e}); record is committed", file=sys.stderr)
    print(committed)
    return 0


def _cleanup(tmp):
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
