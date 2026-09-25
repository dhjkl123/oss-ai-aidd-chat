---
name: Korean LLM-Wiki Q&A Agent
status: final
updated: 2026-09-25
sources:
  - ../../prds/prd-workspace-2026-08-21/prd.md
  - ../../prds/prd-workspace-2026-08-21/addendum.md
  - 1.start.html
  - 2.conversation.html
  - 2-1.thinking.html
  - 2-2.streaming.html
  - 8.run-recovery.html
  - 9.info-policy.html
---

# Korean LLM-Wiki Q&A Agent — Experience Spine

이 문서는 정보 구조, 행동, 상태, 접근성과 사용자 여정을 소유한다. [`DESIGN.md`](DESIGN.md)는
시각 토큰과 컴포넌트 표현을 소유한다. 활성 HTML 미리보기와 충돌하면 두 스파인 문서가 우선한다.

`_bmad-output/`은 `.gitignore`가 통째로 무시하므로, 정본으로 참조되는 이 문서와 `DESIGN.md`만
`docs/`로 복사해 추적한다. `_bmad-output/planning-artifacts/ux-designs/ux-workspace-2026-08-22/`의
사본은 산출 당시의 기록이고, 둘이 어긋나면 `docs/` 쪽이 이긴다.

## Foundation

로컬에서 실행하는 개인용 한국어 반응형 웹 챗봇이다. 사용자는 로그인이나 설정 없이 ChatGPT형 화면에서 질문하고, 에이전트가 `llm-wiki`를 읽어 Wiki에 근거한 답을 준다. 제품 가치는 Wiki를 직접 열지 않고도 질문 한 번으로 Wiki 근거 답변과 근거 문서를 얻는 데 있다.

UI는 특정 프레임워크에 종속되지 않으며 시각 정체성은 [`DESIGN.md`](DESIGN.md)를 따른다. [시작](1.start.html), [대화](2.conversation.html), [생각 중](2-1.thinking.html), [스트리밍](2-2.streaming.html), [복구](8.run-recovery.html), [정보·정책](9.info-policy.html)의 여섯 화면만 활성 조합을 보여 주는 참고 화면이다.

모델이 쓴 답변 문장은 모두 Wiki 문서에 근거한다. Wiki에 근거가 없으면 답을 지어내지 않고 **Wiki 보충 대상** 또는 **Wiki 범위 밖**으로 알린다. 에이전트는 Wiki를 읽기만 하며, 사내 문서·웹·Vector Search는 연결하지 않는다. 참고 화면 여섯 개는 에이전트 도입 이전 모습이며, 단계 목록·근거 목록·판정 안내는 이 문서와 `DESIGN.md`의 Project Application (Cite)이 정본이다. 제품 이름은 **Cite**다.

## Information Architecture

| Surface | Reached from | Purpose |
|---|---|---|
| 새 대화 | 앱 진입 / navigation의 `새 대화` | 가운데 질문(`무엇이 궁금하세요?`)과 `llm-wiki에 물어보세요` Composer로 바로 첫 질문 작성(시작 버튼 없음) |
| 대화 | 질문 전송 / 같은 세션 후속 질문 | User·Assistant 메시지를 순서대로 읽고 질문을 이어감 |
| 생성 상태 | 질문 전송 후 대화 안 | Run은 queued·running·completed, Assistant Message는 streaming·completed를 구분하고 에이전트 단계를 보여 주며 생성 중지 |
| 근거 확인 | 완료 답변 아래 `근거 문서 N` pill | pill을 펼쳐 답변에 쓰인 Wiki 문서 제목·경로와 논쟁 중·신뢰도 낮음 표시 확인 |
| 실행 복구 | failed·timeout·cancelled 확정 | 영향과 Correlation ID 확인 후 재시도 또는 새 대화 |
| 정보·정책 | navigation의 `정보·정책` | Provider·Model Revision, 전송 필드(질문·선택된 이전 대화·읽은 Wiki 발췌), 1시간 Session, 연결된 Wiki 이름 확인 |

데스크톱은 260px sidebar(52px icon rail로 접힘, 브라우저에 기억, `Ctrl/Cmd+Shift+S`)와 중앙 768px 대화 열이다. navigation은 새 대화, 현재 대화와 정보·정책에 집중한다. `현재 대화`는 현재 Conversation의 첫 질문(말줄임)과 남은 시간(`N분 남음`, `expires_at` 기준)을 보여 주고, Conversation이 없으면 `현재 대화`라고만 쓰며 대화 화면으로 돌아가는 길로 남는다. 익명 MVP에서 계정, 조직 Workspace, 영구 저장된 대화 목록이나 Cross-device Sync를 암시하지 않는다.

- 첫 질문: 새 대화 → 전송 → 생성 상태 → 완료 답변. Composer는 정책 확인이 끝나는 즉시 열리고, 첫 전송이 Conversation을 만든다. navigation의 `새 대화`는 언제든 빈 대화를 명시적으로 시작한다.
- 후속 질문: 완료 답변 → Composer → 생성 상태 → 다음 완료 답변.
- 중지·오류: 생성 상태 → 실행 복구 → 동일 질문 재시도 또는 새 대화.
- 새 대화는 이전 문맥과 분리하며 dead end를 만들지 않는다.
- 모든 활성 화면의 navigation `정보·정책`은 동일한 정책 화면으로 연결된다. 정책 화면은 대화 아래에 늘 붙어 있는 영역이 아니라 별도 화면이며, 정책 화면의 `현재 대화`는 대화로 복귀하고 `새 대화`는 빈 대화를 시작한다. navigation은 현재 화면 항목을 `aria-current`로 표시한다.
- Provider 관련 실제 값은 Runtime과 Architecture가 소유한다. UI는 배포 구성을 그대로 표시하고 미정·불러오기 실패를 추정값으로 채우지 않는다.

## Voice and Tone

마이크로카피는 짧고 구체적인 한국어로 상태, 영향, 다음 행동의 순서로 안내한다. 브랜드(Cite, `모든 답에 근거 문서를`)와 시각 태도는 [`DESIGN.md`의 Project Application](DESIGN.md#project-application-cite)에 있다.

| 상황 | Do | Don't |
|---|---|---|
| 생성 시작 | `답변을 준비하고 있어요.` | `AI 추론 체인을 실행합니다.` |
| Streaming | `답변 생성 중 · 아직 완료되지 않았어요.` | 미완료 표시 없이 문장만 노출 |
| 완료 | `답변이 완료됐어요.` | `검증된 정답입니다.` |
| 실패 | `답변을 완료하지 못했어요. 다시 시도할 수 있습니다.` | `알 수 없는 오류` |
| Timeout | `120초 안에 완료하지 못했어요. 다시 시도해 주세요.` | `Gateway timeout` |
| 취소 | `생성을 중지했어요. 미완료 내용은 답변으로 남지 않습니다.` | `작업 종료` |
| 문맥 축소 | `대화가 길어져 오래된 대화 일부를 제외했어요.` | 조용히 이전 문맥 제거 |
| 에이전트 단계 | `Wiki 목록 확인` · `Wiki 검색 중` · `문서 읽는 중: <제목>` · `근거 판단 중` · `답변 작성 중` | 검색어·문서 본문·도구 인자 노출 |
| Wiki 근거 답변 | 답변 + `근거 문서 N` pill과 펼치는 목록 | `검증된 정답입니다.` |
| 부분 근거 | `Wiki에 없는 부분: <범위>` | 없는 부분을 일반 지식으로 채움 |
| Wiki 보충 대상 | `Wiki에서 근거를 찾지 못했어요. Wiki 보충 대상이에요.` | 일반 지식으로 답변 |
| Wiki 범위 밖 | `이 질문은 Wiki가 다루는 범위 밖이라 답할 수 없어요.` | 일반 지식으로 답변 |
| 검색 한도 | `Wiki 검색이 한도에서 끝났어요.` | 한도 도달을 숨김 |
| 인사·사용법 | 고정 사용법 안내 | 모델이 자유롭게 쓴 잡담 |

근거 문서가 논쟁 중이거나 신뢰도가 낮으면 그 사실을 글자로 표시한다. 내부 Prompt, Chain-of-Thought, Provider Trace와 Model Intermediate를 노출하지 않는다.

## Component Patterns

| Canonical ID | Component | Behavioral rules |
|---|---|---|
| `UX-APP-CANVAS` | App shell | navigation과 단일 대화 영역을 제공하고 임시 세션 성격을 유지한다. |
| `UX-NAV-SHEET` | Navigation | 새 대화를 시작한다. 작은 화면에서는 modal Sheet이며 닫으면 trigger로 focus가 돌아간다. |
| `UX-COMPOSER` | Composer | 빈·공백 입력을 차단한다. `Enter` 전송, `Shift+Enter` 줄바꿈이며 Prompt를 URL·GET에 넣지 않는다. 8줄까지 늘어난다. |
| `UX-PRIMARY-ACTION` | 주 행동 | 전송, 새 대화, 재시도 중 현재 하나만 강조한다. |
| `UX-USER-MESSAGE` | User message | 전송된 질문을 순서대로 표시하고 재시도에서 중복 생성하지 않는다. |
| `UX-ASSISTANT-RESPONSE` | Assistant response | Streaming과 completed를 구분하고 내부 추론 정보를 표시하지 않는다. 답변은 Markdown(문단·제목·목록·굵게·inline code·code block)으로 표시하되 HTML·링크·이미지는 만들지 않는다. Code block은 언어와 `복사`를 갖고, 생성 중 열린 fence에는 `복사`가 없다. 완료 답변 아래 `답변 복사`가 있다. |
| `UX-STATUS-ANNOUNCER` | 생성 상태 | 답변 위 한 줄(thinking line): 실행 중에는 현재 단계(없으면 Run State), 끝나면 `Wiki N단계 확인`. 펼치면 에이전트 단계 목록(고정 한국어 라벨), Run State·단계, 마지막 갱신 시각을 제공한다. 단계가 없는 완료 Run에는 표시하지 않는다. Token은 live region에서 제외한다. |
| `UX-STOP-ACTION` | 생성 중지 | queued·running에서만 사용 가능하며 완료·취소 경합은 Server 확정 상태를 따른다. |
| `UX-RUN-RECOVERY` | 실행 복구 | failed·timeout·cancelled의 영향, retryability, Correlation ID와 행동을 표시한다. |
| `UX-KNOWLEDGE-NOTICE` | Wiki 근거 안내 | 판정(`partial`·`wiki_gap`·`out_of_scope`)과 검색 한도 도달을 고정 문구로 알린다. `grounded`·`meta`에는 표시하지 않는다. |
| `UX-SOURCE-LIST` | 근거 문서 | `grounded`·`partial` 완료 답변에만 `근거 문서 N` pill로 표시하고, pill이 목록을 펼친다. 실제로 읽은 문서만, 처음 읽은 순서대로. 제목은 링크가 아니다. 실패·취소·부분 출력에는 표시하지 않는다. |
| `UX-FOCUS-RING` | Focus indicator | 모든 interactive control에서 keyboard focus를 색 이외의 2px 윤곽으로 표시한다. |

Composer는 실행 중 중복 전송을 막고 재활성 조건을 문구로 연결한다. 후속 질문에는 같은 세션에서 완료된 User·Assistant 대화 중 허용된 문맥만 사용한다. Server 한도를 넘으면 가장 오래된 완결 Turn부터 제외하고 문맥 축소를 알린다.

## State Patterns

### Run State

닫힌 집합은 `queued | running | completed | failed | timeout | cancelled`다.

| State | User treatment | Action |
|---|---|---|
| `queued` | `질문을 접수했어요.`와 접수 시각 | 중지 |
| `running` | thinking line에 현재 단계, 펼치면 단계 목록과 마지막 갱신 시각, 그리고 미완료 라벨을 표시한다. Wiki 탐색 중에는 답변 텍스트가 없고, 답변 작성 단계부터 Token이 메시지 영역에 표시된다. 전송 버튼 자리의 ■가 중지다. | 중지 |
| `completed` | 확정 Assistant 답변, 판정에 따른 근거 목록·안내와 완료 표시 | 후속 질문, 새 대화 |
| `failed` | 오류 카드에 한국어 오류 메시지, 영향, retryability, Correlation ID를 표시한다. 부분 출력이나 정적·축소 fallback 답변은 표시하지 않는다. | 재시도, 새 대화 |
| `timeout` | 120초 초과와 미완료 출력 폐기 | 재시도, 새 대화 |
| `cancelled` | 중지 확인. 이미 확정된 완료가 있으면 completed 우선 | 재시도, 새 대화 |

### Message State

- `draft`: Composer에만 있고 빈 값은 전송 불가.
- `sent`: User 메시지가 현재 대화에 확정됨.
- `streaming`: Assistant 출력이 도착 중이며 항상 미완료 안내 동반.
- `completed`: Terminal completed와 결속된 확정 Assistant 메시지.
- `discarded`: 실패·Timeout·취소 Run의 미완료 출력. 완료 메시지로 남기지 않음.

### Session and readiness

- 새 대화는 이전 Conversation 문맥과 분리된다. Conversation이 없다는 것은 전송을 막는 이유가 아니다 — 첫 전송이 Conversation을 만든 뒤 질문을 보낸다.
- Session은 최대 1시간이며 영구 저장·다른 기기 동기화를 제공하지 않는다. 만료 시 새 대화를 제안한다.
- Provider·에이전트·Wiki 미준비(모델 문맥 크기 부족 포함)는 이유와 `다시 확인`을 표시하고 전송을 막는다.
- Network Reconnect와 재시도는 같은 요청의 중복 완료 답변을 만들지 않는다.
- Wiki 검색은 자동이다. 검색을 켜고 끄는 control이나 Wiki 선택 control을 두지 않는다.

| Surface | Empty | Loading | Error / Timeout / Cancelled | Success |
|---|---|---|---|---|
| 새 대화 | Wiki 근거 안내·Composer | readiness 이유와 재확인 | Session 생성 실패와 새로고침 | 질문 전송 후 대화 이동 |
| 대화 | 첫 질문 예시와 Composer | 에이전트 단계·Streaming·중지 | 미완료 차단·복구 묶음 | completed 답변·근거·후속 질문 |
| 정보·정책 | Provider·전송 필드·1시간 Session·Wiki 이름 | Runtime 값 조회 중 | 추정값 금지, 영향과 다시 시도 | 전체 정책·대화 복귀 |

## Interaction Primitives

- Mouse, touch, keyboard로 같은 핵심 행동을 수행할 수 있어야 한다. 행동을 Hover 상태에만 노출하지 않는다.
- `Enter` 전송, `Shift+Enter` 줄바꿈이다. IME 조합 중 `Enter`는 전송하지 않는다.
- `Esc`는 navigation Sheet를 닫고 trigger로 focus를 복귀하며 실행 중지 단축키로 쓰지 않는다.
- 전송 후 User 메시지를 즉시 배치하고 2초 안에 접수 또는 생성 상태를 표시한다.
- Streaming 출력은 도착 순서대로 하나의 Assistant 메시지에 추가되며, 완료 전까지 미완료 상태를 유지한다.
- 완료 시 focus를 강제로 옮기지 않고 `완료된 답변으로 이동` skip link를 제공한다.
- 재시도는 동일 요청 의미를 유지하고 중복 클릭을 막는다. 새 대화는 현재 문맥을 분리한다.

## Accessibility Floor

- WCAG 2.2 AA를 목표로 한다.
- Keyboard만으로 새 대화, 작성·전송, 중지, 재시도와 후속 질문을 완료한다.
- 상태는 정확한 텍스트와 비색상 단서를 사용한다. Focus는 2px `#0071e3` 윤곽이다(`DESIGN.md` Project Application).
- `UX-STATUS-ANNOUNCER`는 `aria-live="polite"`로 Run 전환과 새 에이전트 단계를 각각 한 번만 atomic하게 공지한다. 같은 라벨이 반복되면 다시 공지하지 않는다. Token과 반복 tick은 밖에 둔다.
- 근거 목록과 문서 상태 라벨은 Keyboard로 도달할 수 있는 목록이며, 논쟁 중·신뢰도 낮음은 색이 아니라 글자로 전달한다.
- Streaming Token을 매번 낭독하지 않고 완료 후 이동 가능한 제목 또는 skip link를 제공한다.
- Navigation Sheet는 focus trap, 접근 가능한 닫기, trigger focus 복귀를 제공한다.
- 오류 묶음은 메시지, retryability, Correlation ID와 행동 순서이며 비활성 Composer는 이유를 `aria-describedby`로 연결한다.
- Icon-only button은 접근 가능한 이름을 갖고 중지·전송은 아이콘에만 의존하지 않는다.
- Reduced Motion에서는 shimmer, bounce, caret blink, sidebar 접힘과 transition을 정적으로 대체한다.
- 320 CSS px와 200% 확대에서 page-level 가로 scroll 없이 핵심 흐름을 완료한다.

## Key Flows

### UJ-1. 지민이 첫 질문을 한다

1. 새 대화에서 Wiki에 근거해 답한다는 안내를 본다. **Covers: FR-1, FR-7, FR-8, NFR-6.**
2. 한국어 질문을 입력하고 Enter로 전송한다. **Covers: FR-2, NFR-7.**
3. User 메시지와 2초 내 queued 또는 running 상태를 본다. **Covers: FR-3, FR-5, NFR-1.**
4. `Wiki 목록 확인` → `문서 읽는 중: <제목>` → `답변 작성 중` 단계가 차례로 보인다. **Covers: FR-5, FR-9.**
5. Streaming 출력이 하나의 미완료 Assistant 메시지에 순서대로 추가된다. **Covers: FR-3, FR-5.**
6. **Climax:** completed 확정 후 완성된 한국어 답변을 읽고, 그 아래 `근거 문서 N`을 펼쳐 근거 문서 목록을 본다. **Covers: FR-3, FR-10, NFR-2.**

### UJ-2. 지민이 후속 질문을 이어간다

1. 앞선 답변을 가리키는 후속 질문을 입력한다. **Covers: FR-2, FR-4.**
2. 시스템은 허용된 완료 Turn만 문맥으로 사용하고, 첫 질문과 같은 상태 계약에 따라 답한다. **Covers: FR-4, FR-5, NFR-10.**
3. 문맥 한도 초과 시 오래된 완결 Turn부터 제외하고 알린다. **Covers: FR-4.**
4. 새 대화를 열면 이전 문맥과 분리되고 저장 기록·계정을 암시하지 않는다. **Covers: FR-1, FR-4, NFR-5.**
5. **Climax:** 후속 질문에서는 문맥이 이어지고, 새 대화에서는 이전 문맥이 분리되는 것을 예상대로 확인한다. **Covers: FR-1, FR-4.**

### UJ-3. 지민이 실패에서 복구한다

1. 생성 중 `중지`를 선택한다. **Covers: FR-5, NFR-7.**
2. cancelled가 확정되고 미완료 출력은 답변으로 남지 않는다. **Covers: FR-5, NFR-2.**
3. Provider 실패나 120초 Timeout에서 한국어 오류, retryability, Correlation ID를 본다. **Covers: FR-6, NFR-2, NFR-9.**
4. 재시도해도 중복 완료 답변이 생기지 않는다. **Covers: FR-6, NFR-3.**
5. **Climax:** 새 completed 답변과 실패한 부분 출력을 혼동하지 않는다. **Covers: FR-3, FR-6.**

### UJ-4. 지민이 Wiki에 없는 내용을 묻는다 (PRD UJ-2)

1. Wiki가 다루지 않는 주제를 묻는다. **Covers: FR-2.**
2. `Wiki 검색 중`, `근거 판단 중` 단계가 보이고 답변 작성 단계는 오지 않는다. **Covers: FR-5, FR-9.**
3. **Climax:** 일반 지식 답변 대신 `Wiki 보충 대상` 또는 `Wiki 범위 밖` 고정 안내를 본다. 근거 목록은 없다. **Covers: FR-8.**
4. 질문 일부만 Wiki에 있으면 그 부분만 답하고 `Wiki에 없는 부분`을 따로 본다. **Covers: FR-8, FR-10.**

### Requirement coverage

| Requirement | Projection |
|---|---|
| FR-1–FR-2 | 새 대화, Composer, UJ-1·UJ-2 |
| FR-3–FR-4 | Message State, Session 문맥·격리, UJ-1·UJ-2 |
| FR-5–FR-6 | Run State, 중지·복구, UJ-3 |
| FR-7 | 로컬 반응형 Web, 정보·정책 |
| FR-8 | Wiki 근거 안내, 고정 문구, UJ-4 |
| FR-9–FR-10 | 에이전트 단계, 근거 목록, 문서 상태 라벨 |
| FR-11 | `DESIGN.md` Project Application (Cite) 전체 |
| NFR-1–NFR-3 | 2초 상태, 120초 Timeout, idempotent 재시도 |
| NFR-4–NFR-6 | Secret·본문 비노출, 임시 Session, Provider 정책 고지 |
| NFR-7–NFR-8 | Keyboard, 비색상 상태, 320px·200%·Reduced Motion |
| NFR-9–NFR-12 | content-free 관측, 에이전트·Provider 교체에 안정적인 계약, Wiki 루트 밖 접근 차단 |

## Inspiration & Anti-patterns

- ChatGPT의 구조(접히는 sidebar, 중앙 pill Composer, 오른쪽 질문 bubble과 bubble 없는 답변, thinking line, 답변 아래 출처 pill)와 후속 질문 연속성을 차용한다.
- 768px 읽기 폭, 하단에 고정되는 Composer dock과 명시적인 생성·Streaming·복구 상태를 유지한다.
- 복잡한 dashboard, reasoning trace·검색 과정 원문 노출, Wiki 근거가 없는 답변을 Wiki 답변처럼 표현하는 방식, 계정·조직·저장 기록 암시를 거부한다.

## Responsive & Platform

| Width | Behavior |
|---|---|
| `≥ 1024px` | 260px sidebar(52px icon rail로 접힘) + 중앙 단일 대화 열 |
| `768–1023px` | 접힌 navigation과 menu trigger, 단일 대화 열 |
| `< 768px` | top bar + modal navigation Sheet, 세로 메시지·상태·복구 행동 |

Acceptance viewport는 1440×1000, 1024px, 768px, 320 CSS px와 200% zoom이다. 네이티브 앱, Offline Mode, Cross-device Sync와 공개 배포는 범위 밖이다. 호스트 실행과 로컬 Container는 같은 행동 계약을 사용하고 준비 전 상태는 readiness 안내로 표현한다.

## Data Boundary and Trust

- 로컬 개인용이며 계정, RBAC 또는 지속형 Workspace를 암시하지 않는다.
- 사용자는 Provider·Model Revision, 전송 필드(질문, 선택된 이전 대화, 읽은 Wiki 발췌), 1시간 Session과 연결된 Wiki 이름을 정보·정책 surface에서 확인한다. 실제 값은 Runtime·Architecture가 소유하며 UI는 추정값을 표시하지 않는다.
- 에이전트는 Wiki를 읽기만 한다. UI에는 Wiki를 수정·저장하는 행동이 없다.
- Provider Secret은 Client·응답·화면에 노출하지 않는다. 운영 관측 정보는 Correlation ID, Run 단계, 단계 수, 도구 이름, 지연, 결과 상태와 오류 유형으로 제한한다.
- Prompt, 답변 본문, 검색어, Wiki 문서 본문, Provider Raw Response, Model Intermediate와 Chain-of-Thought를 기록·노출하지 않는다.
- Session은 최대 1시간이고 대화를 영구 저장하지 않으며 응답과 Streaming에는 `no-store`를 적용한다.
- 에이전트나 Provider를 교체해도 질문·상태·단계·근거·답변·오류 계약을 유지한다.
