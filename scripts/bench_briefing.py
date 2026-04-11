#!/usr/bin/env python3
"""Benchmark swarlo /briefing quality and token efficiency.

Cannot validate an upgrade without a before-number. This script:
1. Spins up a local swarlo server against a temp SQLite file
2. Seeds a synthetic board with K=5 relevant posts + N=95 distractors
3. Calls /briefing with a task description and measures:
   - recall@5, recall@10  (how many of the 5 relevant were retrieved)
   - precision@5, precision@10
   - token_savings  (bytes returned / bytes total on board)
   - latency_ms
4. Prints a table.

Run: python scripts/bench_briefing.py [--n 100] [--k 5] [--iters 20]

This is the text-level analog benchmark for the paper's attention-level
compaction (Geist 2026, atris/research/papers/latent-briefing.md).
Phase 1 (regex) → Phase 2 (tf-idf) → Phase 3 (embeddings).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from swarlo import SwarloClient  # noqa: E402
from swarlo.sqlite_backend import SQLiteBackend  # noqa: E402


# ─── Synthetic corpus ───────────────────────────────────────────────

# Relevant posts describe the TOPIC but don't always name the target file.
# This mirrors how agents talk on real boards: symptoms and discoveries,
# not file-path dumps. A simple regex matcher will miss most of these.
RELEVANT_TEMPLATES = [
    "Got a 403 on the quota endpoint last night — traced to a missing member check.",
    "The improvement router was dropping requests silently when the agent_id was numeric.",
    "Added a regression: we now cover the zero-credit branch that the proposal engine was hitting.",
    "Hypothesis: the race we chased last week was in the self-improve loop, not the executor.",
    "Profile shows 80% of /api/improve latency is in the json.dumps step, not the model call.",
]

# Distractors are plausible engineering chatter on the same board.
# Some mention credits/auth/latency too — so keyword matching isn't enough.
DISTRACTOR_TEMPLATES = [
    "Shipped a new onboarding email for Marty.",
    "Stripe webhook 500 on subscription renewal — retry logic landed.",
    "Kurzweil corpus brief ready for next week's pitch.",
    "Solana wallet tool merged — devnet happy path works.",
    "Browser agent v3 flake on captcha flow, retry hardened.",
    "Pulse cycle consumed 12K tokens on the research path — investigating.",
    "Added metric: agents_active per hub. Dashboard wired.",
    "Kernel tuning on the autoresearch loop recovered 30% throughput.",
    "New MEMBER.md for navigator landed; persona tightened.",
    "Sovereign birth night — Ultra Resistance Megastructure on repeat.",
    "Cleaned up stale claims from the RLEF sprint — 17 expired.",
    "Fixed gemini flash import in morning_briefing service.",
    "Wrote a memo on the lab GTM model.",
    "Retry-on-503 added to the openrouter client.",
    "Daily digest service with the new cascade shipped.",
    "Credits facade refactor — still working through the bridge function.",
    "Auth check added to the new billing router path.",
    "Latency on the chat endpoint dropped 40% after removing the sync call.",
    "Memory leak in the long-running agent process, looks like tool retain cycle.",
    "SQL migration for the claims table ran clean on staging.",
]


def seed_board(base_url: str, hub: str, n_total: int, k_relevant: int, target_file: str, rng: random.Random) -> set[str]:
    """Post k_relevant posts mentioning target_file and n_total-k_relevant distractors.

    Uses fresh clients per seed member so posts are attributed to different authors.
    Returns the set of relevant content strings so the scorer can check hits.
    """
    n_distract = n_total - k_relevant
    target = target_file.replace("backend/routers/", "").replace("backend/services/", "").replace("backend/tools/core/", "").replace(".py", "")

    clients = []
    for mid, mname in [("bench_alice", "Alice"), ("bench_bob", "Bob"), ("bench_carol", "Carol")]:
        c = SwarloClient(base_url, hub=hub)
        c.join(mid, name=mname)
        clients.append(c)

    posts = []
    for tpl in RELEVANT_TEMPLATES[:k_relevant]:
        posts.append(("relevant", tpl.format(target=target)))
    for _ in range(n_distract):
        posts.append(("distractor", rng.choice(DISTRACTOR_TEMPLATES)))

    rng.shuffle(posts)

    relevant_contents: set[str] = set()
    for label, content in posts:
        c = rng.choice(clients)
        c.post("general", content, kind="message")
        if label == "relevant":
            relevant_contents.add(content)
    return relevant_contents


# ─── Measurement ────────────────────────────────────────────────────

def _score_one(client: SwarloClient, task: str, relevant_contents: set[str], scorer: str) -> dict:
    t0 = time.perf_counter()
    result = client.briefing(task, limit=10, scorer=scorer)
    latency_ms = (time.perf_counter() - t0) * 1000

    returned = result.get("posts", [])
    returned_contents = [p["content"] for p in returned]

    def hit(content: str) -> bool:
        return any(rel in content or content in rel for rel in relevant_contents)

    def recall_at(k: int) -> float:
        top_k = returned_contents[:k]
        hits = sum(1 for c in top_k if hit(c))
        return hits / max(len(relevant_contents), 1)

    def precision_at(k: int) -> float:
        top_k = returned_contents[:k]
        if not top_k:
            return 0.0
        hits = sum(1 for c in top_k if hit(c))
        return hits / len(top_k)

    return {
        "recall@5": recall_at(5),
        "recall@10": recall_at(10),
        "precision@5": precision_at(5),
        "precision@10": precision_at(10),
        "latency_ms": latency_ms,
        "n_returned": len(returned),
    }


def run_bench(n_total: int, k_relevant: int, iters: int, seed: int) -> dict:
    rng = random.Random(seed)
    import uvicorn
    from swarlo import server as srv

    tmpdir = tempfile.mkdtemp(prefix="swarlo_bench_")
    db_path = Path(tmpdir) / "bench.db"
    srv._BACKEND = None
    import os
    os.environ["SWARLO_DB"] = str(db_path)

    # Start server in background thread
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for server.started
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    try:
        hub = "bench"
        client = SwarloClient(base, hub=hub)
        key = client.join("bencher", name="Bencher")

        metrics_by_scorer: dict[str, list[dict]] = {"regex": [], "tfidf": [], "prf": []}
        for i in range(iters):
            # Each iter runs in its own hub so posts never leak across iters
            iter_hub = f"{hub}-{i}"
            target = rng.choice([
                "backend/routers/improve.py",
                "backend/routers/chat.py",
                "backend/services/credits.py",
                "backend/tools/core/swarlo_tool.py",
            ])
            relevant_contents = seed_board(base, iter_hub, n_total, k_relevant, target, rng)

            iter_client = SwarloClient(base, hub=iter_hub)
            iter_client.join("bench_reader", name="Reader")
            # Task describes the goal, not file paths — the realistic case
            task = (
                "I'm debugging the self-improvement loop. Users report proposals "
                "are getting dropped under load. I think there's an auth or "
                "quota problem in the improvement endpoint. Pull up anything "
                "the team has learned about this."
            )
            for scorer in ("regex", "tfidf", "prf"):
                metrics_by_scorer[scorer].append(
                    _score_one(iter_client, task, relevant_contents, scorer)
                )

        return {s: _aggregate(m) for s, m in metrics_by_scorer.items()}
    finally:
        server.should_exit = True
        thread.join(timeout=2)


def _aggregate(metrics: list[dict]) -> dict:
    if not metrics:
        return {}
    keys = metrics[0].keys()
    out = {}
    for k in keys:
        vals = [m[k] for m in metrics]
        out[f"{k}_mean"] = sum(vals) / len(vals)
        out[f"{k}_min"] = min(vals)
        out[f"{k}_max"] = max(vals)
    return out


# ─── Entry ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="total posts per iter")
    ap.add_argument("--k", type=int, default=5, help="relevant posts per iter")
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print(f"bench: n={args.n} k={args.k} iters={args.iters} seed={args.seed}")
    print("-" * 72)
    results = run_bench(args.n, args.k, args.iters, args.seed)

    # Side-by-side table: only the key metrics
    key_metrics = [
        ("recall@5_mean", "recall@5"),
        ("recall@10_mean", "recall@10"),
        ("precision@5_mean", "prec@5"),
        ("precision@10_mean", "prec@10"),
        ("n_returned_mean", "n_return"),
        ("latency_ms_mean", "lat_ms"),
    ]
    header = f"{'metric':12s}  {'v1 regex':>10s}  {'v2 tfidf':>10s}  {'v3 prf':>10s}  {'Δ v3-v2':>10s}"
    print(header)
    print("-" * 72)
    for field, label in key_metrics:
        r = results["regex"].get(field, 0.0)
        t = results["tfidf"].get(field, 0.0)
        p = results["prf"].get(field, 0.0)
        delta = p - t
        if "latency" in field:
            print(f"{label:12s}  {r:10.2f}  {t:10.2f}  {p:10.2f}  {delta:+10.2f}")
        else:
            print(f"{label:12s}  {r:10.3f}  {t:10.3f}  {p:10.3f}  {delta:+10.3f}")

    out_path = REPO_ROOT / "scripts" / "bench_briefing_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
