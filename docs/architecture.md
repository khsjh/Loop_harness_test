# 구조와 계약

Loop Harness는 승인된 작업을 구현 → 독립 검증 → 다른 도구의 리뷰 순서로 처리한다. 단발 호출과 한도가 있는 반복 실행을 제공하며, 실행 결과와 최종 대화의 저장 상태를 구분한다.

## 구성

| 구성 | 역할 |
|---|---|
| [intake.py](../modules/harness/intake.py), [authorization.py](../modules/harness/authorization.py) | 사용자 원문 보존·승인 출처 확인 |
| [contracts.py](../modules/harness/contracts.py), [run_contracts.py](../modules/harness/run_contracts.py) | 단발·반복 요청과 응답·승인의 구조 및 의미 검증 |
| [adapters/](../modules/harness/adapters/) | Codex·Claude CLI 명령과 응답 형식 연결 |
| [run_agent_once.py](../modules/harness/run_agent_once.py), [run_loop.py](../modules/harness/run_loop.py) | 단발 호출·반복 제어 |
| [verification.py](../modules/harness/verification.py) | 파일 상태 비교·읽기 전용·네트워크 격리 검증 |
| [execution_budget.py](../modules/harness/execution_budget.py), [timeout_recovery.py](../modules/harness/timeout_recovery.py) | 남은 시간과 테스트 시간 초과 복구 한도 |
| [workspace_guard.py](../modules/harness/workspace_guard.py) | 작업 공간 잠금·중복 실행 방지·중단 후 정리 확인 |
| [run_store.py](../modules/harness/run_store.py), [recording.py](../modules/harness/recording.py) | 이벤트·상태·호출 기록의 저장과 저장 전용 재시도 |
| [preflight_run.py](../modules/harness/preflight_run.py), [run_report.py](../modules/harness/run_report.py) | 정적 사전 점검과 읽기 전용 실행 보고 |
| [inspect_run.py](../modules/harness/inspect_run.py) | 조회·중단 복구·대화 연결 |
| [finalize_run.py](../modules/harness/finalize_run.py), [caller_dialog.py](../modules/harness/caller_dialog.py) | 답변 확정·기록 소유권·여러 실행의 공통 대화 연결 |
| [save_dialog.py](../modules/save_dialog.py) | 질문·답변 원문 저장·중복 방지·공통 번호 관리 |

## 실행 계약

반복 실행은 `m5-v5` 승인 계약을 사용한다. 요청·승인 파일은 일반 템플릿의 존재만으로 유효해지지 않는다. 호출자는 실제 사용자 지시가 작업 범위를 승인했는지 확인하고, 원문·목적·변경할 상대경로·검증 명령·규칙 해시·기록 책임자를 결속한다. 상세 필드는 [운영 가이드](usage.md)와 `run_contracts.py`를 기준으로 한다.

같은 작업 공간에는 한 구현자만 쓰기를 수행한다. 검증과 리뷰는 그 구현 결과에 대응해야 하며, 이후 코드가 바뀌면 이전 통과·승인을 그대로 적용하지 않는다. 검증은 읽기 전용 작업 공간, 명시한 런타임 의존성, 격리된 네트워크와 임시 디렉터리에서 실행한다.

기본 상한은 6라운드·실행 전체 24시간·모델 호출당 6시간이다. 검증에도 별도 시간 제한과 남은 전체 시간이 적용된다. 같은 테스트의 연속 시간 초과는 최초 시도를 포함해 최대 3회이며 코드가 바뀌어도 누적한다. 설정값은 [run_policy.py](../modules/harness/run_policy.py)에 있다. 상한 설정과 실제 장시간 검증 완료는 다르다.

종료 상태는 `success`, `failed`, `limit_reached`, `waiting_for_user`다. CLI·입출력·정리 오류나 미완료 기록을 정상 성공으로 바꾸지 않는다. 상태 복구는 중단된 모델 작업을 자동 재개하지 않는다.

## 기록과 저장 위치

- controller의 `approvals/`: 원문·작업 승인·실행 권한 추적.
- controller의 `runs/`: 호출·검증·이벤트·상태·실행 근거.
- controller의 `dialog/`: 최종 사용자 질문·답변.
- target workspace: 승인된 변경 대상과 보호 테스트. 위 controller 기록과 분리한다.

`harness` 소유 모드에서는 동결된 템플릿 답변을 하네스가 저장한다. `caller` 모드에서는 외부 호출자가 원문 질문과 실제 최종 답변을 한 번 저장하고, 관련된 각 실행에 검증된 연결 영수증을 남긴다. 작업 결과와 대화 저장 상태는 별개이며 `pending_caller`를 저장 완료로 해석하지 않는다.

루트 경로는 코드 위치에서 계산하지만, 실행 요청·승인은 로컬 절대경로와 지침 해시에 연결된다. 다른 설치 위치에서는 새 환경에 맞는 요청·승인을 준비한다. 과거 기록을 이동 가능한 설정으로 사용하지 않는다. 작업 공간 스냅샷은 `.git`도 포함하므로 최초 시험에는 기록 저장소와 분리한 작업 사본을 사용한다.

원본 기록은 [공유 정책](sharing.md)에 따라 로컬에 보관한다. 공개 문서와 시험 데이터에는 실제 대화·계정·인증 정보를 복사하지 않는다.

## 범위 밖 기능

CI 연결, 통합 경로 설정 생성 계층, 범용 백그라운드 서비스, 자동 재개, 작업 공간에 쓰는 빌드, 다른 운영체제 지원은 현재 제공 범위 밖이다. 자식 CLI 출력은 종료 전까지 완전히 영속화되지 않는다. 지원 환경·검증의 의미는 [검증 안내](validation.md)에 정리한다.
