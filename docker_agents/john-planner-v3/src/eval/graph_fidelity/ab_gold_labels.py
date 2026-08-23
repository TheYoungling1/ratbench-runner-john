"""Gold labels for the root-selection A/B (Track A) — the 16 live-probed repos.

Each repo maps NON-declared external imports to a ground-truth label, hand-classified from the
4 live-probe subagent reports (2026-07-03). Only non-declared externals need a label; declared
imports are covered by definition and never diverge.

Label vocabulary (see root_selection_ab.BENIGN_LABELS / NEEDED_LABEL):
  needed            — a genuine runtime dep the repo under-declared, not otherwise present
                      (the generator SHOULD add it; the verifier would miss it). The GOOD add.
  optional          — try/except ImportError or runtime-setting-gated optional backend.
  typing            — used only under `if TYPE_CHECKING:` (no runtime footprint).
  transitive        — imported directly but installed via another declared dep's closure.
  alias_of_declared — the dist IS declared, code imports it under a different module name.
  tooling           — dev/test/build tool or maintainer script (not a runtime dep).
  local             — first-party / example / nested-subproject module (not a PyPI dist).
  stdlib            — actually standard library.
Every label except `needed` means a divergent add is a BAD add (over-install).

Provenance: the classifications are copied from the batch reports; a handful become divergence
(their lowercased name is a curated-table key AND the dist is undeclared) — notably requests'
`OpenSSL`→pyOpenSSL and scrapy's `PIL`→Pillow (optional backends the GENERATOR force-installs).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# full_name -> {import_name (as scanned, original case): label}
AB_GOLD: dict[str, dict[str, str]] = {
    "UlionTse/translators": {"setuptools": "tooling"},
    "psf/requests": {
        "OpenSSL": "optional",       # help.py: guarded pyopenssl diagnostics -> curated pyOpenSSL
        "cryptography": "optional",  # guarded legacy no-SNI path
        "setuptools": "tooling",     # setup.py self-import
        "simplejson": "optional",    # compat.py try/except json fallback
        "typing_extensions": "typing",
        "chardet": "optional",       # use_chardet_on_py3 extra, guarded compat import
    },
    "pallets/click": {},
    "psf/cachecontrol": {
        "filelock": "optional",  # filecache extra, guarded lazy import
        "redis": "optional",     # redis extra, TYPE_CHECKING/lazy
    },
    "pallets/flask": {"asgiref": "optional"},  # async extra, guarded
    "encode/httpx": {
        "sniffio": "optional",  # asgi.py try/except, trio detection
        # cli/http2/socks/zstd/brotli optional extras, all guarded imports:
        "brotli": "optional", "brotlicffi": "optional", "h2": "optional",
        "socksio": "optional", "zstandard": "optional",
        "click": "optional", "pygments": "optional", "rich": "optional",  # cli extra
    },
    "encode/starlette": {  # all full-extra deps, guarded imports
        "multipart": "alias_of_declared", "python_multipart": "alias_of_declared",
        "yaml": "alias_of_declared",  # PyYAML
        "itsdangerous": "optional", "jinja2": "optional",
        "httpx": "optional", "httpx2": "optional",
    },
    "tiangolo/typer": {  # docs_src tutorial sibling scripts on sys.path — first-party
        "items": "local", "lands": "local", "reigns": "local",
        "towns": "local", "users": "local",
    },
    "simonw/datasette": {
        "importlib_metadata": "optional",   # version-gated backport
        "importlib_resources": "optional",
        "ipdb": "optional",                 # try/except pdb swap
        "markupsafe": "transitive",         # via Jinja2
        "pydantic_core": "transitive",      # via pydantic
        "pysqlite3": "optional",            # try/except sqlite3 swap
        "pytest": "tooling",                # PEP 735 dev group; own pytest11 plugin
        "bs4": "tooling",                   # beautifulsoup4, dev/test group
        "rich": "optional",                 # rich optional-dependency extra
    },
    "simonw/llm": {
        "httpx": "transitive",              # via openai
        "typing_extensions": "transitive",  # via pydantic (self-documented)
        "ulid": "alias_of_declared",        # python-ulid
    },
    "Textualize/rich": {
        "emoji": "tooling",                 # tools/make_emoji.py maintainer codegen
        "markdown_it": "alias_of_declared", # markdown-it-py
        "setuptools": "tooling",            # setup.py GitHub-detection shim
    },
    "python-poetry/poetry": {},
    "pydantic/pydantic": {
        "github": "tooling",     # PyGithub in .github CI script (scanner excludes .github)
        "micropip": "local",     # nested pydantic-core wasm-preview subproject
        "pyodide": "local",
        "release": "local",      # release/ maintainer scripts (no __init__.py)
        "toml": "optional",      # v1/mypy.py legacy tomllib/tomli/toml fallback
        "email_validator": "optional",  # email extra, guarded
    },
    "scrapy/scrapy": {
        "IPython": "optional", "bpython": "optional", "ptpython": "optional",
        "pygments": "optional",
        "PIL": "optional",        # images pipeline -> curated Pillow (undeclared optional)
        "boto3": "optional", "botocore": "optional", "google": "optional",
        "brotli": "optional", "brotlicffi": "optional", "zstandard": "optional",
        "h2": "optional", "socksio": "optional", "httpx": "optional",
        "httpcore": "typing",
        "robotexclusionrulesparser": "optional",
        "pydispatch": "alias_of_declared",  # PyDispatcher
        "zope": "alias_of_declared",        # zope.interface (namespace truncation)
        "pytest": "tooling",                # tox.ini test deps
        "typing_extensions": "typing",
    },
    "jazzband/pip-tools": {
        "typing_extensions": "optional",  # version-gated Self fallback
        "pytest": "tooling", "pytest_mock": "tooling",  # testing extra
        "tomli_w": "optional",            # testing/coverage extra
    },
    "aio-libs/aiohttp": {
        "backports": "alias_of_declared",  # backports.zstd (speedups extra)
        "aiodns": "optional", "brotli": "optional", "brotlicffi": "optional",  # speedups extra
    },
}


# full_name -> (probe batch dir, clone dir name, git url) for path building + reproducibility.
_REPO_MANIFEST: dict[str, tuple[str, str, str]] = {
    "UlionTse/translators": ("probe_A", "translators", "https://github.com/UlionTse/translators"),
    "psf/requests": ("probe_A", "requests", "https://github.com/psf/requests"),
    "pallets/click": ("probe_A", "click", "https://github.com/pallets/click"),
    "psf/cachecontrol": ("probe_A", "cachecontrol", "https://github.com/psf/cachecontrol"),
    "pallets/flask": ("probe_B", "flask", "https://github.com/pallets/flask"),
    "encode/httpx": ("probe_B", "httpx", "https://github.com/encode/httpx"),
    "encode/starlette": ("probe_B", "starlette", "https://github.com/encode/starlette"),
    "tiangolo/typer": ("probe_B", "typer", "https://github.com/tiangolo/typer"),
    "simonw/datasette": ("probe_C", "datasette", "https://github.com/simonw/datasette"),
    "simonw/llm": ("probe_C", "llm", "https://github.com/simonw/llm"),
    "Textualize/rich": ("probe_C", "rich", "https://github.com/Textualize/rich"),
    "python-poetry/poetry": ("probe_C", "poetry", "https://github.com/python-poetry/poetry"),
    "pydantic/pydantic": ("probe_D", "pydantic", "https://github.com/pydantic/pydantic"),
    "scrapy/scrapy": ("probe_D", "scrapy", "https://github.com/scrapy/scrapy"),
    "jazzband/pip-tools": ("probe_D", "pip-tools", "https://github.com/jazzband/pip-tools"),
    "aio-libs/aiohttp": ("probe_D", "aiohttp", "https://github.com/aio-libs/aiohttp"),
}

_DEFAULT_CLONES_ROOT = "/Users/john/.claude/jobs/366037cb/tmp"


def default_clone_map(clones_root: "Path | None" = None) -> Mapping[str, str]:
    """full_name -> clone dir, under the live-probe layout ``<root>/probe_X/<name>``.

    Override the root via ``--clones-root`` or ``$AB_CLONES_ROOT``; defaults to the live-probe
    job tmp. Missing clones are skipped (with a warning) by the runner."""
    root = Path(clones_root or os.environ.get("AB_CLONES_ROOT", _DEFAULT_CLONES_ROOT))
    return {
        full_name: str(root / batch / name)
        for full_name, (batch, name, _url) in _REPO_MANIFEST.items()
    }
