# Program

Improve Swarlo worker routing so builder and validator members claim the right tasks more often and ignore the wrong ones more consistently. Focus on one bounded target at a time inside the worker-routing loop, measure routing quality on fixed replay cases, keep only changes that improve the score, and revert regressions immediately.
