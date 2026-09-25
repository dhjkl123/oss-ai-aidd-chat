# scripts/run_sm_fixtures.py
"""PRD SM-7/SM-8/SM-9 and NFR-1 evidence against a running app bound to the real model.

Each fixture question runs in its own new Conversation over the public API (the same
contract the Web uses). Timing is measured client-side: acceptance -> first status,
and acceptance -> completed. Wiki cleanliness is `git status --porcelain` on WIKI_ROOT
before and after. The report is Markdown for the owner's human review of SM-7's
"no unsupported claims" check."""
import argparse
import json
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

POLL_TIMEOUT_S = 180  # Run deadline is 120 s; margin above it bounds a stuck poll loop.

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--wiki", required=True)
parser.add_argument("--timing-runs", type=int, default=20)
parser.add_argument("--out", default="docs/superpowers/reports/sm-fixtures.md")
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]


class _SM7Settings(BaseSettings):
    """Same env-file loader the app's own Settings classes use (bootstrap), so
    SM7_FIXTURES can live in .env like every other local knob. Real Wiki document
    paths never belong in a tracked fixture (default points at the gitignored
    local/ dir)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sm7_fixtures: str = "local/sm7_grounded.json"


sm7_path = ROOT / _SM7Settings().sm7_fixtures
if not sm7_path.is_file():
    sys.exit(
        f"SM-7 fixture not found: {sm7_path}\n"
        "Set SM7_FIXTURES (env or .env) to its path, or put the file at local/sm7_grounded.json."
    )


def wiki_status() -> str:
    return subprocess.run(["git", "-C", args.wiki, "status", "--porcelain"], capture_output=True,
                          text=True, check=True).stdout


def ask(client: httpx.Client, question: str) -> dict:
    origin = {"Origin": args.base_url}
    create = client.post("/api/v1/conversations", headers=origin)
    conversation = create.json()
    # The capability cookie is Secure; httpx (like a browser) won't attach a Secure
    # cookie to a plain-http request, so a local run over http://127.0.0.1 needs it
    # forwarded explicitly rather than relying on the client's cookie jar.
    capability = create.cookies.get("conversation_capability")
    auth = {**origin, "Cookie": f"conversation_capability={capability}"} if capability else origin
    started = time.monotonic()
    run = client.post(f"/api/v1/conversations/{conversation['conversation_id']}/runs",
                      headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
                      json={"kind": "question", "content": question}).json()
    first_status = time.monotonic() - started
    while run["state"] in ("queued", "running"):
        if time.monotonic() - started > POLL_TIMEOUT_S:
            run["state"] = "poll_timeout"  # record as failed and move on; never retry silently
            break
        time.sleep(0.2)
        run = client.get(f"/api/v1/runs/{run['run_id']}", headers=auth).json()
    return {"run": run, "first_status_s": first_status, "total_s": time.monotonic() - started}


before = wiki_status()
sm7 = json.loads(sm7_path.read_text(encoding="utf-8"))
sm8 = json.loads((ROOT / "tests/fixtures/sm8_miss.json").read_text(encoding="utf-8"))
lines = ["# SM fixture run", "", f"- base_url: {args.base_url}", ""]
with httpx.Client(base_url=args.base_url, timeout=150) as client:
    hits = 0
    lines += ["## SM-7 grounded", "", "| # | question | outcome | expected in sources | sources | answer |", "|---|---|---|---|---|---|"]
    for index, item in enumerate(sm7, 1):
        result = ask(client, item["question"])
        message = result["run"].get("output_message") or {}
        paths = [source["path"] for source in message.get("sources", [])]
        hit = item["expected_path"] in paths
        hits += hit
        answer = (message.get("content") or result["run"].get("terminal_error", {}) or {}).__str__().replace("\n", " ")
        lines.append(f"| {index} | {item['question']} | {message.get('outcome', result['run']['state'])} | {hit} | {', '.join(paths)} | {answer} |")
    lines += ["", f"**SM-7: {hits}/10 (target ≥ 9). Human review of unsupported claims: pending.**", ""]
    correct = 0
    general = 0
    lines += ["## SM-8 honest miss", "", "| # | question | expected | outcome | answer |", "|---|---|---|---|---|"]
    for index, item in enumerate(sm8, 1):
        result = ask(client, item["question"])
        message = result["run"].get("output_message") or {}
        outcome = message.get("outcome", result["run"]["state"])
        correct += outcome == item["expected"]
        general += outcome in ("grounded", "partial")
        lines.append(f"| {index} | {item['question']} | {item['expected']} | {outcome} | {message.get('content', '')} |")
    lines += ["", f"**SM-8: classified {correct}/10 (target ≥ 8); general-knowledge answers {general} (target 0).**", ""]
    firsts, totals = [], []
    for _ in range(args.timing_runs):
        result = ask(client, sm7[_ % len(sm7)]["question"])
        firsts.append(result["first_status_s"])
        totals.append(result["total_s"])
    totals.sort()
    p95 = totals[max(0, int(len(totals) * 0.95) - 1)]
    lines += ["## NFR-1 timing", "", f"- runs: {len(totals)}", f"- max time to first status: {max(firsts):.2f}s (target ≤ 2s)",
              f"- p50 completed: {statistics.median(totals):.1f}s (target 15s)", f"- p95 completed: {p95:.1f}s (target 45s)", ""]
after = wiki_status()
lines += ["## SM-9 read-only", "", f"- git status before == after: {before == after}", f"- dirty after: {bool(after.strip())}", ""]
out = ROOT / args.out
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(lines), encoding="utf-8")
print(out)
