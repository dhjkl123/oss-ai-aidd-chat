# Final PRD Quality Review — 한국어 Q&A 챗봇 스켈레톤

## Overall verdict

**통과 — UX·Architecture 갱신과 Epic/Story 재생성에 사용할 수 있다.** 이전 리뷰의 critical/high 또는 phase-blocking 성격의 지적은 모두 해소됐다. 제품은 단순 한국어 Q&A Working Skeleton으로 일관되며, Tailoring Canvas·Pilot·Human Review·승인·Export는 역사적 변경 근거 또는 명시적 제외 목록에만 남아 있고 활성 Capability, FR, Journey, Success Metric, NFR, Runtime Dependency로 누출되지 않았다.

## Prior conditional findings resolution

| Prior finding | Status | Evidence |
| --- | --- | --- |
| 공개 데모 필수 여부 불명확 | **Resolved** | §1.1 In Scope이 “공개 배포된 익명 Web 데모”를 포함하고 FR-7이 “Release에 필수인 제공 채널”이라고 확정한다. `addendum.md`의 사전 검증 조건은 범위 유보가 아니라 배포 순서다. |
| Test·Deploy Non-Goal과 Release 증거 충돌 | **Resolved** | §1.1 Out of Scope이 “사용자 요청에 따른 외부 Repository … Action 실행”으로 한정되어 제품 자체의 Test·Deploy와 구분된다. |
| FR-4 문맥 초과 동작 미결정 | **Resolved** | 가장 오래된 완결 User·Assistant Turn부터 제외하고 사용자에게 알리며 Model 기반 요약은 하지 않는다고 결정됐다. Architecture에는 수치 한도만 위임한다. |
| SM-5 측정 기준 없음 | **Resolved** | 대표 사용자 5명 중 4명, 무안내 과업 완료, 실패 Task·막힘 기록으로 기준이 닫혔다. |
| NFR-1 성능 측정 계약 없음 | **Resolved** | Warm 정의, 고정 Prompt Set, 최소 20회, Client 관측 시간, Cold Start·실패 별도 보고가 추가됐다. |
| 핵심 객체 Glossary 없음 | **Resolved** | §2.3이 Conversation, Session, Message, Run, Completed Answer, Streaming Output을 정의한다. |
| Session 데이터 수명 불완전 | **Adequate / non-blocking** | 최대 1시간 TTL과 비영구 저장은 확정됐다. TTL 시작점과 저장 매체는 Architecture에서 상세화할 수 있다. |
| FR-8 고지 검증이 주관적 | **Adequate / non-blocking** | 전달해야 할 핵심 의미는 닫혀 있다. 정확한 위치·문구·접근성 표현은 UX 계약에서 구체화할 수 있다. |

## Decision-readiness — strong

§0은 이번 PRD가 이전 AIDD 테일러링 제품 범위를 대체하며, 현재 제품 범위에 미결정 사항이 없다고 선언한다. 공개 익명 Web 데모도 FR-7에서 Release 필수 채널로 명시되어 Local-only와 Public Release 사이의 선택이 더는 열려 있지 않다. Model Provider, Streaming transport, 구체 Context 한도, Rate Limit·Abuse Protection은 제품 결과를 바꾸지 않는 Architecture 결정으로 정확히 분리됐다.

삭제 범위도 결정으로 표현된다. Tailoring Canvas, Pilot Plan, Human Review, 승인, Artifact·Case Draft Export는 “현재뿐 아니라 장기 제품 범위에서도 제외”되며, 향후 Retrieval Q&A 확장 후보와 섞이지 않는다.

### Findings

- **low** Release Evidence의 조건부 표현 잔존 (§5.3) — 공개 데모가 필수인데 “공개 배포 시 … 확인”이라고 쓰여 있어 문장만 보면 선택적처럼 읽힐 수 있다. FR-7의 명시적 결정이 우선하므로 범위 모호성이나 phase blocker는 아니다. *Fix:* 다음 문서 정리 때 “공개 배포에서 … 확인”으로 바꾸면 완전히 정렬된다.

## Substance over theater — strong

문서의 각 요구는 기본 Chat Loop 또는 그 공개 운영 위험에 연결된다. 세 Journey는 첫 질문, 후속 질문, 실패 복구라는 실제 사용자 경로를 담당하고, NFR은 상태 무결성·개인정보·접근성·관측성을 구체적으로 제한한다. 일반 Model 답변을 사내 지식처럼 표현하지 않는 원칙은 현재 지식 미연결 단계에서 특히 필요한 제품 경계다.

`addendum.md`의 Idempotency, Stream sequence, Cache-Control, Provider Port는 기술 부록에 머물며 PRD 본문은 관찰 가능한 결과 중심이다. 과거 Artifact Aggregate, Evidence Policy, Review Store, Export Renderer는 만들지 않는다고 명시되어 구현 관성이 차단됐다.

## Strategic coherence — strong

“지식 시스템 이전에 실제 동작하는 최소 Chat Loop를 검증한다”는 논지가 §0–§1, Journey, FR, Success Metric, NFR에 걸쳐 유지된다. 새 대화 → 질문 → Streaming/완료 → 후속 질문 → 중지/실패 복구의 일관된 제품 흐름이 있고, Knowledge Graph·Vector·RAG는 현재 호출·Readiness·Release Dependency가 아니다.

SM-1–SM-4는 실행 무결성, SM-5는 무안내 Usability, SM-6은 실제 Provider의 최소 한국어 품질을 검증한다. 답변 길이, 메시지 수, Retrieval 연결률을 성공으로 착각하지 않는 Counter-Metric도 핵심 논지를 보호한다.

## Done-ness clarity — strong

FR-1–FR-8은 모두 테스트 가능한 Consequence를 갖는다. 빈 입력, 키보드 동작, Conversation 격리, 문맥 초과 정책, 여섯 Run State, 120초 Timeout, 중복 완료 방지, 공개 입력 경고가 구체적이다. §5.3은 Web E2E, 오류 계약, 접근성, Log, Docker, 공개 데이터 경계, 실제 Provider Smoke 결과를 Release Evidence로 열거한다.

NFR-1은 측정 표본과 Warm 조건이 생겼고 SM-5/SM-6도 표본·통과율을 정의한다. 정확한 Context Window 수치, Model Revision, Streaming transport는 Architecture 산출물에서 확정해도 제품 완료 의미가 바뀌지 않는다.

### Findings

- **medium** SM-6 사람 검토 Rubric의 판정 척도가 별도 정의되지 않음 (§5.1) — 질문 관련성·언어 적합성의 90% 기준은 있으나 항목별 pass/fail 규칙과 검토자 수가 없다. 이는 Retrieval 사실 정확도 평가가 아니라 Smoke 품질 확인이므로 phase blocker는 아니다. *Fix:* Release Test 문서에서 2개 항목의 합격 예시와 검토자·불일치 처리 방식을 정의한다.
- **low** FR-8 고지의 표시 위치가 UX에 암묵적으로 위임됨 — 전달 의미는 충분히 분명하지만 첫 진입, Composer 인접, 또는 상시 Banner 중 어느 방식인지 정해지지 않았다. *Fix:* UX 갱신 시 노출 시점, Focus/Screen Reader 접근과 재확인 경로를 Acceptance로 만든다.

## Scope honesty — strong

§1.1은 활성 범위, 영구 제외 범위, 향후 Q&A 확장 후보를 분리한다. 사용자 계정·영구 저장·파일·Tool Calling 같은 흔한 챗봇 확장도 명시적으로 제외되어 reader가 기능을 추정할 필요가 없다. 외부 Repository 변경·Test·Deploy 자동 실행 제외는 제품 자체의 개발·검증·배포와 충돌하지 않도록 수정됐다.

Tailoring/Pilot/Human Review/승인/Export 누출 검사를 수행한 결과는 다음과 같다.

- `Tailoring Canvas`, `Pilot Plan`, `Human Review`, `승인`, `Artifact·Case Draft Export`는 §0의 제거 결정과 §1.1 Out of Scope에서만 제품 범위로 언급된다.
- `addendum.md`에서도 과거 기술안 폐기, 만들지 않을 구성요소, 제외된 기술 범위로만 나타난다.
- “사람 검토 Rubric”은 SM-6의 Release QA 절차다. 사용자-facing Human Review, 승인 Workflow, Review State 또는 제품 Capability가 아니다.
- Citation/Evidence는 현재 미연결 고지와 제외 범위로만 존재하며 Evidence Guard, Claim Review 또는 Approval 경로가 없다.
- 활성 FR, UJ, SM, NFR, 실행 경로, Module Seed에는 Canvas·Pilot·Human Review·승인·Export 기능이 없다.

## Downstream usability — strong

Conversation, Session, Message, Run, Completed Answer, Streaming Output의 Glossary가 추가되어 UX State와 API/Domain 모델이 같은 언어를 사용할 수 있다. UJ-1–UJ-3에는 모두 지민이라는 주인공이 있으며 FR-1–FR-8, SM-1–SM-6, NFR-1–NFR-10은 연속·고유하다. 각 요구는 독립적으로 추출 가능한 Consequence를 갖고 있어 새로운 Epic/Story 분해에 적합하다.

제품 문서와 기술 부록의 경계도 명료하다. PRD는 관찰 가능한 결과를, addendum은 Port, Wire, Session storage와 배포 후보를 제공한다. 기존 `aidd_tailoring` Namespace 처리만 실제 Code를 본 뒤 Architecture에서 결정하도록 남겨 Brownfield 상태를 거짓으로 확정하지 않는다.

### Findings

- **low** Session TTL 시작점이 명시되지 않음 (§2.3, NFR-5) — 최대 1시간이라는 제품 상한은 충분하지만 생성 시점, 마지막 활동 시점, Run 완료 시점 중 어느 때부터인지 Test가 정해져 있지 않다. *Fix:* Architecture/API 계약에서 TTL 기준 이벤트와 만료 후 관찰 가능한 응답을 정의한다.

## Shape fit — strong

단일 사용자 중심 ChatGPT형 제품에 짧은 Journey 세 개와 기능 8개가 적절하다. 공개 익명 데모를 Release 범위로 확정했기 때문에 Provider 고지, Rate Limit·Abuse Protection Architecture, Content-Free Log, 접근성 요구는 과잉 형식이 아니라 실제 Surface에 비례한다. 반면 RAG 평가나 지식 Pipeline은 현재 Shape에서 제거됐다.

문서는 chain-top PRD로서 UX, Architecture, Epic/Story 갱신을 지원할 만큼 엄격하지만 장기 플랫폼 Backlog를 가장하지 않는다. 기술 세부를 addendum으로 밀어 본문도 비교적 간결하게 유지됐다.

## Mechanical notes

- Frontmatter `status: draft`는 Final Reviewer Gate 수행 중인 상태와 일치한다.
- FR-1–FR-8, UJ-1–UJ-3, SM-1–SM-6, NFR-1–NFR-10은 연속적이고 중복이 없다.
- UJ 세 개 모두 명명된 주인공 “지민”을 사용한다.
- 깨진 cross-reference나 존재하지 않는 section 참조는 발견되지 않았다.
- inline `[ASSUMPTION]`, `[NOTE FOR PM]`, Open Question은 없다. 공개 배포와 문맥 축소 정책도 본문에서 실제 결정으로 닫혔다.
- Tailoring Canvas·Pilot·Human Review·승인·Export 용어는 제거 결정, 역사 설명 또는 제외 목록에만 존재한다.
- `Review`가 SM-6의 사람 검토 의미로 한 번 사용되지만 제품 Human Review Capability와 상태 계약은 존재하지 않는다.
