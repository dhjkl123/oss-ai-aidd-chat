const status = document.querySelector("#conversation-status");
const controls = [...document.querySelectorAll("[data-new-conversation]")];
const prompt = document.querySelector("#prompt");
const sendButton = document.querySelector("#send-question");
const stopButton = document.querySelector("[data-stop-action]");
const recovery = document.querySelector("[data-run-recovery]");
const recoveryMessage = document.querySelector("[data-recovery-message]");
const retryButton = document.querySelector("[data-retry-run]");
const recoveryState = document.querySelector("[data-recovery-state]");
const recoveryImpact = document.querySelector("[data-recovery-impact]");
const recoveryKind = document.querySelector("[data-recovery-kind]");
const recoveryRetryable = document.querySelector("[data-recovery-retryable]");
const recoveryCorrelation = document.querySelector("[data-recovery-correlation]");
const retryReason = document.querySelector("[data-retry-reason]");
const transcript = document.querySelector("#transcript");
const runSummary = document.querySelector("#run-summary");
const runState = document.querySelector("[data-run-state]");
const runStage = document.querySelector("[data-run-stage]");
const runUpdated = document.querySelector("[data-run-updated]");
const announcer = document.querySelector("#run-announcer");
const contextNotice = document.querySelector("#context-notice");
const composerReason = document.querySelector("[data-composer-reason]");
const navSheet = document.querySelector("[data-ux='UX-NAV-SHEET']");
const sheetTrigger = document.querySelector("[data-sheet-trigger]");
const sheetClose = document.querySelector("[data-sheet-close]");
const mainContent = document.querySelector("#main-content");
const heroAction = document.querySelector(".hero-action");
const policyStatus = document.querySelector("#policy-status");
const policyImpact = document.querySelector("[data-policy-impact]");
const policyRetry = document.querySelector("[data-policy-retry]");
const policySuccess = document.querySelector("[data-policy-success]");
const policyFields = [...document.querySelectorAll("[data-policy-field]")];

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RFC3339_UTC = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/;
const TERMINAL_STATES = new Set(["completed", "failed", "timeout", "cancelled"]);
const ACTIVE_STATES = new Set(["queued", "running"]);
// UX-RUN-RECOVERY covers every terminal a user can act on. `completed` is not one.
const RECOVERABLE_STATES = new Set(["failed", "timeout", "cancelled"]);
// The only server error codes this client will repeat to the user. An
// unrecognized one gets our own sentence, never the server's text. Whether a
// refusal is permanent comes from the envelope's own `retryable`, not from a
// second list here that could drift away from it.
// Every load-shed refusal. They share one shape and one meaning: the request was
// turned away before it changed anything, so the conversation itself is still
// intact and the user may simply send the same thing again in a moment.
const CAPACITY_ERROR_CODES = new Set([
  "conversation_capacity_exceeded", "run_capacity_exceeded", "session_run_capacity_exceeded",
  "queue_capacity_exceeded", "provider_busy", "spend_limit_reached", "rate_limited",
  "stream_capacity_exceeded", "deployment_unconfigured",
]);
const RETRY_ERROR_CODES = new Set([
  "retry_exhausted", "retry_not_allowed", "context_too_large", "conversation_expired",
  "idempotency_conflict", "invalid_origin", "invalid_idempotency_key",
  "run_already_active", "provider_unavailable", "turn_limit_reached",
  // The Provider was rebound under a Run that was otherwise retryable. Shown, but
  // deliberately NOT in RETRY_FAIL_CLOSED_CODES below: only the Provider moved, so
  // the next question in this conversation is still possible.
  "provider_profile_changed",
  ...CAPACITY_ERROR_CODES,
]);
// Which refusals this client will repeat verbatim on the submit path. Membership
// decides whether the server's sentence may be shown -- never whether the refusal
// is permanent. That comes from the envelope's own `retryable`, like everywhere
// else in this file.
const SUBMIT_ERROR_CODES = new Set(["turn_limit_reached", ...CAPACITY_ERROR_CODES]);
// Refusals that mean this conversation cannot carry the *next* question either,
// so the composer closes exactly as an ambiguous submit closes it.
const RETRY_FAIL_CLOSED_CODES = new Set([
  "conversation_expired", "idempotency_conflict", "invalid_origin", "invalid_idempotency_key",
]);
const EXPIRED_NOTICE = "대화 세션이 만료되었습니다. 이전 대화 내용은 더 이상 볼 수 없어요. 새 대화를 시작해 주세요.";
const DISCARDED_NOTICE = "생성 중이던 내용은 답변으로 남지 않습니다.";
const RECOVERY_IMPACT = {
  failed: DISCARDED_NOTICE,
  timeout: DISCARDED_NOTICE,
  cancelled: "미완료 내용은 답변으로 남지 않습니다.",
};
const STATE_LABELS = {
  queued: "답변 대기 중", running: "답변 생성 중", completed: "답변 완료",
  failed: "답변 생성 실패", timeout: "답변 시간 초과", cancelled: "답변 취소됨",
};
const STAGE_LABELS = {
  queued: "대기", preparing: "준비 중", streaming: "생성 중",
  finalizing: "마무리 중", terminal: "완료",
};
const PROVIDER_FAILURES = new Set([
  "provider_auth", "provider_rate_limit", "provider_unavailable", "provider_transport",
  "provider_timeout", "provider_cancelled", "provider_non_text", "provider_empty",
  "provider_incomplete", "provider_suspended", "provider_interrupted",
  "provider_invalid_response", "provider_content_filtered", "provider_unknown",
  "capacity_exceeded",
]);
const EVENT_TYPES = ["run.status", "message.delta", "message.completed", "message.discarded", "run.error", "context.truncated", "conversation.expired", "stream.end"];
const RUN_STAGES = {
  queued: ["queued"], running: ["preparing", "streaming", "finalizing"],
  completed: ["terminal"], failed: ["terminal"], timeout: ["terminal"], cancelled: ["terminal"],
};
const POST_TIMEOUT_MS = 2_000;
const CANCEL_SETTLE_MS = 5_000;
// The exact sentence the acceptance criteria require, announced once. It carries
// the load-bearing half -- that the incomplete text is not kept as an answer.
const CANCEL_NOTICE = "생성을 중지했어요. 미완료 내용은 답변으로 남지 않습니다.";
const POLL_TIMEOUT_MS = 2_000;
const MAX_POLL_RETRIES = 3;

let conversationId = null;
let generation = 0;
let policyReady = false;
let run = null;
let pendingSubmit = null;
let pendingRetry = null;
let newConversationController = null;
let conversationSwitching = false;
let pageHidden = false;
let conversationFailedClosed = false;
// The composer was disabled under the user focus and nothing has taken it yet.
let composerLostFocus = false;
let reasonTimer = null;
const policyRequests = new Set();
let requestGeneration = 0;

function exactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("|") === [...keys].sort().join("|");
}

function visibleText(value) {
  // Mirrors contracts.has_visible_text: printable and not whitespace.
  // \p{C} covers Cc/Cf/Cs/Co/Cn (Python str.isprintable() == False), \p{Z} the separators.
  return rawChunk(value) && [...value].some((character) => !/[\p{C}\p{Z}]/u.test(character));
}

function rawChunk(value) {
  return typeof value === "string" && value.length > 0
    && ![...value].some((character) => {
      const point = character.codePointAt(0);
      return point >= 0xd800 && point <= 0xdfff;
    });
}

function validTimestamp(value) {
  return typeof value === "string" && RFC3339_UTC.test(value) && Number.isFinite(Date.parse(value));
}

// Why the composer is closed, in the same order updateComposer decides it.
// `#composer-reason` is a polite Live Region as well as the aria-describedby target
// of both controls: a disabled control is not focusable, so its description alone
// would never be spoken.
function disabledReason() {
  if (!policyReady) return "서비스 준비 상태와 정책을 확인하기 전에는 질문을 보낼 수 없습니다.";
  // Switching first: a creation already in flight is the more useful thing to say
  // than either "start a conversation" or a verdict on the one being replaced.
  if (conversationSwitching) return "새 대화를 만드는 중에는 질문을 보낼 수 없습니다.";
  if (!conversationId) return "새 대화를 시작하면 질문을 보낼 수 있습니다.";
  if (conversationFailedClosed) return "이 대화는 더 이상 사용할 수 없습니다. 새 대화를 시작해 주세요.";
  if (pendingSubmit || pendingRetry) return "이전 요청을 보내는 중에는 질문을 보낼 수 없습니다.";
  // Terminal but not yet reconciled: the answer is on screen and nothing is being
  // generated, so saying "wait for generation to finish" would simply be false.
  if (run && TERMINAL_STATES.has(run.state)) return "실행 결과를 확인하는 중입니다. 잠시 후 다시 질문할 수 있습니다.";
  return "답변 생성이 끝나면 다시 질문할 수 있습니다.";
}

// Three polite regions can fire on one transition, and a single submit walks the
// reason through several sentences. Coalescing to the settled one keeps the burst
// down to something a listener can follow.
function announceReason(text) {
  if (composerReason.dataset.settled === text) return;
  composerReason.dataset.settled = text;
  clearTimeout(reasonTimer);
  reasonTimer = setTimeout(() => {
    if (composerReason.textContent !== text) composerReason.textContent = text;
  }, 150);
}

// UX-PRIMARY-ACTION is a role, not an element: exactly one control on screen may
// carry it, and it is whichever single action this screen is asking for.
function setPrimaryAction() {
  const primary = !recovery.hidden ? retryButton : (conversationId ? sendButton : heroAction);
  for (const control of [heroAction, sendButton, retryButton]) {
    if (control === primary) control.dataset.ux = "UX-PRIMARY-ACTION";
    else delete control.dataset.ux;
  }
}

function updateComposer() {
  setPrimaryAction();
  const enabled = policyReady && conversationId && !conversationSwitching && !conversationFailedClosed
    && !pendingSubmit && !pendingRetry
    && (!run || (TERMINAL_STATES.has(run.state) && run.reconciled));
  announceReason(enabled ? "" : disabledReason());
  // Either composer control can be the one holding focus when the composer closes.
  const closingOnFocus = document.activeElement === prompt || document.activeElement === sendButton;
  prompt.disabled = !enabled;
  sendButton.disabled = !enabled || !visibleText(prompt.value);
  // Stop is offered only while the Run can still be stopped -- queued or running --
  // and never twice for the same Run. Deliberately not bound to Esc.
  const stoppable = Boolean(run) && run.generation === generation && ACTIVE_STATES.has(run.state);
  stopButton.hidden = !stoppable;
  stopButton.disabled = !stoppable || Boolean(run && run.cancelPending);
  if (enabled) composerLostFocus = false;
  else if (closingOnFocus) composerLostFocus = true;
  // Disabling the element that holds focus drops focus on <body>. Hand it to the
  // control that replaced it -- but only once that control exists and can actually
  // take focus, and never by scrolling the page out from under the user.
  if (composerLostFocus && document.activeElement === document.body) {
    const successor = !stopButton.hidden && !stopButton.disabled ? stopButton
      : (!recovery.hidden && !retryButton.disabled ? retryButton : null);
    if (successor) {
      composerLostFocus = false;
      successor.focus({ preventScroll: true });
    }
  }
  // The stop button held focus and is now hidden; hand focus to the composer the
  // moment the composer is usable -- but never through an inert background, which
  // is what the composer is while the Sheet is modal. The Sheet close handler calls
  // back here, so the handoff happens the instant it goes away.
  if (run && run.focusPrompt && !prompt.disabled && !sheetIsModal()) {
    run.focusPrompt = false;
    prompt.focus({ preventScroll: true });
  }
}

// The one description of a recoverable terminal, used by both the visible panel and
// the announcer, so the two can never drift apart. A cancelled Run has no error, so
// its rows say so rather than borrowing a Provider failure it never had.
function recoveryDetail(state, failure) {
  return {
    message: state === "cancelled"
      ? CANCEL_NOTICE : (failure ? failure.message : "답변을 완료하지 못했습니다."),
    state: STATE_LABELS[state] || state,
    impact: RECOVERY_IMPACT[state] || "",
    // "사용자 중지" belongs to a Cancel, not to any Run that happens to
    // arrive without a failure attached; a failed Run with no error is unknown, not
    // user-initiated.
    kind: failure ? failure.kind : (state === "cancelled" ? "사용자 중지" : "확인되지 않음"),
    retryable: !failure || failure.retryable
      ? "다시 시도할 수 있어요" : "다시 시도해도 같은 결과일 수 있어요",
    correlation: failure ? failure.correlation_id : (state === "cancelled" ? "없음" : "확인되지 않음"),
  };
}

function announceState(state) {
  if (!run || run.announced.has(state)) return;
  run.announced.add(state);
  // #run-announcer is the only live region a run transition writes to; the
  // recovery panel below is deliberately not one, so a stop announces once.
  if (!RECOVERABLE_STATES.has(state)) {
    announcer.textContent = STATE_LABELS[state] || state;
    return;
  }
  // failed·timeout·cancelled: the panel rows reach a screen reader too, once per
  // transition, in the same 상태 · 영향 · 종류 · retryability order. The Correlation
  // ID is deliberately left out of the spoken burst and kept in the panel, where it
  // stays reachable as text and can be copied -- reading a 36-character UUID aloud
  // on every terminal is how a polite Live Region gets switched off.
  const detail = recoveryDetail(state, run.terminalError);
  announcer.textContent = [
    detail.message,
    detail.state && `상태 ${detail.state}`,
    detail.impact && `영향 ${detail.impact}`,
    detail.kind && `오류 종류 ${detail.kind}`,
    detail.retryable && `재시도 ${detail.retryable}`,
  ].filter(Boolean).join(" · ");
}

// Read as a time, never shown as an opaque timestamp.
function formatTime(value) {
  const moment = value ? new Date(value) : null;
  return moment && Number.isFinite(moment.getTime())
    ? moment.toLocaleTimeString("ko-KR") : "확인되지 않음";
}

function renderRun(state, stage) {
  if (!run) return;
  run.state = state;
  run.stage = stage;
  runSummary.hidden = false;
  runState.textContent = STATE_LABELS[state] || state;
  runStage.textContent = STAGE_LABELS[stage] || stage;
  runUpdated.textContent = formatTime(run.updatedAt);
  announceState(state);
  if (RECOVERABLE_STATES.has(state)) renderRecovery(state, run.terminalError);
  updateComposer();
}

// 상태 · 영향 · 오류 종류 · retryability · Correlation ID, in that order, then the
// two controls.
function renderRecovery(state, failure) {
  const detail = recoveryDetail(state, failure);
  recoveryMessage.textContent = detail.message;
  recoveryState.textContent = detail.state;
  recoveryImpact.textContent = detail.impact;
  recoveryKind.textContent = detail.kind;
  recoveryRetryable.textContent = detail.retryable;
  recoveryCorrelation.textContent = detail.correlation;
  // The control and its handler ask the same question, so a live-looking 재시도
  // never turns out to do nothing -- and a lockout the server handed down survives
  // every later re-render of the same terminal.
  retryButton.disabled = !canRetry();
  retryReason.textContent = (run && run.retryBlockedReason) || "";
  recovery.hidden = false;
}

function canRetry() {
  return Boolean(run) && run.generation === generation && RECOVERABLE_STATES.has(run.state)
    && run.reconciled && !run.retryBlocked && !pendingSubmit && !pendingRetry
    && !conversationFailedClosed;
}

function blockRetry(target, reason) {
  target.retryBlocked = true;
  target.retryBlockedReason = reason;
  if (target === run) {
    retryButton.disabled = true;
    retryReason.textContent = reason;
  }
}

function disposeRun() {
  if (!run) return;
  if (run.source) run.source.close();
  if (run.pollTimer) clearTimeout(run.pollTimer);
  if (run.cancelTimer) clearTimeout(run.cancelTimer);
  run.cancelTimer = null;
  if (run.pollController) run.pollController.abort();
  run.source = null;
  run.pollTimer = null;
  run.pollController = null;
  run.polling = false;
}

// Role and position, on the node that just joined the transcript. Removals only
// ever take the last turn off the end (a Cancel drops both of its bubbles), so what
// is left is always a correct prefix and labelling on append keeps the numbering
// honest without re-walking the list.
function labelMessage(article) {
  const position = transcript.children.length;
  article.setAttribute("aria-label", article.dataset.ux === "UX-USER-MESSAGE"
    ? `${position}번째 메시지, 내 질문` : `${position}번째 메시지, AIDD Chat 답변`);
}

function newMessage(role, text, incomplete = false) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  article.dataset.ux = role === "user" ? "UX-USER-MESSAGE" : "UX-ASSISTANT-RESPONSE";
  const label = document.createElement("strong");
  label.textContent = role === "user" ? "나" : "AIDD Chat";
  const content = document.createElement("p");
  content.textContent = text;
  article.append(label, content);
  let note = null;
  if (incomplete) {
    note = document.createElement("small");
    note.className = "incomplete";
    note.textContent = "생성 중인 답변입니다.";
    article.append(note);
  }
  transcript.append(article);
  labelMessage(article);
  return { article, content, note };
}

function validConversation(value) {
  return exactKeys(value, ["conversation_id", "created_at", "expires_at"])
    && UUID_V4.test(value.conversation_id) && validTimestamp(value.created_at) && validTimestamp(value.expires_at);
}

async function createConversation() {
  const request = new AbortController();
  if (newConversationController) newConversationController.abort();
  newConversationController = request;
  conversationSwitching = true;
  for (const control of controls) control.disabled = true;
  updateComposer();
  status.textContent = "새 대화를 만드는 중입니다.";
  const timeout = setTimeout(() => request.abort(), 3_000);
  try {
    const response = await fetch("/api/v1/conversations", {
      method: "POST", credentials: "same-origin", cache: "no-store", signal: request.signal,
    });
    if (!response.ok) {
      // A refused creation is a ceiling, not a broken client: say what the server
      // said rather than the generic failure sentence below it.
      let payload = null;
      if (response.status === 429 || response.status === 503) {
        try { payload = await response.json(); } catch { payload = null; }
      }
      throw Object.assign(new Error("create failed"), {
        notice: payload && validErrorEnvelope(payload, CAPACITY_ERROR_CODES) ? payload.error.message : null,
      });
    }
    const created = await response.json();
    if (!validConversation(created)) throw new Error("invalid conversation");
    generation += 1;
    disposeRun();
    if (pendingRetry && pendingRetry.retryController) pendingRetry.retryController.abort();
    pendingRetry = null;
    run = null;
    pendingSubmit = null;
    conversationId = created.conversation_id;
    conversationFailedClosed = false;
    recovery.hidden = true;
    transcript.replaceChildren();
    runSummary.hidden = true;
    contextNotice.hidden = true;
    status.textContent = "빈 새 대화를 만들었습니다. 1시간 동안 유지됩니다.";
  } catch (error) {
    if (request === newConversationController) {
      if (error.notice) status.textContent = error.notice;
      else status.textContent = error.name === "AbortError"
        ? "새 대화를 만들지 못했습니다. 시간이 초과되어 요청을 중단했습니다. 다시 시도해 주세요."
        : "새 대화를 만들지 못했습니다. 기존 대화를 유지합니다. 다시 시도해 주세요.";
    }
  } finally {
    clearTimeout(timeout);
    if (request === newConversationController) newConversationController = null;
    if (!newConversationController) conversationSwitching = false;
    for (const control of controls) control.disabled = false;
    updateComposer();
  }
}

for (const control of controls) control.addEventListener("click", createConversation);

// UX-NAV-SHEET is one native <dialog> in three modes, named in `data-mode` so the
// stylesheet and the tests can see which one is live:
//   column -- >= 1024px, open and non-modal, laid out as the 256px navigation and
//             exposed as a complementary landmark rather than as a dialog that
//             never closes.
//   panel  -- 768-1023px, folded behind the trigger; a modal side sheet.
//   sheet  -- < 768px, the named modal Sheet at full width.
// Both folded modes are modal on purpose: a non-modal overlay would sit on top of
// the composer while leaving it tabbable, which is exactly the failure this story
// exists to close. `showModal()` is then the whole implementation of inert
// background, focus containment and Esc -- a hand-rolled trap is the commonest
// accessibility defect in this kind of UI.
// The markup ships the dialog `open`, so with no JavaScript -- or no <dialog> at
// all -- the navigation is still a plain block in the flow.
// ponytail: folding it costs one frame of layout below 1024px, because a CSP of
// `script-src 'self'` rules out the inline script that would beat first paint.
const dialogReady = typeof navSheet.showModal === "function" && typeof navSheet.show === "function";
const desktopViewport = window.matchMedia("(min-width: 1024px)");
const narrowViewport = window.matchMedia("(max-width: 767px)");
// Where focus goes when the Sheet closes: the trigger that opened it, or the
// destination the user picked inside it.
let sheetReturn = null;

function sheetMode() {
  if (desktopViewport.matches) return "column";
  return narrowViewport.matches ? "sheet" : "panel";
}

function rendered(node) {
  return Boolean(node) && node.isConnected
    && (node.checkVisibility ? node.checkVisibility() : node.offsetParent !== null);
}

// `:modal` is not universally supported and throws where it is not -- and this is
// asked from updateComposer, which every state path funnels through. `data-mode` is
// maintained anyway and answers the same question without a selector call.
function sheetIsModal() {
  return navSheet.open && (navSheet.dataset.mode === "panel" || navSheet.dataset.mode === "sheet");
}

function syncSheet() {
  const mode = sheetMode();
  if (navSheet.dataset.mode === mode) return;
  const wasModal = sheetIsModal();
  const active = document.activeElement;
  const held = navSheet.contains(active) ? active : null;
  navSheet.dataset.mode = mode;
  // panel <-> sheet is a width change on the same modal Sheet: leave it open, so a
  // resize in the middle of using the menu does not take the menu away.
  if (mode !== "column" && wasModal) return;
  sheetReturn = null;
  if (navSheet.open && (wasModal || mode !== "column")) navSheet.close();
  // Set here as well as in the `close` handler: promoting an open panel to the
  // column restyles rather than closing, so no `close` event ever fires.
  sheetTrigger.setAttribute("aria-expanded", "false");
  if (mode === "column") {
    navSheet.setAttribute("role", "complementary");
    if (!navSheet.open) {
      navSheet.show();
      // show() runs the dialog focusing steps; put focus back where it was.
      if (active && active !== document.body) active.focus({ preventScroll: true });
    }
    return;
  }
  navSheet.removeAttribute("role");
  // Folding it under the caret would drop focus on <body>; the trigger is where the
  // navigation now lives, so that is where the user goes.
  if (held && rendered(sheetTrigger)) sheetTrigger.focus({ preventScroll: true });
}

function openSheet() {
  if (navSheet.open) return;
  sheetReturn = sheetTrigger;
  navSheet.dataset.mode = sheetMode();
  navSheet.removeAttribute("role");
  navSheet.showModal();
  sheetTrigger.setAttribute("aria-expanded", "true");
}

// Only the folded Sheet can be dismissed. In column mode the navigation is
// permanent chrome, and closing it would hide the navigation and its trigger both.
function closeSheet() {
  if (sheetIsModal()) navSheet.close();
}

if (dialogReady) {
  desktopViewport.addEventListener("change", syncSheet);
  narrowViewport.addEventListener("change", syncSheet);
  sheetTrigger.addEventListener("click", openSheet);
  sheetClose.addEventListener("click", closeSheet);
  navSheet.addEventListener("click", (event) => {
    if (navSheet.dataset.mode === "column") return;
    // A click that lands on the dialog itself outside its box is a backdrop click.
    if (event.target === navSheet) {
      const box = navSheet.getBoundingClientRect();
      const outside = event.clientX < box.left || event.clientX > box.right
        || event.clientY < box.top || event.clientY > box.bottom;
      if (outside) closeSheet();
      return;
    }
    // A destination chosen inside the folded navigation has to be where focus
    // lands. Returning focus to the menu button would send the user back to the
    // list they just made a choice in.
    const control = event.target.closest("a[href^='#'], button:not([data-sheet-close])");
    if (!control) return;
    let target = null;
    if (control.matches("a")) {
      // An unvalidated fragment ("#", "#2") is a SyntaxError that would strand
      // focus behind a Sheet that never closed.
      try { target = document.querySelector(control.getAttribute("href")); } catch { target = null; }
    }
    sheetReturn = target || mainContent;
    navSheet.close();
  });
  navSheet.addEventListener("close", () => {
    sheetTrigger.setAttribute("aria-expanded", "false");
    const target = sheetReturn;
    sheetReturn = null;
    if (rendered(target)) target.focus({ preventScroll: true });
    // The composer handoff is suppressed while the Sheet is modal; now that it is
    // not, let it run instead of yanking focus a beat later.
    updateComposer();
  });
  syncSheet();
} else {
  // No <dialog>: the navigation stays the in-flow block the markup shipped, and the
  // two controls that would drive a Sheet are not offered at all.
  sheetTrigger.hidden = true;
  sheetClose.hidden = true;
}

prompt.addEventListener("input", updateComposer);
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!sendButton.disabled) void submitQuestion();
  }
});
sendButton.addEventListener("click", () => void submitQuestion());

async function submitQuestion() {
  const content = prompt.value;
  if (!conversationId || !visibleText(content) || pendingSubmit || (run && !TERMINAL_STATES.has(run.state))) return;
  const body = JSON.stringify({ kind: "question", content });
  const submit = { content, body, key: crypto.randomUUID(), generation, attempts: 0, controller: null };
  pendingSubmit = submit;
  prompt.value = "";
  submit.userMessage = newMessage("user", content);
  updateComposer();
  await postQuestion(submit);
}

async function postQuestion(submit) {
  if (submit !== pendingSubmit || submit.generation !== generation || submit.attempts >= 2) return;
  submit.attempts += 1;
  const controller = new AbortController();
  submit.controller = controller;
  const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/v1/conversations/${conversationId}/runs`, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "Idempotency-Key": submit.key },
      body: submit.body, signal: controller.signal,
    });
    if (!response.ok) {
      if (response.status === 413) {
        let payload;
        try { payload = await response.json(); } catch { payload = null; }
        if (payload && validContextTooLargeEnvelope(payload)) {
          recoverableRejectSubmit(submit, payload.error.message);
          return;
        }
        // Any other 413 (e.g. a proxy body-size limit) is ambiguous, not the
        // server's own recoverable context_too_large -- fail closed like every
        // other unrecognized 4xx below.
      }
      if ([409, 429, 503].includes(response.status)) {
        let payload;
        try { payload = await response.json(); } catch { payload = null; }
        if (payload && validErrorEnvelope(payload, SUBMIT_ERROR_CODES)) {
          // The server already said whether waiting can change this. A load-shed
          // refusal (retryable) created nothing, so the question comes back to the
          // composer; a ceiling this conversation can never get under closes it.
          if (payload.error.retryable) recoverableRejectSubmit(submit, payload.error.message);
          else failClosedSubmit(submit, payload.error.message);
          return;
        }
      }
      if (response.status >= 400 && response.status < 500) {
        failClosedSubmit(submit, "질문을 접수하지 못했습니다. 요청을 확인하고 새 대화를 시작해 주세요.");
        return;
      }
      throw new Error("ambiguous submit failure");
    }
    let projection;
    try { projection = await response.json(); } catch {
      failClosedSubmit(submit, "질문 접수 응답을 확인할 수 없습니다. 새 대화를 시작해 주세요.");
      return;
    }
    if (submit !== pendingSubmit || submit.generation !== generation) return;
    if (!validProjection(projection)) {
      failClosedSubmit(submit, "질문 접수 응답을 확인할 수 없습니다. 새 대화를 시작해 주세요.");
      return;
    }
    pendingSubmit = null;
    startRun(projection, submit.content, submit.userMessage);
  } catch {
    if (submit !== pendingSubmit || submit.generation !== generation) return;
    if (pageHidden) return;
    if (submit.attempts < 2) await postQuestion(submit);
    else {
      failClosedSubmit(submit, "질문 접수 여부를 확인할 수 없습니다. 새 대화를 시작해 주세요.");
    }
  } finally {
    clearTimeout(timeout);
    if (submit.controller === controller) submit.controller = null;
  }
}

function failClosedSubmit(submit, message) {
  if (submit !== pendingSubmit || submit.generation !== generation) return;
  if (submit.controller) submit.controller.abort();
  pendingSubmit = null;
  conversationFailedClosed = true;
  status.textContent = message;
  updateComposer();
}

// The server rejected this one question as too large for the token budget --
// unlike failClosedSubmit, the conversation and composer stay usable so the
// user can shorten the same text and resend, matching the server's own message.
function recoverableRejectSubmit(submit, message) {
  if (submit !== pendingSubmit || submit.generation !== generation) return;
  if (submit.controller) submit.controller.abort();
  pendingSubmit = null;
  if (submit.userMessage) submit.userMessage.article.remove();
  prompt.value = submit.content;
  status.textContent = message;
  updateComposer();
}

function validProjectionBase(value) {
  return exactKeys(value, [
    "schema_version", "run_id", "conversation_id", "input_message_id", "retry_of_run_id",
    "output_message_id", "output_message", "state", "stage", "created_at", "last_updated_at",
    "latest_sequence", "terminal_error",
  ]) && value.schema_version === "1" && UUID_V4.test(value.run_id) && value.conversation_id === conversationId
    && UUID_V4.test(value.input_message_id) && (value.retry_of_run_id === null || UUID_V4.test(value.retry_of_run_id))
    && validTimestamp(value.created_at) && validTimestamp(value.last_updated_at)
    && Number.isSafeInteger(value.latest_sequence) && value.latest_sequence >= 0
    && RUN_STAGES[value.state]?.includes(value.stage);
}

function validProjection(value) {
  if (!validProjectionBase(value)) return false;
  if (["queued", "running", "cancelled"].includes(value.state)) {
    return value.output_message_id === null && value.output_message === null && value.terminal_error === null;
  }
  if (value.state === "completed") {
    return UUID_V4.test(value.output_message_id)
      && exactKeys(value.output_message, ["message_id", "content"])
      && value.output_message.message_id === value.output_message_id
      && visibleText(value.output_message.content) && value.terminal_error === null;
  }
  return ["failed", "timeout"].includes(value.state)
    && value.output_message_id === null && value.output_message === null && validFailure(value.terminal_error);
}

function startRun(projection, question, userMessage) {
  disposeRun();
  contextNotice.hidden = true;
  recovery.hidden = true;
  const assistant = newMessage("assistant", "", true);
  run = {
    id: projection.run_id, generation, state: "queued", stage: "queued",
    cursor: 0, buffer: "", messageId: null, terminalMessage: null, terminalError: null,
    terminalStatus: null, end: false, sawRunning: false, source: null, pollTimer: null,
    pollController: null, polling: false, pollRetries: 0, assistant, announced: new Set(),
    lastProjectionSequence: projection.latest_sequence, reconciled: false, contextTruncated: false,
    question, userMessage, cancelPending: false, cancelRendered: false, cancelTimer: null,
    stopHadFocus: false, focusPrompt: false, expired: false,
    updatedAt: projection.last_updated_at,
  };
  if (TERMINAL_STATES.has(projection.state)) {
    if (!reconcileProjection(projection, false, true)) return failClosedRun();
    disposeRun();
  } else {
    renderRun(projection.state, projection.stage);
    openEventSource();
  }
}

stopButton.addEventListener("click", () => void cancelRun());
// 재시도 is an explicit RetryCommand against the Run that failed -- never a new
// question. The server reuses that Run's input Message and its request Snapshot,
// so no second User Message is created for the same turn.
retryButton.addEventListener("click", () => void retryRun());

async function retryRun() {
  if (!canRetry()) return;
  const target = run;
  // One key per target, reused across attempts, exactly as postQuestion keeps
  // submit.key: a retry that reached the server before the client gave up must be
  // replayed, not charged a second Attempt out of the lineage's limit.
  target.retryKey = target.retryKey || crypto.randomUUID();
  pendingRetry = target;
  retryButton.disabled = true;
  // Same single-flight the question path takes: a question sent while this is in
  // flight would come back 409 run_already_active and fail-close the conversation.
  updateComposer();
  const controller = new AbortController();
  target.retryController = controller;
  const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/v1/conversations/${conversationId}/runs`, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "Idempotency-Key": target.retryKey },
      body: JSON.stringify({ kind: "retry", retry_of_run_id: target.id }),
      signal: controller.signal,
    });
    if (target !== run || target.generation !== generation) return;
    if (!response.ok) {
      let payload;
      try { payload = await response.json(); } catch { payload = null; }
      const known = validErrorEnvelope(payload, RETRY_ERROR_CODES);
      const message = known
        ? payload.error.message : "다시 시도하지 못했습니다. 잠시 후 다시 시도해 주세요.";
      status.textContent = message;
      // A refusal that also invalidates the conversation closes the composer, the
      // way postQuestion closes it -- otherwise the next typed question hits the
      // same wall with no warning.
      if ((known && RETRY_FAIL_CLOSED_CODES.has(payload.error.code))
        || (!known && response.status < 500)) {
        conversationFailedClosed = true;
        blockRetry(target, message);
        return;
      }
      // Otherwise the server's own retryability decides: run_already_active will
      // succeed once the Run in the way ends; retry_exhausted never will.
      if (known && !payload.error.retryable) blockRetry(target, message);
      return;
    }
    let projection;
    try { projection = await response.json(); } catch { projection = null; }
    if (target !== run || target.generation !== generation) return;
    if (!validProjection(projection) || projection.retry_of_run_id !== target.id) {
      failClosedRun();
      return;
    }
    target.assistant.article.remove();
    // Same turn, so the question bubble is reused where it survived (a Cancel
    // removes it) -- a Retry must never show the question twice.
    const userMessage = target.userMessage && target.userMessage.article.isConnected
      ? target.userMessage : newMessage("user", target.question);
    if (prompt.value === target.question) prompt.value = "";
    startRun(projection, target.question, userMessage);
  } catch {
    if (target !== run || target.generation !== generation || pageHidden) return;
    // The POST may have reached the server. Resend the same key once -- an
    // idempotent replay returns the Run the lost response described -- and if that
    // is also lost, stop guessing and close the conversation, exactly as an
    // ambiguous submit does. Leaving the button live would show a terminal Run
    // beside a server that is already generating.
    target.retryAttempts = (target.retryAttempts || 0) + 1;
    if (target.retryAttempts < 2) {
      pendingRetry = null;
      await retryRun();
      return;
    }
    conversationFailedClosed = true;
    status.textContent = "다시 시도 여부를 확인할 수 없습니다. 새 대화를 시작해 주세요.";
  } finally {
    clearTimeout(timeout);
    if (target.retryController === controller) target.retryController = null;
    if (pendingRetry === target) pendingRetry = null;
    retryButton.disabled = !canRetry();
    updateComposer();
  }
}

async function cancelRun() {
  if (!run || run.generation !== generation || run.cancelPending || !ACTIVE_STATES.has(run.state)) return;
  const current = run;
  current.cancelPending = true;
  current.stopHadFocus = document.activeElement === stopButton;
  updateComposer();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/v1/runs/${current.id}/cancel`, {
      method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
    });
    // A 4xx can never be fixed by pressing stop again -- the same distinction
    // pollRun draws right below. A 429 is the exception: it is a ceiling with a
    // window behind it, so pressing stop again in a moment is exactly the right
    // thing, and reporting it as permanent would strand a Run the user can stop.
    if (!response.ok) {
      throw Object.assign(new Error("cancel rejected"),
        { permanent: response.status < 500 && response.status !== 429 });
    }
    let result;
    // A 200 we cannot parse is as permanent as a 4xx: pressing stop again cannot
    // make the server answer differently.
    try { result = await response.json(); } catch { throw Object.assign(new Error("cancel body"), { permanent: true }); }
    if (current !== run || current.generation !== generation) return;
    if (!validCancelResult(result, current)) throw Object.assign(new Error("invalid cancel result"), { permanent: true });
    // Both outcomes are a success. The terminal state itself still arrives through
    // the same committed log every other transition does -- SSE or polling. Bound
    // that wait: a log that never arrives must not disable the composer forever.
    current.cancelTimer = setTimeout(() => settleCancel(current), CANCEL_SETTLE_MS);
  } catch (error) {
    if (current !== run || current.generation !== generation) return;
    if (error.name === "AbortError") {
      // We stopped waiting; the server may well have committed the stop anyway.
      // The committed log is the authority, so reconcile instead of crying failure.
      current.cancelPending = false;
      status.textContent = "중지 요청 결과를 확인하는 중입니다.";
      startPolling(current.end);
    } else if (error.permanent) {
      // Pressing stop again cannot succeed, so it stays disabled; 새 대화 is the way out.
      status.textContent = "이 답변은 중지할 수 없습니다. 새 대화를 시작해 주세요.";
    } else {
      current.cancelPending = false;
      status.textContent = "중지 요청을 보내지 못했습니다. 다시 시도해 주세요.";
    }
    updateComposer();
  } finally {
    clearTimeout(timeout);
  }
}

function settleCancel(current) {
  if (current !== run || current.generation !== generation || TERMINAL_STATES.has(current.state)) return;
  current.cancelPending = false;
  status.textContent = "중지 상태를 아직 확인하지 못했습니다. 다시 중지하거나 새 대화를 시작해 주세요.";
  // Keep asking: the committed log is the only thing that can end this Run, and
  // until it does the composer must stay closed -- a second question while one is
  // still running is exactly what the Run contract forbids. 새 대화 is the escape.
  startPolling(current.end);
  updateComposer();
}

function validCancelResult(value, current) {
  return exactKeys(value, ["schema_version", "cancel_outcome", "run"]) && value.schema_version === "1"
    && ["accepted", "already_terminal"].includes(value.cancel_outcome)
    && validProjection(value.run) && value.run.run_id === current.id;
}

// Cancel is not an error: the streaming bubble, its unanswered question and every
// uncommitted character go away, the notice is announced once by renderRun, and the
// question comes back beside the 재시도/새 대화 controls.
function renderCancelled() {
  if (!run || run.cancelRendered) return;
  run.cancelRendered = true;
  run.cancelPending = false;
  if (run.cancelTimer) clearTimeout(run.cancelTimer);
  run.cancelTimer = null;
  run.focusPrompt = run.stopHadFocus;
  run.buffer = "";
  run.assistant.article.remove();
  // The Turn was never answered; leaving the question in the transcript would show
  // it twice the moment the user resends it.
  if (run.userMessage) run.userMessage.article.remove();
  if (run.question && !visibleText(prompt.value)) prompt.value = run.question;
  // The panel itself belongs to renderRecovery, which renderRun calls immediately
  // after this -- both call sites run the two in that order, synchronously.
}

function openEventSource() {
  if (!run || run.generation !== generation || run.source || run.polling) return;
  try {
    const source = new EventSource(`/api/v1/runs/${run.id}/events`);
    run.source = source;
    for (const type of EVENT_TYPES) source.addEventListener(type, (event) => receiveEvent(type, event));
    source.onerror = () => {
      if (!run || source !== run.source) return;
      source.close();
      run.source = null;
      startPolling(false);
    };
  } catch { startPolling(false); }
}

function receiveEvent(type, event) {
  if (!run || run.generation !== generation) return;
  const sequence = Number(event.lastEventId);
  if (!Number.isSafeInteger(sequence) || sequence < 1) return malformedStream();
  if (sequence <= run.cursor) return;
  if (sequence !== run.cursor + 1) return malformedStream();
  let value;
  try { value = JSON.parse(event.data); } catch { return malformedStream(); }
  if (value.schema_version !== "1" || value.type !== type || value.run_id !== run.id
    || value.sequence !== sequence || !validTimestamp(value.occurred_at)) return malformedStream();
  run.updatedAt = value.occurred_at;
  if (!applyEvent(value)) return malformedStream();
  run.cursor = sequence;
}

function applyEvent(event) {
  const base = ["schema_version", "run_id", "sequence", "occurred_at", "type"];
  if (event.type === "run.status") {
    if (!exactKeys(event, [...base, "state", "stage"])) return false;
    if (event.state === "running" && event.stage === "streaming" && !run.sawRunning && !run.terminalMessage) {
      run.sawRunning = true;
      renderRun("running", "streaming");
      return true;
    }
    if (!["completed", "failed", "timeout", "cancelled"].includes(event.state) || event.stage !== "terminal"
      || !run.terminalMessage || run.terminalStatus) return false;
    if ((event.state === "completed") !== (run.terminalMessage === "completed")) return false;
    if (["failed", "timeout"].includes(event.state) !== Boolean(run.terminalError)) return false;
    run.terminalStatus = event.state;
    if (event.state === "cancelled") renderCancelled();
    renderRun(event.state, "terminal");
    return true;
  }
  if (event.type === "context.truncated") {
    // Neutral one-time notice, not the run-state Live Region: fires after run.status
    // (running) and before the first message.delta, so it must not already have one.
    if (!exactKeys(event, [...base, "dropped_turn_count"]) || !run.sawRunning
      || run.terminalMessage || run.terminalStatus || run.end || run.contextTruncated
      || run.messageId || run.buffer || !Number.isSafeInteger(event.dropped_turn_count)
      || event.dropped_turn_count < 1) return false;
    run.contextTruncated = true;
    contextNotice.hidden = false;
    return true;
  }
  if (event.type === "conversation.expired") {
    // The Conversation reached its 1-hour ceiling underneath this Run. It can
    // arrive at any point in the log; what it may not do is arrive twice, or after
    // a terminal the Run already reported. Deliberately NOT written into
    // `terminalStatus`: `expired` is not a Run state, and every renderer keyed on
    // that field (STATE_LABELS, RUN_STAGES, RECOVERY_IMPACT) has no entry for it.
    if (!exactKeys(event, base) || run.terminalStatus || run.expired || run.end) return false;
    run.expired = true;
    return true;
  }
  if (event.type === "run.error") {
    if (!exactKeys(event, [...base, "error"]) || run.terminalMessage !== "discarded"
      || run.terminalError || run.terminalStatus || !validFailure(event.error)) return false;
    run.terminalError = event.error;
    run.assistant.content.textContent = event.error.message;
    return true;
  }
  if (event.type === "stream.end") {
    if (!exactKeys(event, [...base, "final_state", "final_sequence"]) || run.end
      || event.final_sequence !== event.sequence) return false;
    if (event.final_state === "expired") {
      // The only final_state with no Run state behind it, so it is checked against
      // the expiry flag rather than against a terminal the Run never reached.
      if (!run.expired || run.terminalStatus) return false;
      run.end = true;
      if (run.source) run.source.close();
      run.source = null;
      // Nothing follows an expiry -- there is no Run left to poll for, and asking
      // would only produce the 404 of a Resource that was purged on purpose.
      expireConversation();
      return true;
    }
    if (!run.terminalStatus || event.final_state !== run.terminalStatus
      || ["failed", "timeout"].includes(event.final_state) !== Boolean(run.terminalError)) return false;
    run.end = true;
    if (run.source) run.source.close();
    run.source = null;
    setTimeout(() => startPolling(true), 0);
    return true;
  }
  if (run.terminalMessage || run.terminalStatus || run.end) return false;
  // The one message event a still-queued Run can emit: a Cancel committed before
  // mark_running opens the log at sequence 1 with no run.status(running) ahead of
  // it. Any other out-of-order discard is still malformed.
  if (!run.sawRunning && !(event.type === "message.discarded" && event.sequence === 1
    && run.state === "queued")) return false;
  if (event.type === "message.delta") {
    if (!exactKeys(event, [...base, "message_id", "text"]) || !UUID_V4.test(event.message_id)
      || !rawChunk(event.text) || (run.messageId && run.messageId !== event.message_id)) return false;
    run.messageId = event.message_id;
    run.buffer += event.text;
    run.assistant.content.textContent = run.buffer;
    return true;
  }
  if (event.type === "message.completed") {
    if (!exactKeys(event, [...base, "message_id", "text"]) || !UUID_V4.test(event.message_id)
      || !visibleText(event.text) || (run.messageId && run.messageId !== event.message_id)
      || (run.buffer && event.text !== run.buffer)) return false;
    run.messageId = event.message_id;
    run.buffer = event.text;
    run.terminalMessage = "completed";
    run.assistant.content.textContent = event.text;
    if (run.assistant.note) run.assistant.note.remove();
    return true;
  }
  if (event.type === "message.discarded") {
    if (!exactKeys(event, [...base, "message_id"]) || !UUID_V4.test(event.message_id)
      || (run.messageId && run.messageId !== event.message_id)) return false;
    run.messageId = event.message_id;
    run.buffer = "";
    run.terminalMessage = "discarded";
    // Kept even though a Cancel removes the bubble a tick later: if SSE drops
    // between the discard and run.error, an empty bubble is all the user would get.
    run.assistant.content.textContent = "답변을 완료하지 못했습니다.";
    if (run.assistant.note) run.assistant.note.remove();
    return true;
  }
  return false;
}

// The 413 context_too_large ErrorEnvelopeV1 shape -- checked closed, like every
// other server payload this file trusts, before it drives a UI decision.
// Closed, like every other server payload this file trusts: an exact key set, an
// exactly-empty field_errors, and a `code` from an allow-list the caller supplies
// -- nothing else may put its `message` on screen.
function validErrorEnvelope(value, codes) {
  return exactKeys(value, ["error"]) && exactKeys(value.error, [
    "code", "message", "retryable", "correlation_id", "field_errors",
  ]) && codes.has(value.error.code) && visibleText(value.error.message)
    && typeof value.error.retryable === "boolean" && UUID_V4.test(value.error.correlation_id)
    && exactKeys(value.error.field_errors, []);
}

function validContextTooLargeEnvelope(value) {
  return validErrorEnvelope(value, new Set(["context_too_large"]))
    && value.error.retryable === false;
}

function validFailure(value) {
  return exactKeys(value, ["kind", "retryable", "correlation_id", "message"])
    && PROVIDER_FAILURES.has(value.kind) && typeof value.retryable === "boolean"
    && UUID_V4.test(value.correlation_id) && visibleText(value.message);
}

// 만료: the stream is closed, everything the expired Conversation held is gone
// from this screen too, and the only offer left is a new conversation.
function expireConversation() {
  if (!run) return;
  disposeRun();
  run.reconciled = true;
  run.buffer = "";
  conversationFailedClosed = true;
  transcript.replaceChildren();
  runSummary.hidden = true;
  recovery.hidden = true;
  contextNotice.hidden = true;
  status.textContent = EXPIRED_NOTICE;
  updateComposer();
}

function malformedStream() {
  if (!run) return;
  status.textContent = "실시간 응답을 확인할 수 없어 완료 상태를 다시 확인합니다.";
  fallbackToPolling();
}

function fallbackToPolling() {
  startPolling(false);
}

function startPolling(afterEnd) {
  if (!run || run.generation !== generation || run.polling) return;
  if (run.source) run.source.close();
  run.source = null;
  run.polling = true;
  run.pollAfterEnd = afterEnd;
  run.pollRetries = 0;
  void pollRun();
}

async function pollRun() {
  if (!run || run.generation !== generation || !run.polling || run.pollController) return;
  const current = run;
  const controller = new AbortController();
  current.pollController = controller;
  const timeout = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/v1/runs/${current.id}`, {
      credentials: "same-origin", cache: "no-store", signal: controller.signal,
    });
    if (!response.ok) {
      // 429 is a ceiling, not a broken Run: back off and ask again, exactly as a
      // 5xx is retried, instead of tearing down a conversation that is still fine.
      if (response.status >= 500 || response.status === 429) throw new Error("transient poll failure");
      if (response.status === 410) {
        // The polling half of expiry -- the same end as the SSE tail, reached by a
        // client whose stream had already dropped.
        let payload;
        try { payload = await response.json(); } catch { payload = null; }
        if (payload && validErrorEnvelope(payload, new Set(["conversation_expired"]))) return expireConversation();
      }
      return failClosedRun();
    }
    let projection;
    try { projection = await response.json(); } catch { return failClosedRun(); }
    if (current !== run || current.generation !== generation) return;
    if (!reconcileProjection(projection, current.pollAfterEnd)) return failClosedRun();
    current.pollRetries = 0;
    if (!TERMINAL_STATES.has(projection.state)) current.pollTimer = setTimeout(() => void pollRun(), 100);
    else {
      current.reconciled = true;
      disposeRun();
      updateComposer();
    }
  } catch {
    if (current !== run || current.generation !== generation || pageHidden) return;
    current.pollRetries += 1;
    if (current.pollRetries > MAX_POLL_RETRIES) return failClosedRun();
    const delay = Math.min(800, 100 * (2 ** (current.pollRetries - 1)));
    current.pollTimer = setTimeout(() => void pollRun(), delay);
  } finally {
    clearTimeout(timeout);
    if (current.pollController === controller) current.pollController = null;
  }
}

function reconcileProjection(value, afterEnd, accepted = false) {
  if (!validProjection(value) || value.run_id !== run.id) return false;
  run.updatedAt = value.last_updated_at;
  if (!accepted && value.latest_sequence < Math.max(run.lastProjectionSequence, run.cursor)) return false;
  const allowed = {
    queued: new Set(["queued", "running", "completed", "failed", "timeout", "cancelled"]),
    running: new Set(["running", "completed", "failed", "timeout", "cancelled"]),
    completed: new Set(["completed"]), failed: new Set(["failed"]),
    timeout: new Set(["timeout"]), cancelled: new Set(["cancelled"]),
  };
  if (!accepted && !allowed[run.state]?.has(value.state)) return false;
  if (!TERMINAL_STATES.has(value.state)) {
    run.lastProjectionSequence = value.latest_sequence;
    renderRun(value.state, value.stage);
    return true;
  }
  if (afterEnd && (value.latest_sequence !== run.cursor || !run.end)) return false;
  if (value.state === "completed") {
    if ((run.messageId && run.messageId !== value.output_message_id)
      || (run.buffer && run.buffer !== value.output_message.content)) return false;
    if (afterEnd && (run.terminalStatus !== "completed" || run.terminalMessage !== "completed")) return false;
    run.messageId = value.output_message_id;
    run.buffer = value.output_message.content;
    run.assistant.content.textContent = value.output_message.content;
    if (run.assistant.note) run.assistant.note.remove();
  } else if (["failed", "timeout"].includes(value.state)) {
    if (run.terminalError && !sameFailure(run.terminalError, value.terminal_error)) return false;
    if (afterEnd && (run.terminalStatus !== value.state || run.terminalMessage !== "discarded"
      || !sameFailure(run.terminalError, value.terminal_error))) return false;
    // Polling is the only path that learns the failure without a run.error Event;
    // the recovery panel needs the same kind/retryability/Correlation ID either way.
    run.terminalError = value.terminal_error;
    run.buffer = "";
    run.assistant.content.textContent = value.terminal_error.message;
    if (run.assistant.note) run.assistant.note.remove();
  } else if (value.state === "cancelled") {
    // The same cross-check the completed/failed branches carry: a cancelled
    // projection arriving after message.completed was applied must not delete an
    // answer the user already has.
    if (run.terminalMessage === "completed") return false;
    if (afterEnd && (run.terminalStatus !== "cancelled" || run.terminalMessage !== "discarded")) return false;
    renderCancelled();
  } else return false;
  run.lastProjectionSequence = value.latest_sequence;
  run.reconciled = true;
  renderRun(value.state, value.stage);
  return true;
}

function sameFailure(left, right) {
  return left && right && left.kind === right.kind && left.retryable === right.retryable
    && left.correlation_id === right.correlation_id && left.message === right.message;
}

function failClosedRun() {
  if (!run) return;
  disposeRun();
  run.buffer = "";
  run.reconciled = false;
  // renderCancelled detaches the bubble; a message written into a detached node
  // is a message the user never sees.
  // A cancelled turn detached both bubbles, so this one re-enters an emptied
  // transcript and its old position label would be a lie.
  if (!run.assistant.article.isConnected) {
    transcript.append(run.assistant.article);
    labelMessage(run.assistant.article);
  }
  run.assistant.content.textContent = "실행 상태를 안전하게 확인할 수 없습니다.";
  if (run.assistant.note) run.assistant.note.remove();
  conversationFailedClosed = true;
  status.textContent = "실행 상태를 안전하게 확인할 수 없습니다. 새 대화를 시작해 주세요.";
  updateComposer();
}

window.addEventListener("pagehide", () => {
  pageHidden = true;
  if (newConversationController) newConversationController.abort();
  if (pendingSubmit && pendingSubmit.controller) pendingSubmit.controller.abort();
  if (pendingRetry && pendingRetry.retryController) pendingRetry.retryController.abort();
  disposeRun();
});
window.addEventListener("pageshow", () => {
  pageHidden = false;
  if (pendingSubmit && pendingSubmit.attempts < 2) void postQuestion(pendingSubmit);
  else if (pendingSubmit) {
    failClosedSubmit(pendingSubmit, "질문 접수 여부를 확인할 수 없습니다. 새 대화를 시작해 주세요.");
  } else if (run && !run.reconciled) startPolling(run.end);
});

const POLICY_RATE_RETRIES = 2;
const POLICY_RETRY_MS = 400;
const MAX_POLICY_TEXT_LENGTH = 256;
const MAX_SUBPROCESSOR_COUNT = 32;
const MAX_SUBPROCESSOR_LABEL_LENGTH = 128;
const POLICY_KEYS = [
  "schema_version", "provider_label", "endpoint_disclosure", "model_revision", "transmitted_fields",
  "retention_summary", "deletion_summary", "training_use", "processing_region", "subprocessors",
  "session_ttl_seconds", "retrieval_status", "input_warning_categories",
].sort();
const TRANSMITTED_FIELD_LABELS = { system_instruction: "시스템 안내", current_message: "현재 질문", selected_prior_messages: "선택된 이전 메시지" };
const INPUT_WARNING_LABELS = { personal_information: "개인정보", company_confidential_data: "회사 기밀", credentials: "Credential·비밀번호·API 키" };
const UNKNOWN_POLICY_PATTERN = /(?:unknown|undefined|not[ _-]?configured|unavailable|(?:^|[\s_:/-])n\/?a(?:$|[\s_:/-])|none|null|미정|미확정|알\s*수\s*없음)/i;
const UNSAFE_POLICY_PATTERN = /(?:secret|credential|password|token|api[_ -]?key|authorization|bearer|private[_ -]?(?:endpoint|route|network|host|routing)|internal[_ -]?(?:endpoint|route|network|host|routing)|query(?:[_ -]?string)?|trace|raw[_ -]?response|stack[_ -]?trace|debug|response[_ -]?(?:body|headers?)|chain[_ -]?of[_ -]?thought|intermediate|cookie|jwt|oauth|https?:\/\/|[?&])/i;

function sameItems(actual, expected) {
  return Array.isArray(actual) && actual.length === expected.length
    && expected.every((item) => actual.filter((candidate) => candidate === item).length === 1);
}
function hasForbiddenCodePoint(value) {
  return /[\p{Cc}\p{Cf}\p{Cs}\p{Co}]/u.test(value);
}
function safeRuntimeText(value, maxLength = MAX_POLICY_TEXT_LENGTH) {
  return typeof value === "string" && value && value.length <= maxLength && value.trim() === value
    && !UNKNOWN_POLICY_PATTERN.test(value) && !UNSAFE_POLICY_PATTERN.test(value)
    && !hasForbiddenCodePoint(value);
}
function isExactPolicy(value) {
  return exactKeys(value, POLICY_KEYS) && value.schema_version === "1"
    && safeRuntimeText(value.provider_label) && safeRuntimeText(value.endpoint_disclosure, 64)
    && /^[a-z0-9][a-z0-9._-]{0,63}$/.test(value.endpoint_disclosure)
    && !/(localhost|private|internal)/.test(value.endpoint_disclosure)
    && !/(\.local|\.internal)$/.test(value.endpoint_disclosure)
    && !/^\d+(?:\.\d+){3}$/.test(value.endpoint_disclosure) && safeRuntimeText(value.model_revision)
    && sameItems(value.transmitted_fields, Object.keys(TRANSMITTED_FIELD_LABELS))
    && safeRuntimeText(value.retention_summary) && safeRuntimeText(value.deletion_summary)
    && ["not_used", "provider_policy"].includes(value.training_use) && safeRuntimeText(value.processing_region)
    && Array.isArray(value.subprocessors) && value.subprocessors.length <= MAX_SUBPROCESSOR_COUNT
    && value.subprocessors.every((item) => safeRuntimeText(item, MAX_SUBPROCESSOR_LABEL_LENGTH))
    && new Set(value.subprocessors).size === value.subprocessors.length
    && value.session_ttl_seconds === 3600 && value.retrieval_status === "disabled"
    && sameItems(value.input_warning_categories, Object.keys(INPUT_WARNING_LABELS));
}
function clearPolicyFields() { for (const field of policyFields) field.textContent = ""; }
function showPolicyUnavailable() {
  policyReady = false; clearPolicyFields(); updateComposer(); policySuccess.hidden = true;
  policyRetry.hidden = false; policyRetry.disabled = false; policyImpact.hidden = false;
  policyStatus.textContent = "서비스 준비 상태와 정책을 확인할 수 없습니다.";
  policyImpact.textContent = "정책을 확인할 수 없어 질문 기능을 사용할 수 없습니다. 다시 확인해 주세요.";
}
function renderPolicy(policy) {
  const values = {
    ...policy, subprocessors: policy.subprocessors.length ? policy.subprocessors.join(" · ") : "없음",
    transmitted_fields: policy.transmitted_fields.map((item) => TRANSMITTED_FIELD_LABELS[item]).join(" · "),
    training_use: policy.training_use === "not_used" ? "사용하지 않음" : "Provider 정책에 따름",
    session_ttl_seconds: `${policy.session_ttl_seconds.toLocaleString("ko-KR")}초 (1시간)`, retrieval_status: "비활성화(disabled)",
    input_warning_categories: policy.input_warning_categories.map((item) => INPUT_WARNING_LABELS[item]).join(" · "),
  };
  for (const field of policyFields) field.textContent = values[field.dataset.policyField] || "";
  policyReady = true; updateComposer(); policyStatus.textContent = "서비스 준비 상태와 공개 정책을 확인했습니다.";
  policyImpact.hidden = true; policyRetry.hidden = true; policyRetry.disabled = true; policySuccess.hidden = false;
}
async function fetchPolicyResource(url, currentGeneration, attempt = 0) {
  const controller = new AbortController(); policyRequests.add(controller);
  const timeout = setTimeout(() => controller.abort(), 3_000);
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (currentGeneration !== requestGeneration) throw new Error("stale");
  } finally { clearTimeout(timeout); policyRequests.delete(controller); }
  // The policy endpoint is behind the per-IP gate now, so a busy moment can answer
  // 429. Backing off once and asking again is the difference between a brief wait
  // and an app that is unusable until the user finds the 다시 확인 button.
  if (response.status === 429 && attempt < POLICY_RATE_RETRIES) {
    await new Promise((resume) => setTimeout(resume, POLICY_RETRY_MS));
    if (currentGeneration !== requestGeneration) throw new Error("stale");
    return fetchPolicyResource(url, currentGeneration, attempt + 1);
  }
  return response;
}
async function loadPolicy() {
  const currentGeneration = ++requestGeneration;
  for (const controller of policyRequests) controller.abort();
  policyReady = false; clearPolicyFields(); updateComposer(); policySuccess.hidden = true;
  policyImpact.hidden = false; policyRetry.hidden = true; policyRetry.disabled = true;
  policyStatus.textContent = "서비스 준비 상태와 정책을 확인하는 중입니다.";
  try {
    const ready = await fetchPolicyResource("/ready", currentGeneration);
    if (ready.status !== 204) throw new Error();
    const response = await fetchPolicyResource("/api/v1/policy", currentGeneration);
    if (response.status !== 200) throw new Error();
    const policy = await response.json();
    if (currentGeneration !== requestGeneration || !isExactPolicy(policy)) throw new Error();
    renderPolicy(policy);
  } catch { if (currentGeneration === requestGeneration) showPolicyUnavailable(); }
}
policyRetry.addEventListener("click", loadPolicy);
void loadPolicy().catch(showPolicyUnavailable);
