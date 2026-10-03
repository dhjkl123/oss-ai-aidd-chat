---
title: '한국어 LLM-Wiki Q&A 에이전트 PRD'
status: final
created: '2026-08-21'
updated: '2026-10-01'
---

# PRD: 한국어 LLM-Wiki Q&A 에이전트

## 0. 결정 요약

이 문서는 사용자가 ChatGPT처럼 한국어로 질문하면, 에이전트가 정리된 `my-llm-wiki`를 검색하고 그 내용을 근거로 답하는 개인용 웹 챗봇의 범위와 완료 조건을 정의한다.

2026-09-23 사용자 결정으로 이전 Working Skeleton PRD(2026-08-23)를 다음과 같이 확장한다.

- **에이전트 도입:** 답변 생성을 단일 Model 호출에서 도구를 호출하는 에이전트 실행으로 바꾼다. 에이전트는 `my-llm-wiki`를 검색하고 읽어서 답한다. 직접 Tool Calling과 Agent Loop를 만들지 않고 검증된 에이전트 Runtime을 사용한다. 선택한 Runtime(pi agent)과 연동 방식은 `addendum.md`와 Architecture에서 다룬다.
- **지식 연결:** 이전 PRD가 범위 밖으로 두었던 Tool Calling과 `my-llm-wiki` 연동을 범위 안으로 옮긴다. FR-8의 "지식 미연결 고지"는 "Wiki에 근거가 없으면 답하지 않음"으로 바꾼다.
- **UI 완성도:** 이미 채택한 Apple 기반 디자인 스파인(`docs/DESIGN.md`)을 새로 추가되는 요소까지 포함해 모든 화면에 일관되게 적용한다. 새 디자인 시스템은 도입하지 않는다.
- **기존 자산 보존:** 기존 대화, 실행 상태, 복구, 웹·API 계약과 소스 구조는 최대한 유지한다.

2026-10-01 사용자 결정으로 이미 구현된 두 가지 이식성을 요구사항으로 올린다.

- **Model 교체:** Ollama Model은 설정의 태그만 바꿔 교체한다. 시스템은 Model이 에이전트 실행 조건을 만족하는지 확인한다(FR-12).
- **범용 Wiki:** `my-llm-wiki`뿐 아니라 조건을 만족하는 어떤 Markdown 폴더든 Wiki 루트로 연결한다(FR-13).

제품은 개인용이고 로컬에서만 실행한다. 공개 데모와 다수 사용자 운영은 범위 밖이다.

## 1. 문제 정의

기존 Skeleton은 질문을 받아 일반 Model 답변을 보여 주는 데서 끝난다. 사용자가 공들여 정리한 `my-llm-wiki`의 지식(AIDD 도구, 방법론, 비교, 질의 합성)을 대화로 꺼내 쓸 수 없다. Wiki에는 검색 도구가 없고, `index.md`가 유일한 탐색 수단이다.

또한 Wiki 검색 같은 여러 단계 작업을 하려면 Tool Calling과 Agent Loop가 필요하다. 이를 현재 Model 호출 계층 위에 직접 만들면, 이미 성숙한 에이전트 Runtime이 제공하는 기능(도구 실행, 중단, 이벤트 스트림, Provider 호환)을 다시 구현하게 된다.

제품은 다음 문제를 해결한다.

- 사용자가 Wiki를 직접 열지 않고, 질문 한 번으로 Wiki 근거 답변을 얻어야 한다.
- 답변이 어떤 Wiki 문서에 근거했는지 확인할 수 있어야 한다.
- Wiki에 답이 없을 때 그럴듯한 추측을 Wiki 근거처럼 보여 주지 않아야 한다.
- 에이전트가 여러 단계로 일하는 동안 사용자는 진행 상황을 알 수 있어야 한다.
- 새로 추가되는 UI 요소도 기존 디자인 스파인과 어긋나지 않아야 한다.

### 1.1 제품 범위

#### In Scope

- 한국어 중심 ChatGPT형 Web UI
- 새 대화와 세션 내 후속 질문
- 질문 접수, 생성 중, Streaming, 완료 상태
- 생성 중지, 실패·Timeout·취소 및 재시도
- **에이전트가 설정된 Markdown Wiki(기본 `my-llm-wiki`)를 읽기 전용으로 검색하고 문서를 읽는 기능**
- **설정만으로 Ollama Model과 Wiki 루트를 교체하는 기능**
- **답변의 Wiki 근거(참조 문서) 표시**
- **에이전트 진행 단계 표시**
- **Wiki에 근거가 없을 때의 명시적 처리**
- **Apple 기반 디자인 스파인의 전 화면 적용**
- 문서화된 Chat API
- 대화 본문을 제외한 최소 관측 정보
- 키보드, Focus, Live Region, Reduced Motion과 반응형 지원
- 로컬 실행(Container 포함)

#### Out of Scope

- 에이전트의 Wiki 쓰기: 문서 생성·수정, `queries/` 답변 등록(Filing), `index.md`·`log.md` 갱신
- Wiki 루트 외부의 사내 문서, 웹 검색, 코드 실행, 파일 시스템 쓰기 등 다른 도구
- Vector Index, Embedding, Knowledge Graph, RAGAS 평가
- 서브에이전트와 다중 에이전트 협업
- 사용자 계정, 인증, 대화 영구 저장·검색·공유와 Cross-device Sync
- 파일 첨부, 음성, 이미지, Native App와 Offline Mode
- 새 디자인 시스템 도입 또는 UI Framework 전환
- 이전 테일러링 범위(Context Card, Canvas, Pilot, Review, Export). 2026-08-23 결정대로 제외를 유지한다.

## 2. 대상 사용자와 사용자 여정

### 2.1 대상 사용자

- `my-llm-wiki` 같은 Markdown Wiki를 직접 정리하고, 그 지식을 대화로 빠르게 꺼내 쓰려는 개인 사용자(Wiki 소유자)

### 2.2 핵심 용어

- **Conversation:** 새 대화로 시작해 서로 문맥을 공유하는 Message의 묶음.
- **Session:** 임시 Conversation 상태를 유지하는 최대 1시간의 실행 범위. 영구 Workspace가 아니다.
- **Message:** Conversation 안의 User 입력 또는 Assistant 출력 하나.
- **Run:** User Message 하나를 받아 Assistant Message 하나를 만드는 실행 단위. Run 하나 안에서 에이전트는 여러 번의 Model 호출과 도구 호출(Step)을 수행할 수 있다.
- **Step:** Run 안의 에이전트 단계 하나. 예: Wiki 목록 조회, 문서 읽기, 답변 작성.
- **Wiki 근거:** 답변 작성에 실제로 읽고 사용한 Wiki 문서.
- **Wiki 루트:** 설정으로 지정한 Markdown 폴더. 기본은 `my-llm-wiki`다.
- **Canonical 문서:** `entities/`, `concepts/`, `comparisons/`, `queries/`의 정리된 문서. `raw/`는 원문 증거이고, `inbox/`와 `docs/`는 증거가 아니다.

### 2.3 사용자 여정

#### UJ-1. 석원이 Wiki에 정리한 내용을 묻는다

- **Entry:** 석원은 챗봇을 열고 "BMAD랑 Superpowers의 워크플로 차이가 뭐였지?"라고 묻는다.
- **Path:** 시스템은 2초 안에 실행 상태를 표시한다. 진행 영역에는 "Wiki 목록 확인 → `comparisons/…` 읽는 중" 같은 단계가 차례로 나타난다. 이어서 답변이 Streaming된다.
- **Resolution:** 답변 아래에 근거로 쓴 Wiki 문서 제목이 표시된다. 석원은 필요하면 해당 문서 경로를 확인해 원문을 열어 본다.

#### UJ-2. Wiki에 없는 내용을 묻는다

- **Entry:** 석원은 Wiki가 다루지 않는 도구에 대해 묻는다.
- **Path:** 에이전트는 Wiki를 검색했지만 관련 문서를 찾지 못한다.
- **Resolution:** 시스템은 일반 지식으로 답을 지어내지 않는다. 대신 "Wiki 보충 대상"(Wiki 주제 안이지만 아직 정리되지 않음) 또는 "보충 불가"(Wiki 주제 밖)로 분류해 알린다. 석원은 보충 대상이면 Wiki에 정리할 주제로 적어 둔다.

#### UJ-3. 후속 질문, 중지와 복구

- 석원은 앞선 답변을 가리키는 후속 질문을 같은 대화에서 이어가거나, 새 대화를 열어 문맥을 분리한다.
- 실행이 실패하거나 Timeout되면 석원은 한국어 오류와 Correlation ID를 보고 재시도하거나 새 대화를 시작한다.
- 에이전트 실행 중 중지하면 진행 중인 도구 호출과 Model 호출이 함께 멈추고, 부분 답변은 완료 답변으로 남지 않는다.

## 3. 제품 원칙

1. **Chat first:** 첫 화면과 핵심 흐름은 질문과 답변에 집중한다.
2. **State is explicit:** 접수, 진행 단계, 완료, 실패, Timeout과 취소를 사용자가 구분할 수 있게 한다.
3. **Wiki-only answers:** 답변은 Wiki에서 읽은 내용으로만 만든다. Wiki에 근거가 없으면 답하지 않고 Wiki 보충 대상인지 보충 불가인지 알린다. 인사와 사용법 질문에는 고정 안내만 보여 준다(C-8.5). 읽지 않은 문서를 근거로 표시하지 않는다.
4. **Read-only knowledge:** 에이전트는 Wiki를 읽기만 한다. Wiki 변경은 사용자의 기존 Curation 워크플로가 맡는다.
5. **Recover safely:** 실패와 취소 뒤 부분 결과를 최종 답변처럼 남기지 않는다.
6. **Ephemeral by default:** 대화는 세션 범위를 벗어나 저장하지 않는다.
7. **One visual system:** 모든 UI 요소는 `docs/DESIGN.md`의 토큰과 컴포넌트 규칙을 따른다.

## 4. 기능 요구사항

### 4.1 대화 시작과 질문

#### FR-1: 새 대화 시작

사용자는 로그인이나 사전 설정 없이 새 대화를 시작할 수 있다.

**Consequences:**
- **C-1.1:** 새 대화는 이전 대화 문맥과 분리된다.
- **C-1.2:** 계정, Workspace 또는 저장된 대화 목록을 암시하지 않는다.

#### FR-2: 질문 작성과 전송

사용자는 Composer에 한국어 자연어 질문을 입력하고 전송할 수 있다.

**Consequences:**
- **C-2.1:** 빈 입력과 공백만 있는 입력은 Client와 API에서 모두 차단한다.
- **C-2.2:** `Enter`는 전송하고 `Shift+Enter`는 줄바꿈한다.
- **C-2.3:** Prompt를 URL Query 또는 GET 요청에 직렬화하지 않는다.

### 4.2 에이전트 답변과 대화 문맥

#### FR-3: 에이전트 답변 생성과 표시

시스템은 에이전트 Run을 실행해 Assistant 답변을 만들고, User 메시지 다음에 대화 순서대로 표시한다.

**Consequences:**
- **C-3.1:** 생성 중인 답변과 완료된 답변을 구분한다.
- **C-3.2:** 최종 완료 전 출력은 미완료 상태임을 명시한다.
- **C-3.3:** 내부 Prompt, Provider Trace, 도구 호출 원시 인자·결과와 Chain-of-Thought를 표시하지 않는다.
- **C-3.4:** 한 Run의 Step 수에는 상한이 있다. 상한에 도달하면 그때까지 읽은 근거로 답하거나, 답할 수 없음을 알리고 끝낸다. 상한 때문에 검색을 끝낸 경우 그 사실을 짧게 표시한다. 상한 값은 Architecture에서 정한다.

#### FR-4: 세션 내 후속 질문

사용자는 활성 세션에서 앞선 User·Assistant 메시지 문맥을 바탕으로 후속 질문을 할 수 있다.

**Consequences:**
- **C-4.1:** 새 대화는 이전 세션의 문맥을 사용하지 않는다.
- **C-4.2:** 문맥이 Server 한도를 넘으면 가장 오래된 완결 Turn부터 제외하고, 문맥이 축소되었음을 사용자에게 알린다.
- **C-4.3:** 이전 Turn에서 읽은 Wiki 문서 내용은 후속 Turn의 문맥 한도 계산에 포함된다.

### 4.3 Wiki 검색과 근거

#### FR-8: Wiki 근거 부족 처리 (이전 "지식 미연결 고지"를 대체)

모든 답변 내용은 Wiki를 근거로 한다. Wiki에서 근거를 찾지 못하면 시스템은 답하지 않고, 그 질문을 **Wiki 보충 대상** 또는 **보충 불가**로 분류해 알린다.

**Consequences:**
- **C-8.1:** Wiki 근거가 없는 답변을 Wiki 근거 답변처럼 표시하지 않는다.
- **C-8.2:** 일반 Model 지식으로 답을 보충하거나 대신하지 않는다. 근거가 없을 때의 안내는 Model이 쓰지 않은 고정 문구로 보여 준다.
- **C-8.3:** Wiki 밖의 사내 문서나 웹을 검색한 것처럼 표현하지 않는다.
- **C-8.4:** Wiki가 질문의 일부만 다루면, 다룬 부분만 답하고 Wiki에 없는 부분을 명시한다.
- **C-8.5:** 인사와 이 챗봇의 사용법·기능에 관한 메타 질문에는 Model이 생성하지 않은 고정 안내 문구로 응답하고, 근거 목록은 표시하지 않는다.
- **C-8.6:** 근거가 없는 질문은 둘 중 하나로 분류해 표시한다. **Wiki 보충 대상**은 Wiki가 다루는 주제(AIDD 도구·워크플로·방법론) 안이지만 아직 정리되지 않은 질문이다. **보충 불가**는 Wiki가 다루는 주제 밖의 질문이다.

#### FR-9: Wiki 검색과 읽기

에이전트는 질문에 답하기 위해 설정된 Wiki 루트에서 문서를 찾고 읽을 수 있다.

**Consequences:**
- **C-9.1:** `index.md`와 Canonical 문서를 먼저 탐색하고, Canonical 문서가 다루지 않을 때만 `raw/`를 직접 검색한다. 문서의 `sources`나 `^[raw/…]` 표기를 따라 `raw/`를 확인하는 것은 허용한다.
- **C-9.2:** `inbox/`, `docs/`, `.obsidian/`, `.git/`, `.ua/`(대소문자 무시)와 Wiki 경로 밖의 파일은 읽지 않는다.
- **C-9.3:** 모든 Wiki 도구는 읽기 전용이다. 에이전트는 Wiki 파일을 생성·수정·삭제할 수 없다.
- **C-9.4:** Wiki는 질문 시점의 로컬 파일을 그대로 읽는다. 사용자가 Wiki를 고치면 별도 준비 작업 없이 다음 질문부터 반영된다.
- **C-9.5:** Wiki 경로가 없거나 읽을 수 없으면 Run을 구체적인 한국어 오류와 함께 `failed`로 끝낸다. Wiki 없이 조용히 일반 답변으로 대체하지 않는다.

#### FR-10: 답변의 Wiki 근거 표시

완료된 답변은 에이전트가 실제로 읽고 사용한 Wiki 문서를 함께 보여 준다.

**Consequences:**
- **C-10.1:** 근거 목록에는 문서 제목과 Wiki 기준 상대 경로를 표시한다.
- **C-10.2:** 에이전트가 읽지 않은 문서는 근거로 표시하지 않는다.
- **C-10.3:** 근거 문서의 frontmatter에 `contested: true`가 있거나 `confidence`가 `low`이면 그 사실을 답변 근처에 표시한다.
- **C-10.4:** 부분 출력이나 실패·취소된 Run에는 근거 목록을 확정 표시하지 않는다.

#### FR-13: 범용 Markdown Wiki 연결

사용자는 `my-llm-wiki`뿐 아니라 아래 조건을 만족하는 어떤 Markdown 폴더든 Wiki 루트로 설정해 쓸 수 있다.

**Consequences:**
- **C-13.1:** Wiki 문서는 Wiki 루트 아래(하위 폴더 포함)의 `.md` 파일이다. 다른 형식의 파일은 목록·검색·읽기에서 무시한다.
- **C-13.2:** Wiki 루트에 `index.md`가 있어야 한다. 기동 시 `index.md`가 없으면 서비스는 준비되지 않은 상태로 남고 질문을 받지 않는다. 기동 후 Wiki를 읽을 수 없게 되면 C-9.5를 따른다.
- **C-13.3:** Canonical 폴더와 `raw/`는 선택이다. 있으면 C-9.1의 탐색 순서를 따르고, 없어도 검색과 읽기가 동작한다.
- **C-13.4:** frontmatter는 선택이다. `title`이 없으면 파일명을 제목으로 쓰고, `confidence`·`contested`가 없으면 C-10.3 표시를 하지 않는다.
- **C-13.5:** 지원하는 Wiki 구조와 검색 방식의 한계는 운영 문서에 명시한다.

### 4.4 실행 상태와 복구

#### FR-5: 답변 실행 상태, 진행 단계와 중지

시스템은 `queued`, `running`, `completed`, `failed`, `timeout`, `cancelled` 상태를 구분한다. `running` 중에는 에이전트 진행 단계를 보여 주고, 사용자는 생성을 중지할 수 있다.

**Consequences:**
- **C-5.1:** 진행 단계는 사람이 읽을 수 있는 짧은 한국어 이름(예: "Wiki 목록 확인", "문서 읽는 중: {제목}", "답변 작성 중")과 마지막 갱신 시각으로 표시한다.
- **C-5.2:** Streaming Token은 진행 단계와 분리해 표시한다.
- **C-5.3:** 중지하면 진행 중인 도구 호출과 Model 호출이 모두 멈춘다.
- **C-5.4:** 취소 요청 전에 결과가 확정된 경우에는 `completed` 상태를 유지한다.
- **C-5.5:** Run 상태 집합은 이전과 같다. 진행 단계는 `running`의 하위 정보이지 새 Terminal State가 아니다.

#### FR-6: 오류와 Timeout 복구

에이전트, 도구 또는 Model Provider가 실패하거나 Timeout이 발생하면, 시스템은 미완료 출력을 최종 답변으로 표시하지 않는다. 대신 구체적인 한국어 오류와 `재시도` 또는 `새 대화` 행동을 제공한다.

**Consequences:**
- **C-6.1:** 오류는 Code, 한국어 Message, Retry 가능 여부와 Correlation ID를 포함한다.
- **C-6.2:** 재시도는 같은 요청에 대해 완료 답변을 중복으로 만들지 않는다.
- **C-6.3:** 실패 시 근거 없는 축소 답변이나 정적 대체 답변을 반환하지 않는다.

### 4.5 제품 Surface와 디자인

#### FR-7: 웹·API Q&A Surface

사용자는 한국어 중심 반응형 웹 UI에서 핵심 Q&A 흐름을 수행할 수 있다. 같은 질문·상태·진행 단계·근거·답변 계약은 문서화된 API로도 제공된다.

**Consequences:**
- **C-7.1:** Web과 API는 같은 상태와 오류 의미를 사용한다.
- **C-7.2:** 서비스는 로컬 환경(Container 포함)에서 재현 가능하게 실행할 수 있어야 한다.
- **C-7.3:** 현재 Release는 로컬 실행만 제공한다. 기존 공개 데모는 운영하지 않는다.

#### FR-11: 디자인 스파인 전면 적용

모든 화면과 상태는 `docs/DESIGN.md`와 `docs/EXPERIENCE.md`를 따른다. 이번에 새로 생기는 요소(진행 단계, 근거 목록, 근거 부족 표시, contested·low confidence 표시)도 포함한다.

**Consequences:**
- **C-11.1:** 색상, 글꼴, 간격, 모서리와 그림자는 `DESIGN.md` 토큰만 사용한다. 색상 리터럴은 `:root` 밖에 두지 않는다.
- **C-11.2:** 시작, 대화, 진행 중, Streaming, 완료, 실패·복구, 근거 부족 상태 화면이 모두 같은 시각 규칙을 따른다.
- **C-11.3:** `DESIGN.md`가 다루지 않는 새 컴포넌트가 필요하면, 구현보다 먼저 `DESIGN.md`에 규칙을 추가한다.
- **C-11.4:** 디자인 적용 때문에 NFR-7·NFR-8의 접근성·반응형 기준이 후퇴하지 않는다.

### 4.6 Model 연결

#### FR-12: Ollama Model 교체와 조건 확인

운영자는 코드 변경 없이 설정의 Model 태그만 바꿔 Ollama(OpenAI 호환)의 다른 Model을 연결할 수 있다. 시스템은 기동 시 컨텍스트 길이를 확인하고, 조건을 만족하지 못한 Model이 조용히 잘못된 답을 만들지 않게 한다.

**Consequences:**
- **C-12.1:** Model 교체에 코드 변경은 필요 없고, Web·API 계약은 바뀌지 않는다(NFR-10). 같은 계열 안에서는 설정만 바꾸고, 다른 계열로 바꿀 때는 C-12.4에 따라 Tokenizer도 함께 바꾼다.
- **C-12.2:** Model의 실제 컨텍스트 길이가 설정된 문맥 한도보다 짧으면 서비스는 준비되지 않은 상태로 남고 원인을 기동 Log로 알린다. 문맥 앞부분이 조용히 잘린 채 답하지 않는다.
- **C-12.3:** 에이전트는 도구 호출을 지원하는 Model을 전제로 한다. 기동 시에는 이를 확인하지 않는다. 미지원 Model로 질문하면 Run은 C-6.1 형식의 Provider 오류와 함께 `failed`로 끝나고, 근거 없는 답변을 만들지 않는다.
- **C-12.4:** 문맥 한도는 설정된 Tokenizer로 계산한다. 시스템은 Tokenizer와 Model의 일치를 자동으로 확인하지 않으므로, Model 계열을 바꿀 때 Tokenizer도 함께 바꾸는 절차를 운영 문서로 제공한다.

## 5. 성공 지표와 완료 조건

### 5.1 성공 지표

- **SM-1 Q&A Completion:** 고정된 정상 질문 Fixture의 100%가 User Message → Running(진행 단계 포함) → Completed Assistant Message 흐름을 끝낸다.
- **SM-2 State Integrity:** Failed, Timeout, Cancelled 또는 연결 중단 Fixture에서 부분 출력이 완료 답변으로 표시되는 건수는 0건이다.
- **SM-3 Conversation Isolation:** 새 대화가 이전 대화 문맥을 사용하는 사례는 0건이다.
- **SM-4 Recovery:** 재시도 가능 실패 Fixture의 100%가 한국어 오류, Retryability, Correlation ID와 동작하는 재시도 경로를 제공하며, 완료 답변을 중복으로 만들지 않는다.
- **SM-7 Wiki Grounding:** Wiki가 답을 담고 있는 질문 Fixture 10개 중 9개 이상에서, 정답이 담긴 문서가 근거 목록에 포함된다. 또한 사람이 검토해 답변의 주요 주장이 근거 문서에서 확인되는지 본다(근거 없는 추가 주장 0건).
- **SM-8 Honest Miss:** Wiki가 다루지 않는 질문 Fixture 10개(Wiki 주제 안 5개, 주제 밖 5개)의 100%에서 일반 지식 답변이 없고, 8개 이상이 보충 대상·보충 불가로 올바르게 분류된다.
- **SM-9 Read-only:** 모든 Fixture 실행 전후에 Wiki 파일 변경은 0건이다(`git status` 기준).
- **SM-10 Design Conformance:** 모든 상태 화면이 `DESIGN.md` 토큰 규칙 Test를 통과하고, Wiki 소유자가 시각 검토에서 승인한다.

### 5.2 Counter-Metrics

- 근거 문서 수가 많다고 좋은 답변으로 보지 않는다.
- 에이전트 Step 수나 도구 호출 수를 성과로 보지 않는다.
- 응답 속도를 위해 진행 단계, 근거 부족 표시나 실패 상태를 숨기지 않는다.

### 5.3 Release Completion Evidence

- 한국어 질문부터 진행 단계, Streaming, 근거 목록까지 동작하는 Web E2E 기록
- 새 대화 격리, 후속 질문, 중지(도구 실행 중 포함), 실패, Timeout과 재시도 Test 결과
- API Schema와 오류·진행 단계·근거 계약 Test 결과
- SM-7·SM-8 Fixture 결과와 SM-9 Wiki 무변경 확인
- 320 CSS px, 200% Zoom, Keyboard 및 Reduced Motion 검증 결과
- Secret과 대화 본문이 없는 운영 Log 검증 결과
- 로컬 재현 절차와 성공 기록

## 6. Cross-Cutting NFRs와 Guardrails

### 6.1 성능·신뢰성

- **NFR-1:** Warm 상태에서 요청 후 2초 안에 실행 상태를 표시한다. 진행 단계는 새 Step이 시작될 때마다 갱신한다. 최종 답변은 p50 15초·p95 45초를 목표로 한다(에이전트의 여러 단계를 감안해 이전 8초·20초에서 완화). 고정 Prompt Set을 최소 20회 실행하고 Client에서 측정한다.
- **NFR-2:** 120초를 초과하면 Timeout으로 종료하고 안전한 재시도를 제공한다. 부분 출력은 완료 답변이 될 수 없다.
- **NFR-3:** 같은 질문의 재시도와 Network Reconnect는 완료 답변을 중복으로 만들지 않는다.

### 6.2 보안·개인정보

- **NFR-4:** Provider Credential과 Secret은 Client, 응답 또는 Log에 노출하지 않는다.
- **NFR-5:** Prompt, 답변 본문, 도구 호출 인자·결과, Wiki 문서 본문, Model Intermediate와 Chain-of-Thought를 운영 Log에 기록하지 않는다. 대화는 영구 저장하지 않는다.
- **NFR-6:** 외부 Provider를 쓰는 경우 Wiki 문서 내용이 Provider로 전송된다는 사실과 Provider 정보를 사용자에게 고지한다.
- **NFR-11:** Wiki 접근은 설정된 Wiki 루트 안의 읽기로만 제한한다. 경로 탈출(`..`, Symlink)을 차단한다.

### 6.3 사용성·접근성

- **NFR-7:** Keyboard만으로 새 대화, 질문 작성·전송, 답변 중지, 재시도와 근거 목록 확인을 할 수 있어야 한다. 상태와 contested 표시는 색상만으로 전달하지 않는다.
- **NFR-8:** 320 CSS px와 200% Zoom에서 핵심 대화 흐름이 Page-Level 가로 Scroll에 의존하지 않아야 하고 `prefers-reduced-motion`을 지원해야 한다. 진행 단계 갱신은 Live Region으로 전달하되 단계가 바뀔 때만 알리고 Token 단위로는 알리지 않는다.

### 6.4 관측과 확장 경계

- **NFR-9:** 운영 관측 정보는 Correlation ID, Run 단계, Step 수, 도구 이름, 지연, 결과 상태와 오류 유형으로 제한하고 대화·문서 본문을 포함하지 않는다.
- **NFR-10:** 에이전트 Runtime이나 Model Provider를 교체해도 Web·API의 질문·상태·진행 단계·근거·답변·오류 계약은 바뀌지 않아야 한다. 기존 질문·상태·답변·오류 계약은 하위 호환을 유지하고, 진행 단계와 근거는 추가 필드로 확장한다.
- **NFR-12:** 기존 소스 구조(Web, Chat API, Application Service, Port 경계)와 테스트 자산은 최대한 유지한다. 에이전트 도입은 Model 호출 계층을 교체하는 범위로 제한한다.

## 7. 위험과 대응

| Risk | Impact | Product response |
| --- | --- | --- |
| Wiki에 없는 내용을 Wiki 근거처럼 답함 | 잘못된 확신 | 실제로 읽은 문서만 근거 표시(C-10.2), 근거 부족 선언(FR-8), SM-8 |
| 에이전트 Runtime의 언어가 기존 백엔드와 다름(pi agent는 Node 전용, 기존은 Python) | 구조 변경 확대, 기존 소스 훼손 | NFR-10·NFR-12로 교체 범위를 Model 호출 계층으로 제한. 연동 방식은 Architecture에서 결정 |
| 에이전트 Runtime이 1.0 이전이라 API가 자주 바뀜 | 업그레이드 파손 | 버전 고정, Port 경계 뒤에 격리 |
| 에이전트가 Wiki를 수정하거나 Wiki 밖을 읽음 | 지식 훼손, 정보 노출 | 읽기 전용 도구(C-9.3), 경로 제한(NFR-11), SM-9 |
| 여러 단계 실행으로 지연이 늘어남 | 체감 품질 저하 | 진행 단계 표시(FR-5), Step 상한(C-3.4), Timeout |
| 다른 Model이 컨텍스트 길이나 도구 호출 조건을 만족하지 못함 | 문맥 유실, 첫 질문 실패 | 기동 시 컨텍스트 길이 확인(C-12.2), 명확한 실패(C-12.3), 운영 문서의 Model 조건 |
| 다른 주제의 Wiki를 연결함 | 근거 부족 분류(C-8.6)와 고정 안내(C-8.5)가 어긋남 | 한계를 운영 문서에 명시(C-13.5), OQ-2 |
| 새 UI 요소가 디자인 스파인을 벗어남 | 시각 품질 저하 | `DESIGN.md` 선반영(C-11.3), 토큰 규칙 Test(SM-10) |

## 8. Open Questions

- **OQ-1:** 향후 좋은 답변을 `queries/`에 Filing하는 기능을 사용자 승인 방식으로 추가할지. 현재는 범위 밖이다.
- **OQ-2:** Wiki가 다루는 주제를 설정으로 받아 C-8.5·C-8.6을 다른 주제의 Wiki에도 맞출지. 현재는 AIDD 도구·워크플로·방법론 주제를 가정한다.

## 부록 A. 입력 문서와 변경 근거

- 2026-09-23 사용자 결정: Pydantic AI 기반 직접 구현 대신 pi agent(`@earendil-works/pi-agent-core`) 사용, `my-llm-wiki` 검색 에이전트, Apple 디자인 스파인 전면 적용, 기존 소스 최대 유지, 개인용.
- `my-llm-wiki` 구조(`SCHEMA.md`, `AGENTS.md`, `index.md`): Canonical·raw 계층, frontmatter(`confidence`, `contested`, `sources`) 계약을 FR-9·FR-10에 반영했다.
- `docs/DESIGN.md`, `docs/EXPERIENCE.md`(commit `af3e18e`): FR-11의 기준 문서다.
- 2026-10-01 사용자 결정: 이미 구현되어 README(`모델 조건`, `지원하는 Wiki 구조`)에 문서화된 Model 교체와 범용 Markdown Wiki 지원을 FR-12·FR-13으로 추가, FR-9 문장 일반화, C-9.2에 `.ua/`와 대소문자 무시 반영, OQ-2 추가.
- 이전 PRD(2026-08-23) 대비 변경: FR-3·FR-5·FR-6·FR-7 수정, FR-8 대체, FR-9·FR-10·FR-11 추가, FR-4(C-4.3) 보강, NFR-1·NFR-5·NFR-6·NFR-7·NFR-8·NFR-9·NFR-10 수정, NFR-11·NFR-12 추가, SM-5·SM-6 삭제, SM-7~SM-10 추가.
