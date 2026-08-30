# SSE 계약 -- `GET /api/v1/runs/{run_id}/events`

OpenAPI(`/openapi.json`)는 이 Endpoint가 **어떤 Event Schema**를 보내는지까지만
말할 수 있다. 순서, `Last-Event-ID` Replay, Stream이 닫히는 조건, 만료 Tail은
Schema가 아니라 시간에 대한 규칙이라 OpenAPI 문법에 담기지 않는다. 그 절반이
이 문서다.

만료 자체의 규칙(정확히 3,600초, 활동으로 연장 없음, Purge 시점)과 재시도 Code
표는 [`README.md`](../README.md)가 소유한다. 여기서는 그것들이 **Stream에서 어떻게
보이는지**만 적는다.

## Frame

한 Event는 세 줄이다.

```
id: 3
event: message.delta
data: {"schema_version":"1","type":"message.delta","run_id":"…","sequence":3,"occurred_at":"2026-08-30T05:00:00.001Z","message_id":"…","text":"안녕"}

```

- `id:` -- Base-10 Sequence. 1부터 이 Run 안에서 빈틈없이 증가한다. Client가
  재연결할 때 `Last-Event-ID`로 그대로 돌려보내는 값이다.
- `event:` -- 아래 8개 Type 중 하나. `data`의 `type`과 항상 같다.
- `data:` -- `RunEventV1` compact JSON(UTF-8, `ensure_ascii=False`). 모든 Event가
  `schema_version`·`type`·`run_id`·`sequence`·`occurred_at`을 갖는다.

`Cache-Control: no-store`와 `X-Accel-Buffering: no`가 항상 붙는다. 15초 동안 보낼
Event가 없으면 `: keepalive` Comment 한 줄을 보낸다(Event가 아니므로 Sequence를
쓰지 않는다).

## Event 집합

Server의 `RunEventV1` Union, Client의 `EVENT_TYPES` 배열, 그리고 이 표는 같은
집합이어야 하며 `tests/test_story_1_11.py`가 셋을 대조한다.

| Type | 언제 | 고유 Field |
|------|------|-----------|
| `run.status` | Run이 `running`으로 시작할 때(Sequence 1), 그리고 Terminal에서 한 번 더 | `state`, `stage` |
| `message.delta` | 부분 출력 한 조각 | `message_id`, `text` |
| `message.completed` | 답변이 완결로 Commit됐을 때 | `message_id`, `text` |
| `message.discarded` | 실패·Timeout·Cancel로 부분 출력을 버릴 때 | `message_id` |
| `run.error` | 실패 또는 Timeout의 Typed Failure | `error` |
| `context.truncated` | 예산을 넘겨 오래된 Turn을 통째로 제외했을 때(Run당 최대 1회) | `dropped_turn_count` |
| `conversation.expired` | Stream이 열려 있는 동안 Conversation이 만료됐을 때 | -- |
| `stream.end` | 이 Stream의 마지막 Event | `final_state`, `final_sequence` |

`message.completed`는 **완결된 답변에만** 나온다. 부분 출력은 어떤 경로에서도
`completed`로 표시되지 않고 `message.discarded`로 버려진다.

## 순서

성공:

```
run.status(running) → (context.truncated?) → message.delta* → message.completed → run.status(completed) → stream.end
```

실패·Timeout:

```
run.status(running) → message.delta* → message.discarded → run.error → run.status(failed|timeout) → stream.end
```

Cancel:

```
run.status(running) → message.delta* → message.discarded → run.status(cancelled) → stream.end
```

`run.status(running)`은 언제나 Sequence 1이다. `context.truncated`는 그 바로 뒤,
첫 `message.delta` 앞에서 Run당 최대 한 번만 나온다.
`stream.end`의 `final_sequence`는 언제나 자기 자신의 `sequence`와 같다.

네 순서(성공·실패/Timeout·Cancel·만료) 모두 문서에 적힌 그대로를 실제 Run이 낸 Event
순서와 대조한다 -- `tests/test_story_1_11.py::test_every_documented_order_is_the_order_a_real_run_produces`.

## Replay (`Last-Event-ID`)

Client는 마지막으로 받은 `id`를 `Last-Event-ID` Header로 보낸다. Server는 그
Sequence **다음** Event부터 다시 보낸다. Replay는 같은 Committed Log를 읽으므로
Polling(`GET /api/v1/runs/{run_id}`)과 같은 상태를 본다.

- Cursor가 비었거나 없으면 0으로 취급한다(처음부터).
- Base-10 정수가 아니거나 20자를 넘으면 `400 invalid_event_cursor`.
- Run이 이미 Terminal이고 Cursor가 마지막 Sequence보다 **크면** `400
  invalid_event_cursor`.
- Run이 이미 Terminal이고 Cursor가 마지막 Sequence와 **같으면** `204` -- 보낼 것이
  없으므로 Stream을 열지 않는다.

재연결해도 시간은 새로 늘어나지 않는다. Stream은 그 Run에 **남아 있는** 120초
Deadline만큼만 유지된다.

## Terminal Close

Stream은 다음 중 하나로 닫힌다.

- `stream.end`를 보낸 직후(정상 종료). Run이 Terminal에 도달한 경우다.
- Client가 연결을 끊은 경우.
- 그 Run의 Deadline이 지난 경우 -- Run이 살아 있을 수 없으므로 SSE Slot을 돌려준다.

닫힌 뒤에는 어떤 Event도 만들어지지 않는다. Terminal Run에 다시 붙으려면
`Last-Event-ID` Replay를 쓰고, 이미 끝까지 받았다면 `204`를 받는다.

## 만료 Tail

Stream이 열려 있는 동안 Conversation이 만료되면 정확히 두 Event가 더 나온다.

```
conversation.expired → stream.end(final_state: "expired")
```

`expired`는 Run 상태가 아니다 -- 어떤 Run도 그 상태에 도달하지 않는다. Stream
아래의 Conversation이 끝났다는 Stream 자신의 표현이며, 언제나 `conversation.expired`
바로 뒤에 온다. 이 두 Event는 Purge와 같은 Critical Section에서 Aggregate 자신의
Sequence로 기록되므로, 누가 Purge를 이겼든 모든 Reader와 재연결하는 Client가
동일한 쌍을 받는다.

## Error

Stream을 **열기 전**의 거부는 SSE가 아니라 평범한 JSON Error Envelope이다. 이
Operation이 답할 수 있는 Code 전부 -- OpenAPI의 이 Operation 응답 선언과 같은 집합이며
`tests/test_story_1_11.py`가 둘이 같은지 확인한다:

| Code | Status | `Retry-After` |
|------|--------|---------------|
| `invalid_request` | 422 | -- |
| `invalid_event_cursor` | 400 | -- |
| `conversation_expired` | 410 | -- |
| `rate_limited` | 429 | 60 |
| `stream_capacity_exceeded` | 503 | 5 |
| `deployment_unconfigured` | 503 | 없음 -- 재배포로만 해소된다 |

권한 없음·미존재 Resource는 Code도 본문도 없는 404다.

**`Retry-After`**: Load-shed 거부(`rate_limited`, `stream_capacity_exceeded`) 중
`retryable`이 참인 응답에는 `Retry-After` Header가 붙고, 값은 그 Window의 길이(초)다.
재연결 Backoff는 이 값을 써야 한다 -- 상한 값 자체는 본문에도 Header에도 담기지 않는다.
`deployment_unconfigured`에는 붙지 않는다: 기다려서 풀리는 상태가 아니다.

Stream이 **열린 뒤**의 실패는 HTTP 상태로 표현할 수 없으므로 Event로 온다:
`run.error`가 `ProviderFailureV1`(`kind`·한국어 `message`·`retryable`·
`correlation_id`)을 싣고, 그 앞에 `message.discarded`가, 뒤에 `run.status`와
`stream.end`가 온다.

## 예시

완료:

```
id: 1
event: run.status
data: {"schema_version":"1","type":"run.status","run_id":"3f1d…","sequence":1,"occurred_at":"2026-08-30T05:00:00Z","state":"running","stage":"streaming"}

id: 2
event: message.delta
data: {"schema_version":"1","type":"message.delta","run_id":"3f1d…","sequence":2,"occurred_at":"2026-08-30T05:00:00.4Z","message_id":"9a2c…","text":"안녕"}

id: 3
event: message.completed
data: {"schema_version":"1","type":"message.completed","run_id":"3f1d…","sequence":3,"occurred_at":"2026-08-30T05:00:01Z","message_id":"9a2c…","text":"안녕하세요"}

id: 4
event: run.status
data: {"schema_version":"1","type":"run.status","run_id":"3f1d…","sequence":4,"occurred_at":"2026-08-30T05:00:01Z","state":"completed","stage":"terminal"}

id: 5
event: stream.end
data: {"schema_version":"1","type":"stream.end","run_id":"3f1d…","sequence":5,"occurred_at":"2026-08-30T05:00:01Z","final_state":"completed","final_sequence":5}

```

만료 Tail:

```
id: 7
event: conversation.expired
data: {"schema_version":"1","type":"conversation.expired","run_id":"3f1d…","sequence":7,"occurred_at":"2026-08-30T06:00:00Z"}

id: 8
event: stream.end
data: {"schema_version":"1","type":"stream.end","run_id":"3f1d…","sequence":8,"occurred_at":"2026-08-30T06:00:00Z","final_state":"expired","final_sequence":8}

```
