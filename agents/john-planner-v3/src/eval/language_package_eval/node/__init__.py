"""Node/JS package-fidelity eval (LOCK-mode mirror of the Python package eval).

Node is a LOCK ecosystem: ``package-lock.json`` IS the fully-resolved transitive
closure, so recall against the installed tree is ~1.0 by construction. The story
here is PRECISION: the lockfile lists every platform's optional binary, but
``npm ci`` installs only the target-matching subset. The os/cpu/libc filter
(``platform_filter``) is the fix under test — the Node analogue of Python's
PEP 508 environment markers.

Pure-logic slice (container-free): ``lockfile`` (parse) + ``platform_filter``
(the target filter) + ``run_ours_node`` (OURS closure) + ``compare_node``
(recall/precision). The Docker ORACLE (agent-configured ``npm ci`` + disk walk)
is a later slice. When the Node EcosystemProvider lands (seam Slice 3),
``run_ours_node`` swaps its inline parse for ``NodeProvider.package_obligations``
and ``platform_filter`` promotes into ``ecosystems/node/``.

Design: docs/superpowers/specs/2026-07-04-node-package-fidelity-eval-design.md
"""
