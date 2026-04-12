"""Task-guided post ranking for /briefing.

Two scorers share the same interface:
    score(task: str, candidates: list[str]) -> list[float]

- v1_regex  — file-path + keyword overlap (original implementation)
- v2_tfidf  — TF-IDF cosine similarity with light stemming (Phase 2)

Phase 3 (attention-based compaction against a local model) will slot in
as v3_attention but needs Sovereign running, so we defer it.

The paper (Geist 2026, atris/research/papers/latent-briefing.md) describes
the attention-level approach. v2_tfidf is a cheap proxy: both give high
weight to discriminative terms that the task and the relevant post share.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# ── Tokenization + light stemming ─────────────────────────────────

_STOPWORDS = frozenset({
    'the','a','an','is','are','was','were','be','been','being','and','or','but',
    'in','on','at','to','for','of','with','by','from','this','that','it','its',
    'not','no','do','does','did','will','would','should','can','could','may','must',
    'has','have','had','having','all','each','every','any','some','into','about',
    'up','out','if','then','than','so','as','just','also','how','what','when',
    'where','which','who','why','very','too','only','own','same','few','more',
    'most','other','such','i','me','my','myself','we','our','you','your','he',
    'him','his','she','her','they','them','their','us','im','ive','dont','cant',
    'there','here','now','am','been','been','get','got','one','two','like',
    'thing','think','something','anything','everyone','someone','up','down','off',
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-word, drop short/stop, apply light stem."""
    raw = re.findall(r'[a-zA-Z_/.][\w./-]{2,}', text.lower())
    out = []
    for tok in raw:
        tok = tok.strip('.-_/')
        if not tok or len(tok) < 3:
            continue
        if tok in _STOPWORDS:
            continue
        out.append(_stem(tok))
    return out


def _stem(tok: str) -> str:
    """Tiny suffix stripper. Closes 'dropping'↔'dropped'↔'drops' gaps.

    Not Porter — just the four suffixes that matter most for engineering
    chatter. Order matters: strip longest first.
    """
    for suf in ('ing', 'tion', 'ies', 'ied', 'ers', 'ed', 'es', 'er', 's'):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


# ── v0: random baseline (Codex-proposed falsification test) ──────
#
# Codex flagged this at the start of Phase 2 as the simplest
# falsifier: if a random permutation of candidates matches a
# "smart" scorer's recall at the same K, the smart scorer is a
# bench artifact, not a real win. v0 exists only to run that check.
#
# Deterministic per-call via Python's hash(task) so the bench is
# reproducible but different tasks get different orderings.

def v0_random(task: str, candidates: list[str]) -> list[float]:
    """Return scores that induce a random permutation of candidates.

    Uses a task-seeded RNG so repeated calls with the same task
    produce the same ordering — lets the bench control variance.
    """
    import random as _random
    rng = _random.Random(hash(task) & 0xFFFFFFFF)
    n = len(candidates)
    # A random permutation is achieved by random scores in [0,1]
    return [rng.random() for _ in range(n)]


# ── v1: original regex + overlap (kept for A/B testing) ───────────

def v1_regex(task: str, candidates: list[str]) -> list[float]:
    """Original scorer: file-path regex + keyword fraction. Reproduces
    the behaviour of the shipped /briefing endpoint so benchmarks can
    compare directly.
    """
    file_pats = re.findall(r'[\w./]+\.(?:py|ts|js|md|json|yaml|sql)\b', task)
    dir_pats = re.findall(r'(?:backend|atris|swarlo|scripts|tests)/[\w/]+', task)
    paths = list(set(file_pats + dir_pats))
    words = set(w.lower() for w in re.findall(r'\b\w{3,}\b', task)) - _STOPWORDS

    scores = []
    for c in candidates:
        s = 0.0
        lc = c.lower()
        for p in paths:
            if p.lower() in lc:
                s += 3.0
        if words:
            matches = sum(1 for w in words if w in lc)
            s += min(matches / len(words) * 2.0, 2.0)
        scores.append(s)
    return scores


# ── TF-IDF primitives (shared by v2 and v3) ───────────────────────

def _tfidf_vec_fn(docs: list[list[str]], q_tokens: list[str]):
    """Build IDF over (docs + query) and return (vec_fn, cosine_fn).

    Splitting this lets v2 (single-pass) and v3 (two-pass PRF) share
    the same IDF statistics so their scores are directly comparable.
    """
    n_docs = len(docs) + 1
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    df.update(set(q_tokens))

    def idf(term: str) -> float:
        return math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0

    def vec(tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        tf = Counter(tokens)
        n = sum(tf.values())
        return {t: (count / n) * idf(t) for t, count in tf.items()}

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        na = math.sqrt(sum(x * x for x in a.values()))
        nb = math.sqrt(sum(x * x for x in b.values()))
        if na == 0 or nb == 0:
            return 0.0
        common = set(a).intersection(b)
        dot = sum(a[k] * b[k] for k in common)
        return dot / (na * nb)

    return vec, cosine, idf


# ── v2: TF-IDF cosine (Phase 2) ───────────────────────────────────

def v2_tfidf(task: str, candidates: list[str]) -> list[float]:
    """Rank candidates by TF-IDF cosine similarity to the task.

    Text-level analog of the paper's attention-score ranking: both
    highlight positions the query is selectively attending to.
    """
    docs = [_tokenize(c) for c in candidates]
    q_tokens = _tokenize(task)
    if not q_tokens:
        return [0.0] * len(candidates)
    vec, cosine, _ = _tfidf_vec_fn(docs, q_tokens)
    q_vec = vec(q_tokens)
    return [cosine(q_vec, vec(d)) for d in docs]


# ── v3: Pseudo-Relevance Feedback (Ouro-inspired two-pass) ────────
#
# Ouro (arxiv:2510.25741) argues that a looped smaller model can match
# a deeper one — depth-via-iteration. In IR, Pseudo-Relevance Feedback
# (PRF) is the classical analog: a second pass of a cheap retriever,
# using the first pass's top results to expand the query, often beats
# a single pass of a more sophisticated ranker. Same pattern, different
# domain. This is a text-level experiment on whether looping matters
# for our coordination-graph retrieval case.
#
# Rocchio-style expansion with the γ=0 (negative-feedback-off) variant:
#     q_new = α · q_original + β · centroid(top_k_docs)
# We set α=1.0, β=0.5, k=3, exp_terms=5.

def v3_prf_tfidf(
    task: str,
    candidates: list[str],
    k_feedback: int = 3,
    n_expand: int = 5,
    beta: float = 0.5,
) -> list[float]:
    """Two-pass TF-IDF with Rocchio pseudo-relevance feedback.

    Pass 1: rank candidates with v2_tfidf's vectors.
    Expand: take top k_feedback candidate tokens, pick the n_expand
            highest-IDF terms NOT already in the query, add them to
            the query vector at weight beta.
    Pass 2: rescore with the expanded query vector.

    Returns pass-2 scores, aligned with the input candidate order.
    """
    docs = [_tokenize(c) for c in candidates]
    q_tokens = _tokenize(task)
    if not q_tokens:
        return [0.0] * len(candidates)
    vec, cosine, idf = _tfidf_vec_fn(docs, q_tokens)

    # ── Pass 1 ────────────────────────────────────────────────
    q_vec = vec(q_tokens)
    pass1 = [cosine(q_vec, vec(d)) for d in docs]
    if not any(s > 0 for s in pass1):
        return pass1  # nothing to expand from — bail out

    # ── Expand query from pseudo-relevant top-k ───────────────
    # Only pool from docs that actually scored above zero. Pooling from
    # zero-score docs causes query drift — the canonical PRF failure
    # mode, where expansion terms come from random unrelated posts.
    nonzero = [(i, pass1[i]) for i in range(len(pass1)) if pass1[i] > 0]
    nonzero.sort(key=lambda x: -x[1])
    top_idx = [i for i, _ in nonzero[:k_feedback]]
    if not top_idx:
        return pass1

    # Centroid of top-k as an AVERAGED TF-IDF vector, minus terms in q.
    # Fix: previous version summed without averaging, so effective
    # expansion strength scaled with k_feedback (β=0.5 k=3 → 1.5× expansion).
    # Dividing by len(top_idx) makes the centroid scale-invariant.
    centroid: dict[str, float] = {}
    for i in top_idx:
        for term, weight in vec(docs[i]).items():
            centroid[term] = centroid.get(term, 0.0) + weight
    k = len(top_idx)
    for term in list(centroid):
        centroid[term] /= k
        if term in q_vec:
            # Don't re-inject terms already in the query — that's just
            # reinforcement. PRF wants to discover *new* discriminators.
            centroid.pop(term, None)

    # Rank expansion terms by centroid weight alone. Previous version
    # multiplied by idf(term) — but centroid weight is ALREADY TF·IDF,
    # so multiplying by IDF again squared the rare-term bias. Classic
    # Rocchio selects by the centroid weight.
    scored_terms = sorted(
        centroid.items(),
        key=lambda kv: -kv[1],
    )[:n_expand]

    # Build expanded query: α·q + β·expansion
    expanded = dict(q_vec)
    for term, weight in scored_terms:
        expanded[term] = expanded.get(term, 0.0) + beta * weight

    # ── Pass 2 ────────────────────────────────────────────────
    return [cosine(expanded, vec(d)) for d in docs]


# ── v4: Gated PRF (Ouro "learned depth" analog) ────────────────────
#
# The v3 PRF experiment (see tick 13 in atris/research/papers/
# ouro-looped-lm.md) lost 35pp of recall@5 on the symptom-style
# benchmark. Diagnosis: naive iteration. Ouro's real claim is not
# "iteration helps" but "*learned depth allocation* helps" — the
# model decides how much compute to spend per input. Fixed-k fixed-β
# PRF iterates blindly.
#
# v4 tries the smallest possible instance of learned depth: a
# hand-coded signal-quality gate on pass-1. Expand only if:
#   (1) top-1 score clears a floor (pass-1 is not all noise), AND
#   (2) top-k score is at least tau_ratio × top-1 (top-k is a
#       coherent cluster, not a single real hit + noise).
# Otherwise fall back to v2 single-pass scores.
#
# This is not a learned gate — it's a hand-tuned one. If it beats
# both v2 and v3, that justifies building a learned variant later.
# If it only matches v2, that's still a win: it proves the gating
# *concept* is load-bearing.

def v4_prf_gated(
    task: str,
    candidates: list[str],
    k_feedback: int = 3,
    n_expand: int = 5,
    beta: float = 0.5,
    tau_margin: float = 0.15,
) -> list[float]:
    """Gated PRF: only run expansion when pass-1 has a clear precision edge.

    Gate: normalized margin between the k-th and (k+1)-th best scores
    must exceed tau_margin. Concretely:
        (topk - top_{k+1}) / top1 >= tau_margin
    where top1 must also be strictly positive.

    Rationale: on adversarial retrieval (many posts share weak vocabulary
    with the query), pass-1 produces a flat score distribution where
    top-k bleeds straight into the noise floor. Pooling from top-k then
    contaminates the expansion with near-miss terms. A real precision
    edge — a visible gap between "inside the pool" and "just outside" —
    is what distinguishes a trustworthy pool from a noisy one.

    When the gate fails, returns pass-1 scores unchanged (v2 behavior).
    This is the Ouro "learned depth" principle reduced to its smallest
    hand-coded form: decide per-input whether looping is worth it.
    """
    docs = [_tokenize(c) for c in candidates]
    q_tokens = _tokenize(task)
    if not q_tokens:
        return [0.0] * len(candidates)
    vec, cosine, idf = _tfidf_vec_fn(docs, q_tokens)

    q_vec = vec(q_tokens)
    pass1 = [cosine(q_vec, vec(d)) for d in docs]
    nonzero_sorted = sorted((s for s in pass1 if s > 0), reverse=True)

    # Need at least k+1 nonzero results to measure the precision edge
    if len(nonzero_sorted) < k_feedback + 1:
        return pass1

    top1 = nonzero_sorted[0]
    topk = nonzero_sorted[k_feedback - 1]
    top_kp1 = nonzero_sorted[k_feedback]

    if top1 <= 0:
        return pass1

    margin = (topk - top_kp1) / top1
    if margin < tau_margin:
        return pass1  # no precision edge → pool would drift → stay at v2

    # ── Expansion (same as v3, with the averaging + IDF fixes) ──
    nonzero_idx = sorted(
        (i for i in range(len(pass1)) if pass1[i] > 0),
        key=lambda i: -pass1[i],
    )
    top_idx = nonzero_idx[:k_feedback]

    centroid: dict[str, float] = {}
    for i in top_idx:
        for term, weight in vec(docs[i]).items():
            centroid[term] = centroid.get(term, 0.0) + weight
    k_actual = len(top_idx)
    for term in list(centroid):
        centroid[term] /= k_actual
        if term in q_vec:
            centroid.pop(term, None)

    # Rank by centroid weight only — not centroid * idf (double-counts).
    scored_terms = sorted(
        centroid.items(),
        key=lambda kv: -kv[1],
    )[:n_expand]

    expanded = dict(q_vec)
    for term, weight in scored_terms:
        expanded[term] = expanded.get(term, 0.0) + beta * weight

    return [cosine(expanded, vec(d)) for d in docs]


# ── Dispatcher ────────────────────────────────────────────────────

SCORERS = {
    "random": v0_random,  # falsification baseline only — do not default
    "regex": v1_regex,
    "tfidf": v2_tfidf,
    "prf": v3_prf_tfidf,
    "prf_gated": v4_prf_gated,
}


def score(task: str, candidates: list[str], scorer: str = "tfidf") -> list[float]:
    """Score each candidate against the task. Defaults to v2 (TF-IDF)."""
    fn = SCORERS.get(scorer, v2_tfidf)
    return fn(task, candidates)
