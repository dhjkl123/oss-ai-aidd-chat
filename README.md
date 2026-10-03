# Cite

모든 답에 근거 문서를. 익명 한국어 Q&A 대화 Shell(패키지명 `aidd-chat`). FastAPI + Wiki agent
(Node pi sidecar), In-memory 상태, SSE Streaming.

## 개요

Markdown Wiki 폴더와 Ollama Model을 물리면 Wiki 근거로만 답하는 챗봇이 된다. **Cite**가 대화·
Run·상한을 관리하고, **Wiki agent**가 Wiki를 읽기 전용으로 탐색해 근거 있는 답만 만든다. Model은
태그만 바꾸면 교체된다(조건은 [모델 조건](#모델-조건)).

![아키텍처](docs/architecture.svg)

편집용 원본은 [`docs/architecture.excalidraw`](docs/architecture.excalidraw)이다
(excalidraw.com에서 열어 수정한 뒤 SVG로 다시 export한다).

| 구성 | 역할 |
|------|------|
| Cite | HTTP API + SSE, Domain 상태·용량 상한, `AgentRuntimePort` 뒤의 Adapters. 모든 상태가 In-memory라 Worker 1개·Replica 1개 전제다. |
| Wiki agent | pi sidecar(Node)가 Wiki를 읽기 전용 도구로 탐색(Research)한 뒤, 읽은 문서만으로 답을 짓는다(Compose). `OLLAMA_BASE_URL`이 비면 Deterministic FakeAgent가 묶인다. |
| Ollama | OpenAI 호환 `/v1`로 호출하는 Local·LAN Model. [모델 조건](#모델-조건)을 만족하면 계열은 가리지 않는다. |
| Markdown Wiki | `WIKI_ROOT`의 `.md` 폴더. [지원하는 Wiki 구조](#지원하는-wiki-구조) 참고. |

## Quick start

Model도 Network도 없이 Deterministic Provider로 띄우는 가장 짧은 경로다. Repository Root에서:

```sh
cp .env.example .env
```

`.env`에서 한 줄만 채운다(나머지 용량·Rate 값은 `.env.example`에 이미 들어 있다).

```
MAX_ATTEMPTS_PER_LINEAGE=3
```

`OLLAMA_BASE_URL`은 비워 둔다. `local_test` Profile에서 빈 Endpoint는 Deterministic Provider를
묶으라는 뜻이다.

```sh
uv sync --locked
uv run python -m uvicorn aidd_chat.main:app --port 8000
```

설정은 pydantic-settings가 Working Directory의 `.env`에서 자동으로 읽는다(Docker는 예외 --
`.env`를 Image에 굽지 않으므로 `--env-file`을 직접 넘긴다).

확인:

```sh
curl -i http://127.0.0.1:8000/ready         # 204
curl -i http://127.0.0.1:8000/api/v1/policy # 200
curl -i http://127.0.0.1:8000/              # 200, 대화 Shell
```

실제 Model을 붙이려면 Wiki agent(Node pi sidecar)가 필요하다. Provider·Wiki·Node 설정 전체와
Docker 실행법은 [`docs/wiki-agent-run.md`](docs/wiki-agent-run.md)에 있다.

## 로컬 LLM 연결

로컬 LLM(Ollama)은 Tailscale 네트워크 위의 Proxy로 접근한다. `.env`의 `OLLAMA_BASE_URL`에
그 Proxy의 Origin(`http(s)://host[:port]`)만 넣으면 되고, Model 이름은 태그까지 정확히 쓴다
(예: `qwen3.5:9b`). 나머지 설정은 [`docs/wiki-agent-run.md`](docs/wiki-agent-run.md)에 있다.

### 모델 조건

Ollama 쪽에서 운영자가 맞춰야 하는 조건이 둘 있다. 앱은 맞췄는지 확인만 한다.

- **컨텍스트 길이** -- Ollama 서버의 `OLLAMA_CONTEXT_LENGTH`를 `.env`의 `MODEL_CONTEXT_WINDOW`
  이상으로 둔다(하한 23740). Ollama는 창을 넘는 앞부분을 조용히 버리므로, 기동 시 Context
  Probe가 창 근처 길이의 요청으로 이를 확인하고 모자라면 `/ready`를 503으로 둔다.
- **도구 호출(tools) 지원** -- Agent는 도구 호출로 Wiki를 탐색한다. `ollama show <model>`의
  Capabilities에 `tools`가 있어야 한다. Readiness Probe는 도구 없이 호출하므로, 미지원 Model은
  `/ready` 204로 뜨지만 첫 질문에서 Provider 오류로 실패한다.

Model을 다른 계열로 바꾸면 tokenizer도 그 Model의 것으로 바꾼다
(`uv run python scripts/fetch_tokenizer.py --repo <hf-repo>`).

## 지원하는 Wiki 구조

llm-wiki식 Markdown 폴더를 기준으로 만들었지만, 아래 조건만 맞으면 어떤
폴더든 읽는다. Wiki는 읽기만 하고 절대 쓰지 않는다.

- **`.md` 파일만** -- `WIKI_ROOT` 아래를 하위 폴더까지 읽는다. `.txt`·PDF·이미지 등 다른 형식은
  무시한다.
- **루트의 `index.md`는 필수** -- Agent가 가장 먼저 읽는 목차다. 없으면 `/ready`가 503이다.
- **제외 폴더** -- `inbox/`, `docs/`, `.obsidian/`, `.git/`, `.ua/`(대소문자 무시)는 목록·검색·
  읽기에서 모두 빠진다.
- **우선 폴더(선택)** -- `entities/`, `concepts/`, `comparisons/`, `queries/`의 문서가 검색 결과
  앞에 오고, `raw/`는 정리된 문서로 답할 수 없을 때만 찾는다. 이 폴더들이 없어도 동작한다.
- **Frontmatter(선택)** -- `title`(없으면 파일명), `confidence: high|medium|low`,
  `contested: true`. 근거 목록에 "신뢰도 낮음"·"논쟁 중" 표시로 나타난다.
- **경로 제한** -- 루트 밖을 가리키는 경로와 Symlink는 읽지 않는다.
- **검색 방식** -- 대소문자를 무시한 문자열 일치다(형태소 분석·Embedding 없음). 파일마다 첫
  일치 한 줄씩, 최대 20건을 돌려준다. 매 검색이 모든 파일을 훑으므로 문서가 수천 개면 느려질 수
  있다.

현재 System Prompt는 AIDD 도구·워크플로 도메인을 가정한다. 다른 주제의 Wiki를 물리면
`wiki_gap`/`out_of_scope` 판정과 인사 문구가 어긋날 수 있다.

## `/ready`가 503일 때

원인은 둘뿐이고 기동 Log로 구분된다.

- **Log에 변수 이름이 남았다** -- 필수 변수가 빠졌거나 범위를 벗어났다. Process는 뜨지만
  `/ready`·`/api/v1/policy`가 503이고 새 대화 생성까지 거부된다(설계된 Fail-closed).
- **Log가 깨끗하다** -- 2초 Readiness Probe가 실패했다. Model Cold Start이거나 Endpoint에
  닿지 못한 경우다. `OLLAMA_BASE_URL`만 설정하고 `OLLAMA_API_KEY`를 비워 둔 경우도 여기서
  드러나며, Deterministic Stub으로 조용히 대체되지 않는다.

## 계약과 설정

- API 계약 전체: `GET /openapi.json` (모든 Operation, Schema, 한국어 Error Envelope)
- SSE의 Envelope·순서·`Last-Event-ID` Replay·Terminal Close·만료 Tail:
  [`docs/sse-contract.md`](docs/sse-contract.md)
- 환경변수 이름·유효값·기본값: [`.env.example`](.env.example)

핵심 불변식만 추리면: 대화는 생성 시각 +3,600초에 절대 만료하고 어떤 활동도 이를 연장하지
않는다. Run은 접수부터 120초 Wall-clock Deadline을 가진다. 용량·Rate 상한 17개는 기본값이
없어서, 채우지 않으면 기동은 되지만 `/ready`가 계속 503이다.

CLI에서 `http://`로 API를 직접 몰면 대화 생성 이후 모든 요청이 404다 -- Capability Cookie가
`Secure`라 `http://`로 되돌아가지 않기 때문이며, Browser는 `http://127.0.0.1`을 Secure
Context로 보므로 정상 동작한다.

## Docker

```sh
docker build --pull -t aidd-chat:local .
docker run --rm -p 127.0.0.1:7860:7860 --env-file .env aidd-chat:local
```

Bind를 `127.0.0.1`로 묶은 것은 의도적이다 -- `-p 7860:7860`은 인증 없는 데모를 모든 Interface에
공개한다. Image는 `pyproject.toml`·`uv.lock`·`.python-version`·`src/`와, pi sidecar 전용
Build Stage에서 pruned된 `agent/`(및 Node 자체)만 COPY하므로 Credential이 Layer에 들어갈 수
없고, `tests/test_story_1_11_docker.py`가 이를 고정한다. Release는 이 local Docker Image가
전부다 -- 별도 배포 대상(HF Space 등)은 없다. Wiki agent를 Docker에서 실제로 묶는 절차(Volume,
`.env` 일곱 Key)는 [`docs/wiki-agent-run.md`](docs/wiki-agent-run.md)에 있다.

## Test

```sh
uv run pytest -q
```

Browser Test는 Playwright Chromium이 필요하다(`uv run playwright install chromium`).
Test Suite는 항상 Deterministic Provider를 묶으므로 `.env` 없이도 통과한다.

Node pi sidecar Test는 별도다: `node --test "agent/*.test.mjs" tests/markdown.test.mjs`.
Push나 배포 전 Gate는 [`docs/wiki-agent-run.md`](docs/wiki-agent-run.md)의
`uv run python scripts/adoption_gate.py` 하나로 묶는다.
