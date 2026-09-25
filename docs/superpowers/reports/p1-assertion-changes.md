# P1 assertion changes (Task 6: story suites ported to the agent port)

Every behavioural assertion the port had to change, and why. Mechanical renames from the
Task 6 mapping table (provider base classes, `GROUNDED_RESULT` arguments, `.thread` →
`.future`) are not listed here; the event-list rows are listed because they change what a
test expects to see.

Rulings cited are the Task 4 rulings in `.superpowers/sdd/2026-09-24-wiki-agent/progress.md`:

- **R-deadline** -- one alarm at the Deadline; `_commit_timeout` commits `timeout` first,
  then sends one `abort`; a Task still running `close_grace_ms` later is cancelled. No
  early cancel alarm.
- **R-shutdown** -- `shutdown()` waits at most `close_grace + 2 s`, then cancels what is left.

## Changed assertions

| File | Test | Old | New | Why |
|------|------|-----|-----|-----|
| `test_story_1_3.py` | `test_complete_cas_commits_one_assistant_and_full_equality_chain` | events `run.status, message.completed, run.status, stream.end`; completed event at index 1 | `run.status, message.delta, message.sources, message.completed, run.status, stream.end`; completed event at index 3 | The agent port has one answer path (`on_delta`): a complete-only Provider's answer now arrives as one delta (AD-25, Task 3/5). `message.sources` comes right before `message.completed` (AD-29). |
| `test_story_1_3.py` | `test_generation_thread_registration_is_atomic` | `lock_states == [True]`: the generation was registered AND its thread started inside `_generation_lock`, so no waiter could see a registered generation without its worker | `lock_states == [True]` and `waiter_returned_early == [False]`: the Task is scheduled and registered in one `_generation_lock` section, so every registered generation has the Future waiters block on | Generations are asyncio Tasks now (Task 4). Task 6b restored the stronger invariant: `agent_loop.submit` runs inside `_generation_lock` together with the registration (`lock_states == [True]`), and a waiter started at that moment blocks on the generation instead of skipping it (`waiter_returned_early == [False]`). The 1.8 join-the-submitter workaround is removed. |
| `test_story_1_3.py` | `test_shutdown_uses_completion_join_without_timeout`, renamed `test_shutdown_waits_for_generations_within_the_close_grace_bound` | shutdown joins each generation with `timeout=None` | shutdown's one wait has `0 < timeout <= close_grace_ms/1000 + 2` | R-shutdown: an agent ignoring its abort must not hold the process open. |
| `test_story_1_4.py` | `test_streamed_run_uses_one_contiguous_log_and_projection` | 6 events, no `message.sources` | 7 events with `message.sources` before `message.completed` | AD-29 (mapping table row). |
| `test_story_1_5.py` | `test_history_over_budget_keeps_newest_contiguous_turns_and_truncates_once` | `... message.delta, message.completed ...` | `... message.delta, message.sources, message.completed ...` | AD-29 (mapping table row). |
| `test_story_1_5.py` | `test_tampered_request_variants_fail_closed_for_tokenizer_and_provider_calls` | `ContextIntegrityError` from `complete()` and from `stream()` | `ContextIntegrityError` from the one agent call, `run()` | The port has no `complete`/`stream`; `FakeAgent.run` verifies integrity first (AD-25). |
| `test_story_1_5.py` | `test_count_input_tokens_and_stream_consume_identical_canonical_bytes` | `adapter.stream(request, ...)` returns the answer | `adapter.run(...)` delivers the same single delta | Same: one agent call path. |
| `test_story_1_5_1.py` | `test_both_bindings_pass_the_same_api_sse_policy_and_readiness_contract` | parametrized `deterministic`, `ollama`; last three types `message.completed, run.status, stream.end` | `deterministic` only; last four types start with `message.sources` | The `ollama` case ran the retired PydanticAI Ollama adapter (AD-24); AD-29 for the event. |
| `test_story_1_6.py` | `test_cancel_between_publish_and_mark_running_still_stops_the_provider_call` | the Provider's call handle saw `cancel()` once | the agent saw `abort(run_id)` once | There is no pre-published call handle on the agent port; a Cancel reaches the agent as `abort(run_id)` (AD-25). |
| `test_story_1_6.py` | `test_a_cancelled_generation_task_is_not_reused_before_it_ends` | a new question while the cancelled Task drains raises `ActiveRunConflict` | the new question is accepted only once the first Task has ended (its Future is done) | R-deadline/close grace: an agent ignoring its abort has its Task cancelled after `close_grace_ms`, which frees the slot; "never two at once" still holds and is what is asserted. |
| `test_story_1_6.py` | `test_readiness_refuses_a_provider_that_does_not_confirm_it_closes_its_stream` | five non-cancellable fakes, incl. `NoCloseStreamProvider`, `RefusesCloseProvider` | three: no `abort`, no close budget, zero grace | `closes_stream` is retired (AD-24); the cancellation gate is now `abort` + `run` + positive `close_grace_ms`. |
| `test_story_1_7.py` | `test_a_running_run_past_its_deadline_is_cancelled_then_committed`, renamed `..._committed_then_cancelled` | the Run is still `queued`/`running` when the cancel lands | the Run is already `timeout` when the cancel lands | R-deadline: commit first, then abort. |
| `test_story_1_7.py` | `test_the_alarm_cancels_at_the_grace_but_commits_only_at_the_deadline`, renamed `test_the_alarm_neither_commits_nor_cancels_before_the_deadline` | an early commit alarm commits nothing; a separate cancel alarm (Deadline − Grace) cancels early | the one alarm fired early re-arms, commits nothing and cancels nothing (`cancel_requests == 0`); at the Deadline it commits `timeout` and aborts | R-deadline: the pre-deadline cancel alarm is dropped. |
| `test_story_1_11.py` | `test_every_configuration_*` battery | three configurations: deterministic, Ollama LAN, Target Direct | deterministic only | The two others are PydanticAI adapters retired by AD-24 (they cannot pass the async `probe()`/`run()` port; see the pre-task failure below). The sidecar joins with Task 14's contract tests. |
| `test_story_1_11.py` | `test_every_configuration_answers_one_question_over_the_same_api_and_sse` | `run.status, message.delta, message.completed, run.status, stream.end` | `run.status, agent.step, message.delta, message.sources, message.completed, run.status, stream.end` (`_event_types` collapses consecutive `agent.step` like consecutive deltas) | AD-29: FakeAgent emits steps, and `message.sources` precedes completion. |
| `test_story_1_11.py` | `test_every_configuration_holds_the_ad25_request_contract` | also asserted `ToolPolicyV1.zero` and zero tools on the wire via `stream()` | Digest/ContextIntegrity half only | Zero-tool policy superseded by AD-26 `tool_allowlist` (Task 13); `stream()` retired (AD-24). |
| `test_story_1_11.py` | `test_the_document_client_and_contract_agree_on_the_event_set` | document == contract == client, 8 types | split: `test_the_document_and_contract_agree_on_the_event_set` (10 types) and `test_the_client_and_contract_agree_on_the_event_set` (`xfail(strict=True, reason="P4 Task 20")`) | AD-29 adds `agent.step` and `message.sources`; the Web client learns them in P4 Task 20. |
| `test_story_1_11.py` | `test_every_documented_order_is_the_order_a_real_run_produces` | documented success `run.status, context.truncated, message.delta, message.completed, run.status, stream.end` | `run.status, context.truncated, agent.step, message.delta, message.sources, message.completed, run.status, stream.end` | Task 6 Step 3: `docs/sse-contract.md` success order from AD-29. |

## xfail

| File | Test | Reason |
|------|------|--------|
| `test_story_1_11.py` | `test_the_client_and_contract_agree_on_the_event_set` | `xfail(strict=True, reason="P4 Task 20")` -- `app.js` `EVENT_TYPES` does not list `agent.step`/`message.sources` yet. |

No browser suite needed an xfail: all 41 browser tests pass (they stub API routes).

## Pre-task failure diagnosed

`test_story_1_11.py::test_every_configuration_is_ready_inside_the_probe_budget` failed for
`ollama_lan` and `target_direct`: both bind PydanticAI Direct adapters whose `probe()` is
synchronous and returns a bool. `ChatApplication` now awaits `provider.probe()` on the agent
loop and accepts only an `AgentProbeV1`, so those bindings can never be Ready. Retired
concept (AD-24); the configurations were removed (row above), the deterministic one passes.

## Task 17 (Policy disclosure for the Wiki agent -- FR-7, NFR-6)

`PolicyProjectionV1.retrieval_status` narrows from `Literal["disabled"]` to
`Literal["wiki_readonly"]`, `transmitted_fields` gains `"wiki_excerpts"`, and a new
required `wiki_display_name` field is added. `FakeAgent` and `pi_policy_metadata` now
report `retrieval_status="wiki_readonly"` (the Wiki agent is what P1-P3 built). The
pre-agent PydanticAI adapters (`DeterministicProvider`, `OllamaLanProxyAdapter`, both
`public_demo`/legacy `local_test` bindings retired in Task 22) keep `retrieval_status
="disabled"` -- they bind no Wiki -- so `build_policy_projection` can never validate
their metadata again. `PydanticAIDirectAdapter.policy_metadata` (`src/aidd_chat/
adapters/direct.py`) gained a `wiki_display_name=NO_WIKI_DISPLAY_NAME` argument so the
now-required dataclass field does not raise `TypeError` on construction; this file is
outside the Task 17 brief's file list but the change is required for it (the brief's
own note: "keep its policy metadata valid under the new projection (minimal change)").

### Changed assertions (not weakened -- the Wiki agent is what these bindings now do)

| File | Test | Old | New | Why |
|------|------|-----|-----|-----|
| `test_story_1_2.py` | `test_projection_is_frozen_closed_and_canonical` | `policy.retrieval_status == "disabled"` | `== "wiki_readonly"` | FakeAgent is the Wiki agent binding now. |
| `test_story_1_2.py` | `test_routes_are_status_only_and_policy_is_no_store` | `policy.json()["retrieval_status"] == "disabled"` | `== "wiki_readonly"` | Same. |
| `test_story_1_2.py` | `test_policy_ui_has_runtime_loading_retry_and_knowledge_boundary_contract` | asserted the old "no RAG/Citation" knowledge notice (`"일반 모델"`, `"RAG"`, `"Citation"`) | asserts the new grounded-in-Wiki notice (`"연결된 Wiki"`, `"Wiki 보충 대상"`) | Task 17 replaces `UX-KNOWLEDGE-NOTICE`'s copy: the app now actually grounds answers in the Wiki, so the old "not RAG" disclosure would be false. |
| `test_story_1_2.py` | `test_policy_browser_state_machine` | stubbed `valid_policy` had 13 keys, `transmitted_fields` without `wiki_excerpts`, `retrieval_status: "disabled"` | 14 keys incl. `wiki_display_name`, `transmitted_fields` incl. `"wiki_excerpts"`, `retrieval_status: "wiki_readonly"` | `app.js`'s `isExactPolicy`/`POLICY_KEYS` now require the new shape; a stub in the old shape fails closed and the success panel never appears. |
| `test_story_1_5_1.py` | `test_both_bindings_pass_the_same_api_sse_policy_and_readiness_contract[deterministic]` | expected policy key set without `wiki_display_name` | adds `"wiki_display_name"` | The wire shape grew a field. |
| `test_story_1_11.py` | `test_every_configuration_projects_the_same_closed_policy[deterministic]` | `projection.retrieval_status == "disabled"` | `== "wiki_readonly"` | Same as the 1.2 rows above. |

### xfail (Task 17)

| File | Test | Reason |
|------|------|--------|
| `test_story_1_9.py` | `test_a_compliant_public_demo_binding_passes` | `known_policy` now always refuses (see above) |
| `test_story_1_9.py` | `test_public_demo_refuses_a_private_endpoint` | refused set gains `known_policy` alongside `provider_binding` |
| `test_story_1_9.py` | `test_public_demo_refuses_a_localhost_endpoint` | same |
| `test_story_1_9.py` | `test_public_demo_refuses_a_profile_mismatch` | same |
| `test_story_1_9.py` | `test_public_demo_requires_a_zero_tool_policy` | asserted the guards pass; `known_policy` now refuses |
| `test_story_1_9.py` | `test_public_demo_refuses_multiple_workers_or_an_undeclared_count` (all 3 parametrizations) | refused set gains `known_policy` alongside `single_process` |
| `test_story_1_9.py` | `test_public_demo_refuses_debug_or_default_access_logging` (all 6 parametrizations) | refused set gains `known_policy` alongside `quiet_logs` |
| `test_story_1_9.py` | `test_public_demo_refuses_any_logger_at_debug` (both parametrizations) | same |
| `test_story_1_5_1.py` | `test_ollama_policy_projection_satisfies_the_browser_predicate` | `build_policy_projection(OllamaLanProxyAdapter.policy_metadata)` now raises before any assertion runs |

Every one of these is `apply_public_demo_guards`/`build_policy_projection` exercised
against `OllamaLanProxyAdapter`: `retrieval_status="disabled"` can never satisfy
`PolicyProjectionV1`'s new `Literal["wiki_readonly"]` any more, so `known_policy`
refuses unconditionally and every guard test not already expecting `known_policy` in
its refused set now gets it as an extra member. `apply_public_demo_guards` itself is
untouched (Task 17 brief: "leave it"); `public_demo` is retired outright in Task 22.
