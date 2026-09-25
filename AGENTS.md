<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

## 프론트엔드

UI를 설계·구현·수정·리팩터링·리뷰·스타일링하기 전에 **반드시 [`docs/DESIGN.md`](docs/DESIGN.md)를 읽는다.**
그 문서가 시각 토큰과 컴포넌트 표현의 정본이고, 구현과 어긋나면 문서가 이긴다. "간단한 수정이라서"
건너뛰지 않는다. 행동·상태·접근성 계약은 [`docs/EXPERIENCE.md`](docs/EXPERIENCE.md)가 소유한다.

**프론트 스택은 빌드 스텝 없는 vanilla 3파일이다** — `src/aidd_chat/web/{index.html,styles.css,app.js}`를
FastAPI가 `/static`으로 그대로 서빙한다. 프레임워크·번들러·CSS 라이브러리를 새로 들이지 않는다.
유일한 추가 자산은 자체 호스팅 글꼴 `web/fonts/PretendardVariable.woff2`(SIL OFL 1.1, `fonts/OFL.txt`)다.

- **네이티브 엘리먼트를 먼저 쓴다.** navigation Sheet는 `<dialog>` 하나가 세 breakpoint를 전부
  처리하고 `showModal()`이 inert 배경·포커스 트랩·Esc를 공짜로 준다. 같은 역할을 JS로 새로 만들지 않는다.
- **색 리터럴은 `styles.css`의 `:root` 블록에만 둔다.** 그 밖에서는 `var(--*)`만 쓴다.
  font-size·padding·margin·gap도 전부 토큰을 읽는다. 선언만 하고 안 쓰는 토큰도 실패다.
- **`data-ux="UX-*"` 12개는 markup·CSS·browser 테스트가 공유하는 조인 키다.** 이름을 바꾸거나
  13번째를 추가하려면 `tests/test_story_1_10.py`·`test_story_1_10_browser.py`를 같은 커밋에서 바꾼다.
- 상호작용 색은 `--primary` 하나다. 두 번째 액센트를 만들지 않는다. 검정 버튼을 쓰지 않는다.
- 버튼 문법은 파란 pill(주 행동)과 경계 pill(보조) 둘뿐이다. 주 행동은 화면당 하나다.
- 그림자는 navigation Sheet 하나에만 있다. 위계가 필요하면 크롬 대신 면의 색을 바꾼다.
- **이모지를 UI 요소로 쓰지 않는다.** 아이콘이 필요하면 인라인 SVG에 `aria-hidden`을 준다.
- hover·pressed·focus-visible·disabled·loading·error·empty를 함께 설계한다.
  **로딩 실패나 값 부재를 `0`으로 위장하지 않는다.**
- 상태 문구는 `docs/EXPERIENCE.md`의 정본을 따른다. Run State는 정확한 한국어 의미와 함께 표시하고,
  실패·Timeout·취소 시 남은 미완료 문장을 완료된 답변으로 오인되게 두지 않는다.
- 서버가 마스킹한 값을 프론트에서 복원하지 않는다.

### 프론트엔드 완료 체크리스트

- [ ] `docs/DESIGN.md`를 읽고 토큰·반경·굵기 사다리를 따랐는가? 추측한 값이 없는가?
- [ ] `:root` 밖에 색 리터럴이 없고, font-size·spacing이 전부 토큰인가?
- [ ] 화면에 보이는 상호작용 색이 `--primary` 하나인가? 주 행동이 화면당 하나인가?
- [ ] hover·pressed·focus·disabled·loading·error·empty를 모두 처리했는가?
- [ ] 320px부터 데스크톱까지 가로 스크롤 없이 재배치되고 키보드로 완주 가능한가?
- [ ] 포커스 표시를 제거하지 않았는가? 아이콘 전용 버튼에 접근 가능한 이름을 주었는가?
- [ ] `prefers-reduced-motion`에서 애니메이션이 제거가 아니라 정지 프레임으로 대체되는가?
- [ ] `uv run pytest tests/test_story_1_10.py` 를 실제로 돌리고 출력을 근거로 보고했는가?
