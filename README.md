# aidd-chat

익명 한국어 Q&A 대화 Shell. FastAPI + PydanticAI Direct API, in-memory 상태, SSE Streaming.

## Quick start

Model도 Network도 없이, Deterministic Provider로 앱을 띄우는 가장 짧은 경로다. Repository
Root에서:

```sh
cp .env.example .env
```

`.env`에서 **한 줄만** 채운다. 나머지 용량·Rate 변수는 `.env.example`에 이미 값이 들어 있다.

```
MAX_ATTEMPTS_PER_LINEAGE=3
```

`OLLAMA_BASE_URL`은 **비워 둔 채로 둔다.** `local_test` Profile에서 빈 Endpoint는
Deterministic Provider를 묶으라는 뜻이다.

```sh
uv sync --locked
uv run python -m uvicorn aidd_chat.main:app --port 8000
```

설정은 `.env`에서 **자동으로 읽힌다.** pydantic-settings의 `env_file=".env"`가 Working
Directory의 `.env`를 읽으므로 `export`도 `--env-file`도 필요 없다(Docker는 다르다 --
`.env`를 Image에 굽지 않으므로 `--env-file`을 직접 넘겨야 한다).

확인:

```sh
curl -i http://127.0.0.1:8000/ready         # 204
curl -i http://127.0.0.1:8000/api/v1/policy # 200
curl -i http://127.0.0.1:8000/              # 200, 대화 Shell
```

`GET /ready`는 `204`(준비됨) / `503`(Provider 미준비)이고, `GET /api/v1/policy`는 준비됐을
때만 200이다.

API 계약은 `GET /openapi.json`이 전부 설명한다(모든 Operation, Schema, 한국어 Error Envelope).
OpenAPI가 표현할 수 없는 SSE의 절반 -- Envelope, 순서, `Last-Event-ID` Replay, Terminal Close,
만료 Tail -- 은 [`docs/sse-contract.md`](docs/sse-contract.md)에 있다.

## 실제 Model 연결

같은 `.env`에 OpenAI 호환 Ollama Endpoint 두 줄을 더 채우면 Deterministic Provider 대신
실제 Provider가 묶인다.

```
OLLAMA_BASE_URL=http://<host>:<port>
OLLAMA_API_KEY=<key>
```

`OLLAMA_BASE_URL`은 Origin(`scheme://host[:port]`)이며 URL 안에 credential을 넣으면 기동을
거부한다. 재기동하면 `/ready`가 `204`가 되고 `/api/v1/policy`는 `Ollama LAN proxy`와
`qwen3.5:9b`를 보고한다.

**첫 실행에서는 Cold Start를 만난다.** Readiness Probe에는 **2초 Deadline**이 있다. Provider
Host에 Model이 아직 Load되지 않았으면 첫 `/ready`는 `503`이고, 기동 Log에는 설정에 대한 불평이
하나도 남지 않는다. Model이 Load되고 나면 그대로 `204`가 된다. 즉 **Log가 깨끗한데 503이면
설정이 아니라 Provider 문제다.**

## `/ready`가 503일 때

원인은 둘뿐이고 **기동 Log를 보면 구분된다.**

- **Log에 설정 불평이 있다 -- 변수가 빠졌다.** 필수 변수가 없거나 범위를 벗어나면 기동 Log에
  문제가 된 **변수 이름만** 남는다(값은 남기지 않는다). 이 상태에서 Process는 뜨지만
  `/ready`와 `/api/v1/policy`가 `503`이고 새 대화 생성까지 거부한다. Crash가 아니라 설계된
  Fail-closed 동작이다. Log가 부른 이름을 `.env`에 채우고 재기동한다.
- **Log가 깨끗하다 -- Provider Probe가 실패했다.** 설정은 통과했고, Endpoint를 실제로 찔러 본
  2초 Probe가 실패한 것이다. 위의 Cold Start이거나 Endpoint에 닿지 않는 경우다.
  `OLLAMA_BASE_URL`을 설정하고 `OLLAMA_API_KEY`를 비워 둔 경우도 `/ready` 503으로 드러나며,
  Deterministic Stub으로 조용히 대체되지 않는다.

## CLI에서 `http://`로 API를 직접 호출할 때

CLI에서 이 API를 `http://`로 직접 몰면 대화 생성 이후 모든 요청이 실패한다 -- Bug가 아니라
설계된 동작이다. `POST /api/v1/conversations`는 성공해서 `201`과 `Set-Cookie`를 돌려준다.
문제는 그다음이다: Capability Cookie는 `Secure`(그리고 `SameSite=Strict`, `HttpOnly`,
`path=/api/v1`)로 발급되는데, `Secure`를 지키는 CLI Client는 그 Cookie를 `http://`로 되돌려
보내지 않는다. 그러면 요청에 Capability가 아예 실려 있지 않고, 서버는 Capability가 일치하지
않을 때와 같은 본문 없는 최소 `404`를 돌려준다. Browser는 다르다 -- `http://127.0.0.1`을
Secure Context로 취급해 Cookie를 그대로 저장·반환하므로 `http://127.0.0.1:8000`의 대화
Shell은 정상 동작한다. 수동 확인은 Browser Shell로 하고, 스크립트로 API를 다루려면 앞단에서
TLS를 종료하거나 `Set-Cookie` 값을 요청마다 직접 replay한다 -- 다만 후자는 Local Debugging용
임시 수단일 뿐이다.

```sh
curl -i -X POST http://127.0.0.1:8000/api/v1/conversations   # 201 + Set-Cookie
curl -i -H 'Cookie: conversation_capability=<Set-Cookie 값>' \
  http://127.0.0.1:8000/api/v1/conversations/<id>
```

## Docker

```sh
docker build --pull -t aidd-chat:local .
docker run --rm -p 127.0.0.1:7860:7860 --env-file .env aidd-chat:local
```

> **`.env.example`을 그대로 복사한 `.env`로는 `/ready`가 503이다.** 용량·Rate 변수와
> `MAX_ATTEMPTS_PER_LINEAGE`에는 **Code 기본값이 없다.** `.env.example`은 용량·Rate 값을 채워
> 두지만 `MAX_ATTEMPTS_PER_LINEAGE`만은 의도적으로 비워 두므로(1..8은 운영자가 정한다), 복사만
> 한 `.env`는 아직 완성되지 않았다. 마찬가지로 용량·Rate 변수를 빠뜨린 `.env`도 값을 채우기
> 전까지 Process는 뜨지만 `/ready` 503이고 새 대화 생성까지 거부된다(아래 Configuration 절의
> 업그레이드 주의와 같은 규칙이다). 어떤 변수가 문제인지는 기동 Log에 **이름만** 남는다.
>
> `-p`를 `127.0.0.1:7860:7860`으로 쓴 것도 의도적이다. `-p 7860:7860`은 인증 없는 데모를 이
> 기기의 **모든 Interface**에 공개한다. Test도 `127.0.0.1`에만 Bind한다.

Image는 `.python-version`의 `3.12.14`와 `uv.lock`을 그대로 쓰고 `uv sync --locked --no-dev`로만
환경을 만든다. Base Image 두 개는 Tag가 아니라 **Digest**로 고정되어 있으므로 `--pull`을 붙여
그 Digest를 실제로 가져오게 한다. UID 1000으로 `0.0.0.0:7860`에 Worker 1개·Process 1개로 Bind
하며 Disk는 Ephemeral이다(Volume 요구 없음, 재시작하면 이전 대화가 남지 않는다).

**Credential은 Image에 굽지 않는다.** 보장하는 것은 Dockerfile의 **명시적 COPY 허용목록**
(`pyproject.toml`, `uv.lock`, `.python-version`, `src/` 넷뿐)이다 -- 어떤 COPY도 이름을 부르지
않은 파일은 Layer에 들어갈 수 없다. `.dockerignore`는 두 번째 방어선으로 Build Context 자체를
줄인다. `docker run --rm aidd-chat:local ls -a /app`으로 직접 확인할 수 있고
`tests/test_story_1_11_docker.py`가 이를 Test로 고정한다.

외부 공유 TTL Store 없이 Worker·Replica를 늘리면 In-memory 상한과 상태가 조용히 나뉜다.
Hugging Face Docker Space용 Manifest와 배포 절차는
[`deploy/huggingface/README.md`](deploy/huggingface/README.md)에 있다(같은 Image, 다른 Manifest).
TLS를 종료하는 Router 뒤에 둘 때 필요한 `FORWARDED_ALLOW_IPS`·`TRUSTED_PROXY_HOPS` 설정도
거기 있다 -- **설정하지 않으면 모든 변경 요청이 403이 된다.**

## Configuration

설정은 환경변수 또는 `.env`로만 전달한다. 이름·유효값·기본값은 [`.env.example`](.env.example)에 있다.

> **업그레이드 주의 (Story 1.8):** 아래 17개 용량·Rate 변수는 이전 버전에 없었고 **기본값이
> 없다**. 기존 배포를 그대로 올리면 프로세스는 기동하지만 `/ready`가 계속 `503`이고 새 대화
> 생성이 `conversation_capacity_exceeded`로 거부된다. `.env.example`의 해당 block을 `.env`에
> 복사하고 값을 정한 뒤 재기동해야 한다. 어떤 변수가 문제인지는 기동 시 Log에 변수 이름만
> 남는다(값은 남기지 않는다).

| 변수 | 기본값 | 의미 |
|------|--------|------|
| `DEPLOYMENT_PROFILE` | `local_test` | `local_test`와 `public_demo`만 허용한다. pydantic-settings의 Literal이라 다른 값·빈 값·공백은 설정을 읽는 시점에 거부되며, 공백이 조용히 `local_test`로 떨어지는 경로는 없다. `local_test`는 Endpoint 없이 Deterministic Provider를 묶을 수 있다. `public_demo`는 다음 중 하나라도 성립하면 Fail-closed로 거부한다: Endpoint 없음·Private·Loopback, Test/Fake Provider, Provider Tool 활성, Retrieval 활성, 검증 불가한 공개 Policy, Worker·Replica 선언이 1이 아니거나 없음, Debug 또는 Uvicorn 기본 Access Log. 거부해도 Process는 뜨고 `/ready`가 503이며, 어떤 Guard가 걸렸는지만 값 없이 Log에 남는다. |
| `WEB_CONCURRENCY`, `REPLICA_COUNT` | (없음) | 이 Process가 몇 Worker·몇 Replica로 실행된다고 **선언**하는 값. `public_demo`에서는 둘 다 정확히 `1`이어야 하고, 없거나 비어 있으면 거부한다(모든 상한이 In-memory라 Worker가 늘면 조용히 나뉜다). 선언일 뿐 실제 Topology를 확인하지는 못한다 -- `uvicorn --workers 4`와 `WEB_CONCURRENCY=1`을 함께 쓰면 통과한다. |
| `LOG_LEVEL`, `UVICORN_ACCESS_LOG` | (없음) | `public_demo`에서는 `LOG_LEVEL`이 `info`/`warning`/`error`/`critical`/`fatal` 중 하나여야 하고(숫자 Level은 거부), `UVICORN_ACCESS_LOG`는 `0`/`false`/`off`/`no` 중 하나로 **명시**해야 한다(Uvicorn 기본값이 켜짐이라 미선언은 거부한다). 어떤 Logger든 DEBUG면 거부한다. Uvicorn Access Log와 Provider SDK Log는 기동 시 어차피 비활성화되지만, Debug를 요청한 배포 자체를 거부한다. |
| `OLLAMA_BASE_URL` | (없음) | Provider Endpoint Origin(`scheme://host[:port]`). URL에 credential을 넣으면 기동을 거부한다. |
| `OLLAMA_API_KEY` | (없음 = `None`) | Provider Credential. **Default 값이 없다**: 설정하지 않으면 `None`(부재)이고, 이는 빈 문자열 같은 어떤 값과도 구별된다. Deterministic `local_test` Binding은 부재가 정상이며, Endpoint가 설정된 경우 부재·공백 모두 Fail-closed로 `/ready` 503이 되고 Deterministic Stub으로 되돌아가지 않는다. 값은 응답·Log·Policy·Digest 어디에도 나타나지 않는다. |
| `MAX_ATTEMPTS_PER_LINEAGE` | (없음, 필수) | 한 질문 Lineage가 쓸 수 있는 Attempt 수(1..8). 최초 질문이 Attempt 1이다. 설정하지 않으면 기동을 거부한다. |
| `MAX_RESIDENT_CONVERSATIONS`, `MAX_GLOBAL_ACTIVE_RUNS`, `MAX_SESSION_ACTIVE_RUNS`, `MAX_QUEUED_RUNS`, `MAX_TURNS_PER_CONVERSATION`, `MAX_OUTPUT_BYTES`, `MAX_REPLAY_EVENTS`, `MAX_REPLAY_BYTES`, `MAX_SSE_CONNECTIONS`, `CREATE_RATE_PER_MINUTE`, `SUBMIT_RATE_PER_MINUTE`, `POLL_RATE_PER_MINUTE`, `RETRY_RATE_PER_MINUTE`, `CANCEL_RATE_PER_MINUTE`, `MAX_REQUESTS_PER_IP_PER_MINUTE`, `MAX_PROVIDER_CONCURRENCY`, `SPEND_WINDOW_SECONDS`, `SPEND_LIMIT_UNITS`, `SPEND_UNIT` | (없음, 필수) | 용량 상한과 Rate. 하나라도 없거나 범위를 벗어나면 **기동은 되지만** `/ready`가 503이고, 질문은 물론 **새 대화 생성까지** 거부된다. 값·범위는 [`.env.example`](.env.example) 참고. |
| `TRUSTED_PROXY_HOPS` | `0` | 앞단 Reverse Proxy 수. `0`이면 `X-Forwarded-For`를 전혀 신뢰하지 않고 Socket Peer만 Client IP로 쓴다. |

## Run Deadline과 재시도

Run은 접수(질문 또는 재시도) 시점부터 **120초 Wall-clock Deadline**을 가진다. Queue 대기,
문맥 준비, Token 계산, Provider I/O, Stream Close Grace가 모두 이 안에 들어간다. Deadline에
도달하면 Run은 `timeout` Terminal 상태로 끝나고 생성 중이던 내용은 답변으로 남지 않는다.
`timeout`은 `failed`와 같은 Event 순서(`message.discarded` → `run.error` → `run.status` →
`stream.end`)를 쓰며 Polling과 SSE가 같은 상태를 반환한다.

`failed`·`timeout`·`cancelled` Run은 같은 Run Endpoint에 `{"kind":"retry","retry_of_run_id":…}`
Command를 보내 다시 시도할 수 있다. 재시도는 새 User Message를 만들지 않고 원래 질문과 원래
요청 Snapshot을 그대로 다시 보낸다. 한 질문의 Lineage는 `MAX_ATTEMPTS_PER_LINEAGE`번의
Attempt만 쓸 수 있고(최초 질문이 Attempt 1), `retry_of_run_id`는 그 Lineage의 가장 최근
Terminal Run(Tail)이어야 한다. 거부는 아래 Code로 돌아온다.

| Code | Status | 의미 |
|------|--------|------|
| `retry_exhausted` | 409 | Lineage의 Attempt를 모두 사용했다. `retryable=false`, 새 대화가 유일한 경로다. |
| `retry_not_allowed` | 409 | 대상이 `completed`이거나 아직 Terminal이 아니라 재시도할 수 없다. |
| `run_already_active` | 409 | 다른 Run이 진행 중이다. `retryable=true`이며 끝난 뒤 다시 시도할 수 있다. |
| `provider_profile_changed` | 409 | 이 Run이 쓴 Provider Binding이 지금 묶여 있는 것과 다르다. 대상은 원래 재시도할 수 있었지만 Binding이 바뀌었으므로 이 Run은 다시 보낼 수 없다. `retryable=false` -- 다만 `retry_not_allowed`와 달리 이 대화는 살아 있고 **같은 질문을 새로 보내면 된다.** Run·Message·Attempt·Provider 호출을 하나도 만들지 않고 거부된다. |

## 만료·용량 상한과 Abuse Gate

Conversation은 생성 시각으로부터 **정확히 3,600초** 뒤에 절대 만료한다. 질문·조회·재시도·중지
어떤 활동도 `expires_at`을 연장하지 않는다. 만료는 두 곳에서 집행하되 기준은 하나다. Periodic
Sweeper가 아무도 열지 않는 Conversation을 회수하고, 접근 시점에도 같은 `expires_at`을 본다.
만료를 **최초로 감지한** 유효 Capability 요청은 `410 conversation_expired`를 받고, 그 요청이
Message·Buffer·Replay Event·Idempotency Receipt·Capability·Run Index를 함께 Purge한다. 그
뒤의 모든 접근은 존재하지 않는 Resource에 대한 **Minimal 404**다. 만료 시점에 SSE가 열려 있으면
`conversation.expired` → `stream.end`(`final_state: "expired"`) 두 Event를 쓴 뒤 아무 Event도
만들지 않고, 진행 중이던 Run의 Provider 작업은 취소된다.

공개 API Endpoint(`POST /api/v1/conversations`, `POST …/runs`, `GET /api/v1/runs/{id}`,
`POST …/cancel`, `GET …/events`, `GET /api/v1/policy`) 앞에는 Client IP Abuse Gate가 있고,
한도 초과는 Domain 상태와 Provider 작업이 만들어지기 **전에** 거부된다. Health Check인
`/live`와 `/ready`는 **의도적으로 제외**한다 -- 부하가 걸린 순간에 Health Check가
Rate Limit으로 실패하면 플랫폼이 서비스를 죽은 것으로 판단하기 때문이다. Shell(`GET /`)과
정적 자산(`/static/*`)도 제외한다: Conversation 상태도 Provider 작업도 없고, 이를 막으면
거부 사실을 알려 줄 화면 자체를 못 받는다. 거부는 아래 Code로
돌아오며 본문에 상한 값 자체는 담지 않는다.

| Code | Status | 의미 |
|------|--------|------|
| `conversation_capacity_exceeded` | 503 | 상주 Conversation 상한. 기존 Live Run은 손상되지 않는다. |
| `run_capacity_exceeded` | 503 | 전역 동시 Run 상한. |
| `session_run_capacity_exceeded` | 429 | 한 Session의 동시 Run 상한. 다른 Session은 영향받지 않는다. |
| `queue_capacity_exceeded` | 503 | 대기 중인 Run 상한. |
| `provider_busy` | 503 | 전역 Provider 동시성 상한. Provider 호출을 만들지 않는다. |
| `spend_limit_reached` | 503 | Spend Circuit Breaker Open. Window가 지나야 닫힌다. |
| `rate_limited` | 429 | Submit·Poll·Retry·Cancel의 Session Rate 또는 Client IP Rate. |
| `stream_capacity_exceeded` | 503 | 동시 SSE 연결 상한. 기존 Stream은 유지된다. |
| `turn_limit_reached` | 409 | 이 Conversation의 Turn 상한. `retryable=false`, 새 대화가 유일한 경로다. |
| `deployment_unconfigured` | 503 | 용량 설정이 검증되지 않은 Process. `retryable=false` -- 재배포로만 해소된다. |

거부 응답에는 다시 시도할 수 있는 경우에 한해 `Retry-After` 헤더가 붙는다. 값은 해당 Window
길이이며 상한 값 자체는 담지 않는다.

답변이 `MAX_OUTPUT_BYTES` 또는 Replay 상한을 넘으면 Run은 상한 안에서 `failed`로 끝나고
`terminal_error.kind`는 `capacity_exceeded`다 -- Provider 잘못이 아니므로 Provider 실패로
보고하지 않는다.

Run과 SSE 연결이 끝나면 그 Capacity는 즉시 회수된다. SSE 연결은 그 Run에 **남아 있는**
Deadline만큼만 유지되므로 `Last-Event-ID`로 재연결해도 시간이 새로 늘어나지 않는다.
Client IP는 Socket Peer만 쓰고, `TRUSTED_PROXY_HOPS`가 0보다 클 때에만 `X-Forwarded-For`를
그 수만큼 벗겨 읽으며, 그 값이 IP 주소로 파싱될 때만 신뢰한다.

만료 기준 시각은 기록용 Wall-clock(`expires_at`)과 집행용 Monotonic 값 두 개로 보관한다.
NTP·DST 조정이 만료를 통째로 정지시키거나 살아 있는 대화를 한꺼번에 Purge하지 않게 하기
위해서이며, Story 1.7의 Run Deadline과 같은 구조다.

**주의:** `OLLAMA_BASE_URL`을 설정하고 `OLLAMA_API_KEY`를 비워 두면 **기동은 되지만** `/ready`와
`/api/v1/policy`가 계속 `503`을 반환하고 질문이 거부된다. Deterministic Stub으로 조용히 대체되지
않으므로, 잘못된 설정은 Startup 실패가 아니라 Health Check에서 드러난다.

## Test

```sh
uv run pytest -q
```

Browser Test는 Playwright Chromium이 필요하다(`uv run playwright install chromium`).
Test Suite는 항상 Deterministic Provider를 묶으므로 `.env` 없이도 통과한다.
