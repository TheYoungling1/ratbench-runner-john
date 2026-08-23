"""L1 validation (MANUAL — not in CI; Docker is slow/flaky in CI): run the Slice-A
deterministic chain against a REAL container on one repo and print the certified
states + the rendered replay spine.

    python3 scripts/l1_engine_swap_smoke.py <repo_path> <base_image>

Wiring: build the dep-graph via the existing build path, bind run_blocks' executor
callables to the real `docker exec` adapter the orchestrator already constructs
(sandbox_execute: (cmd)->(ok,out), exec_readonly: (cmd)->(rc,out)), run block_emit,
then render compile_replay_blocks(certified_graph). Reuse — do NOT reimplement — the
container adapter and the graph builder.
"""
# Implementer: this is a thin driver, not a unit test. Import the existing container
# exec adapter + dep-graph builder used by the orchestrator/build path, run block_emit,
# print each node's certified state and render_setup_sh(compile_replay_blocks(graph)).
# If the exact exec-adapter / build_dep_graph entry points are unclear, report it and
# STOP (do not guess a Docker API). Excluded from pytest collection by living in scripts/.
