import json

CONVERSATION_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
INPUT_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
NOW = "2026-09-24T00:00:00Z"
SOURCE = {"path": "concepts/alpha.md", "title": "Alpha 개념", "confidence": "low", "contested": True}


def projection(state="queued", latest=0, content=None, failure=None, outcome="grounded", sources=None,
               search_truncated=False, uncovered=None):
    completed = state == "completed"
    message = None
    if completed:
        message = {"message_id": MESSAGE_ID, "content": content, "outcome": outcome,
                   "sources": sources if sources is not None else [], "search_truncated": search_truncated,
                   "uncovered": uncovered}
    return {
        "schema_version": "1", "run_id": RUN_ID, "conversation_id": CONVERSATION_ID, "input_message_id": INPUT_ID,
        "retry_of_run_id": None, "output_message_id": MESSAGE_ID if completed else None, "output_message": message,
        "state": state,
        "stage": "terminal" if state in {"completed", "failed", "timeout", "cancelled"} else ("streaming" if state == "running" else "queued"),
        "created_at": NOW, "last_updated_at": NOW, "latest_sequence": latest, "terminal_error": failure,
    }


def event(sequence, kind, **values):
    return {"schema_version": "1", "run_id": RUN_ID, "sequence": sequence, "occurred_at": NOW, "type": kind, **values}


async def install_event_source(page, events):
    payload = json.dumps({"events": events}, ensure_ascii=False)
    await page.add_init_script("const {events} = " + payload + ";" + """
      (() => {
        class FakeEventSource {
          constructor() { this.listeners = {}; this.closed = false; setTimeout(() => this.play(), 50); }
          addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
          play() {
            events.forEach((value, index) => setTimeout(() => {
              if (this.closed) return;
              for (const handler of this.listeners[value.type] || [])
                handler({lastEventId: String(value.sequence), data: JSON.stringify(value)});
            }, index * 10));
          }
          close() { this.closed = true; }
        }
        window.EventSource = FakeEventSource;
      })()
    """)


async def stub_api(page, final_projection):
    async def api(route):
        request = route.request
        if request.url.endswith("/api/v1/conversations"):
            await route.fulfill(json={"conversation_id": CONVERSATION_ID, "created_at": NOW,
                                      "expires_at": "2026-09-24T01:00:00Z"})
        elif request.method == "POST":
            await route.fulfill(status=202, json=projection())
        else:
            await route.fulfill(json=final_projection)

    await page.route("**/api/v1/conversations", api)
    await page.route("**/api/v1/conversations/*/runs", api)
    await page.route("**/api/v1/runs/*", api)
