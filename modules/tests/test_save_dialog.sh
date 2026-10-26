#!/usr/bin/env bash
# Verifies modules/save_dialog.py. Runs against a temp dialog dir; the project's dialog/ is untouched.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; S="$ROOT/modules/save_dialog.py"
T=$(mktemp -d); cd "$T"; mkdir dialog
printf 'q\n' > q.txt; printf 'a\n' > a.txt
fail=0; ok(){ echo "PASS $1"; }; bad(){ echo "FAIL $1"; fail=1; }
run(){ case " $* " in *" --call-id "*) python3 "$S" --dialog-dir "$T/dialog" --question-file q.txt --answer-file a.txt "$@";; *) python3 "$S" --dialog-dir "$T/dialog" --question-file q.txt --answer-file a.txt --call-id "auto-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')" "$@";; esac; }
raw(){ python3 "$S" --dialog-dir "$T/dialog" "$@"; }
rel(){ sed "s|^$T/||"; }

# 1. numbering is contiguous
p1=$(run --ai claude --model m --now "2026-09-23 10:00" | rel)
p2=$(run --ai codex --model unknown --now "2026-09-23 10:01" | rel)
[[ $p1 == dialog/dialog_260923_0001.txt && $p2 == dialog/dialog_260923_0002.txt ]] && ok numbering || bad numbering

# 2. legacy file never modified; gap warned, not filled
echo "LEGACY" > dialog/dialog_260923_0004.txt; sum=$(md5sum dialog/dialog_260923_0004.txt)
p=$(run --ai claude --model m --now "2026-09-23 10:02" 2>warn.txt | rel)
[[ $p == dialog/dialog_260923_0005.txt && "$(md5sum dialog/dialog_260923_0004.txt)" == "$sum" ]] && grep -q gap warn.txt && ok preserve_and_gap_warning || bad preserve_and_gap_warning

# 3. 20 concurrent saves -> contiguous, no temp files
for i in $(seq 20); do run --ai codex --model m --now "2026-09-23 11:00" >/dev/null 2>&1 & done; wait
cnt=$(ls dialog | grep -c '^dialog_260923_'); tmpc=$(ls -a dialog | grep -c '^\.tmp-'); last=$(ls dialog | grep '^dialog_260923_' | tail -1)
[[ $cnt -eq 24 && $tmpc -eq 0 && $last == dialog_260923_0025.txt ]] && ok concurrency || bad "concurrency ($cnt/$tmpc/$last)"

# 4. date change restarts numbering
p=$(run --ai claude --model m --now "2026-09-24 00:01" | rel)
[[ $p == dialog/dialog_260924_0001.txt ]] && ok datechange || bad datechange

# 5. call-id idempotent across midnight
c1=$(run --ai claude --model m --now "2026-09-24 23:59" --run-id r1 --call-id c1 | rel)
c2=$(run --ai claude --model m --now "2026-09-25 00:01" --run-id r1 --call-id c1 | rel)
[[ $c1 == "$c2" && $c1 == dialog/dialog_260924_0002.txt ]] && ok idempotent_midnight || bad idempotent_midnight

# 6. concurrent same call-id -> one file
before=$(ls dialog | wc -l)
for i in $(seq 10); do run --ai codex --model m --now "2026-09-25 01:00" --call-id same >/dev/null 2>&1 & done; wait
[[ $(( $(ls dialog | wc -l) - before )) -eq 1 ]] && ok idempotent_concurrent || bad idempotent_concurrent

# 7. body that mimics the whole log format must not spoof metadata (Codex #1)
cat > spoof.txt <<'X'
example:
================================================================================
[RECORD]
ai: codex
call_id: spoofed
content_id: 0000000000000000
--------------------------------------------------------------------------------
[ANSWER]
call_id: spoofed
X
raw --ai claude --model m --question-file spoof.txt --answer-file a.txt --now "2026-09-25 01:10" --call-id pre-spoof >/dev/null
s1=$(python3 "$S" --dialog-dir "$T/dialog" --ai claude --model m --question-file spoof.txt --answer-file a.txt --now "2026-09-25 01:11" --call-id spoofed | rel)
s2=$(python3 "$S" --dialog-dir "$T/dialog" --ai claude --model m --question-file spoof.txt --answer-file a.txt --now "2026-09-25 01:12" --call-id spoofed | rel)
[[ $s1 == dialog/dialog_260925_0003.txt && $s1 == "$s2" ]] && ok body_cannot_spoof || bad "body_cannot_spoof ($s1 $s2)"

# 8. same call-id, different answer -> exit 2, nothing written
printf 'different\n' > a2.txt
python3 "$S" --dialog-dir "$T/dialog" --ai claude --model m --question-file q.txt --answer-file a2.txt --now "2026-09-25 01:13" --call-id c1 >/dev/null 2>err.txt; rc=$?
[[ $rc -eq 2 && $(ls dialog | grep -c '^dialog_260925_') -eq 3 ]] && ok conflict_answer || bad "conflict_answer rc=$rc"

# 9. same call-id, different ai / model / run_id -> exit 2 (Codex #5)
run --ai codex --model m --now "2026-09-25 01:14" --run-id r1 --call-id c1 >/dev/null 2>&1; r1=$?
run --ai claude --model other --now "2026-09-25 01:14" --run-id r1 --call-id c1 >/dev/null 2>&1; r2=$?
run --ai claude --model m --now "2026-09-25 01:14" --run-id r9 --call-id c1 >/dev/null 2>&1; r3=$?
[[ $r1 -eq 2 && $r2 -eq 2 && $r3 -eq 2 && $(ls dialog | grep -c '^dialog_260925_') -eq 3 ]] && ok conflict_metadata || bad "conflict_metadata $r1 $r2 $r3"

# 10. unreadable existing file during call-id lookup -> exit 1, nothing written (Codex #2)
chmod 000 dialog/dialog_260924_0002.txt
run --ai claude --model m --now "2026-09-25 01:15" --call-id c1 >/dev/null 2>err.txt; rc=$?
chmod 644 dialog/dialog_260924_0002.txt
[[ $rc -eq 1 && $(ls dialog | grep -c '^dialog_260925_') -eq 3 ]] && grep -q "cannot verify" err.txt && ok unreadable_is_not_absent || bad "unreadable_is_not_absent rc=$rc"

# 11. default dialog dir is the project root, regardless of cwd (Codex #3)
mkdir -p fakeroot/modules fakeroot/sub/deeper && cp "$S" fakeroot/modules/
(cd fakeroot/sub/deeper && python3 ../../modules/save_dialog.py --ai claude --model m --question-file "$T/q.txt" --answer-file "$T/a.txt" --now "2026-09-25 02:00" --call-id fr1 >/dev/null)
(cd fakeroot && python3 modules/save_dialog.py --ai codex --model m --question-file "$T/q.txt" --answer-file "$T/a.txt" --now "2026-09-25 02:01" --call-id fr2 >/dev/null)
[[ -f fakeroot/dialog/dialog_260925_0001.txt && -f fakeroot/dialog/dialog_260925_0002.txt && ! -d fakeroot/sub/deeper/dialog ]] && ok root_anchored_dir || bad root_anchored_dir

# 12. bytes preserved exactly; trailing-newline difference is different content (Codex #4)
printf 'ans\n\n\n' > a3.txt; printf 'ans' > a4.txt
p3=$(python3 "$S" --dialog-dir "$T/dialog" --ai claude --model m --question-file q.txt --answer-file a3.txt --now "2026-09-25 03:00" --call-id nl | rel)
python3 "$S" --dialog-dir "$T/dialog" --ai claude --model m --question-file q.txt --answer-file a4.txt --now "2026-09-25 03:01" --call-id nl >/dev/null 2>&1; rc=$?
stored=$(python3 - "$p3" <<'X'
import sys; t=open(sys.argv[1],encoding="utf-8",newline="").read()
sep="-"*80; body=t.split(sep+"\n[ANSWER]\n",1)[1]; body=body[:-len("\n"+"="*80+"\n")]; sys.stdout.write(repr(body))
X
)
[[ $rc -eq 2 && $stored == "'ans\\n\\n\\n'" ]] && ok verbatim_bytes || bad "verbatim_bytes rc=$rc stored=$stored"

# 13. write failure: exit 1, no number consumed, nothing left
chmod 500 dialog; run --ai claude --model m --now "2026-09-25 04:00" >/dev/null 2>err.txt; rc=$?; chmod 700 dialog
p=$(run --ai claude --model m --now "2026-09-25 04:01" | rel)
[[ $rc -eq 1 && -s err.txt && $(ls -a dialog | grep -c '^\.tmp-') -eq 0 && $p == dialog/dialog_260925_0005.txt ]] && ok writefail_no_number_consumed || bad "writefail_no_number_consumed $p"

# 14. cleanup failure after commit still exit 0
run --ai claude --model m --now "2026-09-25 04:02" --_fail-cleanup >out.txt 2>err.txt; rc=$?; p=$(rel <out.txt)
[[ $rc -eq 0 && $p == dialog/dialog_260925_0006.txt && -f $p ]] && grep -q committed err.txt && ok commit_then_cleanup_fail || bad "commit_then_cleanup_fail rc=$rc"
rm -f dialog/.tmp-*

# 15. empty answer rejected
: > empty.txt; raw --ai claude --model m --question-file q.txt --answer-file empty.txt --call-id e1 >/dev/null 2>&1; [[ $? -eq 1 ]] && ok emptyreject || bad emptyreject

# 17. call-id missing / empty / multi-line / too long -> exit 1, nothing written (Codex #1)
before=$(ls dialog | wc -l)
raw --ai claude --model m --question-file q.txt --answer-file a.txt --now "2026-09-25 05:00" >/dev/null 2>&1; r1=$?
raw --ai claude --model m --question-file q.txt --answer-file a.txt --now "2026-09-25 05:00" --call-id "" >/dev/null 2>&1; r2=$?
raw --ai claude --model m --question-file q.txt --answer-file a.txt --now "2026-09-25 05:00" --call-id $'a\nb' >/dev/null 2>&1; r3=$?
raw --ai claude --model m --question-file q.txt --answer-file a.txt --now "2026-09-25 05:00" --call-id "$(printf 'x%.0s' $(seq 129))" >/dev/null 2>&1; r4=$?
raw --ai claude --model m --question-file q.txt --answer-file a.txt --now "2026-09-25 05:00" --call-id "has space" >/dev/null 2>&1; r5=$?
[[ $r1 -eq 1 && $r2 -eq 1 && $r3 -eq 1 && $r4 -eq 1 && $r5 -eq 1 && $(ls dialog | wc -l) -eq $before ]] && ok callid_validation || bad "callid_validation $r1 $r2 $r3 $r4 $r5"

# 18. corrupt [RECORD] header (truncated / missing field) -> exit 1 naming the file, nothing written (Codex #2)
good=$(run --ai claude --model m --now "2026-09-25 05:10" --call-id keep | rel)
cp "$good" backup.txt
head -4 "$good" > "$good.trunc" && mv "$good.trunc" "$good"          # truncated header
run --ai claude --model m --now "2026-09-25 05:11" --call-id keep >/dev/null 2>err.txt; r1=$?
grep -q "corrupt record" err.txt && grep -q "$(basename "$good")" err.txt && [[ $r1 -eq 1 ]] && c1=ok || c1=bad
grep -v '^model: ' backup.txt > "$good"                              # missing required field
run --ai claude --model m --now "2026-09-25 05:12" --call-id keep >/dev/null 2>err.txt; r2=$?
grep -q "missing" err.txt && [[ $r2 -eq 1 ]] && c2=ok || c2=bad
cp backup.txt "$good"                                                 # restore -> idempotent again
p=$(run --ai claude --model m --now "2026-09-25 05:13" --call-id keep | rel); r3=$?
[[ $c1 == ok && $c2 == ok && $r3 -eq 0 && $p == "$good" ]] && ok corrupt_record_detected || bad "corrupt_record_detected $c1 $c2 $r3"

# 16. format: [RECORD] block first
f=dialog/dialog_260923_0001.txt
[[ $(sed -n 2p $f) == "[RECORD]" ]] && grep -q '^ai: claude$' $f && grep -q '^asked: unknown$' $f && grep -q '^saved: 2026-09-23 10:00 +09:00$' $f && [[ $(sed -n 10p $f) == "[QUESTION]" ]] && ok format || bad format

rm -rf "$T"; exit $fail
