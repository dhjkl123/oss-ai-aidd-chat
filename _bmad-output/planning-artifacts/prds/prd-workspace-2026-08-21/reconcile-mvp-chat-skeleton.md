---
title: 'mvp.md ↔ 한국어 Q&A 챗봇 스켈레톤 PRD 정합성 검토'
status: complete
reviewed: '2026-08-23'
sources:
  - '../../../../mvp.md'
  - 'prd.md'
  - 'addendum.md'
---

# mvp.md ↔ 한국어 Q&A 챗봇 스켈레톤 PRD 정합성 검토

## 1. 검토 목적과 판정 기준

이 문서는 루트 `mvp.md`의 기존 “근거 기반 AIDD 테일러링 에이전트” 기준선과, 2026-08-23 사용자 Override를 반영한 `prd.md` 및 `addendum.md`를 대조한다. 최신 사용자 의도는 다음과 같다.

- 현재 마일스톤은 Knowledge Graph, Vector Search 및 RAG를 연결하기 전의 실제 동작하는 Working Skeleton이다.
- 제품 Surface는 ChatGPT처럼 한국어로 간단히 질문하고 답을 받는 챗봇이다.
- Model Provider까지의 기본 Chat Loop는 실제로 동작해야 한다.
- 향후 Knowledge Graph·Vector·RAG 연결을 위한 Retrieval 경계는 보존하되 현재 Runtime에서 호출하지 않는다.
- Tailoring Canvas, Pilot Plan, Human Review, 승인 및 Artifact·Case Draft Export는 현재 범위에서 미루는 기능이 아니라 목표 제품 범위에서 제거한다.

따라서 정합성 판정의 우선순위는 `최신 사용자 Override > prd.md > addendum.md > 루트 mvp.md의 구범위`다. `mvp.md`는 변경 근거로만 사용할 수 있으며 현재 제품 계약으로 병행 해석해서는 안 된다.

## 2. 전체 판정

**조건부 정합(Conditionally aligned).** `prd.md`와 `addendum.md`는 최신 사용자 의도를 실질적으로 반영했다. PRD는 Chat Loop, 실행 상태, 실패 복구, 지식 미연결 고지와 비영속 Session을 제품 핵심으로 재정의했고, 부록은 Model Provider까지의 실행 경로 및 비활성 Retrieval 경계를 분리했다. 또한 사용자가 원하지 않은 테일러링 계열 기능을 명시적 Non-Goal과 폐기 범위로 닫았다.

다만 아래 네 가지 정합성 Gap을 닫기 전에는 루트 기준선, PRD 및 Architecture 입력을 동시에 읽는 구현자가 서로 다른 범위를 채택할 가능성이 남는다.

## 3. Gap Register

### GAP-1 — 루트 `mvp.md`가 여전히 활성 기준선처럼 보인다 (High)

`mvp.md`는 제목과 MVP 정의부터 AIDD 테일러링을 제품 목적으로 둔다(1, 7–12행). Context Card 입력(30–42행), GraphRAG 중심 에이전트 흐름(44–56행), Tailoring Canvas(70–90행), Pilot·Case Draft(53, 67–68행), 사람 검토·승인 의미(38, 54, 68, 76, 88행), RAGAS 평가(118–136행) 및 14주 통합 계획(150–161행)도 그대로 남아 있다. 특히 완료 조건의 Canvas·Pilot 생성(144행)과 RAGAS 보고서(145행)는 새 PRD 완료 조건과 직접 충돌한다.

새 PRD는 자신이 기존 범위를 대체한다고 명시하고(`prd.md` 12–14행), 구기능을 Non-Goal로 닫는다(165–185행). 그러나 루트 파일에는 폐기·대체 표지가 없어 문서 탐색 순서에 따라 구범위가 재유입될 수 있다.

**권고:** 루트 `mvp.md` 상단에 `superseded` 상태와 새 PRD 경로를 명시하거나, 후속 변경에서 문서 전체를 새 MVP 요약으로 갱신한다. 그 전까지 구현·Architecture·Epic 생성의 단일 제품 입력은 `prd.md`와 `addendum.md`로 제한한다.

### GAP-2 — Retrieval Port 보존 요구가 선택적으로 쓰였다 (Medium)

사용자 의도는 미래 연결을 위한 Retrieval 경계를 준비하는 것이다. PRD NFR-10은 Model Provider 또는 향후 Retrieval 구현을 교체·추가해도 외부 계약이 바뀌지 않아야 한다고 요구하고(`prd.md` 251–254행), 부록도 `RetrievalPort`와 비활성 Adapter를 설명한다(`addendum.md` 21–26행).

그러나 부록은 “`RetrievalPort`는 미래 확장 경계로만 **존재할 수 있다**”라고 표현해(`addendum.md` 24행) 구현 여부를 선택 사항으로 남긴다. 최소 모듈 Seed 역시 설명적 후보일 뿐이다(29–42행). 이 상태에서는 구현자가 Retrieval 경계를 전혀 만들지 않아도 계약을 만족한다고 해석할 수 있다.

**권고:** Architecture에서 최소 `RetrievalPort` 계약과 명시적 `disabled` Adapter를 필수 Seed로 확정한다. 정상 Chat 경로에서는 호출 횟수가 0이어야 하고, Readiness 조건이 아니어야 하며, UI에 활성 기능처럼 노출해서는 안 된다. Knowledge·Vector·RAG 구현은 계속 제외한다.

### GAP-3 — 공개 데모가 Release 필수인지 조건부인지 불명확하다 (Medium)

기존 `mvp.md`는 Hugging Face Spaces 공개 URL을 MVP 완료 조건으로 둔다(115, 147행). 새 PRD는 “로컬 Container 실행과 공개·합성 데이터 데모”를 In Scope에 포함하고(`prd.md` 189–200행), 공개 배포 시 확인할 증거도 적는다(230행). 반면 부록은 Provider Credential, 비용, Cold Start와 데이터 정책을 검증한 뒤에만 Hugging Face 배포를 수행한다고 조건화한다(`addendum.md` 66–72행).

따라서 Working Skeleton 완료가 로컬 E2E와 Container 재현만으로 가능한지, 공개 URL까지 필요한지 모호하다. 단순 챗봇 기능 축소와 별개로 배포 Gate가 일정과 Provider 선택을 크게 바꿀 수 있다.

**권고:** 현재 Release Gate는 Web/API→실제 Model Provider의 로컬 Container E2E로 고정하고, 공개 배포는 조건 충족 시 별도 후속 Gate로 분리한다. 공개 URL이 사용자에게 반드시 필요한 경우에만 PRD 완료 조건으로 다시 승격한다.

### GAP-4 — 폐기된 Export 용어가 보안 NFR에 남아 있다 (Low)

PRD는 Artifact·Case Draft Export를 제품 Non-Goal로 명시한다(167–174행). 그런데 NFR-4는 Secret을 “Client, 응답, **Export** 또는 Log”에 노출하지 않는다고 적는다(240–244행). 보안 의도는 타당하지만, `Export`라는 Surface가 존재하는 것처럼 읽혀 폐기된 Artifact Export를 암시한다.

**권고:** 후속 PRD 문구 정리 시 `Export`를 삭제하고 “Client, API·Streaming 응답 또는 Log”로 한정한다. 기능 요구나 구현 Backlog에는 Export Endpoint, Renderer 또는 Artifact Schema를 생성하지 않는다.

## 4. 구범위 누수 점검

| 구 `mvp.md` 범위 | 새 문서 처리 | 판정 |
| --- | --- | --- |
| AIDD Context Card와 AM·ITO Delta | `prd.md` 169행 및 `addendum.md` 87행에서 제거 | Closed |
| Tailoring Canvas·AI Adoption Level | `prd.md` 170행 및 `addendum.md` 88행에서 제거 | Closed |
| Pilot Plan | `prd.md` 171행 및 `addendum.md` 90행에서 제거 | Closed |
| Human Review·승인·Review State | `prd.md` 172행 및 `addendum.md` 90행에서 제거 | Closed |
| Artifact Revision·Case Draft Export | `prd.md` 173행 및 `addendum.md` 90행에서 제거 | Closed, 단 GAP-4 문구 잔재 |
| 현재 GraphRAG·Vector·RAG 통합 | `prd.md` 178–185행 및 `addendum.md` 91–94행에서 미래 후보로 분리 | Closed |
| Citation·Evidence Guard·RAGAS 평가 | 현재 구현·Release Gate에서 제거 | Closed |
| LangGraph 멀티스텝 흐름 | 현재 Runtime Dependency가 아니라고 명시 | Closed |
| 조직 Portfolio·Skill Factory | `prd.md` 174행에서 제거 | Closed |

테일러링 계열 용어가 새 문서에 등장하는 곳은 범위 대체 근거, Non-Goal 또는 폐기 목록이다. 이는 기능 누수가 아니라 금지 경계다. 구현 Story에서는 해당 용어를 Epic 또는 “나중에 구현” Backlog로 변환하지 않아야 한다.

## 5. 유지해야 할 실행 요구사항

루트 `mvp.md`에서 새 제품에도 유효한 실행 골격은 아래와 같이 좁혀 유지됐다.

1. **Web/API 실행:** 기존 FastAPI·웹 서비스 방향(`mvp.md` 108, 146행)은 `prd.md` FR-7과 `addendum.md`의 Browser→Chat API→Application Service 흐름으로 보존됐다.
2. **실제 Model 경로:** 일반 모델 Adapter 개념(`mvp.md` 112행)은 `ModelProviderPort`와 configured Provider 호출로 구체화됐다. 사용자 실행에서 정적 가짜 답변으로 조용히 대체하면 안 된다(`addendum.md` 72행).
3. **재현 가능한 실행:** Docker·로컬 재현(`mvp.md` 114, 146행)은 PRD In Scope 및 Release Evidence에 유지됐다(`prd.md` 153, 200, 229행).
4. **기본 Q&A:** 기존 근거 Q&A 행(`mvp.md` 62행)에서 방법론·Citation 의미를 제거하고, 새 대화·질문·답변·후속 질문의 일반 Chat Loop로 재정의했다(`prd.md` FR-1~FR-4).
5. **상태와 복구:** `queued | running | completed | failed | timeout | cancelled`, 120초 Timeout, 중지, 재시도 및 부분 출력 비완료 처리는 새 Skeleton의 핵심 완료 조건이다.
6. **지식 경계:** 현재 Build에서는 `my-llm-wiki`, KG, Vector, RAG 및 Citation을 호출하거나 사용한 것처럼 표시하지 않는다. Retrieval 비활성은 정상 상태이며 Readiness 실패가 아니다.
7. **데이터 최소화:** Session은 비영속이며 공개 Session TTL은 최대 1시간이다. Prompt·답변·Provider Raw Response·Chain-of-Thought·Secret은 운영 Log에 남기지 않는다.
8. **접근 가능한 Chat Surface:** Keyboard, Focus, Live Region, 320 CSS px, 200% Zoom 및 Reduced Motion 검증은 Release Evidence로 유지한다.

## 6. 구현·Epic 생성용 범위 경계

### 반드시 생성할 구현 Slice

- 한국어 Chat Composer, User·Assistant Message와 새 대화
- Session 내 후속 질문과 새 대화 격리
- Chat API와 `ModelProviderPort`를 통한 실제 Provider 호출
- Streaming 또는 완료 응답, 명시적 Run State와 중지
- 실패·Timeout·취소·멱등 재시도 및 한국어 오류
- Retrieval 미연결·별도 검증 필요 고지
- 비영속 Session, Content-Free Log와 Secret 보호
- 로컬 Container E2E, 계약 Test와 Browser Acceptance Test
- Architecture에서 확정된 비활성 `RetrievalPort` Seed

### 생성하면 안 되는 구현 Slice

- Context Card, Delta, Canvas, AI Adoption Level
- Pilot, Review, Approval 또는 Revision State
- Artifact·Case Draft 생성, Download 또는 Export API
- Citation, Evidence Policy, Claim Support 또는 Abstention Workflow
- Neo4j, GraphRAG, Embedding, Vector DB, Hybrid Retrieval
- RAGAS, Retrieval 비교 또는 Knowledge 승격 Pipeline
- 조직 Workspace, RBAC, Portfolio 또는 Skill Factory

## 7. 최종 권고

현재 PRD 내용은 CE를 다시 수행할 수 있을 만큼 제품 범위가 명확하다. 다만 CE와 Architecture 갱신 전에 최소한 다음을 적용해야 한다.

1. 루트 `mvp.md`를 공식적으로 superseded 처리해 구범위 재유입을 차단한다.
2. Architecture에서 `RetrievalPort`와 disabled Adapter의 최소 Seed를 필수로 확정한다.
3. 공개 배포를 현재 Release Gate로 볼지 조건부 후속 단계로 볼지 한 문장으로 확정한다.
4. PRD NFR-4의 `Export` 용어를 제거한다.

이 네 항목 중 기능 범위를 바꾸는 것은 없다. 모두 최신 사용자 결정이 구현·Architecture·Epic으로 정확히 전달되도록 계약을 닫는 정합성 조치다.
