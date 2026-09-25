from fastapi.testclient import TestClient

from aidd_chat.main import app


def test_policy_discloses_wiki_excerpts_and_wiki_name():
    with TestClient(app) as client:
        policy = client.get("/api/v1/policy").json()
    assert policy["retrieval_status"] == "wiki_readonly"
    assert policy["wiki_display_name"] == "local-test-wiki"
    assert policy["transmitted_fields"] == [
        "system_instruction", "current_message", "selected_prior_messages", "wiki_excerpts"]
