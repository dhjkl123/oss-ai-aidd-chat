# PRD Quality Review — 근거 기반 AIDD 테일러링 챗봇

## Overall verdict

현재 PRD는 Evidence-first thesis, 챗봇의 읽기 전용 범위, Human Review 경계, 기능·지표·NFR·위험 대응을 일관되게 연결하며 빌드와 후속 UX·아키텍처·스토리 작업에 사용할 수 있는 수준이다. 이전의 핵심 결함이던 Evidence State 변환 정책, Artifact 필수 Contract, 상태 어휘 불일치는 대부분 해소되었고 critical/high finding은 없다. 남은 위험은 Release를 막는 일부 평가·완결성 조건의 판정 규칙을 더 정밀하게 고정하는 일이다.

## Decision-readiness — strong

§1과 §4는 제품을 일반 비서가 아닌 근거 기반 의사결정 지원 챗봇으로 명확히 규정하고, §6~§7은 실행 자동화, 인증·승인자 식별, Snapshot 생성·승격을 명시적으로 제외한다. §5.4 FR-13과 §10은 `accepted`가 Session 사용자 확인일 뿐 공식 결재 증명이 아니라는 선택과 결과를 함께 적고 있다. §11의 미결 사항 없음도 제품 동작 결정을 닫은 현재 문서와 일치하며, 외부 Fixture 준비 시점은 구현 Dependency로 정직하게 분리되어 있다.

## Substance over theater — strong

Vision은 AM·ITO Context, Claim-level Evidence, Abstention, Pilot 전환이라는 고유 문제에 고정되어 있다. UJ-1~UJ-3은 동일한 1차 사용자 지민의 검색·테일러링·검토 흐름을 각각 담당하고, 기능 또는 결정을 만들지 않는 persona furniture가 없다. NFR도 p50/p95, 보관·삭제 시간, Fail-Closed 조건, 로그 금지 필드처럼 제품 특화 경계를 제시해 수사적 요구로 남지 않는다.

## Strategic coherence — strong

§1.1의 문제 진술에서 §4의 제품 원칙, FR-4~FR-16의 Evidence Guard·Abstention·Human Review, SM-1~SM-12의 안전·근거 지표로 이어지는 thesis가 뚜렷하다. §8.3은 답변률, 자동화 판정률, Graph 사용률, Output 수를 Counter-Metrics로 두어 핵심 가치가 활동량 최적화로 훼손되는 것을 막는다. 검색 방식도 Lexical Baseline과 동일 조건에서 비교하며 Graph 우위를 전제하지 않는다.

## Done-ness clarity — adequate

FR에는 검증 가능한 Consequences가 붙고, Citation Path·Claim Support·Artifact Hash·Review State·Snapshot Pinning·Export 차단과 NFR 시간이 구체적이다. 새 Evidence 판정 정책(§5.4)은 State 우선순위와 필요·충분조건을, Artifact Required Contract(§5.5)는 versioned export 필드를 제공해 이전의 핵심 모호성을 크게 해소했다. 다만 몇몇 판정 입력과 Release Gate의 평가 자산·조건부 완결성은 여전히 팀별 해석 차이를 허용한다.

### Findings

- **medium** Evidence 판정표의 일부 입력 규칙이 정의되지 않음 (§3 Claim Support Result; §5.4 FR-10~FR-11 Evidence 판정 정책) — `invalid`~`supported` 우선순위는 고정되었지만 “Material Claim”의 범위, Claim Support Result의 `pass`·`fail`·`manual-review` 판정 절차, `stale`이 참조하는 “Version/Freshness Policy”의 임계값이 없다. 동일 Claim이 구현·QA 팀에 따라 다른 State 또는 Export 결과를 가질 수 있다. *Fix:* Material Claim 포함 규칙, Claim Support 판정자·판정 기준·불일치 처리, Source 유형별 Freshness 임계값을 짧은 제품 정책 표로 추가한다.
- **medium** Release-blocking 평가 조건이 완전히 수치화되지 않음 (§8.2 SM-5~SM-11; §8.4 Evaluation Protocol) — 평가 세트 Version 고정, 동일 비교 조건, Confusion Matrix, Blind Pair, 전 지표 Release 차단은 추가되었지만 “명시적인 운영상 이점”(SM-8), “Rubric 점수가 높고”와 “Critical Omission”(SM-10~SM-11), “대표 비숙련”의 표본·자격·최소 효과 크기가 정의되지 않는다. 모든 SM이 Release Gate이므로 이 표현들은 실제 완료 판정을 갈라놓는다. *Fix:* 버전된 Evaluation Contract에 Fixture 최소 수·클래스 구성, 전문가 Rubric과 Critical Omission taxonomy, Baseline 대비 최소 개선 폭 또는 허용 운영상 이점 목록, 사용자 평가 표본·자격을 고정한다.
- **medium** Artifact Required Contract가 조건부 의미 완결성을 닫지 않음 (§5.5 Artifact Required Contract; FR-14~FR-16; §8.1 SM-3) — canonical 필드 목록과 Schema Version은 생겼지만 AM/ITO, Evidence Gap 유무, Abstention, Case Draft에 따라 어떤 목록이 비어도 되는지와 `null`·빈 문자열·`not-applicable`의 유효 조건이 없다. 필드만 존재하는 빈 구조가 “필수 필드 충족률 100%”를 통과할 여지가 있다. *Fix:* Artifact 유형과 상태별 Required/Conditional/Forbidden 및 empty/null/not-applicable 허용 규칙을 Schema acceptance 표로 덧붙인다.

## Scope honesty — strong

§6과 §7.2는 챗봇 전용 범위, Knowledge Supply Pipeline의 외부 소유, 인증·RBAC·Reviewer Identity·공식 결재 제외를 명시한다. §5.4 FR-13, §9.3, §10은 이 선택의 한계를 Audit·Export·위험 대응까지 일관되게 반영한다. 본문에 `[ASSUMPTION]`이 없고 §12도 확인 대기 가정 없음으로 일치한다.

## Downstream usability — strong

UJ-1~UJ-3, FR-1~FR-20, SM-1~SM-12와 SM-C1~SM-C4는 연속적이고 고유하며, 본문의 FR↔UJ 및 SM↔FR 참조가 모두 해석된다. UJ에는 named protagonist와 상황이 있고, Glossary는 새 Claim Support Result와 Artifact Revision까지 포함한다. 이전의 `Unsupported`/`insufficient`, `Approval State`/`Review State` 혼용은 FR-4, SM-4, NFR-14에서 canonical state로 정리되었으며, 기술 구현은 부록으로 분리되어 source extraction이 용이하다.

## Shape fit — strong

단일 1차 운영자 역할의 내부 의사결정 지원 도구에 맞게 capability spec이 중심이고, 의미 있는 UX 전환점만 세 개의 UJ로 표현한다. Snapshot Release는 별도 Knowledge Supply Pipeline이 소유하고 챗봇은 외부 `active_release`를 Pin해 읽기만 한다. 구현 기술은 부록에, 제품 행동과 완료 조건은 PRD에 두어 과도한 persona/UJ 형식이나 구현 중심 shape로 치우치지 않았다.

## Mechanical notes

- ID continuity: UJ-1~UJ-3, FR-1~FR-20, SM-1~SM-12, SM-C1~SM-C4에 gap·duplicate가 없다.
- Cross-reference: 본문의 UJ·FR·SM 참조와 `addendum.md` 링크는 모두 존재한다.
- Assumptions Index roundtrip: inline `[ASSUMPTION]`이 없고 §12의 “없다”와 일치한다.
- UJ protagonist naming: UJ-1~UJ-3 모두 지민의 역할과 상황을 inline으로 포함한다.
- Minor terminology: “Unsupported Claim”과 “Material Claim”은 상태 enum은 아니지만 반복되는 판정 용어이므로 Glossary에 정의하면 추출 안정성이 더 높아진다.
