"""Unit tests for the briefing scorers in swarlo._briefing.

These run against the pure-Python scorer without a live server, so they
are fast (<10ms each) and can cover edge cases that the integration
benchmark doesn't stress.
"""
from swarlo import _briefing


def test_tokenize_drops_stopwords_and_stems():
    toks = _briefing._tokenize("The proposals were getting dropped under load")
    # 'the', 'under' are stopwords; 'proposals' → 'proposal' via -s stem;
    # 'getting' → 'gett' via -ing stem; 'dropped' → 'dropp' via -ed stem
    assert "the" not in toks
    assert "under" not in toks
    assert "proposal" in toks
    assert "load" in toks
    # 'were' is a stopword
    assert "were" not in toks


def test_stem_closes_tense_gap():
    # This is the specific gap that cost the regex scorer recall in the bench
    assert _briefing._stem("dropping") == _briefing._stem("dropped")
    assert _briefing._stem("proposals") == _briefing._stem("proposal")


def test_tfidf_ranks_topical_posts_above_unrelated():
    task = "fix the quota bug in the improvement endpoint"
    candidates = [
        "Shipped a new onboarding email for Marty.",
        "The improvement router drops requests when quota is exceeded.",
        "Browser agent v3 captcha flow hardened.",
        "Found a missing auth check on the quota endpoint in improve router.",
        "Daily digest service landed with the cascade.",
    ]
    scores = _briefing.v2_tfidf(task, candidates)
    # The two topical posts (indexes 1, 3) should outrank the three unrelated ones
    ranked = sorted(range(len(candidates)), key=lambda i: -scores[i])
    assert ranked[0] in (1, 3), f"expected topical post first, got ranking {ranked}"
    assert ranked[1] in (1, 3), f"expected topical post second, got ranking {ranked}"


def test_tfidf_returns_zero_when_no_overlap():
    scores = _briefing.v2_tfidf(
        "solana wallet integration",
        ["stripe webhook retry logic", "gmail oauth renewal"],
    )
    assert all(s == 0.0 for s in scores)


def test_tfidf_empty_task_returns_zero_vector():
    scores = _briefing.v2_tfidf("", ["any post", "another post"])
    assert scores == [0.0, 0.0]


def test_v1_regex_matches_file_paths():
    scores = _briefing.v1_regex(
        "Fix the bug in backend/routers/improve.py line 42",
        [
            "Touched backend/routers/improve.py yesterday",
            "Unrelated solana wallet work",
        ],
    )
    assert scores[0] > scores[1]
    assert scores[0] >= 3.0  # file-path bonus is 3.0


def test_dispatcher_defaults_to_tfidf():
    scores_default = _briefing.score("foo bar", ["foo bar baz"])
    scores_tfidf = _briefing.v2_tfidf("foo bar", ["foo bar baz"])
    assert scores_default == scores_tfidf


def test_dispatcher_can_select_regex():
    scores = _briefing.score("backend/routers/x.py", ["touched backend/routers/x.py"], scorer="regex")
    assert scores[0] >= 3.0


# ── v3 PRF ──

def test_v3_prf_amplifies_topical_posts_when_signal_is_clean():
    """When the top-k is genuinely relevant, PRF expansion should
    strengthen the ranking of related posts."""
    task = "fix the quota bug in the improvement endpoint"
    cands = [
        "Onboarding email for Marty shipped.",
        "The improvement router drops requests when quota is exceeded.",
        "Browser agent v3 captcha flow hardened.",
        "Found a missing auth check on the quota endpoint in improve router.",
    ]
    v2 = _briefing.v2_tfidf(task, cands)
    v3 = _briefing.v3_prf_tfidf(task, cands)
    # The two topical posts (1, 3) should score at least as high in v3
    assert v3[1] >= v2[1] - 1e-9
    # Zero-score distractors must remain zero — no query drift
    assert v3[0] == 0.0
    assert v3[2] == 0.0


def test_v3_prf_does_not_drift_onto_zero_score_candidates():
    """Regression test for the query-drift bug caught in the first
    implementation: pooling from zero-score docs leaks unrelated terms
    into the expanded query and boosts unrelated candidates."""
    task = "solana wallet devnet integration"
    cands = [
        "Kubernetes pod autoscaling review.",  # unrelated
        "Merged the devnet wallet tool after testing on solana.",  # topical
        "Retry-on-503 added to the openrouter client.",  # unrelated
    ]
    v3 = _briefing.v3_prf_tfidf(task, cands)
    # Unrelated candidates must stay at zero. If they have any score,
    # the expansion drifted.
    assert v3[0] == 0.0
    assert v3[2] == 0.0
    assert v3[1] > 0.0


def test_v3_prf_bails_when_first_pass_is_all_zero():
    """If nothing in pass 1 has signal, there's nothing to feed back
    from. Must return zeros, not blow up."""
    scores = _briefing.v3_prf_tfidf(
        "kubernetes pod autoscaler",
        ["solana wallet", "gmail oauth renewal"],
    )
    assert scores == [0.0, 0.0]
