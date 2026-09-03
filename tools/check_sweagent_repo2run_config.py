#!/usr/bin/env python3
"""Validate producers/sweagent_repo2run_config.yaml against the INSTALLED SWE-agent.

No Docker, no API key, no network, no cost. Run under the sweagent venv:

    $SWEAGENT_VENV_PY tools/check_sweagent_repo2run_config.py

Why this exists: that config is a port of the Repo2Run paper's appendix I.2 onto a SWE-agent
version whose schema has moved. Three of the paper's six tool bundles no longer exist upstream and
were substituted; the parser and history processor were kept. Whether the result actually LOADS is
a property of the installed package, not of anything a unit test in this repo can see.

`RunSingleConfig(**cfg)` runs the full pydantic validation and `RunSingle.from_config` resolves the
tool bundles — both happen BEFORE `env.start()` touches Docker. So a wrong bundle path, a renamed
field, or a dropped parser type fails here in seconds instead of after a container pull and a
paid-for LLM call.

Exit 0 = the config is loadable and the bundles resolve. Exit 1 = it would have failed at runtime.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "producers" / "sweagent_repo2run_config.yaml"


def main() -> int:
    if sys.version_info < (3, 11):
        print(f"FAIL: sweagent needs Python >=3.11; this is {sys.version.split()[0]}.\n"
              "      Run me under $SWEAGENT_VENV_PY, not the runner venv.")
        return 1
    try:
        import yaml
        from sweagent.agent.problem_statement import TextProblemStatement
        from sweagent.run.run_single import RunSingle, RunSingleConfig
    except ImportError as exc:
        print(f"FAIL: cannot import sweagent ({exc}).\n"
              "      pip install 'git+https://github.com/SWE-agent/SWE-agent.git' into this venv.")
        return 1

    with open(CONFIG) as fh:
        cfg = yaml.safe_load(fh)
    print(f"loaded {CONFIG.relative_to(REPO_ROOT)}")

    # A throwaway git repo so `repo: type: local` validation passes without touching the network.
    tmp = tempfile.mkdtemp(prefix="sweagent-cfgcheck-")
    repo_dir = Path(tmp) / "repo"          # basename MUST be `repo` — see repo_clone_dir()
    repo_dir.mkdir(parents=True)
    os.system(f"cd {repo_dir} && git init -q && git commit -q --allow-empty -m init")

    cfg["env"]["repo"]["path"] = str(repo_dir)
    cfg["output_dir"] = tmp
    cfg["problem_statement"] = TextProblemStatement(text="config check", id="cfgcheck")

    try:
        config = RunSingleConfig(**cfg)
    except Exception as exc:
        print(f"FAIL: config rejected by RunSingleConfig — {type(exc).__name__}: {exc}")
        return 1
    print("ok: RunSingleConfig validation passed")

    # Same guard the runner uses: from_config rewrites bare literals that collide with real
    # directories in the cwd (`type: docker` vs our repo's docker/). Validate from an empty dir.
    safe_cwd = Path(tmp) / ".sweagent_cwd"
    safe_cwd.mkdir(exist_ok=True)
    os.chdir(safe_cwd)

    try:
        runner = RunSingle.from_config(config)
    except Exception as exc:
        print(f"FAIL: RunSingle.from_config — {type(exc).__name__}: {exc}\n"
              "      This is where a bad tools/ bundle path surfaces.")
        return 1
    print("ok: RunSingle.from_config resolved (tool bundles exist)")

    # Report back what the config actually resolved to, so a silent default is visible.
    model = cfg["agent"]["model"]
    tools = cfg["agent"]["tools"]
    print("\nresolved:")
    print(f"  model            {model.get('name')}")
    print(f"  temperature      {model.get('temperature')}   (paper: 0.2; sweagent default: 0.0)")
    print(f"  parser           {tools.get('parse_function', {}).get('type')}")
    print(f"  bundles          {[b['path'] for b in tools.get('bundles', [])]}")
    print(f"  history          {cfg['agent'].get('history_processors')}")
    print(f"  deployment image {cfg['env']['deployment'].get('image')}")

    # The repo-landing coupling the runner asserts at run time — check it here too, for free.
    landed = getattr(getattr(runner.env, "repo", None), "repo_name", None)
    print(f"  repo lands at    /{landed}")
    if landed != "repo":
        print("\nFAIL: the prompt hardcodes /repo but the repo would land elsewhere.")
        return 1

    print("\nPASS — config is loadable. Docker and a key are still needed for a real smoke.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
