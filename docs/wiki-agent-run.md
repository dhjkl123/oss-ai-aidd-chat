# Wiki agent -- Docker로 실행하기

pi sidecar(Node)는 Image 안에 이미 들어 있다. 남은 건 Wiki와 tokenizer를
Volume으로 꽂고, `.env`의 일곱 Key를 넘기는 것뿐이다.

## 1. tokenizer 준비

`tokenizer.json`은 절대 commit하지 않는다(`.dockerignore`·`.gitignore` 둘 다
막는다). Host에서 한 번 받아 둔다.

```bash
uv run python scripts/fetch_tokenizer.py            # -> .cache/tokenizer.json
```

## 2. `.env` 채우기

Provider Binding 두 Key(`OLLAMA_BASE_URL`, `OLLAMA_API_KEY`)와 `.env.example`의
"Wiki agent (pi sidecar)" 절 다섯 Key, 아래 표는 합쳐 일곱 Key다. `OLLAMA_BASE_URL`이
비어 있으면 뒤의 다섯은 아예 읽히지 않고 결정론적 Test Provider가 대신 Bind된다.

| Key | 값 |
|---|---|
| `OLLAMA_BASE_URL` | LAN Ollama Origin (`scheme://host[:port]`) |
| `OLLAMA_API_KEY` | Provider 자격증명 |
| `WIKI_ROOT` | Container 안 경로 -- 아래 `docker run`에서 `/wiki`로 Mount |
| `TOKENIZER_PATH` | Container 안 경로 -- 아래 `docker run`에서 `/tokenizer.json`으로 Mount |
| `OLLAMA_MODEL` | 예: `qwen3.5:9b` |
| `MODEL_CONTEXT_WINDOW` | LAN Ollama의 `OLLAMA_CONTEXT_LENGTH` 이하, AD-30 하한(23740) 이상 |
| `NODE_BINARY` | Container 안에서는 `node` 그대로(Image에 이미 있다) |

이전 이름 `NODE_PATH`는 더 이상 읽지 않는다. Node 자신의 Module 검색 경로 변수와
이름이 겹쳐서 `NODE_BINARY`로 바꿨다. 기존 `.env`에 `NODE_PATH`가 있다면
`NODE_BINARY`로 이름을 바꾼다.

## 3. `docker run`

Wiki 원본은 절대 쓰기 대상이 아니므로 `:ro`. `<clone>`은 완결된 상태만 읽도록
전용 clone을 쓰고(`git pull`로만 갱신), 작업 중인 checkout을 그대로 물리지 않는다.

`--env-file .env`가 `OLLAMA_BASE_URL`·`OLLAMA_API_KEY`를 포함해 나머지 값을 채운다.
`WIKI_ROOT`·`TOKENIZER_PATH`만 뒤에 `-e`로 다시 준다 -- Host `.env`의 값은 Host
경로이고, Container 안에서는 아래 Volume이 Mount된 경로(`/wiki`, `/tokenizer.json`)라야
하기 때문이다. 같은 Key가 겹치면 `-e`가 `--env-file`보다 Flag 순서와 무관하게 항상
우선하므로, 이 두 Key는 Host `.env`의 값이 아니라 여기 적은 Container 경로로 Bind된다.

```bash
docker build -t aidd-chat .
docker run -d --name aidd-chat -p 7860:7860 \
  -v <clone>:/wiki:ro \
  -v <host>/.cache/tokenizer.json:/tokenizer.json:ro \
  --env-file .env \
  -e DEPLOYMENT_PROFILE=local_test \
  -e WIKI_ROOT=/wiki -e TOKENIZER_PATH=/tokenizer.json \
  -e OLLAMA_MODEL=qwen3.5:9b -e MODEL_CONTEXT_WINDOW=32768 -e NODE_BINARY=node \
  aidd-chat
curl -i http://127.0.0.1:7860/ready
```

`/ready`가 204면 Bind 성공. 503이면 Log 한 줄의 `error_class`가 원인을 정확히
남긴다 -- `probe_timeout`(2초 Readiness Probe 예산 안에 Probe가 끝나지 못함),
`digest_mismatch`(Probe 도중 Provider Binding이 바뀜), `provider_probe_failed`
(Provider 응답 실패 -- Probe 자체가 실패했거나 `provider_ok=false`), `wiki_probe_failed`
(`wiki_ok=false`), `context_probe_failed`(Context 확인 실패 -- 예: 모델의 실제
`num_ctx`가 `MODEL_CONTEXT_WINDOW`보다 작음) 중 하나다. Code 이름뿐이고, 본문·경로·
Secret은 절대 남기지 않는다.

Provider Probe는 Completion이 아니라 `GET {OLLAMA_BASE_URL}/v1/models` 한 번이다
(pi-ai와 같은 `Authorization` Header, 1.5초 예산). 그래서 답변 생성 중인 단일 Slot
Ollama 뒤에 줄 서지 않는다. 목록에 `OLLAMA_MODEL`이 없으면 `provider_ok=false`이고
sidecar Log의 `error_class`는 `provider_model_missing`이다. 실제 Completion은 Context
확인(AD-30) 하나뿐이다. 기동 시 Ollama가 꺼져 있거나 느려서 그 확인이 Provider 오류로
끝났다면, 이후 Provider Probe가 성공할 때 한 번에 하나씩 다시 시도한다. 실제 Context
부족(shortfall)이나 성공은 Binding당 한 번으로 끝난다.

## 4. 한 Command로 채택 Gate (AD-26)

Push나 배포 전에, Node sidecar Test·Python Adapter/Contract/Application
Suite·content-free Log Test를 하나의 종료 Status로 묶어 확인한다.

```bash
uv run python scripts/adoption_gate.py   # exit 0 이어야 통과
```

## 5. 로컬 LLM 연결

로컬 LLM(Ollama)은 Tailscale 네트워크 위의 Proxy로 접근한다. `OLLAMA_BASE_URL`에 그 Proxy의
Origin만 넣는다(`scheme://host[:port]`, path·credential 금지). Proxy가 Key를 요구하면
`OLLAMA_API_KEY`에 넣고, 아니면 빈 문자열만 아니면 된다. 연결이 안 되면 `/ready` 503의
`error_class`로 원인을 본다 -- 네트워크는 `probe_timeout`, Model 태그 불일치는
`provider_model_missing`, Context 부족은 `context_probe_failed`.
