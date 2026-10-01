# 기술 부록: 한국어 LLM-Wiki Q&A 에이전트

## 목적

이 부록은 `prd.md`의 제품 요구와 분리해 관리해야 할 기술 선택 사항, 확장 경계, 검증 항목을 기록한다. 2026-08-23 범위 변경에 따라 기존 테일러링·근거·검토·내보내기 중심의 기술안은 폐기되었다.

## 결정 상태

다음 항목은 Architecture 갱신에서 확정한다.

- Model Provider와 정확한 Model Revision
- 에이전트 Runtime(pi agent) 연동 방식, Step 상한과 문맥 한도
- SSE, Fetch Stream, WebSocket 중 스트리밍 전송 방식과 최소 계약
- Timeout, Cancel Propagation과 Idempotency Storage
- 현재 Code Namespace와 새 `aidd_chat` 구조의 Migration 방법

기존 Architecture는 입력 문맥 한도를 8,192 token으로 고정했다. 에이전트가 Wiki 문서 여러 개를 읽으면 이 한도를 쉽게 넘으므로, 문맥 한도와 문서별 읽기 예산을 Architecture에서 다시 정해야 한다. 한도를 넘으면 가장 오래된 완결 Turn부터 제거하고 Model 기반 요약은 하지 않는다.

## 현재 실행 경로(에이전트 도입 전)

```text
Browser Web UI
  -> Chat API
  -> Chat Application Service
  -> ModelProviderPort
  -> configured Model Provider
```

한국어 질문을 Model Provider로 전달하고 생성 상태와 Assistant 답변을 Browser에 표시하는 경로다.

## 구조 원칙

- Web과 API는 Provider SDK를 직접 호출하지 않는다.
- Application Service가 질문 처리, Session 문맥, Run State, 취소, Timeout과 재시도를 조정한다.
- `ModelProviderPort`는 Streaming, 취소, Typed Failure와 Model Revision Metadata를 공통 계약으로 제공한다.
- 단순한 Chat Loop에 필요하지 않은 Artifact Aggregate, Revision CAS, Evidence Policy, Review Store, Export Renderer와 Evaluation Harness를 만들지 않는다.

### Wiki 검색 경계 (2026-09-23 개정)

- PRD 개정(FR-8~FR-10)으로 이전의 "Retrieval 비활성·지식 미연결 고지" 경계는 폐기되었다.
- Wiki 검색은 에이전트 도구(읽기 전용)로 제공한다. 기존 `RetrievalPort`를 재사용할지, 에이전트 도구로 대체할지는 Architecture에서 정한다.
- Wiki 경로 접근 가능 여부는 Readiness 조건이다(C-9.5).
- LangGraph, Neo4j, GraphRAG, Embedding, Vector DB와 RAGAS는 여전히 Runtime 또는 Release Dependency가 아니다.

## Runtime 계약

### Message와 Run

- ID는 Opaque UUID를 사용한다.
- Timestamp는 RFC 3339 UTC로 저장하고 UI에서 Asia/Seoul로 표시한다.
- Run State는 `queued | running | completed | failed | timeout | cancelled`의 닫힌 집합이다.
- 오류 응답은 `{error:{code,message,retryable,correlation_id,field_errors}}` 형식을 유지한다.
- Streaming과 Polling을 함께 제공한다면 동일 Run의 Monotonic Sequence를 공유한다.
- 완료 전에 전송된 Token은 `incomplete`로 표시하고 Terminal State와 구분한다.
- 취소와 완료가 경합하면 이미 확정된 완료가 우선한다.
- 재시도 요청에는 Client Idempotency Key와 요청 Digest를 사용해 Completed Message의 중복 생성을 막는다.

### Session과 데이터

- Session TTL은 최대 1시간이다.
- 현재 MVP는 대화를 영구 저장하거나 Cross-device Sync하지 않는다.
- 운영 Log에는 Prompt, 답변 본문, Provider Raw Response, Model Intermediate, Chain-of-Thought와 Secret을 기록하지 않는다.
- 관측 정보는 Correlation ID, Run Stage, Step 수, 도구 이름, Duration, Terminal State와 Error Class로 제한한다(PRD NFR-9). Run ID와 Model Revision은 운영 Log에 기록하지 않는다.
- 현재 MVP는 범용 DLP나 민감 정보 Pattern의 자동 Redaction 기능을 제공하지 않는다.
- UI에는 개인정보, 회사 기밀, Credential 입력 금지를 사전에 고지한다.
- 서비스가 소유한 Provider Credential은 Server 외부에 노출하지 않는다.
- Client와 Streaming Response에는 `Cache-Control: no-store`를 적용한다.

## 최소 Module Seed

```text
src/aidd_chat/
  application/       # chat use case, session context, run coordination
  domain/            # message and run-state contracts
  adapters/
    inbound/         # FastAPI routes and streaming transport
    outbound/        # model provider or agent runtime; read-only wiki search
  contracts/         # versioned API and error schemas
  bootstrap/         # settings and dependency wiring
web/                 # Korean-first chat UI
tests/               # contract, application, adapter and browser tests
```

기존 `aidd_tailoring` Namespace는 새 제품 의미와 맞지 않는다. Architecture를 갱신할 때 실제 Code 상태를 확인해 Rename 또는 Compatibility 경로를 결정한다.

## 실행·배포 방향

- Python 3.12와 FastAPI를 후보로 유지하되 정확한 Version은 현재 Repository에서 `uv lock`이 성공하고 Test를 통과한 뒤 확정한다.
- Local Container 실행을 우선 완료한다. 공개 배포는 범위 밖이다(PRD C-7.3).
- `/live`는 Process Health, `/ready`는 Model Provider 설정, 에이전트 Runtime, Wiki 루트 읽기 가능 여부와 Chat Application 준비 상태를 검사한다.
- 실제 Model Provider를 사용할 수 없는 CI에서는 명시적인 Deterministic Test Provider를 사용한다. 사용자용 실행을 정적인 가짜 답변으로 대체하면서 그 사실을 숨겨서는 안 된다.

## 제외된 기술 범위

- Context Card와 Delta Schema
- Tailoring Canvas, AI Adoption Level과 ITO Lifecycle Model
- Evidence State, Claim Support와 Evidence Guard(단, Wiki 근거 문서 표시는 PRD FR-10으로 범위 안이다)
- Pilot, Review, Approval, Artifact Revision과 Export Contract
- Neo4j·GraphRAG·Vector·Hybrid Retrieval
- Retrieval·RAGAS 품질 비교와 Release Gate

`my-llm-wiki` 파일 기반 검색은 2026-09-23 개정으로 범위 안이다. 지식 그래프·Vector 검색은 향후 확장 후보로만 남기며, 이를 도입하려면 별도의 PRD 변경과 Architecture Decision, 데이터 경계 정의, 품질 검증이 필요하다.

## 2026-09-23 추가: 에이전트 Runtime과 Wiki 연동 기술 메모

이 절은 `prd.md` 2026-09-23 개정(FR-9~FR-11)의 기술 배경이다. 최종 결정은 Architecture에서 한다.

### 선택한 에이전트 Runtime: pi agent

- 패키지: `@earendil-works/pi-agent-core` 0.87.x(1.0 이전), MIT, TypeScript/ESM, Node 22.19 이상. Python SDK는 없다.
- 제공: 상태를 가진 `Agent`(`prompt`, `continue`, `steer`, `followUp`, `abort`, `subscribe`), 저수준 `agentLoop`, 이벤트 스트림(`agent_start`, `turn_*`, `message_*`, `tool_execution_*`, `agent_end`), TypeBox 스키마 기반 `AgentTool`, 도구 호출 전후 훅, `transformContext` 훅.
- Provider: `@earendil-works/pi-ai`가 40개 이상의 Provider와 OpenAI 호환 엔드포인트(Ollama, vLLM, LM Studio)를 지원한다. 현재 Ollama LAN 프록시(`qwen3.5:9b`)를 `baseUrl`로 연결할 수 있다.
- 없는 것: 서브에이전트 기본 기능, 영속화, 실제 컨텍스트 압축 로직(훅만 있음. `pi-coding-agent` 또는 `pi-durable` 참고).
- 연동 선택지(기존 Python/FastAPI 유지 전제, NFR-12):
  1. Node 사이드카 + RPC/stdio 또는 로컬 HTTP. 새 `ModelProviderPort`(또는 Agent Port) Adapter가 사이드카를 호출하고, pi 이벤트를 기존 SSE 계약으로 변환한다.
  2. `pi-coding-agent` RPC 모드(JSON over stdin/stdout)를 재사용한다. 코딩 에이전트 성격의 기본 도구를 꺼야 한다.
  3. 백엔드 전체 Node 전환. NFR-12와 충돌하므로 비권장.
- 기존 코드 영향: `adapters/direct.py`의 `ToolPolicyV1.zero`와 non-text 거부(`provider_non_text`)는 에이전트 경로에서 폐기 또는 교체해야 한다. Story 1.5.1의 "도구 0개" 제약도 폐기된다. SSE 계약(`docs/sse-contract.md`)에는 진행 단계·근거 이벤트를 추가 필드로 확장한다.
- 출처: https://github.com/earendil-works/pi/tree/main/packages/agent , https://github.com/earendil-works/pi/tree/main/packages/ai , https://github.com/earendil-works/pi/tree/main/packages/coding-agent

### `my-llm-wiki` 특성

- 경로: `../my-llm-wiki`(설정 가능). 문서 143개, 텍스트 약 750 KB. Canonical 문서는 주로 한국어, `raw/`는 주로 영어 공식 문서다.
- 계층: `raw/`(원문 증거 118개) → Canonical(`comparisons/` 8, `queries/` 7, `concepts/` 1, `entities/` 0) → `SCHEMA.md`·`index.md`·`log.md`.
- 검색 도구와 색인은 없고, `index.md`가 유일한 카탈로그다. 규모가 작으므로 초기 도구는 `index.md` 읽기, 파일 읽기, 텍스트 검색(grep류)으로 충분할 수 있다. Vector 색인은 범위 밖이다.
- frontmatter: `title, created, updated, type, tags, sources, confidence, contested, contradictions`. 근거 표시(FR-10)에 `title`, `confidence`, `contested`를 사용한다.
- `scripts/validate_wiki.py`는 Wiki 계약 Linter다. 에이전트가 쓰기를 하지 않으므로 실행 대상은 아니지만 SM-9 확인에 참고할 수 있다.
