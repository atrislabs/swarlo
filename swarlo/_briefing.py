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


# ── v2: TF-IDF cosine (Phase 2) ───────────────────────────────────

def v2_tfidf(task: str, candidates: list[str]) -> list[float]:
    """Rank candidates by TF-IDF cosine similarity to the task.

    Builds the IDF over (candidates + task) so discriminative terms
    surface: a word that appears in every post has zero weight, a word
    unique to a few posts that also appears in the task gets high weight.

    This is a text-level analog of the paper's attention-score ranking:
    both highlight positions the query is selectively attending to.
    """
    docs = [_tokenize(c) for c in candidates]
    q_tokens = _tokenize(task)
    if not q_tokens:
        return [0.0] * len(candidates)

    # Document frequency across the candidate pool. Include the query itself
    # so a term only present in the query and one post still carries signal.
    n_docs = len(docs) + 1
    df: Counter[str] = Counter()
    for d in docs:
        df.update(set(d))
    df.update(set(q_tokens))

    def idf(term: str) -> float:
        return math.log((n_docs + 1) / (df.get(term, 0) + 1)) + 1.0

    def tfidf_vec(tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        tf = Counter(tokens)
        n = sum(tf.values())
        return {t: (count / n) * idf(t) for t, count in tf.items()}

    def norm(v: dict[str, float]) -> float:
        return math.sqrt(sum(x * x for x in v.values()))

    def cosine(a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        na, nb = norm(a), norm(b)
        if na == 0 or nb == 0:
            return 0.0
        common = set(a).intersection(b)
        dot = sum(a[k] * b[k] for k in common)
        return dot / (na * nb)

    q_vec = tfidf_vec(q_tokens)
    return [cosine(q_vec, tfidf_vec(d)) for d in docs]


# ── Dispatcher ────────────────────────────────────────────────────

SCORERS = {
    "regex": v1_regex,
    "tfidf": v2_tfidf,
}


def score(task: str, candidates: list[str], scorer: str = "tfidf") -> list[float]:
    """Score each candidate against the task. Defaults to v2 (TF-IDF)."""
    fn = SCORERS.get(scorer, v2_tfidf)
    return fn(task, candidates)
