# FR-12 Ollama Model 교체와 조건 확인 — 사후 설계

- **Date:** 2026-10-01
- **Normative source:** PRD `_bmad-output/planning-artifacts/prds/prd-workspace-2026-08-21/prd.md` FR-12 (C-12.1..C-12.4)
- **Background:** `docs/superpowers/specs/2026-09-24-wiki-agent-design.md` (FR-1..FR-11). FR-12는 그 구현이 끝난 뒤
  2026-10-01 PRD 업데이트로 추가됐다(PRD 부록 A). 기능은 이미 코드와 README `모델 조건`에 있다.
- **Kind:** 사후(retrospective) 설계. 새 기능을 만들지 않는다. 기존 구현을 Consequence별로 대응시키고,
  Test가 비어 있는 곳만 채운다.

## 1. 목표와 범위

목표는 FR-12의 네 Consequence가 각각 어떤 코드와 Test로 성립하는지 기록하고, 근거가 없는 Consequence에
최소한의 Test를 더해 `done` 증적을 완성하는 것이다.

범위 밖: 기동 시 도구 호출 지원 확인(C-12.3이 명시적으로 확인하지 않는다고 정함), Tokenizer와 Model의 자동
일치 확인(C-12.4가 운영 문서로 대신함), 실제 LAN Ollama에 대한 Live 실행.

## 2. Consequence별 구현 대응

### C-12.1 설정만으로 Model 교체, Web·API 계약 불변

- 설정: `AgentSettings.ollama_model` (`OLLAMA_MODEL`, 기본 `qwen3.5:9b`) — `src/aidd_chat/bootstrap/__init__.py:59-70`
- 흐름: `build_agent_binding` 이 태그를 `AgentBindingV1.model_revision` 으로 옮기고(`bootstrap/__init__.py:82-96`),
  sidecar `buildModel` 이 그 값을 Model id로 쓴다(`agent/model.mjs:3-13`). 코드 어디에도 Model 이름 분기가 없다.
- Web·API 계약: Model 태그는 `/policy` Projection의 `model_revision` 문자열 값으로만 나간다
  (`src/aidd_chat/application/__init__.py:399-408`, Web 표시 `src/aidd_chat/web/index.html:128`). 태그를 바꾸면
  값만 바뀌고 필드 구성과 Chat API·SSE 형식은 그대로다(NFR-10). FakeAgent(`deterministic-v1`)와 pi Binding
  (`qwen3.5:9b`)이 같은 Projection Test를 통과하는 것이 그 근거다(`tests/test_wiki_agent_browser.py:391`).
- 다른 계열 교체: Tokenizer 교체 절차는 C-12.4.
- **Test 공백:** `OLLAMA_MODEL` 값이 Binding까지 그대로 가는지 확인하는 Test가 없다. `tests/test_wiki_agent_bootstrap.py`
  는 기본 태그만 쓴다.

### C-12.2 실제 컨텍스트가 짧으면 준비되지 않음 + 기동 Log

- Probe: sidecar `contextProbe` 가 `model_context_window - max_output_tokens - 512` 토큰 근처의 요청을 한 번
  보내고, Ollama가 보고한 입력 토큰 수와 자체 Tokenizer 계산을 비교한다(`agent/sidecar.mjs:141-164`). Ollama가
  앞부분을 조용히 버리면 보고값이 작아져 `effective_context_ok=false` 가 된다.
- 준비 상태: `PiSidecarAdapter` 가 결과를 `context_ok` 로 노출하고(`src/aidd_chat/adapters/pi_sidecar.py:281-283, 321-322`),
  `AgentProbeV1.ready` 가 이를 요구한다(`src/aidd_chat/contracts/__init__.py:255-259`). `/ready` 는 503.
- Log: Readiness가 `error_class=context_probe_failed` 를 남긴다(`src/aidd_chat/application/__init__.py:2173-2174`).
  Provider 오류로 Probe 자체가 실패하면 sidecar도 `run_stage=context_probe` Log를 남긴다(`agent/sidecar.mjs:159-161`).
- 정적 하한: `MODEL_CONTEXT_WINDOW` 가 AD-30 예산 합(23740)보다 작으면 Binding 자체를 거부한다
  (`contracts/__init__.py:423-430`, `bootstrap/__init__.py:261-264`).
- Test: `agent/sidecar.test.mjs:107-151`, `tests/test_wiki_agent_application.py:591`, `tests/test_wiki_agent_adapter.py:236`,
  `tests/test_wiki_agent_contracts.py:128`, `tests/test_wiki_agent_bootstrap.py:60`. 공백 없음.

### C-12.3 도구 호출 미지원 Model은 Run `failed`, 근거 없는 답변 없음

- 기동 Probe는 도구 없이 호출하므로(`agent/sidecar.mjs:86`) 미지원 Model도 `/ready` 204가 된다(의도된 동작, README
  `모델 조건`).
- 첫 질문에서 Ollama가 HTTP 400(`... does not support tools`)을 돌려주면 `failureFrom` 이 `provider_http` / 400 으로
  분류하고(`agent/model.mjs:24-33`), sidecar가 `error` frame으로 Run을 `failed` 로 끝낸다(`agent/sidecar.mjs:61-66`).
  이 형식이 C-6.1의 Provider 오류다.
- **Test 공백:** `agent/run.test.mjs:91` 은 429 일반 HTTP 오류만 본다. 도구 미지원 400 메시지와 "delta 0건"
  (근거 없는 답변 없음)을 함께 고정하는 Test가 없다.

### C-12.4 설정 Tokenizer로 문맥 한도 계산, 계열 교체 절차는 운영 문서

- 설정: `TOKENIZER_PATH` → `HfTokenizer` (`bootstrap/__init__.py:254-260`), Binding의 `tokenizer_authority` (sha256, path).
- 계산: Python `HfTokenizer.count` (`src/aidd_chat/adapters/tokenizer.py`), Node `loadTokenizer().count`
  (`agent/tokenizer.mjs`). 두 구현이 같은 값을 내는지 공유 Vector로 고정한다.
- 운영 문서: README `모델 조건` 77-78행(계열 교체 시 `scripts/fetch_tokenizer.py --repo <hf-repo>`),
  `docs/wiki-agent-run.md` 1절.
- Test: `agent/tokenizer.test.mjs`, `tests/test_wiki_agent_tokenizer.py`, `tests/test_wiki_agent_bootstrap.py:55`. 공백 없음.

## 3. 변경 사항

제품 코드 변경은 없다. Test 두 개만 더한다.

1. `tests/test_wiki_agent_bootstrap.py`: 다른 `OLLAMA_MODEL` 태그가 코드 변경 없이 `binding.model_revision` 이 된다(C-12.1).
2. `agent/run.test.mjs`: `400 ... does not support tools` 오류가 `RunFailure(provider_http, 400)` 이 되고 delta가
   하나도 나가지 않는다(C-12.3).

두 Test는 이미 있는 동작을 고정하는 특성(characterization) Test라 처음부터 통과하는 것이 정상이다. 실패하면
구현이 Consequence와 어긋난 것이므로 계획을 멈추고 보고한다.

## 4. 완료 조건

- 위 두 Test 추가, `uv run pytest -q tests/test_wiki_agent_*.py` 와 `node --test agent/` 통과.
- C-12.1..C-12.4 증적: 이 spec → plan → 코드·Test·운영 문서. 각 Consequence의 단계 체크 후 `done`, 이어서 FR-12 `done`.
