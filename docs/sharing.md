# 공유 정책과 비밀정보 검사

공유 기준은 파일의 위치가 아니라 내용이다. [AGENTS.md](../AGENTS.md)와 [사람용 규칙](../rules/default_rules.md)의 SHR-001을 적용한다.

## 범주별 기준

| 범주 | 정책 |
|---|---|
| 실제 대화·인용·요약 | 로컬 보관 |
| 승인·권한 요청·응답·출처 추적 | 로컬 보관 |
| 실행 입력·출력·로그·상태 원본 | 로컬 보관. 공개 검증 요약은 별도 작성 |
| 토큰·비밀번호·개인 키·인증 파일 | 비공유 |
| 개인 환경·계정·구독·비용 선호·사용량 | 로컬 보관. 필요한 설정은 일반 예시로 제공 |
| 내부 개발 메모·대화 기반 검토·승인 경위 | 원본은 로컬 보관. 필요한 기술 결정은 사적 맥락을 제거해 설명 |
| 프로그램 코드·공통 설정 | 비밀값 없는 상태로 공유 |
| 테스트·예제 | 합성 데이터만 공유. 생성된 실제 실행 자료는 제외 |
| 사용·설계·검증 문서 | 개인 대화·운영 정보를 제거한 공개 가이드만 공유 |

`.gitignore`는 `dialog/`, `approvals/`, `runs/`에서 빈 `.gitkeep`만 허용한다. 내부 milestone 문서는 원래 위치에서 보존하고 공유하지 않는다. `docs/`의 공개 목록은 `usage.md`, `architecture.md`, `validation.md`, `sharing.md`다. 새 공개 문서를 추가할 때는 내용 검토와 제외 규칙의 허용 목록 갱신을 함께 수행한다.

개인 설정·인증 디렉터리와 환경 파일도 제외한다. 이 경로 규칙이 다른 위치에 복사된 인증값·사적 내용을 자동 판별하지는 않는다. `.env.example`·`.env.sample` 등 공유 예시에는 실제 값을 넣지 않는다. 기존 로컬 원본을 삭제하거나 변경해 공개용으로 만들지 않는다.

이미 추적된 파일은 `.gitignore`를 추가해도 계속 추적된다. 공유 후보, 추적된 제외 대상, 실제 스테이징 내용을 각각 확인한다.

```sh
git ls-files --cached --others --exclude-standard -z
git ls-files --cached --ignored --exclude-standard -z
git diff --cached --name-only
```

후보 목록의 첫 명령은 NUL로 경로를 구분한다. 추적·미추적 목록의 합집합을 사용하며, 파일 수 자체를 합격 기준으로 삼지 않는다. 후보만 별도 디렉터리에 복사해 검사하면 로컬 원본 기록과 공개 대상을 구분할 수 있다.

## Hook 설치와 검사

Gitleaks v8.30.1을 기준으로 검증한 설정은 [.gitleaks.toml](../.gitleaks.toml)이다. 기본 탐지 규칙을 상속하며 프로젝트 전용 allowlist는 두지 않는다. Git 작업 사본마다 [설치 도구](../modules/security/install_hooks.py)를 실행한다.

```sh
python3 modules/security/install_hooks.py --check
python3 modules/security/install_hooks.py
git config --local --get core.hooksPath
```

설치는 로컬 `core.hooksPath=modules/security`를 설정한다. 기존의 다른 hook 경로나 기본 hook 파일이 있으면 보존하고 중단한다. 반복 설치는 같은 설정을 다시 쓰지 않는다. Git 초기화·Gitleaks 다운로드·전역 설정 변경은 하지 않는다. 프로젝트가 Git 루트이고 hook이 실행 가능하며 Gitleaks가 PATH에 있어야 한다.

프로젝트 루트에서 작업 디렉터리 또는 후보만 복사한 디렉터리를 검사한다.

```sh
gitleaks dir --config .gitleaks.toml --redact .
```

Git 저장소에서 스테이징된 변경을 검사한다. [pre-commit](../modules/security/pre-commit)은 최초 커밋을 포함해 이 변경을 검사하며, Git이 선택한 인덱스를 사용한다. 탐지·도구/설정 누락·검사 오류는 커밋을 차단한다. 출력은 비밀값을 마스킹하며 파일 내용을 수정하거나 스테이징하지 않는다.

```sh
gitleaks git --pre-commit --staged --config .gitleaks.toml --redact .
```

커밋이 존재한 뒤 매번 push 전에 로컬의 모든 참조에서 접근 가능한 이력을 검사한다. 탐지와 검사 오류를 해결한 뒤 공유한다.

```sh
gitleaks git --log-opts="--all" --config .gitleaks.toml --redact .
```

이 검사는 가져오지 않은 원격 이력을 확인하지 않는다. `pre-commit`은 `--no-verify`로 우회할 수 있고 merge·rebase·cherry-pick 경로에서 실행이 보장되지 않는다. 현재 pre-push hook이나 CI 강제 검사는 제공하지 않는다.

## 검사와 업데이트의 한계

현재 hook은 Git diff를 검사하며 Office·PDF 문서의 텍스트를 추출하지 않는다. 바이너리·Office·PDF의 검사 통과만으로 비밀값 부재를 보장할 수 없다. 압축 내부 탐색 기본값은 `--max-archive-depth=0`이며 현재 명령은 이를 활성화하지 않으므로 ZIP 내부도 자동으로 풀어 검사하지 않는다. 필요한 문서의 내용·추출 텍스트·메타데이터를 별도로 검토하고 불필요한 생성 결과는 공유하지 않는다.

설치 전 `modules/security/`를 검토한다. 업데이트할 때도 fetch한 변경에서 이 디렉터리의 수정·새 실행 가능한 hook·권한을 확인한 뒤 merge·rebase·checkout으로 적용한다. 설정된 디렉터리에 다른 hook이 추가되면 해당 Git 이벤트에서 실행될 수 있다.

비밀정보 검사 통과와 사적 정보 제외는 별도 확인이다. 개인 대화나 구독 상태를 일반 문장으로 적으면 자동 탐지되지 않을 수 있다. 원본의 위치·이름·해시를 공개 문서에 복사하는 대신 [검증 범위](validation.md)처럼 재현 방법·결과·한계만 설명한다.
