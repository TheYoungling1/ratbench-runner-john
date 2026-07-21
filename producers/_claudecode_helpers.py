"""Pure, RAT-tree-free helpers for the claudecode-dockerfile PRODUCER.

Folded out of the runner's claudecode model + dockerfile helpers so producers/ never
imports the runner package or RAT model modules (the produce -> measure seam only
crosses via bench.schema). Stdlib-only, so producers stay importable without the RAT tree.
"""

W = "/testbed"
AUTH_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
DOCKERFILE_GEN_PATH = "/testbed/Dockerfile.gen"

# The Claude Code CLI accepts model aliases (sonnet/opus/haiku) or full IDs.
_CLAUDE_PREFIXES = ("claude", "sonnet", "opus", "haiku")


def _as_text(v) -> str:
    """Decode subprocess output that may be str (text mode), bytes, or None."""
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def _normalize_model(llm: str) -> str:
    """Claude Code needs a Claude model. Fall back to 'sonnet' for non-Claude slugs
    (e.g. the runner's default deepseek/...), so `bench claudecode` works even if the
    variety llm wasn't overridden."""
    low = (llm or "").lower()
    if low.startswith(_CLAUDE_PREFIXES):
        return llm
    return "sonnet"


_PROMPT_TEMPLATE = (
    "You are configuring a Python repository at /testbed so its EXISTING test suite can "
    "run, and then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "First, install ALL Python dependencies and any required system packages so that "
    "pytest can collect and run the tests. Install into the SYSTEM Python using "
    "`sudo pip install ...` and `sudo apt-get install -y ...` for system libraries — do "
    "NOT create a virtualenv (the grader runs the system python3). You may edit "
    "configuration files. DO NOT modify, add, or delete any test files. DO NOT run the "
    "test suite yourself.\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment "
    "FROM A CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` "
    "(do NOT rely on any files from this container — the build starts empty);\n"
    "  - install the SAME system packages and Python dependencies you installed, as RUN "
    "steps. The build runs as ROOT, so DROP every `sudo` prefix (use "
    "`apt-get install -y ...`, `pip install ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. "
    "`RUN sed -i ...` or a heredoc), since the clone is pristine;\n"
    "  - NOT run pytest or the test suite in the Dockerfile.\n"
    "When the Dockerfile is written, stop."
)


def build_prompt(full_name: str, base: str) -> str:
    """The agentic setup+emit prompt with the repo and emitted-base injected."""
    return _PROMPT_TEMPLATE.format(gen_path=DOCKERFILE_GEN_PATH, base=base, full_name=full_name)
