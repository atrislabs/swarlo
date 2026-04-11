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
