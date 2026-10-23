# Verification — David CLI 0.2.0

## 검증 범위와 환경

macOS에서 실제 파일시스템, 루프백 HTTP/TLS 서버, POSIX PTY와 격리된 Python 가상환경을 사용합니다. 서버 응답은 테스트 fixture이며 실제 사내 모델의 코딩 성능 평가가 아닙니다. Python 3.11 문법 호환성은 배포 시 AST 파싱으로 검사합니다.

런타임 의존성은 prompt_toolkit, pathspec와 간접 의존성 wcwidth입니다. 배포 ZIP에는 플랫폼 독립 wheel과 해시가 포함된 requirements-lock.txt를 넣습니다. 설치 검증은 임시 HOME/PATH에서 수행하며 사용자의 실제 전역 PATH나 설정은 변경하지 않습니다.

## 재현 명령

```sh
PYTHON=.venv/bin/python sh scripts/run_tests.sh
```

Windows에서는 `scripts\run_tests.cmd`를 사용할 수 있습니다. 네이티브 Windows 실행 결과는 아직 없습니다.

최종 ZIP 검증의 전체 테스트 개수·실행 시간·아카이브 SHA256은 함께 생성되는 `verification-0.2.0.json`에 기록합니다. 테스트 실행 시간은 LLM 속도나 생산성 수치가 아닙니다.

## 검증 항목

- 엄격한 설정/JSON 검증, 명시적 설정 로드, 모델 재적용 시 이전 대화 분리.
- 실제 루프백 HTTP/SSE 및 TLS 인증서·호스트 검증, 리다이렉트 거부, 환경 프록시 무시, 요청/응답 크기와 전체 시간 제한.
- Native tool loop와 strict JSON fallback, 중첩 인자를 포함한 전체 도구 묶음 사전 검증, 대화 예산.
- 파일 읽기 → 해시 확인 → 명시적 승인 → 실제 수정 → 실제 Python 검사 명령 → 결과 전달.
- full-screen TUI의 분리 입력, 백그라운드 작업, 스트리밍, 상태 표시, 취소, 승인, 출력 접기, diff, 제한된 transcript.
- 파일 완성에서 선택한 이름을 실제 첨부까지 전달하는 round-trip: 공백, 작은따옴표, 이스케이프된 큰따옴표, 유니코드. 잘못된 참조는 부분 파일 읽기 전에 거부.
- DNS/응답 대기 및 TLS 소켓 교체 경계에서의 취소. 실제 로컬 소켓과 동기화 gate로 TLS 전환 경합 재현.
- 기본 저장 OFF, 명시적 저장/재개, 재개 후 자동 저장 OFF, 새 세션과 모델 변경 시 기록 분리.
- POSIX 저장 backend와 강제 portable backend에서 실제 저장/복원, 링크·hardlink·reparse·특수 파일 거부, JSON/크기/깊이 상한, workspace/model 격리.
- 관리된 파일 변경의 undo, 사용자 편집 충돌 거부, 권한만 바뀌는 undo의 명시적 승인 표시, restrictive umask에서 생성/복구된 디렉터리 권한 일치.
- mkdir/move/delete/multi-file patch와 실패 rollback, gitignore 검색, 실패 명령 종료 코드와 출력 요약.
- 실제 POSIX PTY에서 파일 수정 → 명령 실행 → 세션 저장 → 재실행·복원 → undo.
- 보호 경로, 심볼릭 링크, 하드링크, 특수 파일, macOS Unicode 별칭/파일 identity, 승인 중 파일 변경과 원자 교체 실패.
- 명령의 비밀 환경 제거, 출력 상한, 시간 초과와 일반 자식 프로세스 종료.
- ANSI/OSC 및 방향 제어 문자 무해화, EOF 거부와 Ctrl+C 취소.

## 독립 리뷰

리뷰에서 발견된 파일명 첨부 불일치, TLS 취소 경합, undo 권한 표시 누락, umask/journal 불일치에는 각각 실패 재현 회귀 테스트와 수정이 추가됐습니다. 파일명/TLS 수정은 독립 재검토에서 54개 집중 테스트와 추가 경계 입력·실제 TLS handshake 취소 검사를 통과했습니다. workspace 권한/umask 수정도 독립 재검토에서 재확인됐습니다. 저장된 transcript의 잘못된 payload는 복원 전에 거부하며, 정상 MCP 확장 필드는 유지하도록 보완했습니다. 네이티브 모델의 일반 JSON 답변을 도구 호출로 오인하지 않도록 실제 tool mode를 저장·검증합니다. MCP 호출은 전체 JSON-RPC envelope의 깊이·항목 수·바이트 크기를 승인과 시작 전에 검사합니다. 테스트 통과나 코드 리뷰는 보안 인증 또는 침투 테스트 완료를 뜻하지 않습니다.

## MCP·스킬 확장 검증

부모 에이전트가 전체 테스트를 직접 실행하여 **268개, 실패 0, skip 0**을 확인했습니다. 이후 릴리스 검사의 최신 개수는 배포 JSON에 기록됩니다.

- 실제 MCP fixture 프로세스로 initialize/list/call, 페이지 순회, 승인 거부, read-only, stderr/stdout/큐 상한, 잘못된 JSON/ID, blocked-write timeout, 취소/종료 및 환경 비밀 격리 검사.
- 실제 SKILL.md 읽기, 설정 상대 경로, 명시적 선택·크기/인코딩/링크/변경 경합 검사. POSIX와 강제 portable 경로 실행.
- TUI 키 입력과 plain/one-shot 스킬 선택, 세션 컨텍스트 분리·undo 유지·재개 wrapper 유지, MCP 실패 결과의 요약·저장 검사.
- 저장 결과가 객체가 아닐 때 복원 전 거부하고 기존 세션을 유지하며, 정상 MCP 확장 필드는 보존하는 회귀 검사.

전체 재검증 중 PTY 테스트가 async 입력창 정리 전에 /quit을 이어 입력해 한 번 timeout이 발생했습니다. 테스트 helper에서 기존 입력을 명시적으로 지운 뒤 종료 명령을 보내도록 수정하고 268개 전체 재실행이 통과했습니다. 제품 코드는 이 테스트 보정으로 변경하지 않았습니다.

사내 MCP 제품과 실제 모델의 결합 테스트 또는 Windows 네이티브 검증을 뜻하지 않습니다.

## ZIP 및 전역 실행 검증

배포 스크립트는 소스·설정·문서·테스트와 범용 wheel만 포함하며 `.git`, 가상환경, 캐시, 비밀 파일을 제외합니다. wheel 내 코드와 현재 소스를 비교하고 ZIP 무결성을 검사한 뒤 새 임시 폴더에서 다음을 실행합니다.

1. 새 가상환경에 `--no-index --require-hashes`로 오프라인 설치.
2. 압축 해제 소스의 `david.py --help`, `--check` 및 전체 테스트.
3. 설치 폴더 밖에서 런처 실행.
4. 임시 PATH의 설치된 `david`로 서로 다른 작업 폴더 파일 읽기, `--init`, 설정 우선순위와 기존 설정 보존 검사.
5. 설치된 명령으로 실제 PTY TUI 시작·종료 검사.

프로젝트의 동명 `david.py` 또는 `setting.json`을 자동으로 신뢰하지 않는지 검사합니다. 최종 검사 실패 시 배포 성공 결과를 출력하지 않습니다.

## 미검증 / 보장하지 않는 사항

- **사내 실모델:** 실제 엔드포인트 연결, 코딩 품질, tokens/s, GPU 성능, SWE-bench 점수는 측정하지 않았습니다.
- **Windows/Linux 네이티브:** macOS portable backend 실행과 capability simulation은 네이티브 검증이 아닙니다. Windows IME·실제 ACL·콘솔 동작은 별도 확인이 필요합니다. POSIX 전용 테스트는 다른 플랫폼에서 건너뛸 수 있습니다.
- **portable 저장 보안:** 경로 기반 검사 사이의 악의적인 동시 디렉터리 교체를 완전히 막지 못합니다. Windows 접근 제한은 상속된 디렉터리 ACL에 의존하며 별도 ACL 적용, 디렉터리 fsync 및 암호화를 제공하지 않습니다.
- **OpenAI 호환성:** 실제 서버의 스트림 종료·토큰 파라미터·tool calling 지원은 서버별 확인이 필요합니다.
- **OS 샌드박스:** 제공하지 않습니다. 승인된 명령은 사용자 권한으로 실행되며 명령이 만든 임의 변경은 관리된 파일 undo 대상이 아닙니다. 취소해도 이미 끝난 변경은 남습니다.

운영 보안 경계와 남은 제약은 `SECURITY.md`를 참고하세요.
