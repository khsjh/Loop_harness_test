# 운영 가이드

사전 점검·실행·보고·복구와 시험 관리 명령을 설명한다. 계약은 `m5-v5`다. [구조와 계약](architecture.md), [검증 범위](validation.md), [공유 정책](sharing.md)을 함께 참고한다.

## 1. 실행 환경과 자료 배치

### 의존성 일괄 설치와 호환성 검사

최소 설치 대상은 Linux x86_64, glibc 2.35 이상이다. 자동 시스템 패키지 설치는 Debian/Ubuntu의 apt를 사용한다. 설치 시작에는 Bash와 Python 3.10 이상, CA 인증서와 HTTPS 접속이 필요하다. Python이 없는 최소 OS 이미지에서는 먼저 `sudo apt-get install python3 ca-certificates`를 실행한다. 실행용 controller Python은 별도로 설치한다.

```sh
bash install.sh
source "$HOME/.local/share/loop-harness/deps/env.sh"
bash install.sh --check
```

`modules/setup/dependencies.json`에 시스템·CLI 의존성의 버전·다운로드 주소·SHA256을 기록한다. Gitleaks·bubblewrap·두 CLI는 Python 패키지가 아니며, 하네스는 외부 Python 패키지를 요구하지 않는다. 루트의 `install.sh`가 이 목록을 읽는 `modules/setup/dependencies.py`를 실행한다. 루트 설치 진입점은 LAY-001의 예외이며, 지원 구현과 테스트는 `modules/` 아래에 둔다.

| 구성 요소 | 정책 | 설치 방식 |
|---|---|---|
| controller Python | >=3.11,<3.13, 신규 설치 3.12.13 고정 | uv 관리 런타임, 시스템 Python과 분리 |
| Gitleaks | 8.30.1 고정 | 공식 Linux x64 바이너리, SHA256 확인 |
| Codex CLI | 0.163.0 고정 | 공식 Linux x86_64 musl 바이너리, SHA256 확인 |
| Claude Code | 2.1.310 고정 | 공식 Linux x64 바이너리, SHA256 확인 |
| uv | 0.11.26 고정, 설치 시에만 필요 | 공식 바이너리, SHA256 확인 |
| Git / Bash / bubblewrap | 각각 >=2.34 / >=5.1 / >=0.6.1 | 누락된 apt 패키지 설치, 실제 버전 검사 |
| CA 인증서 / libatomic1 / libstdc++6 | 배포판 패키지 | apt로 누락 여부 확인 |

위 최소값은 설치 허용 범위이며 모든 버전 조합을 검증했다는 뜻은 아니다. 실제 시험 조합과 한계는 [검증 문서](validation.md#의존성-설치-검증)를 따른다. CLI는 API·권한 옵션 변경을 막기 위해 정확한 버전을 요구하고, 시스템 패키지는 배포판 보안 업데이트를 허용하되 기능 검사를 수행한다. 고정 버전은 최신 릴리스 보장이 아니다. 갱신 시 공식 배포 정보와 SHA256을 함께 수정하고 설치·회귀·권한 검사를 다시 수행한다.

설치 위치 기본값은 `$HOME/.local/share/loop-harness/deps`이다. `--prefix '/원하는/설치 경로'`로 변경할 수 있다. 도구는 이 경로에 두고 기존 전역 CLI·로그인·shell 시작 파일·Git 설정을 변경하지 않는다. 부족한 시스템 패키지는 apt로 설치하므로 sudo가 필요할 수 있다. 이미 설치된 시스템 패키지가 최소 버전보다 낮으면 OS/패키지 갱신을 요구하고 중단한다. Node.js/npm은 필요 없다.

`env.sh`는 설치한 `bin`을 PATH 앞에 놓고 Claude 자동 업데이트를 비활성화한다. 하네스와 Git hook을 실행할 각 shell에서 source한다. 다른 terminal/GUI가 별도 PATH를 사용하면 그 환경에서도 같은 경로를 제공해야 한다. Python 검증 자식 프로세스의 `runtime_paths`는 요청에서 별도로 지정하며, controller Python 설치가 이를 자동 변경하지 않는다.

- `--dry-run`: 목록과 설치 경로만 출력한다. 다운로드·설치·실행 검사를 하지 않는다.
- `--check`: 현재 PATH의 도구를 검사한다. `--prefix`를 주어도 PATH를 자동 변경하지 않는다. 임시 합성 저장소를 생성하고 검사 후 정리한다.
- 기본 설치: SHA256 검증 후 바이너리를 배치하고, CLI 필수 옵션·Python 모듈·Gitleaks의 정상 입력 허용/합성 토큰 차단(파일과 Git staged 각각)·harness의 실제 bubblewrap probe를 실행한다. namespace 실패도 skip 없이 실패 처리한다.
- 실패하면 0이 아닌 종료 코드를 반환한다. 이미 설치된 파일은 재시도를 위해 남긴다. 동일 버전은 재사용하고 검사를 다시 수행한다. 다른 버전이나 외부 바이너리 symlink와 충돌하면 덮어쓰지 않고 새 prefix를 요구한다.

로그인·모델 사용 권한·실제 모델 호출·버전 변경 후 모델 권한 경계 검증은 설치 검사 밖이다. 설치 성공이 실제 작업 실행 성공을 보장하지 않는다. Git hook 연결은 별도이며, 환경 활성화 후 `python3 -B modules/security/install_hooks.py --check`로 점검하고 같은 명령에서 `--check`를 빼면 연결한다.

공식 근거: [Python tomllib](https://docs.python.org/3/library/tomllib.html), [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), [Codex 고정 릴리스](https://github.com/openai/codex/releases/tag/rust-v0.163.0), [Claude 설치·배포 검증](https://code.claude.com/docs/en/setup), [Gitleaks 고정 릴리스](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1), [uv Python 설치](https://docs.astral.sh/uv/guides/install-python/). 네이티브 배포물 SHA256은 공식 GitHub 릴리스 메타데이터와 Claude 버전별 manifest에서 가져온다.

### 실행 자료 배치

프로젝트 루트에서 Python 3로 실행한다. 독립 검증은 Linux x86_64와 bubblewrap을 사용한다. `codex`, `claude`, `bwrap`이 PATH에 있어야 하며, CLI 로그인과 요청 모델 사용 권한은 별도로 유효해야 한다. CLI 버전이 바뀌면 권한·통합 검증을 다시 확인한다. 중첩 sandbox가 network namespace 생성을 막으면 `unsupported_configuration`으로 종료한다. 격리를 끄지 말고 해당 기능을 허용한 실행 환경을 사용한다.

- controller: `approvals/`에 원문·승인, `runs/`에 실행 근거, `dialog/`에 최종 대화를 보관한다.
- target workspace: 변경할 코드와 보호 테스트를 둔다. controller의 위 데이터 디렉터리와 겹칠 수 없다.
- 스크립트: `modules/`, 테스트: `modules/tests/`, 문서: `docs/`에 둔다. 설치 진입점 `install.sh`만 프로젝트 루트에 둔다.
- 검증 중 target은 읽기 전용이다. 임시 산출물은 private `/tmp`를 사용한다. target에 빌드 결과를 쓰는 작업은 현재 지원 범위 밖이다.

다음 명령의 대문자 값은 실제 절대 경로와 새로운 식별자로 바꾼다. 기존 실행의 ID를 재사용하지 않는다.

## 2. 사용자 원문과 승인된 실행 요청

```sh
python3 -B -m modules.harness.intake --question-file approvals/TASK_ID/user-message.txt
```

원문은 줄바꿈까지 보존한다. 반환된 `input_ref`를 요청에 넣는다. 같은 질문에 여러 실행을 수행하면 같은 intake와 caller 대화 식별자를 사용한다. 작업을 구체화한 문장은 요청의 `objective`에 둔다. 수집 자체는 승인이 아니다.

caller가 기존 대화에서 승인된 범위를 확인하고 요청 JSON과 승인 JSON을 작성한다. 이미 승인된 일을 다시 승인받는 절차는 필요하지 않다. 전체 필드 정의는 [run_contracts.py](../modules/harness/run_contracts.py)의 `RUN_SCHEMA`, `approved_scope`, `load_approval`에 있다.

| 요청 항목 | 작성 기준 |
|---|---|
| `schema_version`, `run_id`, `parent_run_id` | `1`, 새 ID, 이전 실행 연결이 없으면 `null` |
| `input_ref`, `authorization_ref` | 원문 intake의 절대 경로, 승인 식별자 |
| `workspace`, `objective`, `allowed_paths` | 정규화된 절대 작업 경로, 승인된 목적, 변경할 상대 경로 목록 |
| `instruction_files` | 적용할 규칙 파일의 절대 경로 목록 |
| `assignments` | implementer/reviewer 각각 agent·model·reasoning_effort; 두 agent는 달라야 함 |
| `verification_commands` | argv·cwd·timeout_seconds·protected_paths·runtime_paths를 명시 |
| `limits` | max_rounds ≤ 6, run_timeout_seconds ≤ 86400, call_timeout_seconds ≤ 21600 |
| `timeout_policy` | max_test_timeout_seconds ≤ 21600, max_consecutive_timeouts ≤ 3 |
| `dialog_owner`, `report_identity`, `caller_dialog` | 아래 기록 책임 기준 적용 |

검증의 `protected_paths`는 존재하는 테스트·입력의 상대 경로다. `runtime_paths`는 검증 실행에 필요한 실행 파일·표준 라이브러리·공유 라이브러리만 명시한다. `/`, home 전체, controller 자료를 노출하면 거부한다. 현재 Python 예제의 런타임 선정은 [runtime_fixture.py](../modules/tests/runtime_fixture.py)를 참조한다.

승인 JSON은 controller의 `approvals/` 아래에 둔다. 필드는 `schema_version: 1`, `authorization_ref`, 원문 `user_instruction`, intake의 `source`, `scope: approved_scope(request)`, 규칙별 `instruction_hashes`, 원본 intake 바이트의 `intake_sha256`, `allow_iterations: true`, `template_version: m5-v5`다. 요청 복사나 해시 생성만으로 사용자 승인이 생기지 않는다.

### 작업 승인과 실행 권한의 추적

사용자가 요청한 작업 범위와 샌드박스 밖 실행·네트워크 등의 도구 권한을 구분한다(APR-001). 이미 승인된 작업을 다시 승인받지 않으며, 실행 도구가 별도로 요구하는 권한만 해당 도구를 통해 요청한다.

별도 실행 권한이 필요한 경우 호출자가 `approvals/<task-id>/permission-trace.jsonl`에 요청을 먼저 기록하고 관찰 결과를 새 줄로 추가한다. 각 줄은 JSON 객체이며 다음 정보로 서로 연결한다.

| 항목 | 기록 기준 |
|---|---|
| `event`, `permission_id` | `request` / `result`, 두 항목을 묶는 식별자 |
| 작업 근거 | 원문 intake/source, 작업 승인 파일 또는 범위, 필요한 대화 맥락 |
| 실행 요청 | 정확한 `command`, `cwd`, `sandbox_permissions`, `reason`; 비밀은 생략 사실만 표시 |
| 관찰 결과 | 확인 가능한 도구 참조, `observed_permission_decision`, 실제 종료 코드·결과 근거 |
| 확인 한계 | UI 선택을 볼 수 없으면 `unknown`; 소급 정리라면 `retrospective: true` |

이는 호출자의 보조 기록이며 기존 `m5-v5` 승인 스키마나 실행기의 승인 판정을 바꾸지 않는다. 기록 파일의 존재나 성공 종료를 사용자 승인 증거로 대신하지 않는다. 기존 승인·대화를 고치거나 도구 권한 질문을 사용자 메시지로 만들어 저장하지 않는다.

### 기록 책임

- **caller**: `report_identity`는 실제 caller의 `ai`와 실제 모델 또는 `unknown`. `caller_dialog`는 원문의 `message_id`와 이번 최종 답변의 공통 `call_id`다. 실행 요약은 최종 사용자 답변을 대신하지 않는다.
- **harness**: `report_identity: {"ai":"harness","model":"none"}`, `caller_dialog: null`. 동결된 harness 답변을 그대로 전달한다.
- `model: none`은 harness 전용이다. 내부 에이전트는 `LOG_BY_HARNESS=1`로 대화 저장을 중복 수행하지 않는다.

## 3. 모델을 호출하지 않는 사전 점검

```sh
python3 -B -m modules.harness.preflight_run --request REQUEST.json --approval APPROVAL.json
```

별도 controller를 쓰면 `--controller CONTROLLER_ROOT`를 추가한다. 승인 없이 `--request`만 주면 승인 확인이 빠진 `incomplete`를 반환한다.

| 결과 | 종료 코드 | 뜻 |
|---|---:|---|
| `ready` | 0 | 정적 필수 조건과 제공된 승인이 확인됨 |
| `blocked` | 1 | 요청·경로·승인·환경·잠금 등에 차단 사유가 있음 |
| `incomplete` | 2 | 승인 확인 등 필수 확인이 빠짐 |

이 명령은 파일 생성·잠금 획득·모델/테스트 호출을 하지 않는다. 로그인, 네트워크, 모델 제공 여부, 실제 sandbox 실행은 `not_checked`다. `ready`가 실행 성공을 보장하지 않으며 실행기는 승인·규칙·잠금을 다시 확인한다.

## 4. 실행과 읽기 전용 보고

```sh
python3 -B -m modules.harness.run_loop --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.harness.inspect_run RUN_ID --report
```

일반 `run_loop`는 이 프로젝트를 controller로 사용한다. `--request`에는 순수 실행 요청을 준다. 시험용 `live_m6_check`의 `{controller_root, request, ...}` wrapper를 주지 않는다. 별도 controller의 시험 실행은 아래 시험 진입점 또는 백그라운드 관리 명령을 사용한다. 조회는 `--controller CONTROLLER_ROOT`를 지원한다.

실행 CLI는 `success`만 종료 코드 0이며, 나머지는 1이다. 보고 CLI는 조회 자체가 정상인 `ok=0`, 불완전한 기록 `incomplete=2`, 손상/조회 오류 `error=1`이다. **실패한 작업을 정상 조회하면 보고 종료 코드는 0일 수 있다.**

종료 코드 2는 사용법 오류(argparse: 인자 누락, 함께 쓸 수 없는 옵션)에도 쓰인다. 사용법 오류는 stdout에 JSON 없이 stderr에 사용법만 출력한다. 사전 점검의 `incomplete`와 보고의 `incomplete`는 항상 stdout JSON의 `status`/`report_status`로 함께 나온다. 자동화는 종료 코드만 보지 말고 **stdout JSON의 상태 필드를 먼저 확인**하고, JSON이 없으면 사용법 오류로 처리한다.

`report_status: ok`는 조회가 정상으로 끝났다는 뜻이다. 무결성 검증까지 뜻하는 것은 `kind: loop`일 때뿐이다. 기록 종류별 값은 다음과 같다.

| 기록 종류 | `report_status` | `integrity.status` | 보고 종료 코드 |
|---|---|---|---:|
| `loop` 정상 조회 | `ok` | `verified` | 0 |
| `loop`, 실행 요청이 아직 이벤트에 기록되지 않음 | `incomplete` | `partial` | 2 |
| `evidence_bundle` | `ok` | `not_checked` | 0 |
| `legacy_single_call` | `incomplete` | `not_checked` | 2 |
| `unknown_record` | `incomplete` | `not_checked` | 2 |
| 손상·조회 오류 | `error` | `error` | 1 |

`verified`는 기록의 무결성을 확인했다는 뜻이며 작업 성공을 뜻하지 않는다. 작업 결과는 `execution.status`로 판단한다.

보고에서 구분할 값:

- `execution.status`: success / failed / limit_reached / waiting_for_user / 아직 미종료인 null.
- `execution.task_outcome`: 작업 자체의 판정. 작업 성공 후 저장 실패라면 실행 status와 다를 수 있다.
- `dialog.recording_status`: saved / pending_caller / error 등 기록 상태.
- `integrity`: 읽은 이벤트·artifact·대화 영수증의 검증. 작성자의 독립 인증을 뜻하지 않는다.
- `as_of_sequence`: 이번 조회 시점의 이벤트 번호. 진행 중 실행은 이후 바뀔 수 있다.
- `source.at_execution`과 `source.at_inspection`: 실행 당시/조회 당시 소스. 과거 기록에 실행 소스가 없으면 null이며 현재 소스로 대체하지 않는다.
- `verification`, `final_code_reviews`: 테스트·리뷰 근거. 요청 모델과 실제 메타데이터도 구분하며 미보고 모델은 unknown이다.
  - `verification[].passed`: 실행기와 같은 프로세스 판정. 종료 코드 0이고 시간 초과·취소·출력 한도 초과·종료 후 출력 대기 초과·하위 프로세스 강제 종료·실행/입출력 오류가 없으며 정리가 끝났을 때만 true다.
  - `verification[].matches_final_code`: 그 테스트가 최종 코드 상태에서 실행됐는지. **과거 코드에서 통과한 테스트는 `passed=true`라도 `matches_final_code=false`다.** 최종 코드의 통과 근거는 두 값이 모두 true인 항목이다.
- `pending_call_recordings`: 호출 결과 저장에 실패해 `recovery/`에 남은 영수증 중 아직 저장되지 않은 것. 최종 대화가 저장됐어도 여기에 항목이 있으면 `next_actions`에 저장 전용 재시도(`--retry-recording`)가 나온다.

보고는 복구하지 않는다. `--report`와 `--recover-state`, `--retry-recording`, `--attach-caller-dialog`는 함께 쓸 수 없다. 과거 M3 기록은 확인 가능한 호출 목록만 표시한다.

### 실행 기록과 증거 요약 묶음

`runs/*-evidence/`는 여러 시험의 요약·로그를 모은 디렉터리일 수 있다. 실행 요청·이벤트·호출 디렉터리가 없는 이런 묶음은 `kind: evidence_bundle`, `report_status: ok`로 표시한다. 이는 디렉터리 조회가 완료됐다는 뜻이며 `execution: null`, `integrity.status: not_checked`로 유지한다. 요약 안의 success·verified 값을 실제 실행 판정으로 복사하지 않는다.

`evidence.files`는 파일 목록, `evidence.run_references`는 manifest·요약·완료 기록에 남은 원본 실행 위치와 읽기 전용 조회 명령이다. 참조 경로를 자동으로 열거나 명령을 실행하지 않으며 `references_verified: false`다. 참조가 손상됐으면 `reference_warnings`에 표시한다. 경로를 확인한 뒤 해당 실행을 `--controller CONTROLLER_ROOT --report`로 별도 조회해야 무결성을 확인할 수 있다.

참조를 읽는 파일은 `fixture-manifest.json`, `summary.json`, `completion.json`, `*-report-final.json`이다. 네 종류 모두 최상위 객체, `runs[]`의 각 항목, `outcome` 객체를 확인한다. `outcome` 필드가 없으면 건너뛰고, 필드는 있지만 값이 객체가 아니면(`null`, 문자열, 배열 등) `$.outcome` 위치의 경고를 표시한다. 객체에 `run_id`가 없으면 참조로 처리하지 않는다.

controller는 해당 참조의 `controller_root`를 먼저 사용한다. `outcome`에 그 필드가 없을 때만 최상위 값을 이어받는다. 명시적인 `null`은 최상위 값을 이어받지 않으며, `runs[]` 항목도 최상위 값을 이어받지 않는다. controller가 아직 없으면 해당 참조의 절대 `run_path`가 `runs/<동일 run_id>`로 끝나는지 확인해 controller 경로를 구하고, 그래도 없으면 현재 controller 아래에 같은 실행 디렉터리가 있는지 확인한다. 이 과정에서 외부 controller의 존재나 내용을 확인하지 않는다. 끝내 controller를 정할 수 없으면 해당 `run_id`를 포함한 경고를 남기고 참조 목록에서는 제외한다.

파일 자체의 읽기·JSON 오류는 `file`과 `error`로, 개별 항목 오류와 controller 미확인 경고는 `record` 위치(예: `$`, `$.outcome`, `$.runs[1]`)를 추가해 표시한다. 항목 하나가 잘못되거나 controller를 찾지 못해도 같은 파일의 나머지 정상 참조를 계속 수집하며 중복은 제거한다. 참조 경고만으로 요약 묶음의 `report_status: ok`나 `integrity.status: not_checked`를 바꾸지는 않는다.

실제 실행 요청이나 이벤트·호출 디렉터리가 있으면 이름이 `-evidence`로 끝나도 실행 기록 판정을 우선한다. 이벤트가 있는데 실행 요청이 사라졌다면 계속 오류다. 어느 형식에도 해당하지 않는 디렉터리는 `unknown_record / incomplete`로 표시한다. 기본 조회도 같은 구분을 사용하며 기존 증거의 위치나 내용을 바꾸지 않는다.

```sh
python3 -B -m modules.harness.inspect_run EVIDENCE_BUNDLE_ID --report
python3 -B -m modules.harness.inspect_run RUN_ID --report
```

## 5. 중단·오류·기록 복구

| 상태/사유 | 대응 |
|---|---|
| 테스트 실패·리뷰 반려 | 승인된 라운드·예산 안에서 자동 수정 후 테스트·리뷰를 새로 수행 |
| 같은 테스트 연속 시간 초과 3회 | 최초 시도 포함 3회로 중단; 코드가 바뀌어도 누적 |
| `limit_reached` | 소진한 시간·라운드를 확인; 기존 run_id로 재개하지 않음 |
| `waiting_for_user` | 질문 또는 workspace 충돌 사유를 확인; 성공으로 처리하지 않음 |
| CLI 오류·잘못된 응답 | 오류 artifact 확인; 실행기가 모델을 자동 재호출하지 않음 |
| 중단·정리 미확인 | controller와 자식 상태 확인 후 아래 상태 복구; guard 수동 삭제 금지 |
| 저장 실패 | 아래 저장 전용 복구; 모델·테스트 재실행 없음 |

```sh
python3 -B -m modules.harness.inspect_run RUN_ID --recover-state
python3 -B -m modules.harness.inspect_run RUN_ID --retry-recording
```

별도 controller에는 `--controller`를 추가한다. 상태 복구는 실행을 재개하지 않고 중단을 기록한다. 저장 복구가 성공해도 과거 `failed` 실행을 `success`로 바꾸지 않는다. 손상 기록은 자동 삭제·덮어쓰기하지 않는다.

caller 모드에서는 계획한 실행을 마친 뒤 원문 질문과 실제 최종 답변을 공통 저장기로 한 번 저장한다.

```sh
python3 modules/save_dialog.py --ai codex --model unknown --question-file QUESTION.txt --answer-file ANSWER.txt --call-id SHARED_CALL_ID
python3 -B -m modules.harness.inspect_run RUN_ID --attach-caller-dialog SAVED_DIALOG_PATH
```

각 실행에 같은 대화를 연결한다. 새 shared caller 대화에는 `--run-id`를 주지 않는다. 저장 실패는 같은 call_id로 저장만 재시도한다. 답변은 저장한 그대로 전달한다.

## 6. M6 실제 시험과 백그라운드 운영

새 예제를 준비하는 명령은 모델을 부르거나 승인을 생성하지 않는다. 모델/effort를 명시한다.

```sh
python3 -B -m modules.tests.live_m6_check --prepare NEW_PLAN.json --implementer codex --codex-model CODEX_MODEL --codex-effort xhigh --claude-model CLAUDE_MODEL --claude-effort medium --stress-seconds 2
```

반대 방향은 `--implementer claude`다. 출력의 controller에 caller가 위 승인 형식으로 승인 파일을 제공한 뒤 실행한다.

```sh
python3 -B -m modules.tests.live_m6_check --request NEW_PLAN.json --approval FIXTURE_APPROVAL.json
```

이 시험의 입력·harness 답변은 명시적 합성 시험 데이터이며 실제 사용자 대화와 별도 controller에 보관한다. JSONL 집계 과제를 바로 올바르게 구현하도록 요청한다. 자연 실패가 없으면 정상 경로만 확인한 것으로 보고한다. 실행 결과는 controller와 루트의 `runs/*-evidence/`에 보관한다.

기능 구현·회귀·교차 리뷰·짧은 중단 시험을 마친 뒤, 준비된 **순수 request JSON**과 승인으로 백그라운드 실행할 수 있다.

```sh
python3 -B -m modules.tests.background_m6_check start --controller CONTROLLER_ROOT --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.tests.background_m6_check status --controller CONTROLLER_ROOT --run-id RUN_ID
python3 -B -m modules.tests.background_m6_check stop --controller CONTROLLER_ROOT --run-id RUN_ID
```

`background_m6_check`는 `modules/tests/`에 둔 **시험용 관리 도구**다. 범용 운영 도구나 자동 재개 기능을 제공하지 않는다. 지원 범위는 [검증 안내](validation.md)를 참고한다.

작업 ID는 run_id이며 `runs/.m6-jobs/RUN_ID/`에 controller.log·신원·시작 자료·종료 결과가 남는다. worker가 실패하면 `exit.json`의 `error`에 원인이 남는다(adapter 설정 오류 포함). 예상하지 못한 예외는 `오류 종류: 메시지` 형식으로 남고, 상세 traceback은 controller.log에 있다. start는 중복을 거부하고 코드 해시를 worker 시작 때 다시 확인한다. stop은 해당 작업의 협조적 취소 표식을 남기며 추측한 PID로 신호를 보내지 않는다. **stop 요청 성공은 종료·정리 완료가 아니다.** status에서 종료 결과, recovery_required, 실제 run 상태를 확인한다. 시작 프로세스 생성 실패는 launch_failed와 launch_error로 표시한다. interrupted나 starting_or_unconfirmed는 성공이 아니며 자동 재시작하지 않는다. 프로세스 신원 조회는 worker와 같은 호스트/PID namespace에서 수행한다.

자식 CLI 출력은 종료 전까지 완전히 영속화되지 않는다. controller 로그가 조용한 것만으로 hang이라 판단하지 않는다. 승인·실행·fixture 근거는 보존하고 자동 삭제하지 않는다.

장시간 시험은 실제 CPU 작업을 수행하는 독립 무작위 검증을 사용할 수 있다. 이 경우 1시간 검증 프로세스의 유지·예산·정리 근거이며, 모델 호출이나 인증 갱신이 1시간 지속됐다는 근거가 아니다. 단순 대기로 시간을 채우지 않고 실제 지속 시간을 기록한다. 6시간 모델 호출·24시간 연속 실행은 별도 미검증으로 남긴다.
