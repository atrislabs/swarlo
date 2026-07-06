---
name: autoresearch
description: Karpathy-style keep/revert experiment loop for bounded targets with external metrics. Use with atris/experiments packs.
version: 1.0.0
---

# Autoresearch Skill

Use this when improving a bounded target through experiments instead of intuition.

## Loop

1. Read `program.md`
2. Identify the one bounded mutation target
3. Run `measure.py` to get the baseline
4. Apply one candidate change
5. Run the same metric again
6. Keep only if the score improves
7. Append the result to `results.tsv`
8. Revert regressions immediately

## Hard rules

- One target per experiment pack
- External metric only; no self-scored wins
- Keep/revert must be deterministic
- `results.tsv` stays append-only
- If the metric is noisy, document the margin before keeping

## In this repo

Run experiments from `atris/experiments/`.

Good first lanes:

- `worker-routing`
- claim conflict behavior
- summary quality
- CLI ergonomics with fixed replay cases

## Commands

```bash
atris experiments validate
atris experiments benchmark
atris experiments init <slug>
```

## Avoid

- changing multiple surfaces at once
- vague goals like "make it better"
- keeping a change without a measured improvement
