"""Python ecosystem provider — a pass-through wrapper over ``build.py``.

Delegates Phase 1/2 to the module-level helpers extracted from
``build_dep_graph`` (``_python_package_obligations`` / ``_python_native_obligations``)
and adds NO behavior. ``certify_mode`` is INSTALL (each Package node certified by
one check_command). Importing this module pulls in ``build.py`` (already fully
loaded whenever ``build_dep_graph`` dispatches), so there is no import cycle.
"""

from __future__ import annotations

from pathlib import Path

from ecosystems.base import CertifyMode, ClosureMode
from python_deps.depgraph.build import (
    _project_build_manifest,
    _python_native_obligations,
    _python_package_obligations,
)
from python_deps.depgraph.executor import Executor
from python_deps.depgraph.repair import RecordProvider
from python_deps.depgraph.schema import DepGraph
from python_deps.evidence import collect_python_dependency_evidence


class PythonProvider:
    name = "python"
    certify_mode = CertifyMode.INSTALL

    def detect(self, repo: str) -> float:
        # Spec delegation table: _project_build_manifest + evidence.
        # collect_python_dependency_evidence ("does this repo declare/import
        # Python?"). Manifest -> 1.0; else ANY declared dependency OR import
        # (covers requirements.txt/setup.cfg/constraints even with no *.py) -> 0.8;
        # else 0.0. Dispatch still never rejects a repo: Task 7 passes this provider
        # as select_provider(..., default=), so a 0.0 repo falls back to Python.
        if _project_build_manifest(repo) is not None:
            return 1.0
        evidence = collect_python_dependency_evidence(repo)
        if evidence.declared_dependencies or evidence.imports:
            return 0.8
        return 0.0

    def closure_mode_for(self, repo: str) -> ClosureMode:
        if (Path(repo) / "uv.lock").is_file():
            return ClosureMode.LOCK
        return ClosureMode.RESOLVE

    def package_obligations(
        self,
        repo: str,
        container_executor: Executor,
        *,
        host_executor: Executor | None = None,
        target_python: str | None = None,
        target_platform: str | None = None,
        exclude_newer: str | None = None,
        needed_extras: frozenset[str] = frozenset(),
        record_provider: RecordProvider | None = None,
    ) -> tuple[DepGraph, list, object, str | None]:
        return _python_package_obligations(
            repo,
            container_executor,
            host_executor=host_executor,
            target_python=target_python,
            target_platform=target_platform,
            exclude_newer=exclude_newer,
            needed_extras=needed_extras,
            record_provider=record_provider,
        )

    def native_obligations(self, graph: DepGraph, container_executor: Executor) -> DepGraph:
        return _python_native_obligations(graph, container_executor)

    def service_obligations(
        self, graph: DepGraph, repo: str, service_classifier: object | None = None
    ) -> DepGraph:
        if service_classifier is None:
            return graph
        return service_classifier(graph, repo)
