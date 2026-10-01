# PRD Quality Review — 한국어 Q&A 챗봇 스켈레톤

## Overall verdict

**조건부 통과 — 소수의 높은 우선순위 수정을 거치면 구현·UX·Story 생성에 사용할 수 있다.** 단순 한국어 Chat Loop라는 제품 논지, 지식 연동 전 단계라는 경계, Tailoring Canvas·Pilot·Human Review·승인·Export의 영구 제거는 `prd.md`와 `addendum.md` 전반에서 명확하고 일관된다. 다만 Non-Goals와 Release Completion Evidence가 서로 충돌하고, 공개 데모가 필수 Release 범위인지 선택적 후속인지 불분명하며, 일부 완료 조건이 구현팀마다 다르게 해석될 여지가 있다.

## Decision-readiness — adequate

§0–§1은 이번 결정이 기존 “근거 기반 AIDD 테일러링 챗봇” 범위를 대체하며, 지금 만들 것은 실제 Model Provider까지 이어지는 Working Skeleton이라고 분명히 선언한다. §5는 Tailoring Canvas, Pilot Plan, Human Review, 승인, Artifact·Case Draft Export를 현재 마일스톤뿐 아니라 제품 목표 범위에서도 제거한다. `addendum.md`의 “폐기된 기술 범위”도 같은 결정을 기술 수준에서 반복하므로 핵심 범위 결정은 행동 가능하다.

반면 Local Working Skeleton과 공개 데모 사이의 Release 결정이 닫혀 있지 않다. §6.1은 “로컬 Container 실행과 공개·합성 데이터 데모”를 모두 In Scope로 두지만, §7.3은 “공개 배포 시”라고 조건부로 쓰고, `addendum.md`는 Hugging Face 배포를 Provider 비용·Credential·Cold Start 정책 검증 이후로 미룬다. 공개 배포 여부에 따라 인증·Rate Limit·Abuse Protection·비용·개인정보 고지의 구현량이 크게 달라지므로 이는 Architecture에만 넘길 기술 세부가 아니라 Release 범위를 바꾸는 제품 결정이다.

### Findings

- **high** 공개 데모의 필수 여부가 닫히지 않음 (§6.1, §7.3, `addendum.md` “실행·배포 방향”) — 공개·합성 데이터 데모는 In Scope이면서 동시에 “공개 배포 시”에만 검증하는 조건부 항목이고, 기술 부록에서는 선행 검증 이후 수행한다고 한다. 이 상태로는 Story와 Release Gate의 크기가 달라진다. *Fix:* 이번 Release를 `로컬 Container 필수 / 공개 배포 후속` 또는 `공개 데모까지 필수` 중 하나로 명시하고 §6.1, §7.3, addendum을 같은 문장으로 정렬한다.
- **medium** 필수와 목표가 혼재됨 (§7.1, §8.1) — SM은 Release 통과 기준처럼 쓰였지만 NFR-1의 p50·p95는 “목표”이고, Provider Cold Start는 별도 측정한다고만 되어 있다. *Fix:* 각 항목을 Release Gate와 관찰 목표로 구분하고, 목표 미달이 출시를 막는지 명시한다.

## Substance over theater — strong

문서의 대부분은 실제 Working Skeleton의 위험에 직접 대응한다. 명시적 Run State, 부분 출력 비완료 처리, 세션 격리, 지식 미연결 고지, Content-Free Log는 각각 §1.1의 문제 정의 및 §9의 위험과 연결된다. 페르소나는 한 명의 지민으로 최소화됐고 세 여정 모두 기능 또는 실패 복구 결정을 이끌어 Persona theater가 아니다. NFR도 대체로 수치 또는 검증 가능한 행위로 표현됐다.

### Findings

- **low** 기술 계약 세부가 제품 요구에 다소 앞섬 (§4.3, `addendum.md` “Wire Contract 후보”) — Monotonic Sequence, Client Idempotency Key와 요청 Digest 등은 유용하지만 단순 Chat Loop의 제품 판단보다 구현 설계에 가깝다. addendum에 둔 방향은 적절하나 FR Consequence가 특정 메커니즘을 암시하지 않도록 경계를 계속 유지할 필요가 있다. *Fix:* PRD에는 “중복 완료 답변 0건” 같은 관찰 가능한 결과만 유지하고 구체 메커니즘은 Architecture에서 확정한다.

## Strategic coherence — strong

제품 논지는 “지식 시스템을 만들기 전에 가장 얇은 실제 Chat Loop를 검증한다”로 명확하다. §1의 비전, §3의 제품 원칙, FR-1–FR-8, §5–§6의 범위, §7의 성공 지표가 새 대화 → 질문 → 상태가 명확한 답변 → 후속 질문/복구라는 하나의 흐름을 지지한다. Retrieval 사용률을 성공으로 보지 않는 Counter-Metric도 이번 범위 축소를 보호한다.

Tailoring Canvas, Pilot, Human Review, 승인 및 Export는 제품과 기술 부록 양쪽에서 폐기되어 과거 PRD의 관성이 남아 있지 않다. Knowledge Graph·Vector·RAG는 미래 후보로만 격리됐고 현재 Runtime Dependency가 아니라는 문장도 일관된다.

### Findings

- **medium** 성공 지표가 제품 성공보다 구현 검증에 치우침 (§7.1) — SM-1–SM-4는 좋은 Release Acceptance이지만 모두 Fixture 기반 정확성이고, 사용자 가치 검증은 SM-5 하나뿐인데 측정 기준이 없다. Working Skeleton이라는 성격상 기술 지표 중심은 타당하지만 SM-5가 현재 표현대로면 성공 여부를 판정할 수 없다. *Fix:* SM-5에 대표 사용자 수, 과업별 성공률, 도움 요청 허용 여부와 측정 시점을 지정하거나, 이를 정성적 Usability Check로 명확히 분류한다.

## Done-ness clarity — adequate

FR ID는 1–8로 연속되고 각 FR에 최소 두 개 이상의 Consequence가 있어 대부분 Story와 Test로 변환 가능하다. 특히 빈 입력 차단, Enter/Shift+Enter, Terminal State 집합, 120초 Timeout, 오류 Envelope 필드, 부분 출력 처리, 320 CSS px와 200% Zoom은 완료 상태가 구체적이다. §7.3의 Release Completion Evidence도 E2E·계약·접근성·Log·Docker 검증 산출물을 요구한다.

그러나 문맥 초과 정책과 지식 경계 고지처럼 사용자 경험에 직접 영향을 주는 요구 일부가 선택지 또는 형용사로 남아 있다. 성능 수치 역시 측정 환경이 없어 동일한 구현을 두 팀이 다르게 판정할 수 있다.

### Findings

- **high** 세션 문맥 초과 동작이 선택지로 남음 (FR-4) — “오래된 메시지를 제외하거나 요약할 수 있으며 그 정책은 일관되어야 한다”는 서로 다른 사용자 동작 두 가지를 모두 허용하고, 초과 시 사용자 고지 여부도 없다. 후속 질문 품질과 개인정보 처리, Test Fixture가 달라진다. *Fix:* MVP 정책 하나를 선택하고 문맥 한도, 제외 순서, 요약 여부, 사용자에게 보이는 결과를 Acceptance 수준으로 정의한다.
- **medium** 성능 목표의 측정 계약이 없음 (NFR-1) — p50 8초·p95 20초는 질문 집합, 표본 수, 측정 시작·종료점, Model/Region, Streaming 시 최초 Token과 최종 완료 중 무엇을 재는지 정의되지 않았다. *Fix:* 고정 Fixture·Provider Revision·Warm 조건·표본 수와 측정 구간을 §7.3 또는 Architecture-owned 성능 계약으로 연결한다.
- **medium** 지식 경계 고지의 완료 조건이 주관적임 (FR-8) — “확인할 수 있게”, “짧고 구체적으로”는 위치, 노출 시점, 지속성, 접근성 이름이 없어 UI 구현과 검증이 흔들린다. *Fix:* 첫 진입/대화 화면의 노출 위치, 최소 전달 의미, 스크린리더 접근 가능 여부를 정의하되 정확한 문구와 시각 스타일은 UX에 위임한다.

## Scope honesty — adequate

§5와 §6은 이번 MVP의 In/Out Scope를 직접 나누며, 특히 사용자가 원하지 않는 Tailoring Canvas·Pilot·Human Review·승인·Export를 “향후”가 아니라 제품 목표 범위에서 제거한다. 반대로 Knowledge Graph·Vector·RAG는 영구 제거가 아니라 미래 Q&A 확장 후보라고 구분해 두 종류의 제외를 정직하게 다룬다. Open Question이나 `[ASSUMPTION]`을 숨겨 둔 흔적도 없다.

다만 Non-Goals 한 항목이 문서의 핵심 완료 조건과 정면으로 충돌한다. 이 충돌을 문맥으로 추측해 넘기면 이후 Epic/Story 추출에서 Test와 Deploy가 삭제될 위험이 있다.

### Findings

- **high** “실제 소스코드 변경·Test·Deploy 실행” Non-Goal이 Release 요구와 충돌 (§5, §7.3, §6.1) — Working Skeleton을 만든다는 문서가 실제 소스코드 변경·Test·Deploy 실행을 제품 Non-Goal로 선언하지만, 동시에 Web E2E, 계약 Test, Docker 실행과 선택적 공개 배포 증거를 완료 조건으로 요구한다. “챗봇이 사용자의 소스코드를 대신 변경·배포하는 기능”을 뜻했다면 현재 표현은 완전히 다르게 읽힌다. *Fix:* 의도가 Tool Calling 기반 개발 자동화 제외라면 “챗봇이 사용자의 저장소를 변경하거나 Test·Deploy를 실행하는 기능”으로 바꾸고, 제품 자체의 개발·검증·배포는 Release Evidence로 유지한다.

## Downstream usability — adequate

UJ-1–UJ-3에는 모두 지민이라는 주인공이 있고, FR·SM·NFR ID는 각각 연속적이며 중복이 없다. 기능이 대화, 답변, 복구, Surface/지식 경계로 묶여 있어 UX·Architecture·Story workflow가 섹션별로 추출하기 쉽다. `addendum.md`도 제품 결과와 기술 선택을 대체로 잘 분리하며, 폐기된 기존 Architecture 구성요소를 명시해 잘못된 재사용 가능성을 낮춘다.

하지만 이 문서는 UX·Architecture·Story 생성의 chain-top이므로 핵심 도메인 용어의 닫힌 의미가 필요하다. 현재 Session/대화, Run/요청/생성, Message/답변, Build/MVP/Release가 정의 없이 혼용되고, “공개 Session 최대 1시간 후 삭제”는 영구 저장을 하지 않는다는 문장과 함께 읽을 때 실제 데이터 생명주기를 추론하게 한다.

### Findings

- **medium** Glossary와 핵심 객체 경계가 없음 (전반, 특히 FR-3–FR-6, NFR-5) — `대화`, `세션`, `Run`, `요청`, `Message`, `답변`의 관계와 수명이 명시되지 않아 API Schema, UX 상태, Story Aggregate가 서로 다른 모델을 만들 수 있다. *Fix:* 짧은 Glossary를 추가해 Conversation/Session/Run/Message, terminal state, 공개 Session TTL을 정의하고 한글·영문 표기를 정규화한다.
- **medium** 공개 Session 데이터 수명 표현이 불완전함 (NFR-5, `addendum.md` “Session과 데이터 경계”) — “영구 저장하지 않음”과 “최대 1시간 후 삭제”는 임시 저장 위치·기준 시점·Server restart 시 동작을 정하지 않아 개인정보 및 Test 요구로 바로 변환하기 어렵다. *Fix:* 세션 데이터가 메모리/임시 저장 중 어디에 존재하는지 Architecture에서 결정하도록 명시하되, PRD에는 TTL 시작점과 1시간 이내 삭제라는 관찰 가능한 결과를 확정한다.

## Shape fit — strong

단일 ChatGPT형 UX이지만 질문, 후속 질문, 실패 복구가 핵심 경험이므로 세 개의 짧은 사용자 여정이 적절하다. 문서는 장기 플랫폼 PRD처럼 기능을 부풀리지 않고, 이번 Working Skeleton의 제품 경계와 향후 Q&A 확장 가능성을 분리한다. 기술 세부 대부분을 addendum에 둔 것도 PRD 본문을 Capability와 사용자 결과 중심으로 유지하는 데 도움이 된다.

현재 문서 길이는 단순 Skeleton치고 엄격한 편이지만 공개 Provider, Streaming, 취소·Timeout, 개인정보, 접근성까지 실제로 검증하려는 범위에는 대체로 비례한다. 공개 데모를 후속으로 내린다면 관련 요구를 함께 줄여 더 가벼운 모양으로 맞출 수 있다.

### Findings

- **low** Local Skeleton과 공개 서비스의 Shape가 한 문서에 섞임 (§6–§9) — 익명 공개 서비스에 필요한 Provider 고지·Subprocessor·Abuse Protection과 로컬 Working Skeleton의 핵심 Chat Loop가 같은 우선순위로 읽힌다. *Fix:* 공개 데모 결정을 닫은 뒤 필수가 아니라면 별도 후속 마일스톤 또는 `[NON-GOAL for MVP]`로 이동한다.

## Mechanical notes

- FR-1–FR-8, UJ-1–UJ-3, SM-1–SM-5, NFR-1–NFR-10은 모두 연속적이고 중복이 없다.
- 미해결 cross-reference는 발견되지 않았다. §6.2의 `Non-Goals` 참조는 유효하다.
- inline `[ASSUMPTION]`, `[NOTE FOR PM]`, Open Question은 없다. §0.1이 Open Question 없음이라고 선언한 것과 형식적으로 일치하지만, 공개 데모 범위는 실질적으로 닫히지 않은 결정이다.
- UJ 세 개 모두 명명된 주인공 “지민”을 사용한다.
- `status: draft`는 Reviewer Gate가 진행 중인 현재 상태에 맞다.
- Tailoring Canvas, Pilot Plan, Human Review, 승인, Review State, Artifact Revision, Export는 PRD의 Non-Goals와 addendum의 폐기 범위 모두에 존재해 제거 결정이 왕복 보존된다.
- 용어 대소문자와 언어가 혼합된다: `Web/web`, `Model Provider/Provider`, `Streaming`, `Build/MVP/Release`, `Session/세션`, `User·Assistant Message`. 짧은 Glossary와 표기 규칙으로 정리하면 downstream 추출 안정성이 높아진다.
