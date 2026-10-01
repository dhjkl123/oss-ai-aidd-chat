# `plan.md` PRD source extract

- Source: `plan.md`
- Source title: 오픈소스 AI 자동화 에이전트 프로젝트 계획서
- Source status: 장기 비전과 교육 프로젝트 MVP 분리·재설정 완료, 세부 기술 결정 상담 진행 중
- Source last updated: 2026-08-20
- Extraction rule: **명시**는 문서에 직접 선언된 내용, **시사점**은 여러 명시 문장을 조합해 읽을 수 있지만 문서가 요구사항으로 직접 선언하지 않은 내용이다. 시사점은 새 요구사항으로 간주하지 않는다.

## 1. Product vision

### 명시

- 상위 제품 개념은 **Enterprise AIDD Tailoring Evidence & Enablement Factory**이다.
- 핵심 업무는 적용 맥락이 다른 하나의 AIDD 테일러링이다.
  - 신규·전환 프로젝트(AM): 시스템과 개발 프로젝트의 As-Is/To-Be를 바탕으로 AI 기반 개발 수행 방식을 설계한다.
  - ITO: 기존 SR lifecycle의 As-Is와 AI 적용 후 To-Be를 바탕으로 접수, 영향도 분석, 개발, 검증, 산출물 작성과 전파 방식을 설계한다.
- 장기 비전은 테일러링, 근거 생성, 승인, 스킬 패키징·배포, 현업 지원, 관측, 사례 축적을 잇는 enterprise factory이다.
- 이번 교육 프로젝트의 제품은 **근거 기반 AIDD 테일러링 에이전트**로 한정한다.
- MVP 한 문장: AI 확산 담당자가 AM 또는 ITO 업무 맥락을 구조화해 입력하면, `my-llm-wiki`의 GraphRAG 근거를 이용해 AIDD 테일러링 초안과 검증이 필요한 가설·Pilot 계획을 만들고 모든 판단에 출처와 근거 수준을 표시하는 AI 서비스이다.
- 장기 비전은 유지하되 조직 전체 Enablement·Skill Factory는 교육 프로젝트 구현 범위에서 제외한다.
- 제품의 핵심 질적 지향은 숙련자의 최대 생산성보다 **비숙련자가 검증된 순서·판단 기준·가드레일을 따라 최소 품질과 안전 기준을 달성하도록 조직 AI 역량의 최저점을 높이는 것**이다.

### 시사점(요구사항 아님)

- 이 제품은 범용 대화형 비서보다 의사결정 지원, 근거 추적, 불확실성 통제에 중심을 둔 전문 업무 서비스로 해석된다.
- 검색은 목적 자체가 아니라 테일러링 판단과 pilot 설계를 뒷받침하는 수단으로 해석된다.
- 장기 비전의 규모가 크므로 MVP 판단에서는 교육 시나리오의 검증 가능성과 공개 가능한 데이터 경계가 우선하는 것으로 읽힌다.

## 2. Users and actors

### 명시

- 1차 사용자: 사내 AI 확산 조직에서 AIDD 방법론을 조사·전파하고 각 조직과 프로젝트 상황에 맞게 테일러링하는 담당자.
- 이 조직은 타 부서 실제 과제를 지속 지원해 실증 사례를 확보하며, 과제 발굴부터 확산까지 일관된 운영 contract가 필요하다.
- 2차 사용자: 테일러링 결과를 협의·적용하는 개발 조직의 Tech Lead, PL, 아키텍트, 개발자.
  - 이들은 추천 근거와 적용 조건의 소비자이다.
  - 동시에 workflow와 스킬을 사용해 실제 개발을 수행하고 피드백을 제공하는 실행 주체이다.
- MVP 대상 사용자: 사내 AI 확산 조직의 AIDD 방법론·테일러링 담당자.
- MVP 데모 사용자: 공개·합성 AM/ITO 시나리오로 테일러링을 검토하는 개발자.
- 현업 개발자용 OpenCode·Copilot 스킬 실행은 후속 단계이다.
- 역할 경계:
  - AI 확산 조직은 개발 대행 조직이 아니며, 테일러링·스킬·교육·코칭·평가 프레임을 제공한다.
  - 현업 조직은 도메인·맥락·제약을 제공하고, 실제 코드·테스트·산출물 작성, 승인, 배포 및 결과 책임을 유지한다.
- 장기 운영의 추가 행위자: 업무·개발·보안·운영 책임자, executive sponsor, business owner, delivery owner.

### 시사점(요구사항 아님)

- MVP의 주된 사용 장면은 최종 실행 자동화보다 전문가가 초안을 검토·수정·승인하는 human-in-the-loop 분석 작업으로 해석된다.
- 2차 사용자는 장기 비전에서는 핵심 실행자이지만 MVP에서는 주로 데모·검토자 역할에 머문다.

## 3. Problems and needs

### 명시

- AIDD 방법론 조사·비교·설명과 프로젝트별 테일러링에는 반복 업무가 존재한다.
- 현재 공개 자료는 제품·방법론 문서와 workflow contract 중심이며, 실제 기업의 As-Is/To-Be 전환이나 ITO SR 전체 lifecycle을 AIDD로 테일러링한 구체 사례가 부족하다.
- 이 사례 부족은 검색 품질만으로 해결할 수 없는 핵심 업무 병목이다.
- 현재 `my-llm-wiki`는 완성된 정답 집합이 아니라 공개 근거와 앞으로 생성할 실험·사례를 함께 축적하는 출발점이다.
- 조직의 AI 활용 성숙도는 구성원·팀마다 편차가 크고 전반적 하한선이 낮다.
- 근거가 부족한 상황에서 강한 추천을 만들면 잘못된 조직 표준과 과신으로 이어질 수 있다.
- 방법론·도구·시스템 버전이 변하며, 최신성·호환성 판단 실패 가능성이 있다.
- 평균 생산성만 보면 숙련자 개선이 비숙련자 실패를 가릴 수 있다.
- 장기 현업 지원에서는 단계·영향도·승인·산출물 누락, 과도한 권한, 프로젝트별 예외의 공통 스킬 오염, 지원 과제의 측정 부재, sponsor 부재, 성공 사례 선택 편향 등이 문제다.

### 시사점(요구사항 아님)

- 핵심 사용자 문제는 “어떤 방법론이 좋은가”보다 “이 맥락에서 무엇을 유지·변경·자동화하고 어디에 사람 책임과 검증을 둘 것인가”를 근거와 함께 결정하는 것으로 해석된다.
- 제품 신뢰는 답을 많이 생성하는 능력보다 근거가 없을 때 멈추거나 가설로 낮추는 능력에 크게 좌우되는 것으로 읽힌다.

## 4. Desired outcomes

### 명시

#### MVP outcomes

- 구조화된 AM/ITO Context Card로부터 AIDD Tailoring Canvas를 만든다.
- 개발활동별로 `Human only / AI assist / AI execute with approval / AI execute with verification / Automated` 판정을 제공한다.
- 유지·변경·개선 대상, AI 적용 단위, 사람·AI 역할, human gate, 방법론 요소, 적용·비적용 조건, 리스크, 미확정 사항을 표현한다.
- 판단마다 관련 `my-llm-wiki` citation, 근거 수준, confidence·version 맥락을 드러낸다.
- 근거 부족 시 단정 대신 검증 필요 가설과 Pilot 계획을 만든다.
- 사람이 검토한 결과를 Markdown Case Draft로 내보낸다.
- 교육 과정이 요구하는 개인 지식베이스 기반 RAG/GraphRAG, 멀티스텝 에이전트, 평가, API/UI, 컨테이너 배포를 좁은 업무 시나리오로 경험하고 공개 산출물로 남긴다.

#### Long-term outcomes

- 승인된 테일러링 결과를 프로젝트 수행 가이드, 조직 표준, versioned Agent Skills 패키지로 변환·배포한다.
- 검증된 workflow, 판단 기준, 가드레일, validator를 통해 비숙련자의 시행착오와 품질 편차를 줄인다.
- gap → hypothesis → pilot → evaluation → case → reusable pattern의 근거 생성 loop를 운영한다.
- 타 부서 지원 portfolio에서 성공·실패·중단·수정까지 동일 evidence contract로 축적하고 재사용 가능 패턴을 확산한다.
- 중앙 enablement와 현업 local execution의 책임·데이터 경계를 유지한다.

### 시사점(요구사항 아님)

- MVP의 성공은 완전 자동화보다 설명 가능하고 검토 가능한 초안, 유효한 citation, 적절한 abstention으로 판단될 가능성이 높다.
- 장기 성과는 평균 속도뿐 아니라 하위 숙련 집단의 품질과 독립 수행 개선으로 평가되어야 한다는 방향성이 강하다.

## 5. Capabilities

### 명시: MVP

1. **Structured context intake**
   - Word/PDF 등 임의 사내 문서를 자동 수집하지 않고 `Context Card`를 구조화 입력으로 받는다.
   - 입력 항목: AM/ITO 맥락, As-Is 시스템·업무·개발활동, To-Be 목표·변경 기술, 유지 대상, 변경·개선 대상, 팀 AI 성숙도·역할, 자동화 허용 범위·사람 승인 요구, 보안·품질·일정 제약.
   - 정보 완전성과 충돌을 검사한다.
2. **As-Is/To-Be delta analysis**
   - 업무, 기능, 기술, 개발활동, 통제의 다섯 축으로 입력을 정규화하고 delta를 분류한다.
3. **Evidence search and Q&A**
   - `my-llm-wiki`의 방법론 workflow, human gate, 도입 조건을 검색하고 답한다.
   - canonical, raw provenance, tag, version, wikilink 관계를 활용한 GraphRAG 검색을 제공한다.
4. **Tailoring draft generation**
   - 유지·변경·AI 적용 수준·사람 역할·gate·조건·방법론 요소의 조합을 제안한다.
   - 하나의 방법론을 그대로 추천하기보다 유지·변경·자동화·협업·제외·추가 통제 항목으로 테일러링한다.
5. **Evidence Guard**
   - citation, confidence, version, 근거 부족, 과도한 주장을 검사한다.
   - 근거 상태를 `공개 근거 있음 / 사내 사례 있음 / 합성 실험만 있음 / 검증 필요 가설`로 구분하는 장기 모델이 제시되며, MVP 출력도 근거 수준과 미확정 사항을 표시한다.
6. **Pilot Plan**
   - 사례가 부족한 영역에 가설, baseline, 평가지표, 비교 조건·위험·중단 기준을 제안한다.
7. **Human review**
   - 사람이 초안을 검토·수정한 뒤 저장하도록 한다.
8. **Export**
   - Tailoring Canvas와 Markdown Case Draft를 내보낸다.
   - Case Draft는 다운로드 또는 별도 draft 공간에만 생성한다.
9. **Evaluation and comparison**
   - 골든 Q&A와 AM·ITO 합성 Context Card 평가 세트를 사용한다.
   - lexical/vector/GraphRAG를 동일 데이터셋으로 비교한다.
   - RAGAS와 결정적 custom metric을 함께 보고한다.
10. **Demo delivery**
    - FastAPI 기반 서비스와 간단한 웹 UI를 Docker로 실행하고 Hugging Face Spaces에 배포한다.

### 명시: long-term capabilities, MVP 제외

- 프로젝트·SR 전체 lifecycle 테일러링 및 운영 contract.
- Evidence & Case Factory lifecycle과 실증 portfolio 관리.
- 반복·표준화 가능 활동을 core skill → 업무 profile → 시스템/repository adapter의 3계층으로 모델링.
- 스킬 discover, model, compose, evaluate, approve, package, distribute, observe, improve lifecycle.
- Agent Skills 호환 패키지, 조직 registry, 버전·호환성 manifest, rollback, 재검증 trigger.
- AM GitHub Copilot profile과 ITO OpenCode profile 및 surface별 compatibility/fallback 관리.
- 교육, 설치·사용 가이드, 코칭, 질문·장애 대응, 현업 관측과 피드백 환류.
- 지원 과제 intake, readiness/planning, charter, field validation, outcome review, 상태·portfolio 관리.

## 6. Constraints and principles

### 명시

- 교육 프로젝트는 Python을 사용하며, 교육 요구 기술군에는 FastAPI, LangChain, LangGraph, GraphRAG, RAGAS, Obsidian/LLM-Wiki, Neo4j, Docker, Hugging Face Spaces가 포함된다.
- MVP는 공개 모델·프레임워크·평가 도구를 조합한 실전 AI 서비스여야 한다.
- `my-llm-wiki` Markdown과 provenance가 source of truth이다.
- Neo4j, vector index, embedding cache는 재생성 가능한 파생 데이터이다.
- Neo4j는 MVP source of truth가 아니며 Markdown에서 읽기 전용 projection으로 재생성한다.
- 기존 wiki validator를 선행 gate로 사용하고 앱은 wiki에 대해 읽기 전용이다.
- 에이전트 출력은 사람 검토·승인 없이 canonical 지식으로 승격하지 않는다.
- 현재 corpus는 raw 약 118개, canonical 16개로 작다.
- 기술 도입은 baseline에서 시작해 요소를 하나씩 추가하고 동일 골든셋으로 비교해 정당화한다.
- 근거가 없으면 단정하지 않고 abstain하거나 검증 필요 가설 및 pilot으로 전환한다.
- version, confidence, 적용·비적용 조건, 미확정 사항을 노출한다.
- 위험하거나 되돌리기 어려운 작업은 사람 승인 대상으로 올린다.
- 중앙 Enablement Plane은 원칙적으로 현업 소스코드·운영 시스템에 직접 접근하지 않고, 허용된 비식별 지표·실패 유형·수정 피드백만 받는 구조를 우선 검토한다.
- 외부 API 비용·보안, 공개 데모와 사내 데이터 분리, 사내 정책 연결 여부, GPU/사내 endpoint 등은 미확정이다.
- 기술 제약이 확정되기 전에는 구현을 시작하지 않는다는 권고가 문서 말미에 있다.
- 확정된 비가역 결정은 없다. 모델 종속, Neo4j source-of-truth화, 장기 정보 저장, wiki 자동 수정 권한, 특정 비공개 배포 기능 의존은 작은 실험 뒤 결정해야 한다.

### 시사점(요구사항 아님)

- 작고 변경 가능한 데이터셋 때문에 아키텍처는 교체·재생성·비교가 쉬워야 한다는 방향으로 읽힌다.
- 외부 공개 데모는 합성·공개 시나리오 중심이어야 한다는 결론이 자연스럽지만, 구체 데이터 분리 방식은 아직 질문으로 남아 있다.

## 7. Scope boundaries

### 명시: MVP in scope

- 공개 `my-llm-wiki` 기반 근거 검색·Q&A와 GraphRAG 비교.
- AM/ITO Context Card 입력과 완전성·충돌 검사.
- As-Is/To-Be delta 분류.
- 근거 기반 테일러링 초안과 활동별 AI 적용 등급.
- citation·근거 수준·version·abstention 검사.
- 근거 부족 영역의 Pilot Plan.
- 사람 검토 후 Tailoring Canvas·Markdown Case Draft export.
- 합성 AM/ITO 시나리오, 골든 질문, 평가 보고서.
- FastAPI, 간단한 웹 UI, Docker, Hugging Face Spaces 데모.
- PRD, 아키텍처, 평가·한계 문서 공개.

### 명시: MVP out of scope

- 실제 소스코드 개발·수정과 배포.
- OpenCode·GitHub Copilot 스킬 자동 생성·배포.
- SR·문서·사내 포털 시스템 연동.
- 조직 전체 Pilot portfolio 및 사용자·권한 관리.
- 사내 비공개 문서 자동 수집.
- 에이전트의 `my-llm-wiki` canonical·raw 자동 수정.
- 현업 개발자용 스킬 실행.
- 조직 전체 Enablement·Skill Factory 구현.

### 명시: long-term scope

- 승인된 테일러링을 실행 가능한 스킬·가이드·조직 표준으로 변환하고 중앙 registry를 통해 배포.
- AM 및 ITO 실행 환경 profile, 시스템 adapter, permissions/hooks/MCP 설정.
- pilot portfolio, 사례 lifecycle, 현업 교육·코칭, 조직 확산, 관측·개선.

## 8. Success measures and completion criteria

### 명시: MVP completion criteria

- canonical 문서와 raw provenance를 색인하고 citation 경로 유효성 **100%**를 검증한다.
- 근거 Q&A 골든 질문과 AM·ITO 합성 Context Card 평가 세트를 작성한다.
- 최소 1개 AM과 1개 ITO 시나리오에서 Tailoring Canvas와 Pilot Plan을 생성한다.
- lexical/vector/GraphRAG 검색을 동일 데이터셋으로 비교한다.
- RAGAS와 citation validity, abstention, 근거 수준 정확성 custom metric 보고서를 작성한다.
- 근거가 없을 때 단정하지 않고 `검증 필요 가설`로 전환한다.
- FastAPI 서비스와 간단한 웹 UI를 Docker로 실행한다.
- Hugging Face Spaces에 데모 URL을 배포한다.
- PRD, 아키텍처, 평가 보고서, 한계 문서를 공개한다.
- 14주 계획은 데이터 계약·평가 baseline부터 검색, 그래프, 에이전트, evidence guard, pilot/export, 평가, UI, 배포, portfolio 순서로 제시된다.
- 기술 사용 자체가 완료 기준은 아니며, 검색 개선·설명 가능성·실패와 불확실성 통제를 평가 결과로 정당화해야 한다.

### 명시: evaluation measures

- retrieval hit@k.
- citation 존재·유효성.
- 근거 없는 주장률.
- 답변 거절 정확도/abstention.
- 버전 질문 정확도.
- RAGAS 지표와 사람의 실패 사례 검토.
- lexical/vector/wikilink/graph/hybrid retrieval 비교.

### 명시: long-term success perspectives

- 최소 품질: 필수 산출물·테스트·근거 충족률, 하위 25% 결과 품질.
- 업무 완결성: 단계·영향도·승인·산출물 누락률.
- 독립 수행: 숙련자 개입, 질문·재작업·에스컬레이션 횟수.
- 학습·온보딩: 첫 성공까지 시간, 스킬 없이 수행 가능한 비율.
- 표준 준수: 정책·보안·개발 표준 위반률.
- 결과 편차: 사용자·팀별 품질 분산과 실패율.
- 효율: SR lead time, 테일러링 시간, 모델·검토 비용.
- 실증 portfolio: 접수·선정·완료·중단 과제 수와 단계별 전환율.
- 사례 재사용: 타 부서 재현 pattern 비율, 스킬 재사용·수정률.
- 현업 확산: pilot 이후 지속 사용률, 만족도, 추가 적용률.

### 시사점(요구사항 아님)

- MVP completion criteria에는 수치 목표가 citation path 100% 외에는 대체로 제시되지 않았다.
- 장기 success measures는 측정 관점과 예시이며 baseline·target·측정 주기·책임자는 아직 정의되지 않았다.

## 9. Risks and responses

### 명시

| Risk | Stated impact | Stated response |
| --- | --- | --- |
| 작은 데이터로 GraphRAG 효과가 보이지 않음 | 핵심 기술 필요성 설명 곤란 | lexical/vector/graph ablation 결과 자체를 성과로 문서화 |
| LLM이 근거보다 강한 결론 생성 | 잘못된 도입 추천 | citation allowlist, 근거 검사, 부족 시 답변 거절 |
| 버전 정보 노후화 | 최신성 오판 | verified version, updated, revalidation trigger 노출 |
| raw/canonical 경계 훼손 | provenance 신뢰도 하락 | 앱 read-only, wiki validator 선행 gate |
| Neo4j·vector DB·모델 동시 도입 | 디버깅·일정 지연 | baseline부터 하나씩 추가, 동일 골든셋 비교 |
| 외부 API 비용·키 유출 | 운영 중단·보안 사고 | `.env` 비커밋, secret 사용, 예산 상한·로컬 대안 |
| 평가 LLM 편향 | 점수와 실제 품질 불일치 | 결정적 지표와 사람 실패 사례 검토 병행 |
| HF Spaces 비영속 디스크 | index/상태 유실 | 빌드 시 재생성 또는 외부 저장소, 원본 별도 보존 |
| 스킬 과권한 | 잘못된 변경·정보 노출 | 최소권한 allowlist, dry-run, human gate, 감사 로그 |
| 시스템별 예외가 공통 스킬에 누적 | 복잡도·오작동 증가 | core와 adapter 분리, 지원 범위 명시 |
| 방법론·시스템·스킬 버전 불일치 | 재현 실패·낡은 절차 | compatibility manifest, 재검증 trigger, 버전·rollback 관리 |
| 사례 부족 상태의 강한 추천 | 잘못된 표준·과신 | evidence level, confidence, abstention, pilot 우선 |
| 단일 pilot 일반화 | 다른 맥락에서 실패 | 적용·비적용 조건, 반복 검증, 사례 비교 후 승격 |
| 성공 사례만 축적 | 위험·실패 조건 은폐 | 실패·중단·사람 수정도 동일 schema로 기록 |
| 측정 없이 지원 시작 | 성과·학습 사례화 불가 | 시작 전 baseline·지표·중단 기준 합의 |
| sponsor·담당자 부재 | pilot 중단·일회성 데모 | screening에서 책임자·시간·확산 의지 확인 |
| 쉬운 과제만 선정 | 적용성 과대평가 | 선정·탈락 기준과 전체 portfolio 상태 기록 |
| AM 환경 차이 | 일부 환경에서 스킬 미동작 | compatibility matrix, 최소 공통 profile, fallback |
| AM/ITO 내용이 core에 혼합 | 재사용성 저하·잘못된 호출 | core/profile/adapter 3계층과 contract test |

## 10. Qualitative intent and product principles

### 명시

- **Evidence before assertion:** 사례 부족 영역을 검증된 관행처럼 추천하지 않는다.
- **Human accountability:** 업무 규칙, 아키텍처, 보안, 최종 통합·배포 등 책임 판단은 사람이 유지한다.
- **Transparent uncertainty:** confidence, 근거 수준, 충돌, 미확정 사항, 적용·비적용 조건을 표시한다.
- **Fail safely:** 필수 입력이 부족하면 임의 진행하지 않고 누락을 안내하며, 위험·비가역 작업은 승인으로 올린다.
- **Reproducibility:** 입력·절차·output·evidence·failure route·version을 contract로 만들고 결정적 validator와 fixture를 사용한다.
- **Source integrity:** immutable raw와 reusable canonical의 경계를 지키고 Markdown/provenance를 원본으로 유지한다.
- **Progressive technical justification:** 복잡한 기술은 동일 데이터셋의 비교 결과가 가치를 보일 때 채택한다.
- **Raise the floor:** 단계, 완료 조건, 체크리스트, template, validator, 학습 가능한 실패 피드백으로 비숙련자의 최소 품질을 높인다.
- **Contextual tailoring over wholesale adoption:** 하나의 방법론을 통째로 추천하지 않고 맥락별 요소·통제·역할을 조합한다.
- **Separation of concerns:** 공통 contract와 업무 profile, 시스템 adapter, 공개 근거와 비공개 계층, 중앙 enablement와 현업 execution을 분리한다.
- **Learn from failure:** 성공뿐 아니라 실패, 중단, 사람 수정, 일반화 금지 조건을 사례로 남긴다.

### 시사점(요구사항 아님)

- 제품의 어조와 UX는 권위적 자동 추천보다 근거를 제시하는 협업형 검토 도구에 가까워야 한다는 의도가 읽힌다.
- “모르는 것을 모른다고 말하기”와 “검증 계획으로 다음 행동을 제시하기”가 차별적 품질로 간주된다.

## 11. Assumptions and unresolved decisions

### 명시: assumptions

- 프로젝트별 AIDD 테일러링에 반복 조사·비교·설명 업무가 존재한다 — 실제 업무 맥락으로 확인됨.
- 사내·사외 end-to-end AIDD 테일러링 사례가 부족하다 — 실제 업무 맥락으로 확인됨.
- 현재 16개 canonical 문서가 첫 20–30개 골든 질문에 충분하다 — 미검증.
- 한국어 질문과 영어 제품명이 섞인 검색에 multilingual embedding이 유효하다 — 미검증.
- wikilink/provenance가 vector-only보다 설명 가능성을 높인다 — 미검증.
- 공개 모델 또는 외부 API 사용이 교육·보안 조건상 허용된다 — 미검증.
- 최종 데모에서 사람 검토·승인 흐름이 허용된다 — 미검증.
- 미검증 가정은 동료 인터뷰, 제한 pilot, 골든 질문 및 baseline 평가 순으로 검증한다.

### 명시: unresolved requirements/questions

- 공개 AIDD 지식만 사용할지, 사내 정책·표준을 별도 연결할지.
- SR 관리 시스템, 필드, 분류체계.
- 영향도 분석·개발 산출물의 표준 양식과 승인 절차.
- AM IDE/Copilot surface inventory와 최소 지원 범위.
- 표준 OpenCode 버전과 ITO 중앙 설정·배포 방식.
- 실제 입력 정보의 종류·보안 등급.
- 과거 문서·SR의 비식별 평가 활용 가능성.
- 외부 LLM API, 데이터 전송 제한, 비용 상한, 로컬 GPU/사내 endpoint.
- 최종 UI 수준과 사내 포털 필요성.
- Neo4j가 교육상 필수인지 평가 후 선택 가능한지.
- 외부 공개 데모와 사내용 데이터 분리 방식.
- 조직 역량 하한선의 기준 집단·지표 및 다음 2차 목표.
- 실제 또는 비식별·합성 pilot 가능 여부와 공개 범위.
- 요청형/C레벨 지정형 과제의 자원·일정 배분 기준.
- 스킬 전달 후 코칭 기간·채널·운영 방식.

## 12. Extraction guardrails

- 문서의 long-term lifecycle, registry, portfolio, 현업 스킬 실행은 MVP 요구사항으로 승격하지 않았다.
- 기술표의 “1차 권장”과 “대안”은 확정 기술 결정으로 바꾸지 않았다. FastAPI·LangGraph·GraphRAG·RAGAS·Docker·Hugging Face Spaces는 교육/MVP 문맥에서 직접 언급되지만, Python 버전, 패키지 도구, 생성 모델, 실제 GraphRAG 런타임 채택 등은 상담·평가 대상이다.
- 문서의 성공 측정 예시는 수치 목표로 변환하지 않았다.
- ambiguous requirements에 대한 답을 추정하지 않았다.
