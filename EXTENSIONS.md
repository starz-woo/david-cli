# MCP와 스킬 등록

David CLI 0.2.0은 **MCP stdio 도구**와 **로컬 SKILL.md 지침**을 지원합니다. 둘 다 사용자가 선택한 `setting.json`에 명시적으로 등록합니다. 작업 저장소에서 자동 검색하거나 외부 패키지를 자동 설치하지 않습니다.

## 1. SKILL.md 등록과 사용

ZIP의 `examples/skills/review/SKILL.md`를 사용자 스킬 전용 디렉터리로 복사하세요. 예를 들어 공통 설정이 `~/.david/setting.json`이면 `~/.david/skills/review/SKILL.md`에 두고 설정에 아래 항목을 추가합니다. 기존 model 등 다른 항목은 유지하세요.

```json
{
  "skills": {
    "registered": {
      "review": "skills/review/SKILL.md"
    },
    "enabled": []
  }
}
```

상대 경로는 **설정 파일이 있는 디렉터리 기준**입니다. 절대 경로도 가능합니다. Windows JSON 경로는 `C:/Users/사용자/.david/skills/review/SKILL.md`처럼 `/`를 사용하거나 `\\`로 이스케이프하세요. `~`나 환경 변수의 자동 치환에 의존하지 마세요.

설정 변경 후 `/reload` 또는 재실행합니다. TUI와 `--plain` 모두 지원합니다.

```text
/skills
/skill review
현재 변경사항을 리뷰해줘. 수정은 하지 마.
/skill off
```

- `/skills`: 등록 목록과 현재 선택 표시. 모델 요청은 하지 않습니다.
- `/skill review`: 그 스킬 하나를 선택합니다. 이전 선택에 추가하는 명령은 아닙니다.
- `/skill off`: 모든 스킬 해제.
- 선택 변경은 기존 대화 컨텍스트를 비웁니다. TUI에서는 새 세션 ID·저장 OFF로 바뀌며 관리된 파일 undo는 유지됩니다. 기존 디스크 변경은 그대로입니다.
- 시작 시 기본 적용하려면 `enabled: ["review"]`를 설정합니다.
- 세션별 여러 스킬 선택은 `david --skill review --skill another`처럼 옵션을 반복합니다. 설정의 enabled 목록을 대체하며 파일을 수정하지 않습니다.
- 단발 요청에도 사용할 수 있습니다: `david --skill review --read-only --prompt "현재 코드의 오류 가능성을 검토해줘"`. 이 명령은 실제 설정된 LLM 서버에 요청합니다.

스킬 파일 예시:

```markdown
---
name: review
description: 코드 변경의 오류와 회귀를 검토합니다.
---

# 코드 리뷰

관련 파일을 읽고 확인된 결함과 추측을 구분한다.
결함에는 경로, 영향, 재현 조건을 적는다.
테스트를 실행하지 않았다면 미실행이라고 쓴다.
사용자 요청 없이 코드를 수정하지 않는다.
```

**스킬은 텍스트 지침이지 Python 플러그인이 아닙니다.** 링크·보조 파일을 자동으로 읽거나 scripts를 실행하지 않으며 권한을 늘릴 수 없습니다. 완전한 YAML 파서가 아니라 선택적 한 줄 name/description 메타데이터만 읽습니다. 등록 별칭이 frontmatter name보다 우선합니다.

한도: 최대 32개 등록, 8개 동시 선택, 문서당 UTF-8 32 KiB, 합성 지침 64 KiB. 모델 컨텍스트 한도는 별도입니다. 심볼릭 링크·reparse point·hardlink·특수 파일은 거부합니다. **등록 문서의 부모 디렉터리도 모델 파일 도구에서 보호되므로 프로젝트 루트에 SKILL.md를 두지 말고 전용 폴더를 사용하세요.**

## 2. MCP 서버 등록

먼저 신뢰할 수 있는 stdio MCP 서버를 별도로 설치합니다. 설치/업데이트는 David가 자동 수행하지 않습니다. 아래는 **경로를 실제 서버에 맞게 바꿔야 하는 예시**입니다.

```json
{
  "mcp": {
    "servers": {
      "internal": {
        "command": ["{python}", "/absolute/path/to/trusted_mcp_server.py"],
        "env": {},
        "timeout_seconds": 30,
        "max_response_bytes": 262144
      }
    }
  }
}
```

- `command`는 argv 배열입니다. 첫 항목은 실행 파일 절대 경로나 `{python}`입니다. `{python}`은 David를 실행하는 인터프리터이므로 서버 의존성을 다른 환경에 설치했다면 그 환경의 Python 절대 경로를 쓰세요.
- Node 서버는 node 절대 경로와 서버 JS 엔트리를 지정합니다. shell 문자열, `npx` 자동 다운로드, `.cmd`/`.bat` shim은 사용하지 마세요.
- 인자의 상대 경로는 **작업공간 기준**이므로 서버 스크립트는 절대 경로를 권장합니다.
- 서버 timeout은 1~120초, 응답 상한은 1 KiB~2 MiB입니다. 최대 32개 서버를 등록할 수 있습니다.
- 환경 변수는 `"env": {"SERVICE_TOKEN": "MY_SERVICE_TOKEN"}`처럼 **서버 변수명 → 호스트 환경 변수명**으로 연결합니다. 토큰 값을 설정에 적지 마세요. 명시하지 않은 임의 환경 비밀은 전달하지 않습니다. 서버가 필요한 PATH 등도 명시적 매핑을 검토하세요.

사용 흐름:

```text
/reload
/mcp
internal MCP 서버의 도구 목록을 확인해줘.
```

`/mcp`는 등록 이름만 보여주며 프로세스를 시작하지 않습니다. 모델에 `mcp_list_tools`와 `mcp_call` 두 도구가 노출됩니다. 목록 조회로 첫 연결할 때도 **서버 프로세스 시작 승인**이 필요합니다. 도구 호출마다 서버·도구·인자가 표시되며 별도 승인을 요구합니다. 비대화형에서는 승인할 수 없으므로 자동 실행되지 않습니다.

일반 인자 형식은 다음과 같습니다. 사용자가 직접 이 JSON을 입력할 필요는 없으며 모델이 목록의 schema를 보고 구성합니다.

```json
{"server":"internal","tool":"tool_name","arguments_json":"{\"query\":\"example\"}"}
```

서버 출력은 `mcp_result` 아래에 담고 `isError`는 David의 실패 표시·요약으로 연결합니다. 취소·시간 초과 시 연결을 폐기하고 프로세스 정리를 시도합니다. 실패한 호출을 자동 재시도하지 않습니다. 이미 시작 승인된 서버에서 개별 호출을 거부하면 그 호출은 전송하지 않지만 기존 서버 연결은 유지합니다. 취소는 연결을 닫습니다. 서버는 세션 동안 연결을 유지할 수 있고 `/reload`와 종료 시 닫습니다.

## 지원 범위와 보안

- **지원:** stdio JSON-RPC initialize/initialized, tools/list의 제한된 페이지 순회, tools/call, text·structuredContent 등 제한 크기의 결과 전달.
- **미지원:** 원격 Streamable HTTP/SSE 전송, OAuth, resources/prompts API, sampling, elicitation, tasks. 서버의 instructions를 시스템 지침으로 적용하지 않고 resource URI를 자동 조회하지 않습니다.
- 도구별 전체 JSON Schema 검증기는 없습니다. David는 일반 호출 구조·JSON 유효성·크기·중첩을 검사하고 실제 도구 인자 schema 검증은 서버에 맡깁니다.
- **MCP 서버는 임의 코드를 실행하는 신뢰 대상입니다.** 시작 승인 이후 서버 자체의 파일/네트워크 동작을 호출별 승인으로 통제할 수는 없습니다. David 파일 도구의 경로 차단이나 undo가 MCP 서버에도 적용된다고 생각하면 안 됩니다.
- MCP 변경은 undo 대상이 아닙니다. `--read-only`에서는 서버가 스스로 읽기 전용이라고 주장해도 MCP 시작·호출을 모두 막습니다.
- 스킬 본문과 MCP 결과도 선택한 LLM 서버로 전달될 수 있습니다. 기밀 데이터와 서버 로그 정책을 검토하세요.
- Windows용 경로/프로세스 분기는 포함했지만 실제 Windows와 IME 검증은 아직 하지 않았습니다.

## 로컬 오프라인 검증

의존성이 설치된 소스 폴더에서:

```sh
python -m david --check
```

설정 구조만 검사하며 모델/MCP 서버나 스킬 문서의 실제 동작을 보증하지 않습니다. 전체 테스트는 macOS/Linux에서 `sh scripts/run_tests.sh`, Windows에서는 `scripts\run_tests.cmd`입니다. `tests/test_mcp.py`는 실제 fixture 프로세스와 통신하고 `tests/test_extensions.py`는 CLI/TUI 연결·스킬 변경·MCP 실패 저장을 검사합니다. fixture는 실제 사내 서비스나 LLM이 아닙니다.
