# Loop Harness

[English](README.md) | **한국어**

Loop Harness는 Claude와 Codex CLI를 연결해 구현 → 독립 검증 → 교차 리뷰를 반복하는 도구입니다. 한 에이전트가 승인된 작업을 구현하면 하네스가 지정된 테스트를 실행하고, 다른 에이전트가 같은 코드 상태를 리뷰합니다. 테스트 실패나 리뷰 지적이 있으면 승인된 한도 안에서 다음 라운드를 진행할 수 있습니다.

작업 승인, 실행 근거, 최종 질문과 답변을 구분해 기록합니다. 단발 에이전트 호출, 한도가 있는 반복 실행, 정적 사전 점검, 실행 보고, 기록 복구를 지원합니다.

## 현재 지원 범위

단발 호출, 한도가 있는 구현·리뷰 반복, 승인 확인, 격리 검증, 실행 보고와 기록 복구를 구현했습니다. 회귀 테스트 232개(비밀정보 hook 검사 22개·이메일 정책 검사 27개 포함)와 별도 대화 저장기 검사 18개가 있습니다. 결과와 지원 환경은 [검증 범위와 한계](docs/validation.md)를 참고하세요.

기본 상한은 6라운드, 실행 전체 24시간, 모델 호출당 6시간입니다. 이는 설정된 한도이며, 6시간 모델 호출과 24시간 연속 운영을 검증했다는 뜻은 아닙니다.

## 실행 환경과 준비

- 검증된 환경은 Linux x86_64와 Python 3.12입니다. 현재 검증 런타임은 Windows·macOS·다른 아키텍처를 지원하지 않습니다.
- 하네스는 Python 표준 라이브러리를 사용하며, 하네스 자체에 필요한 외부 Python 패키지는 없습니다.
- 실제 에이전트 반복 실행에는 `PATH`에 있는 `codex`, `claude`, bubblewrap(`bwrap`), 유효한 CLI 인증과 요청 모델 사용 권한이 필요합니다.
- 검증에는 Linux namespace 기능과 사전 점검이 확인하는 런타임 파일이 필요합니다. 테스트는 읽기 전용 대상 작업 폴더, 격리된 네트워크, 전용 임시 디렉터리를 사용합니다. 대상 폴더에 결과물을 써야 하는 빌드는 현재 검증 지원 범위 밖입니다.
- 대화 저장기 테스트 스크립트에는 Bash가 필요합니다. 비밀정보 검사에는 별도로 Gitleaks를 사용합니다.

프로젝트를 복제하거나 복사한 뒤 루트 디렉터리에서 터미널을 여세요. 폴더 이름은 `loop_harness`가 아니어도 됩니다. 해당 디렉터리에서 다음 초기 확인 명령을 실행합니다.

```sh
python3 --version
command -v codex claude bwrap
python3 -B -m modules.harness.run_loop --show-defaults
python3 -B -m modules.harness.preflight_run --help
```

마지막 두 명령은 모델을 호출하지 않습니다. 정책 기본값과 명령 사용법을 표시하며, 실제 실행 요청을 검증하지는 않습니다.

## 작업 준비와 실행

에이전트 작업 전에 최우선 지침인 [AGENTS.md](AGENTS.md)를 읽으세요. 상세 [운영 가이드](docs/usage.md)에 요청 필드, 승인 기록, 런타임 경로, 대화 기록 책임을 설명했습니다. 상세 가이드는 현재 한국어로 제공됩니다.

1. 대상 작업 폴더와 보호할 테스트를 준비합니다. controller의 `approvals/`, `runs/`, `dialog/`는 대상 폴더 밖에 둡니다. 첫 시험에는 Git 메타데이터가 없는 별도 작업 사본을 사용하세요. 현재 작업 폴더 스냅샷에는 `.git`이 있으면 포함됩니다.
2. intake로 사용자 원문을 보존합니다. 운영 가이드에 따라 사용자가 실제 승인한 범위의 새 요청과 승인 기록을 준비합니다. JSON 파일을 만드는 것만으로 승인이 생기지는 않습니다.
3. 아래 명령으로 정적 사전 점검을 수행하고, 승인된 요청을 실행한 뒤 결과를 조회합니다.

대문자 자리표시자는 실제 파일과 새 실행 ID로 바꾸세요. 사용자 메시지 원본과 승인 기록은 `approvals/` 아래에 둡니다.

```sh
python3 -B -m modules.harness.intake --question-file approvals/TASK_ID/user-message.txt
python3 -B -m modules.harness.preflight_run --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.harness.run_loop --request REQUEST.json --approval APPROVAL.json
python3 -B -m modules.harness.inspect_run RUN_ID --report
```

요청을 준비할 때 intake가 반환한 `input_ref`를 사용합니다. 요청·승인 필드는 [run_contracts.py](modules/harness/run_contracts.py)에 정의되어 있습니다. 일반 `run_loop` 명령은 이 프로젝트를 controller로 사용하며, 시험용 wrapper가 아닌 순수 실행 요청을 받습니다.

사전 점검의 `ready`는 정적 확인 결과입니다. 인증, 모델 제공 여부, 네트워크 접근, 실제 sandbox 실행은 아직 확인하지 않은 상태입니다. 보고에서는 `execution.status`로 작업 결과를, `dialog.recording_status`로 최종 답변 기록 상태를 확인하세요. 보고 명령의 성공이 작업 성공을 뜻하지는 않습니다. caller가 대화 기록을 맡는 모드에서는 운영 가이드에 따라 최종 답변을 저장하고 각 실행에 저장된 대화를 연결해야 합니다.

## 경로와 다른 PC에서의 사용

기본 프로젝트 루트는 소스 파일 위치에서 계산합니다. README 링크와 프로젝트 내부 경로는 고정된 설치 디렉터리나 `loop_harness`라는 폴더 이름에 의존하지 않습니다.

다만 실행 요청과 승인은 로컬 절대경로, 지침 파일 해시, 사용자 승인에 연결됩니다. 다른 PC에서는 그 환경의 작업 폴더와 런타임 경로를 사용해 새 요청·승인을 준비하세요. 기존 실행 기록은 과거 근거이며 이동 가능한 설정 파일이 아닙니다. 하나의 루트 경로에서 모든 요청을 자동 생성하는 통합 설정 계층은 아직 구현하지 않았습니다.

## 프로젝트 구조

| 경로 | 역할 |
|---|---|
| `modules/harness/` | 실행, 검증, 어댑터, 보고, 복구 |
| `modules/save_dialog.py` | 공통 질문·답변 저장기 |
| `modules/tests/` | 회귀 테스트와 합성 시험 자료 |
| `docs/` | 공개 사용법·구조·검증·공유 가이드 |
| `AGENTS.md`, `rules/` | 에이전트 지침과 사람용 규칙 문서 |
| `approvals/` | 로컬 사용자 원문, 승인, 실행 권한 추적 |
| `runs/` | 로컬 실행 상태와 근거 |
| `dialog/` | 로컬 질문·답변 기록 |

## 로컬 기록과 비밀정보 검사

[.gitignore](.gitignore)는 로컬 대화·승인·실행 기록, 실제 시험에서 생성된 작업 폴더, 환경변수 파일, 캐시를 제외합니다. 세 기록 디렉터리는 공유를 위한 `.gitkeep`만 남깁니다. 제외 규칙은 Git이 이미 추적하는 파일을 제거하지 않으므로, 공개 전 실제 스테이징 파일을 확인하세요.

내부 milestone 메모와 개인 환경·계정 정보는 로컬에 보관합니다. `docs/`에서는 `.gitignore`에 명시한 공개 가이드만 공유합니다. 대화·승인 경위·실행 원본은 문서에 옮겨 적어도 비공유 대상입니다. 합성 예제와 사적 정보를 제거한 기술 요약을 사용하세요. [공유 정책](docs/sharing.md)을 참고하세요.

[.gitleaks.toml](.gitleaks.toml)은 Gitleaks의 기본 탐지 규칙을 확장하며, Gitleaks v8.30.1에서 검증했습니다. Gitleaks를 `PATH`에서 사용할 수 있도록 준비한 뒤 프로젝트 루트에서 현재 디렉터리를 검사합니다.

```sh
gitleaks dir --config .gitleaks.toml --redact .
```

Git 저장소를 초기화한 뒤에는 다음 명령으로 스테이징된 변경을 검사합니다.

```sh
gitleaks git --pre-commit --staged --config .gitleaks.toml --redact .
```

커밋이 만들어진 뒤에는 매번 push하기 전에 로컬에서 확인할 수 있는 모든 참조가 가리키는 이력을 검사합니다.

```sh
gitleaks git --log-opts="--all" --config .gitleaks.toml --redact .
```

탐지 결과와 검사 오류를 해결한 뒤 push하세요. `pre-commit`·`pre-push`는 각 명령의 `--no-verify`로 건너뛸 수 있습니다. merge·rebase·cherry-pick으로 변경을 가져오는 경로에서는 커밋 시점의 검사가 보장되지 않습니다. 이력 검사는 hook을 보완하며, 아래 파일 형식의 탐지 한계를 없애지는 않습니다.

기존 Git 작업 사본에서 자동 검사를 활성화하려면 각 PC에서 프로젝트 hook을 설치합니다.

```sh
python3 modules/security/install_hooks.py --check
python3 modules/security/install_hooks.py
git config --local --get core.hooksPath
```

`--check`는 설정을 바꾸지 않고 실행 조건과 충돌을 확인합니다. 설치 시 해당 저장소의 로컬 hook 경로를 `modules/security`로 지정하며, Git 초기화나 Gitleaks 설치는 하지 않습니다. 두 hook에는 실행 권한이 있어야 하고, Python 3와 Gitleaks는 `PATH`에서 찾을 수 있어야 합니다. 기존 hook 경로가 다르거나 기본 hook 디렉터리에 기존 hook 파일이 있으면 덮어쓰지 않고 중단합니다. 같은 설정으로 설치를 반복해도 문제가 없습니다.

pre-commit hook은 실제 적용될 작성자·커미터 이메일을 확인한 뒤 최초 커밋을 포함해 스테이징된 변경을 검사합니다. 비밀정보 탐지, 검사 도구·설정 누락, 검사 오류가 있으면 커밋을 차단합니다. 탐지된 비밀값은 가리고, 파일 수정이나 스테이징은 하지 않습니다. 비밀정보를 스테이징했다면 제거한 파일을 다시 스테이징한 뒤 재시도하세요. 검사는 작업 폴더의 `.gitleaks.toml`과 Git이 선택한 인덱스를 사용합니다.

비밀정보 검사기는 Git diff를 검사하며 문서의 텍스트를 추출하지 않습니다. 검사 통과만으로 바이너리, Office 문서(`.docx`, `.pptx`, `.xlsx`), PDF에 비밀정보가 없다고 판단할 수 없습니다. 압축 내부 검사의 기본값은 비활성(`--max-archive-depth=0`)이며 현재 명령은 이를 활성화하지 않으므로, ZIP 내부도 자동으로 풀어 검사하지 않습니다. 이런 파일은 공유 전에 내용과 별도로 추출한 텍스트를 확인하고, 불필요한 생성 문서는 저장소에서 제외하세요.

이메일 정책은 기본적으로 GitHub noreply 주소 형식과 커미터 역할의 정확한 `noreply@github.com` 시스템 주소를 허용합니다. 이 예외가 GitHub에서 생성된 커밋임을 인증하지는 않습니다. 다른 주소는 공개 사용을 결정한 뒤 저장소 로컬 `harness.publicEmail`에 명시해야 합니다. pre-push hook은 전송 대상 모든 커밋의 작성자·커미터와 annotated tag의 tagger를 검사합니다. 새 원격 ref는 전체 조상 이력을 검사하며, 불완전한 이력은 차단합니다. 검사기는 실제 이메일을 출력하거나 기존 커밋을 수정하거나 GitHub 계정 설정을 조회하지 않습니다. 로컬 정책 통과가 주소 소유권이나 GitHub의 수락을 보장하지 않습니다. 이력의 비밀정보 검사는 별도 단계로 유지합니다. [이메일 정책과 기존 커밋 처리 안내](docs/sharing.md)를 참고하세요.

hook 소스는 Git으로 공유되지만 각 작업 사본에서 설치해야 합니다. 설치 전에 `modules/security/`를 확인하세요. fetch한 변경을 merge·rebase·checkout으로 적용하기 전에도 이 디렉터리의 변경과 새로 추가되는 실행 가능한 hook 이름을 확인하세요. 업데이트 시에는 확인 없이 pull을 적용하기보다 fetch 후 먼저 검토합니다. 지정된 디렉터리에 `post-merge` 같은 실행 가능한 hook이 추가되면 해당 Git 이벤트에서 실행될 수 있기 때문입니다. CI 연결은 후속 작업입니다. 인증 정보와 실제 대화 기록은 로컬에 보관하세요. [hook 운영과 공유 안내](docs/sharing.md)을 참고하세요.

## 개발 검증

프로젝트 루트에서 실행합니다.

전체 회귀나 OS 격리 검사를 실행할 프로젝트의 실제 경로는 `/tmp`·`/var/tmp` 밖에 두세요. 읽기 격리 검사가 프로젝트 내부에 의존 파일을 만들며, 검증기는 전용 임시 공간 아래의 런타임 경로를 거부합니다. 경로 오류와 환경 제한에 따른 skip의 차이, 격리 검사 2개의 실행 명령과 결과는 [검증 범위와 한계](docs/validation.md)를 참고하세요.

```sh
python3 -B -m unittest discover -s modules/tests -p 'test_*.py' -v
bash modules/tests/test_save_dialog.sh
```

이 검사는 합성 시험 자료를 사용하며 실제 모델 호출이 필요하지 않습니다. 비밀정보 검사 테스트는 임시 로컬 Git 저장소를 사용합니다. 그중 실제 검사 도구를 사용하는 테스트는 Gitleaks가 필요하며, 없으면 건너뜁니다. 호스트가 필요한 namespace 동작을 제한하면 OS 격리 검사가 건너뛰어질 수 있습니다. 건너뛴 검사는 통과로 계산하지 않습니다. 실제 CLI 검증은 운영 가이드의 별도 절차입니다.

## 라이선스

이 프로젝트는 [MIT License](LICENSE)를 따릅니다.

## 관련 문서

- [운영 가이드](docs/usage.md)
- [검증 범위와 한계](docs/validation.md)
- [구조와 계약](docs/architecture.md)
- [사람용 규칙](rules/default_rules.md)

동작이나 사용법을 바꿀 때는 이 파일과 [README.md](README.md)를 함께 수정하세요. 언어 링크는 별도 문서를 열며, 번역은 수동으로 관리합니다.
