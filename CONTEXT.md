# 토큰·모델 프로필·요청 시간 제한

출력 한도, 현재 컨텍스트 점유량, 누적 사용량, 요청 바이트 크기, 시간 제한은 서로 다른 값입니다. 모델 이름으로 사양을 추측하거나 서버 설정을 자동 변경하지 않습니다.

## 기본값과 기존 설정

- 새 출력 예산 `model.max_tokens`: **32000**.
- 새 컨텍스트 예산 `model.context_window_tokens`: **131072**.
- 기본 안전 여유분: **1024**.
- 기존 설정에 명시된 `max_tokens: 2048` 또는 `context_window_tokens: 0`은 그대로 유지합니다. 설치가 사용자 설정을 덮어쓰지는 않습니다.
- 컨텍스트 `0`은 legacy 문자 예산 모드입니다. 새 설정은 토큰 단위를 사용합니다.

확인한 Claude Code 문서는 모델별 한도를 사용하며 알려지지 않은 모델의 기본 출력을 32,000으로 설명합니다.[1] 확인한 OpenCode 소스도 기본 출력 상한을 32,000으로 정의합니다.[2] 이를 참고한 운영 시작값이지 GLM이나 모든 서버의 최적값/지원 상한은 아닙니다. 모델이 지원하지 않으면 낮춰야 합니다. 입력 예산은 다음과 같습니다.

```text
입력 예산 = context_window_tokens - max_tokens - context_safety_margin_tokens
```

출력 예약과 여유분이 창보다 크거나 같으면 설정을 거부합니다. thinking이 출력 한도에 포함되는지는 서버 정책을 확인하세요. 서버가 reasoning/cache를 입력·출력의 부분량으로 보고하면 이를 총합에 다시 더하지 않습니다.

## 1M 예산 설정

**서버가 실제로 그 길이로 서빙 중인지 확인**한 후 기존 설정의 해당 키만 수정합니다. 아래 예시로 전체 설정을 덮어쓰지 마세요.

```json
{
  "model": {
    "context_window_tokens": 1000000,
    "max_request_bytes": 16777216,
    "context_safety_margin_tokens": 1024
  }
}
```

출력 한도는 배포 서버에 맞춰 별도로 정합니다. 1M 창에는 입력뿐 아니라 출력 예약과 여유분도 들어갑니다. 숫자를 설정했다고 실제 1M 입력 토큰 전체를 사용할 수 있다는 뜻은 아닙니다.

모델별 등록은 `model_profiles`, 선택은 `model.profile`입니다. 서버로 전송할 모델명은 `model.name`이며 자동으로 프로필을 추측하지 않습니다. 우선순위는 **기본값 → 선택한 프로필 → 명시적 model 키**입니다. 기존 설정의 명시적 키가 프로필을 덮어쓰므로 프로필 값을 쓰려면 중복 키를 제거하세요. 예시와 지원 필드는 [docs/local-tokenizer.md](docs/local-tokenizer.md)를 참고하세요.

예산 범위: 컨텍스트 최대 2,000,000; 출력 최대 131,072. 이는 클라이언트 설정 검증 상한이며 모델/서버 지원 사양이 아닙니다.

## 세 종류의 토큰 수

1. **서버 보고 사용량:** API가 반환한 prompt/completion/total, 가능한 경우 reasoning/cache. 이번 질문의 여러 API 호출 합계와 현재 실행 세션 누적을 구분합니다. 반복 SSE usage snapshot은 중복 합산하지 않습니다.
2. **로컬 tokenizer 계수:** 사용자가 제공한 tokenizer JSON과 지원되는 chat template로 실제 렌더 문자열을 토큰화한 수입니다. 로컬 계수 자체와 서버 점유량 추정이라는 의미를 구분합니다. 숨은 서버 프롬프트나 템플릿 차이까지 맞는다는 보장은 아닙니다.
3. **tokenizer 미설정 추정:** 직렬화된 메시지/schema의 UTF-8 바이트 수 + framing 여유분. `ESTIMATE`이며 실제 tokenizer 결과가 아닙니다. 한국어 등에서 실제 사용 가능 문맥보다 일찍 제한할 수 있습니다.

정확한 서버 사용량은 서버가 제공해야 합니다. 미제공 값은 unknown, 실패/취소로 일부 요청만 집계되면 PARTIAL입니다. 스트리밍 서버가 지원한다면 `model.stream_include_usage: true`로 usage를 요청할 수 있습니다. 기본 false이며 서버가 먼저 보내는 usage는 설정과 무관하게 읽습니다.

`/usage`에서 보고율·현재 문맥·이번 질문·세션 누적을 확인합니다. 이 카운터는 **live-only**로 저장/복원하지 않습니다. 새 세션/resume/reload는 초기화하며 `/clear`는 문맥만 비우고 누적 사용량은 유지합니다.

로컬 tokenizer는 `python -m pip install ".[tokenizer]"`로 선택 의존성을 설치하고 서버 배포와 일치하는 로컬 파일을 지정합니다. 런타임 자동 다운로드나 remote code 실행은 없습니다. 지원 템플릿 문법은 제한적이며 **많은 production HF 템플릿을 그대로 지원하지 않습니다**. 설정한 tokenizer의 오류를 숨기고 byte 추정으로 바꾸지 않습니다. [지원 문법·설정·한계](docs/local-tokenizer.md)를 먼저 확인하세요.

## 문자 제한과 바이트 안전 한도

명시적 토큰 모드는 기존 60,000자 컨텍스트 제한과 첨부 합계 문자 상한을 사용하지 않습니다. 다만 다음 안전 제한은 계속 적용됩니다.

- `@파일` 최대 8개; 개별 파일 바이트·도구 출력 제한. 큰 파일은 필요한 부분을 탐색하세요.
- legacy `context_window_tokens: 0`에서만 첨부 합계는 32,768자와 `agent.max_context_chars // 2` 중 작은 값입니다.
- `model.max_request_bytes`: 실제 UTF-8 JSON 요청 크기. 기본 2,097,152바이트, 설정 범위 1,024~67,108,864바이트. 프록시/서버 상한은 별도입니다.
- 응답 바이트, 저장 세션 8 MiB, 화면 기록 제한은 별도입니다.

과거 문맥은 완전한 턴 단위로 제거해 도구 호출/결과를 분리하지 않습니다. 현재 요청 자체가 예산을 넘으면 API 호출 전에 거부합니다. 토큰/바이트 사전 검증 실패로 기존 대화 기록을 지우지 않습니다.

## 연결·무응답·전체 시간 제한

- `model.timeout_seconds`: 기존 전체 시간 제한. 기본 90초, 최대 600초.
- `model.total_timeout_seconds`: 명시적 전체 제한, 최대 3600초. **0이면 기존 timeout_seconds를 사용**합니다.
- `model.connect_timeout_seconds`: 연결 단계 제한, 최대 600초. **0이면 전체 시간 제한을 상속**합니다.
- `model.idle_timeout_seconds`: 응답 활동이 없는 구간의 제한, 최대 600초. **0이면 전체 시간 제한을 상속**합니다.

새 키의 기본값은 0이라 기존 시간 정책을 자동으로 바꾸지 않습니다. 긴 생성 작업에는 운영자가 연결/무응답/전체 상한을 각각 정해야 합니다. reasoning이나 응답 데이터 수신은 idle 제한을 갱신하지만 **전체 시간 제한은 늘리지 않습니다**. 출력 토큰을 늘린다고 deadline 오류가 해결되지는 않습니다.

화면에는 실제 이벤트에 따른 connecting/waiting/thinking/generating 단계와 움직이는 진행 표시가 나옵니다. thinking은 reasoning 필드 수신으로만 판단하며, 화면에 아무 내용이 없다고 서버가 아무 작업도 하지 않는다고 단정할 수 없습니다.

## 오류 구분

- `finish_reason=length`: 서버가 생성 한도 도달을 보고했습니다. 출력 한도/thinking 집계/서버 문맥 정책을 확인하세요.
- `[DONE]` 누락: 스트리밍 정상 종료 표시가 없습니다. 서버/프록시/호환 형식을 확인하세요.
- deadline/idle/connect timeout: 제한된 시간 안에 해당 단계가 끝나지 않았습니다. 짧은 요청 성공 여부와 실제 수신 활동을 함께 확인하세요.

불완전한 도구 인자를 추측해 실행하지 않습니다. 오류/취소는 이미 완료한 파일 변경의 자동 rollback이 아닙니다. 사내 모델 성능, 실제 1M 처리, Windows 네이티브 동작은 별도 검증 대상입니다.

## Sources

[1] https://code.claude.com/docs/en/env-vars.md
[2] https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/opencode/src/provider/transform.ts
