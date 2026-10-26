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

설치는 로컬 `core.hooksPath=modules/security`를 설정한다. 기존의 다른 hook 경로나 기본 hook 파일이 있으면 보존하고 중단한다. 반복 설치는 같은 설정을 다시 쓰지 않는다. Git 초기화·Gitleaks 다운로드·전역 설정 변경은 하지 않는다. 프로젝트가 Git 루트이고 `pre-commit`·`pre-push`가 실행 가능하며 Python 3와 Gitleaks가 PATH에 있어야 한다. 이미 이 hook 경로가 설정된 사본은 새 hook 파일이 배치되면 해당 검사도 적용된다.

프로젝트 루트에서 작업 디렉터리 또는 후보만 복사한 디렉터리를 검사한다.

```sh
gitleaks dir --config .gitleaks.toml --redact .
```

Git 저장소에서 스테이징된 변경을 검사한다. [pre-commit](../modules/security/pre-commit)은 아래의 이메일 정책을 먼저 확인한 뒤 최초 커밋을 포함해 스테이징된 변경을 검사하며, Git이 선택한 인덱스를 사용한다. 탐지·도구/설정 누락·검사 오류는 커밋을 차단한다. 출력은 비밀값을 마스킹하며 파일 내용을 수정하거나 스테이징하지 않는다.

```sh
gitleaks git --pre-commit --staged --config .gitleaks.toml --redact .
```

커밋이 존재한 뒤 매번 push 전에 로컬의 모든 참조에서 접근 가능한 이력을 검사한다. 탐지와 검사 오류를 해결한 뒤 공유한다.

```sh
gitleaks git --log-opts="--all" --config .gitleaks.toml --redact .
```

이 비밀정보 검사는 가져오지 않은 원격 이력을 확인하지 않는다. `pre-commit`·`pre-push`는 각 명령의 `--no-verify`로 우회할 수 있고 hook 설정 자체를 바꿔 우회할 수도 있다. 모든 merge·rebase·cherry-pick 경로에 커밋 전 검사가 실행되는 것은 아니므로 push 전 저장된 이력도 검사한다. CI나 서버 측 강제 검사는 제공하지 않는다.

## 커밋 메타데이터의 이메일 정책

[이메일 검사기](../modules/security/check_emails.py)는 기본적으로 GitHub `users.noreply.github.com` 주소 형식을 허용한다. 실제 사용할 주소는 GitHub 계정의 Settings → Emails에서 확인한다. 사용자명만 있는 구형 주소, 숫자 ID와 사용자명을 조합한 주소, bot 주소 형식을 지원하지만 주소 소유권은 검증하지 않는다. 다른 주소는 공개해도 되는 주소임을 확인한 후 저장소 로컬 `harness.publicEmail`에 정확한 값으로 추가해야 한다. 전역 허용 목록과 와일드카드는 사용하지 않는다.

GitHub 웹에서 만든 커밋의 시스템 커미터를 지원하기 위해 **커미터 역할의 정확한 `noreply@github.com` 주소**도 기본 허용한다. 이 예외는 author·tagger나 `github.com`의 다른 주소에 적용하지 않는다. 이 주소가 포함된 초기 커밋을 조상으로 가진 새 브랜치·태그도 검사할 수 있다. 주소 허용은 이메일 정책일 뿐 GitHub가 실제로 생성하거나 서명한 커밋인지 인증하는 기능은 아니다.

```sh
# 자리표시자를 계정에서 확인한 실제 noreply 주소로 바꾼다.
git config --local user.email "YOUR_GITHUB_NOREPLY_EMAIL"

# 일반 이메일을 공개하기로 명시적으로 결정한 경우에만 사용한다.
git config --local --add harness.publicEmail "public@example.org"
```

`user.email`은 앞으로 만들 커밋의 기본 신원이고 `harness.publicEmail`은 공개 허용 정책이다. 설정만 바꾸어도 기존 커밋의 이메일은 바뀌지 않는다. 허용 목록에 주소를 추가하는 것은 해당 주소를 숨기거나 GitHub의 이메일 보호 설정을 해제하는 동작이 아니다.

- **커밋 전:** `git var GIT_AUTHOR_IDENT`·`GIT_COMMITTER_IDENT`로 실제 적용될 신원을 읽는다. 환경변수, `--author`, amend 시 유지되는 작성자도 검사한다.
- **push 전:** [pre-push](../modules/security/pre-push)가 Git에서 받은 각 ref의 실제 로컬·원격 object ID로 전송 범위를 정한다. 최신 커밋만 검사하지 않고 해당 범위의 모든 커밋 작성자·커미터와 전송 대상 annotated tag의 tagger를 확인한다. merge의 다른 부모 이력도 포함한다.
- **새 원격 ref:** 원격 기준점이 없으므로 해당 ref에서 도달 가능한 전체 이력을 보수적으로 검사한다. 다른 원격의 tracking ref를 근거로 검사를 생략하지 않는다. 삭제만 하는 ref는 새 이력을 공개하지 않으므로 건너뛴다.
- **불완전한 이력:** 원격 기준 object가 로컬에 없거나 shallow·graft 이력인 경우 실패로 처리한다. 필요한 이력을 가져온 뒤 다시 확인한다. replace ref가 원본 작성자를 가리지 못하도록 원본 object를 읽는다. commit을 가리키지 않는 ref는 지원하지 않는다.

기존 커밋을 바꾸지 않고 직접 검사할 수도 있다.

```sh
python3 -B modules/security/check_emails.py prospective
python3 -B modules/security/check_emails.py commit HEAD
python3 -B modules/security/check_emails.py range --base origin/main --head HEAD
```

`range`의 `--base`를 생략하면 전체 도달 가능한 이력을 검사한다. 수동 명령의 `origin/main`은 로컬 tracking 정보이며 최신 원격 상태를 보장하지 않는다. 실제 pre-push는 Git이 전달한 원격 object ID를 사용한다.

JSON 결과의 `email_policy`는 로컬 이메일 정책 결과다. 오류 출력과 위반 목록에 실제 이메일을 출력하지 않는다. `github_account_settings`와 `email_ownership`은 `not_checked`, `secret_scan`은 `not_run_by_this_check`로 표시한다. GitHub 계정 API를 호출하지 않으며 이 검사 통과가 GitHub의 수락이나 모든 개인정보의 비공개를 보장하지 않는다. 파일·메시지·작성자 이름 등 다른 정보와 Gitleaks 검사는 별도로 검토한다.

검사기는 기존 커밋을 자동 수정하지 않으며, 커밋이 이미 공개됐는지도 판정하지 않는다. 이미 공개된 이력이 차단되면 먼저 주소·역할과 공개 허용 정책을 검토한다. 이력 재작성이 필요하다면 협업자와 영향을 확인하고 별도 승인을 받아야 하며, 오류 메시지만 보고 자동으로 재작성하거나 강제 push하지 않는다.

아직 공유하지 않은 커밋에서 개인 이메일을 빼려면 승인된 noreply 주소 등으로 작성자와 커미터를 교체해 커밋을 다시 만들 수 있다. 커밋 ID가 바뀌며, 후속 커밋 하나만 추가해도 이전 커밋의 이메일은 그대로 남는다. 이전 object는 로컬 reflog·백업 등에 남을 수 있고, 이미 공개된 사본의 정보까지 지워지는 것은 아니다. 커밋 메시지의 `Co-authored-by`·`Signed-off-by` 등 trailer에 포함된 이메일은 이 메타데이터 검사의 대상이 아니며 별도로 검토한다.

## 검사와 업데이트의 한계

현재 비밀정보 hook 검사는 Git diff를 검사하며 Office·PDF 문서의 텍스트를 추출하지 않는다. 바이너리·Office·PDF의 검사 통과만으로 비밀값 부재를 보장할 수 없다. 압축 내부 탐색 기본값은 `--max-archive-depth=0`이며 현재 명령은 이를 활성화하지 않으므로 ZIP 내부도 자동으로 풀어 검사하지 않는다. 필요한 문서의 내용·추출 텍스트·메타데이터를 별도로 검토하고 불필요한 생성 결과는 공유하지 않는다.

설치 전 `modules/security/`를 검토한다. 업데이트할 때도 fetch한 변경에서 이 디렉터리의 수정·새 실행 가능한 hook·권한을 확인한 뒤 merge·rebase·checkout으로 적용한다. 설정된 디렉터리에 다른 hook이 추가되면 해당 Git 이벤트에서 실행될 수 있다.

비밀정보 검사 통과와 사적 정보 제외는 별도 확인이다. 개인 대화나 구독 상태를 일반 문장으로 적으면 자동 탐지되지 않을 수 있다. 원본의 위치·이름·해시를 공개 문서에 복사하는 대신 [검증 범위](validation.md)처럼 재현 방법·결과·한계만 설명한다.
