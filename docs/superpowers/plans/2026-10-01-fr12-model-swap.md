# FR-12 Ollama Model 교체와 조건 확인 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FR-12의 C-12.1·C-12.3에 비어 있는 특성 Test 두 개를 더하고, 네 Consequence 전체를 검증해 traceability를 `done`으로 닫는다.

**Architecture:** 제품 코드는 바꾸지 않는다. 이미 있는 동작(Model 태그가 Binding으로 가는 경로, 도구 미지원 400의 Provider 오류 분류)을 Test로 고정하고, C-12.2·C-12.4는 기존 Test 실행으로 확인한다.

**Tech Stack:** Python 3.12, pytest, pydantic-settings; Node ≥ 22.19.0, `node:test`.

**Spec:** `docs/superpowers/specs/2026-10-01-fr12-model-swap-design.md`

## Global Constraints

- 제품 코드(`src/`, `agent/*.mjs` 중 Test가 아닌 파일) 변경 금지. 특성 Test가 실패하면 구현을 고치지 말고 멈추고 보고한다.
- Commands: Python `uv run pytest -q <path>`; sidecar `node --test agent/`.
- 작업 트리에 소유자의 미커밋 변경(README.md, pyproject.toml, agent/wiki-tools.test.mjs 등)이 있다. 항상 명시한 경로만 stage한다.
- Commit message 끝: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Commit은 소유자가 요청할 때만.
- traceability.yaml은 손으로 고치지 않는다. `trace_set`만 쓴다.

## Review Focus

1. 레지스트리 경로와 양자화 접미사가 붙은 태그(`hf.co/org/model:Q4_K_M`)도 변형 없이 `model_revision`이 되어야 한다 — Task 1 parametrize.
2. Ollama가 `400: ...`처럼 콜론을 붙인 형식으로 오류를 줘도 같은 `provider_http`/400이어야 한다 — Task 2 parametrize.
3. 목록에 없는 태그로 바꾸면 `/ready` 503, `provider_model_missing` — 기존 `agent/sidecar.test.mjs:86`가 고정, Task 3에서 실행.
4. 기동 시 Ollama가 꺼져 있어 Context Probe가 Provider 오류로 끝나면 다음 Probe에서 다시 시도 — 기존 `agent/sidecar.test.mjs:130`, Task 3에서 실행.
5. `MODEL_CONTEXT_WINDOW`를 AD-30 하한보다 작게 두면 Binding 거부 — 기존 `tests/test_wiki_agent_bootstrap.py:60`, Task 3에서 실행.

---

### Task 1: C-12.1 Model 태그가 코드 변경 없이 Binding이 된다

**Files:**
- Modify: `tests/test_wiki_agent_bootstrap.py` (파일 끝에 Test 추가)

**Interfaces:**
- Consumes: `build_provider()`, `PiSidecarAdapter.binding.model_revision`, 같은 파일의 `real_env(monkeypatch, **over)`
- Produces: 없음

- [ ] **Step 1: Test 작성**

```python
@pytest.mark.parametrize("tag", ["llama3.1:8b", "hf.co/org/model:Q4_K_M"])
def test_ollama_model_tag_alone_swaps_the_model(monkeypatch, tag):
    real_env(monkeypatch, OLLAMA_MODEL=tag)
    provider = build_provider()
    assert isinstance(provider, PiSidecarAdapter)
    assert provider.binding.model_revision == tag
```

- [ ] **Step 2: 실행**

Run: `uv run pytest -q tests/test_wiki_agent_bootstrap.py -k ollama_model_tag`
Expected: 2 passed. 특성 Test이므로 바로 통과해야 한다. 실패하면 멈추고 보고한다.

- [ ] **Step 3: Commit (소유자가 요청한 경우만)**

```bash
git add tests/test_wiki_agent_bootstrap.py
git commit -m "test: pin that OLLAMA_MODEL alone swaps the bound model (C-12.1)"
```

### Task 2: C-12.3 도구 미지원 Model은 Provider 오류로 실패하고 답을 만들지 않는다

**Files:**
- Modify: `agent/run.test.mjs` (기존 `provider http error becomes RunFailure with status` Test 바로 아래)

**Interfaces:**
- Consumes: 같은 파일의 `go(turns, over)`, `deltas(frames)`; `errorTurn` (`agent/test-helpers.mjs`); `RunFailure` (`agent/run.mjs`)
- Produces: 없음

- [ ] **Step 1: Test 작성**

`go()`는 reject되면 frames를 돌려주지 않으므로 `emit`을 바꿔 frames를 직접 모은다.

```js
for (const message of ["400 registry.ollama.ai/library/gemma3:4b does not support tools",
  "400: registry.ollama.ai/library/gemma3:4b does not support tools"]) {
  test(`a model without tool support fails as provider_http 400 with no answer: ${message}`, async () => {
    const frames = [];
    await assert.rejects(go([errorTurn(message)], { emit: (type, fields) => frames.push({ type, ...fields }) }),
      (error) => error instanceof RunFailure && error.cause === "provider_http" && error.httpStatus === 400);
    assert.deepEqual(deltas(frames), []);
  });
}
```

- [ ] **Step 2: 실행**

Run: `node --test agent/run.test.mjs`
Expected: 새 Test 2개 포함 전부 pass. 실패하면 멈추고 보고한다.

- [ ] **Step 3: Commit (소유자가 요청한 경우만)**

```bash
git add agent/run.test.mjs
git commit -m "test: pin tool-unsupported model failure as provider_http 400 (C-12.3)"
```

### Task 3: 전체 검증과 traceability 종료

**Files:** 없음 (실행과 `trace_set`만)

- [ ] **Step 1: Python Test**

Run: `uv run pytest -q tests/test_wiki_agent_bootstrap.py tests/test_wiki_agent_binding.py tests/test_wiki_agent_adapter.py tests/test_wiki_agent_application.py tests/test_wiki_agent_contracts.py tests/test_wiki_agent_tokenizer.py`
Expected: all passed.

- [ ] **Step 2: Node Test**

Run: `node --test agent/`
Expected: all pass (sidecar context probe, tokenizer vectors, run 포함).

- [ ] **Step 3: 운영 문서 대조**

README `### 모델 조건`(66-78행)과 `docs/wiki-agent-run.md` 1·3절이 spec 2절의 동작(컨텍스트 하한 23740, `/ready` 503 + `context_probe_failed`, 도구 미지원은 첫 질문에서 실패, 계열 교체 시 `scripts/fetch_tokenizer.py --repo`)과 일치하는지 읽고 확인한다. 어긋나면 멈추고 보고한다.

- [ ] **Step 4: traceability**

각 Consequence의 등록된 단계를 `steps_done`으로 체크하고 `done`으로 옮긴다(C-12.1..C-12.4). 증적은 이 plan 아래에 Test 실행 결과로 쓴 파일을 이미 연결해 두었으므로 새로 추가하지 않는다. 네 개가 모두 `done`이면 FR-12를 `done`으로 옮긴다.
