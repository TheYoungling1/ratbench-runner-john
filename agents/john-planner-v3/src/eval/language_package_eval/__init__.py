"""Language-agnostic PACKAGE-layer fidelity eval (Phase 1).

Measures whether the construction-only PACKAGE closure captures what a working
test env actually needs. Members:

- ``coverage``               — ``build_graph_construction_only`` + ``base_image_for_repo``
                               (the construction call) and the coverage grader.
- ``oracle``                 — held-out human-recipe grader used by ``coverage``.
- ``run_ours_pkg``           — OUR side: emit the PACKAGE closure per repo.
- ``compare_pkg``            — OURS vs the agent-configured ``pip freeze`` (collect-oracle).
- ``compare_collect_vs_run`` — collect-oracle vs RUN-oracle recall delta.

Python today; Node/Rust package evals land here too (the multi-language seam).
"""
