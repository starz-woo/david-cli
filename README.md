# David CLI 0.2.0

사내 OpenAI 호환 LLM 서버를 연결하는 독립형 코딩 에이전트입니다. 엔드포인트·모델·권한·명령·답변 규칙을 `setting.json`으로 바꿉니다. Python 3.11+와 작은 Python 라이브러리(prompt_toolkit, pathspec, 간접 의존성 wcwidth)를 사용하며 Hermes와 Docker에 의존하지 않습니다.

**초기 제품이며 OS 샌드박스나 보안 인증 제품이 아닙니다.** 파일 도구의 경로 검사와 승인은 호스트 코드가 강제하지만, 승인된 명령은 사용자 권한으로 실행합니다. 신뢰하는 저장소의 별도 작업 사본에서 먼저 사용하세요. 코드·프롬프트·도구 출력은 사용자가 설정한 서버로 전송됩니다.

## 설치 및 시작

### GitHub 소스에서 설치

Python 3.11+와 pip가 있는 환경에서 저장소를 clone한 뒤 설치합니다. 의존성 다운로드에는 인터넷이 필요합니다. 가상환경 사용을 권장합니다.

```sh
git clone https://github.com/starz-woo/david-cli.git
cd david-cli
python -m pip install .
david --init
david --config-path
```

macOS/Linux에서는 `python` 대신 `python3`를 사용하세요. 최초 도입 PR이 병합되기 전에는 `git switch feat/david-cli-initial`로 구현 브랜치를 선택해야 합니다. GitHub의 자동 생성 Source ZIP에는 오프라인 wheel이 포함되지 않습니다.

### 별도 오프라인 배포 ZIP에서 설치

ZIP을 풀고 해당 폴더에서 한 번 설치합니다. 기존 0.1.1 설치도 같은 명령으로 업그레이드합니다. 사용자 설정은 덮어쓰지 않습니다.

```sh
python -m pip install --user --no-index --find-links wheels --require-hashes -r requirements-lock.txt
david --init
david --config-path
```

Windows는 `python`, macOS/Linux는 `python3`를 사용하세요. Python/pip는 별도 준비해야 합니다. ZIP에는 실행 패키지와 의존성 wheel이 포함되어 **설치 중 인터넷 접근이 필요 없습니다**. PATH, 격리 설치, 소스 실행은 [INSTALL.md](INSTALL.md)를 참고하세요.

공통 설정은 Windows `%USERPROFILE%\.david\setting.json`, macOS/Linux `~/.david/setting.json`입니다. 아래 주소와 모델명은 예시입니다.

```json
{
  "model": {
    "base_url": "https://llm.example.internal/v1",
    "name": "YOUR_SERVED_MODEL"
  }
}
```

생략한 항목은 기본값으로 채워집니다. `base_url`에는 `/chat/completions` 직전까지 적고 모델 이름은 서버의 값을 그대로 사용하세요.

```sh
cd my-project
david
```

현재 디렉터리가 작업공간입니다. 프로젝트 내부의 설정·프롬프트를 자동으로 신뢰하거나 읽지 않습니다. 대화형 터미널에서는 전체 화면 TUI가 기본이며 `--plain`은 기존 한 줄 콘솔입니다. `--prompt`는 단발 실행입니다.

```sh
david --check
david --read-only
david --workspace 다른프로젝트경로
david --settings 별도설정파일.json
david --plain
david --read-only --prompt "도구를 사용하지 말고 OK라고만 답해줘"
```

`--check`는 설정 구조를 오프라인 검사할 뿐 서버 연결 검사가 아닙니다. 마지막 `--prompt` 명령은 실제 서버에 요청합니다. `CHANGE_ME` 모델로는 실행하지 않습니다. 심볼릭 링크를 포함한 작업 루트는 거부하므로 macOS `/var` 같은 별칭 대신 정규 경로를 사용하세요.

## TUI 사용법

- **Enter / Ctrl+S:** 요청 전송. **Alt+Enter / Ctrl+J:** 줄바꿈.
- 여러 줄 붙여넣기를 지원합니다. 터미널의 bracketed-paste 지원이 필요합니다.
- **Tab:** `@파일` 또는 `/명령` 완성. 완성 후보가 없으면 패널 간 포커스 이동. **Shift+Tab:** 이전 패널.
- **PgUp / PgDn:** 출력 스크롤. 승인 패널에 포커스를 옮기면 긴 diff도 스크롤합니다.
- **Ctrl+O:** 도구 상세 출력 접기/펼치기.
- **Ctrl+C:** 실행 중 취소; 유휴 상태에서는 입력창 비우기. **Ctrl+Q / `/quit`:** 취소 후 작업 종료를 기다려 안전하게 닫기.
- 모델 응답 대기, 파일 탐색/편집, 승인 대기, 명령 실행, 완료/실패 상태를 표시합니다. 스트리밍 답변은 완료 전에 실패할 수도 있는 미확정 텍스트입니다.
- diff에서 추가/삭제를 구분합니다. 변경 대상, 명령의 실제 argv·cwd를 읽은 뒤 정확히 **`yes` / `no`**, 또는 **Ctrl+Y / Ctrl+N**으로 결정합니다. 포괄적 자동 승인 옵션은 없습니다.
- 한글/유니코드 버퍼와 좁은 창 리사이즈를 테스트했습니다. **실제 Windows IME 조합, 터미널별 키 전달과 마우스는 사용자 장비에서 별도 확인이 필요합니다.**

```text
@src/main.py 입력 검증 흐름을 설명해줘. 아직 수정하지 마.
@"docs/my notes.md"와 코드를 비교해줘.
해당 문제만 수정하고 test 명령으로 검사해줘.
```

첨부는 현재 파일 내용을 다시 읽어 해시와 함께 서버에 보냅니다. 최대 8개, 합계는 32,768자와 컨텍스트 예산 절반 중 작은 값입니다. 큰 파일은 @첨부 대신 파일명과 필요한 부분을 요청하세요. 보호 경로는 첨부로도 우회할 수 없습니다.

화면 기록은 약 200,000자/300항목으로 제한하고 생략을 표시합니다. 모델 컨텍스트와 저장 세션의 한도는 별도입니다. 컨텍스트 표시는 **문자 수이며 토큰 수가 아닙니다**.

## 명령과 세션

다음 명령은 TUI에서 사용합니다. `--plain`은 `/help /settings /tools /mcp /skills /skill /clear /reload /quit`를 제공합니다.

- `/help`, `/settings`, `/tools`: 도움말·현재 모델/권한·도구 목록.
- `/files`: `.gitignore`를 반영해 파일 완성 목록 새로고침.
- `/changes`: 세션의 관리된 변경 목록. 현재 Git diff 전체가 아니라 파일 도구가 기록한 변경입니다.
- `/edit-last`: 이전 요청을 입력창으로 가져와 수정. `/retry`: 이전 요청을 현재 파일 상태에서 다시 실행.
- `/undo`: 마지막 **변경이 있는 턴**의 관리된 파일 작업을 되돌림. 모든 대상의 현재 상태가 기록과 일치해야 하며 별도 승인이 필요합니다. 사용자 수정이 감지되면 덮어쓰지 않습니다.
- `/save on`: 대화·읽은 코드·undo 원본 스냅샷을 `~/.david/sessions/`에 **평문**으로 저장하고 이후 작업 종료마다 갱신.
- `/save off`: 자동 저장 중지. 기존 저장 파일은 삭제하지 않습니다. **기본값은 OFF**입니다.
- `/sessions`: 현재 작업공간·모델·동작 정책에 맞는 저장 세션 목록.
- `/resume <ID>`: 대화와 undo 기록 복원. 파일을 자동 변경하거나 명령을 재실행하지 않습니다. 저장은 OFF로 복원됩니다.
- `/clear`: 모델 대화·화면 초기화. 파일 변경 및 undo 기록은 유지.
- `/new`: 대화·undo를 새 메모리 세션으로 초기화. 이전 디스크 변경은 유지.
- `/reload`: 설정을 다시 읽고 새 세션 시작. 이전 대화를 새 모델/서버에 전달하지 않습니다. 설정 검증 실패 시 이전 설정 유지.

**취소·재요청·세션 복원은 자동 롤백이 아닙니다.** `/retry` 전 필요하면 `/undo`를 먼저 실행하세요. `/new`·`/reload`·다른 세션 복원 전 이어갈 기록이 필요하면 `/save on`으로 저장하세요. 저장 파일은 암호화하지 않으며 OS 접근 권한과 디스크 암호화로 보호해야 합니다. 파일 하나 최대 8 MiB입니다. 상한/권한 오류로 저장이 실패하면 경고하고 자동 저장을 끄며 메모리 상태는 유지합니다.

**명령이 수정한 파일은 undo 대상이 아닙니다.** 테스트/빌드가 코드를 수정할 수 있으므로 Git/백업은 계속 필요합니다. 여러 파일 변경은 사전 검증과 승인 후 순서대로 적용하며 일반 오류 시 보상 복구를 시도합니다. 프로세스 충돌·전원 장애까지 원자적으로 복구하는 트랜잭션은 아닙니다.

## 파일 작업

- UTF-8 파일 읽기와 SHA-256, 부분 읽기와 명시적인 잘림 표시.
- root/nested `.gitignore` 기반 목록·리터럴 문자열 검색, 선택적 `glob` 파일 필터. 부모가 무시된 디렉터리는 재탐색하지 않습니다. ignore는 탐색 정책이며 보안 경계가 아닙니다. 명시적 파일 읽기는 ignore와 별개입니다.
- 파일 생성·정확한 단일 문자열 치환, 디렉터리 생성, 파일 이동·삭제.
- `apply_patch`: 최대 20개 파일의 **전체 새 내용**과 기존 SHA-256을 받는 구조화된 다중 파일 편집. 외부 unified diff 파서는 아니며 승인 미리보기를 unified diff로 만듭니다.
- 이동 대상 덮어쓰기 및 디렉터리 재귀 삭제는 제공하지 않습니다. 부모 디렉터리는 먼저 명시적으로 생성해야 합니다.
- undo 한도: 턴당 200경로, 세션당 100개 변경 턴/2,000경로/스냅샷 16 MiB. 저장 파일 상한은 그보다 작은 8 MiB입니다.

## 서버·모델·명령 커스터마이징

`model`:
- `base_url`, `name`: 언제든 수정 후 `/reload`.
- `api_key_env`: 키 **값이 아닌 환경 변수명**. 값은 자신의 터미널에서 설정하고 채팅·코드·설정에 붙여 넣지 마세요.
- `stream`: 서버가 SSE를 지원하지 않으면 `false`.
- `timeout_seconds`, `max_tokens`, `temperature`, `max_response_bytes`: 시간·응답 한도와 샘플링.
- `ca_file`: 사내 CA 파일의 절대 경로 권장. TLS 검증 비활성화 옵션은 없습니다.
- `allow_insecure_http`: 루프백 외 HTTP는 기본 거부합니다. 사내망도 HTTPS를 권장합니다. 환경 프록시·리다이렉트·자동 재시도를 사용하지 않습니다.

Native tools가 없는 모델은 `agent.tool_mode: "json"`을 사용합니다. JSON 모드는 완료된 구조 검증 후 출력하며, 잘못된 응답을 추측으로 복구하거나 실행하지 않습니다. 모델 다운로드/서빙, Anthropic 전용 API, 멀티모달은 포함하지 않습니다.

명령은 기본 비활성화입니다. 아래처럼 필요한 고정 recipe를 직접 등록합니다. `commands`를 지정하면 목록 전체를 교체합니다.

```json
{
  "security": { "enable_commands": true },
  "commands": {
    "test": {
      "argv": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
      "timeout_seconds": 60
    }
  },
  "ui": { "theme": "dark", "input_height": 5 },
  "agent": { "system_prompt_file": "prompts/style.example.txt" }
}
```

`{python}`은 **David 실행 인터프리터**입니다. 프로젝트 가상환경의 도구가 필요하면 그 Python의 절대 경로를 등록하세요. 추가 패키지를 자동 설치하지 않습니다. 명령은 최소 환경과 임시 HOME을 사용하므로 사용자 도구 설정이 필요한 빌드는 별도 조정해야 합니다. 실행 파일 검색은 OS 기본 경로로 제한되므로 Homebrew/사용자 도구는 절대 경로를 권장합니다. Shell·`.bat`·`.cmd`는 거부합니다. Windows npm shim 대신 신뢰하는 node 실행 파일과 실제 JS 엔트리를 지정하세요.

UI 테마는 `dark`, `light`, `mono`, 입력 높이는 3~15줄입니다. 테마/높이 변경은 앱을 다시 실행하세요. 커스텀 프롬프트 상대 경로는 **설정 파일 디렉터리** 기준이며, 프롬프트로 권한을 확장할 수 없습니다.

`security.denied_names/denied_globs`, `max_file_bytes`, `max_output_chars`, `max_scan_entries`, `command_output_bytes`, `agent.max_context_chars/max_steps`로 범위와 비용을 조절합니다. 기본 차단 목록을 제거하면 보호가 약해집니다. 파일명 필터는 비밀 탐지기의 대체가 아닙니다.

## MCP 및 스킬

`setting.json`에서 stdio MCP 서버와 로컬 `SKILL.md`를 명시적으로 등록할 수 있습니다. `/mcp`는 서버 목록, `/skills`는 스킬 목록, `/skill NAME`은 선택, `/skill off`는 해제입니다. 시작 시 `--skill NAME`을 반복해 여러 스킬을 적용할 수 있습니다.

MCP는 최초 프로세스 시작과 각 도구 호출에 승인이 필요하며 읽기 전용 모드에서는 차단합니다. MCP 서버는 샌드박스가 아니고 변경은 undo 대상이 아닙니다. 스킬은 텍스트 지침이며 스크립트를 자동 실행하지 않습니다. 원격 HTTP/OAuth MCP는 미지원입니다. 설정 예시·사용 절차·제약은 [EXTENSIONS.md](EXTENSIONS.md)를 보세요.

## 검증과 한계

```sh
sh scripts/run_tests.sh
```

Windows는 `scripts\run_tests.cmd`입니다. 실행 의존성을 먼저 설치하세요. 테스트는 unittest, 루프백 HTTP/TLS fixture, 실제 파일/명령, prompt_toolkit 키 입력 및 POSIX PTY를 사용합니다. fixture는 실제 LLM 출력이 아닙니다. Docker·사내 서버는 필요 없습니다.

실제 사내 모델 품질·속도·SWE-bench 성적은 측정하지 않았습니다. Windows/Linux 실기기, Windows IME, 모든 서버 호환성을 보장하지 않습니다. 상세한 실측 검증은 [VERIFICATION.md](VERIFICATION.md), 보안 범위는 [SECURITY.md](SECURITY.md)를 확인하세요.

## 소스 구조

`mcp.py` stdio MCP / `skills.py` 로컬 스킬 / `extensions.py` 확장 통합 / `config.py` 설정 / `transport.py` HTTP·SSE / `agent.py` 도구 루프 / `workspace.py` 안전 파일 작업·명령·undo / `controller.py` 작업·세션 조정 / `sessions.py` 저장 검증 / `tui.py` 화면·키 입력 / `cli.py` 실행 진입점.

새 도구는 `WorkspaceTools.schemas()`·`execute()` 및 테스트에 등록합니다. 임의 저장소 플러그인의 자동 실행은 제공하지 않습니다. UI와 실행 정책을 분리하여 화면 커스터마이징이 승인·경로 검사를 우회하지 않도록 유지하세요.
