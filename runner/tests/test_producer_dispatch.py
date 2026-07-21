"""Deterministic structural smoke for the M4.5b producer-dispatch refactor.

No LLM, no docker. This is the acceptance gate for wiring the producer registry
DIRECTLY into the runner (the three produce-only wrappers under runner/models/ were
deleted; _ProducerModel re-homes their produce -> write_env_packet -> run_produced.json
duties into runner/benchmark.py).

IMPORT STRATEGY (documented, per task): we load runner/benchmark.py BY PATH
via importlib rather than `import runner.benchmark`. Loading it by path exercises the
module's own top-level code, which sets up sys.path (RAT_ROOT / <repo> / <repo>/bench)
exactly as a real run does — making `import producers`, `from bench.schema import
RepoSpec`, etc. resolve without the test having to replicate the path shim.

Covers:
  1. _make_model dispatches the 3 produce-able names -> _ProducerModel, and the 3
     native-lane names -> NOT _ProducerModel (live models under runner/live/).
  2. THE KEY TEST — real produce dispatch for `dockeragent` with a FAKE adapter
     (no LLM, no docker): success path lands eval_build/Dockerfile + _meta.json{produced}
     + run_produced.json; error path (adapter returns no Dockerfile) lands _meta.json{error}
     with NO eval_build/ and NO run_produced.json.
  3. Commit-pin threading — _ProducerModel.predict(commit=...) reaches the emitted Dockerfile.
  4. Language threading (fix-language-threading) — _ProducerModel.predict(language=...)
     reaches RepoSpec -> _meta.json["language"], lower-cased; the no-language default
     stays "python" for byte-identity with pre-fix Python runs.
  5. _run_one forwards its `language` param into the single model.predict(...) call site.
"""
import importlib.util
import json
import os

_RUNNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <repo>/runner
_RRB_PATH = os.path.join(_RUNNER_DIR, "benchmark.py")


def _load_rrb():
    """Load benchmark.py by path; its module-level code sets up sys.path."""
    spec = importlib.util.spec_from_file_location("rrb_under_test", _RRB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rrb = _load_rrb()


# ── 1. Dispatch: produce-able -> _ProducerModel ──────────────────────────────
def test_make_model_dispatch_produce_able(tmp_path):
    for name in ("dockeragent", "repo2run", "claudecode-dockerfile"):
        m = rrb._make_model(name, root_path=str(tmp_path), timeout=60, llm="x/y", num_turn=3)
        assert isinstance(m, rrb._ProducerModel), name
        assert m.name == name
        assert m.llm == "x/y"
        assert m.num_turn == 3


# ── 1b. Dispatch: native-lane names are NOT _ProducerModel ────────────────────
def test_make_model_dispatch_native_not_producer(tmp_path):
    # These import live models from runner.live.* — heavy deps may be absent in this
    # environment. If _make_model raises during that lazy import, dispatch is still proven
    # correct: the name is not in _PRODUCE_ABLE, so the _ProducerModel branch was skipped.
    for name in ("rat", "sweagent", "claudecode"):
        assert name not in rrb._PRODUCE_ABLE
        try:
            m = rrb._make_model(name, root_path=str(tmp_path), timeout=60, llm="x/y", num_turn=3)
        except Exception:
            continue  # live-model import attempted; NOT dispatched to _ProducerModel
        assert not isinstance(m, rrb._ProducerModel), name


# A fake adapter mirrors MultiDockerEvalAdapter's shape well enough for the producer:
# process_single_instance(inst, **kw) -> {dockerfile, base_image, head_sha, logs}.
_SUCCESS_ADAPTER = (
    "class MultiDockerEvalAdapter:\n"
    "    def __init__(self, output_dir=None, **kw):\n"
    "        self.output_dir = output_dir\n"
    "    def process_single_instance(self, inst, **kw):\n"
    "        return {\n"
    "            'dockerfile': 'FROM python:3.11\\nRUN pip install --no-cache-dir pytest\\n',\n"
    "            'base_image': 'python:3.11', 'head_sha': 'deadbeef',\n"
    "            'logs': {'tokens_in': 5, 'tokens_out': 7},\n"
    "        }\n"
)

_ERROR_ADAPTER = (
    "class MultiDockerEvalAdapter:\n"
    "    def __init__(self, output_dir=None, **kw):\n"
    "        self.output_dir = output_dir\n"
    "    def process_single_instance(self, inst, **kw):\n"
    "        return {'dockerfile': None, 'logs': {}}\n"
)

# Emits a /testbed clone line so the commit pin has somewhere to inject.
_CLONE_ADAPTER = (
    "class MultiDockerEvalAdapter:\n"
    "    def __init__(self, output_dir=None, **kw):\n"
    "        self.output_dir = output_dir\n"
    "    def process_single_instance(self, inst, **kw):\n"
    "        return {\n"
    "            'dockerfile': 'FROM python:3.11\\nRUN git clone --depth=1 https://github.com/o/r /testbed\\n',\n"
    "            'base_image': 'python:3.11', 'head_sha': 'deadbeef', 'logs': {},\n"
    "        }\n"
)


def _write_fake_agent_root(dir_path, source):
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "multi_docker_eval_adapter.py"), "w") as f:
        f.write(source)


# ── 2. KEY TEST — produce dispatch, success path ─────────────────────────────
def test_producer_predict_success_writes_packet_and_marker(tmp_path, monkeypatch):
    agent_root = tmp_path / "agent_ok"
    _write_fake_agent_root(str(agent_root), _SUCCESS_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_ok"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    assert isinstance(m, rrb._ProducerModel)

    out = m.predict("o/r")
    assert out["status"] == "success"
    assert out["produced"] is True

    out_repo = run_dir / "output" / "o" / "r"
    dockerfile = out_repo / "eval_build" / "Dockerfile"
    assert dockerfile.exists()
    assert "pytest" in dockerfile.read_text()

    meta = json.loads((out_repo / "_meta.json").read_text())
    assert meta["status"] == "produced"
    assert meta["producer"] == "dockeragent"
    assert meta["total_tokens"] is None
    assert meta["tokens_in"] == 5

    produced = json.loads((out_repo / "run_produced.json").read_text())
    assert produced["status"] == "produced"


# ── 2b. KEY TEST — produce dispatch, error path ──────────────────────────────
def test_producer_predict_error_writes_no_marker(tmp_path, monkeypatch):
    # Use a DIFFERENT DOCKERAGENT_ROOT so the fake adapter is loaded FRESH: the producer's
    # loader caches by a root-unique module name, so reusing the success root would return
    # its cached (success) adapter instead of this error one.
    agent_root = tmp_path / "agent_err"
    _write_fake_agent_root(str(agent_root), _ERROR_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_err"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    out = m.predict("o/r")
    assert out["status"] == "error"

    out_repo = run_dir / "output" / "o" / "r"
    assert not (out_repo / "run_produced.json").exists()
    assert not (out_repo / "eval_build").exists()

    meta = json.loads((out_repo / "_meta.json").read_text())
    assert meta["status"] == "error"


# ── 3. Commit-pin threading — _ProducerModel.predict(commit=...) -> emitted Dockerfile ───
def test_producer_predict_threads_commit_into_dockerfile(tmp_path, monkeypatch):
    agent_root = tmp_path / "agent_pin"
    _write_fake_agent_root(str(agent_root), _CLONE_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_pin"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    out = m.predict("o/r", commit="cafef00d")
    assert out["status"] == "success"

    df = (run_dir / "output" / "o" / "r" / "eval_build" / "Dockerfile").read_text()
    # The dataset commit was threaded RepoSpec -> produce -> injected after the /testbed clone.
    assert "checkout --detach cafef00d" in df


def test_producer_predict_default_commit_is_none(tmp_path, monkeypatch):
    # predict() with no commit leaves the emitted Dockerfile unpinned (live HEAD, unchanged behavior).
    agent_root = tmp_path / "agent_nopin"
    _write_fake_agent_root(str(agent_root), _CLONE_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_nopin"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    out = m.predict("o/r")
    assert out["status"] == "success"

    df = (run_dir / "output" / "o" / "r" / "eval_build" / "Dockerfile").read_text()
    assert "checkout --detach" not in df


# ── 4. Language threading — _ProducerModel.predict(language=...) -> RepoSpec -> _meta.json ───
# This is the Critical bug's exact seam: predict() used to hardcode RepoSpec's default
# language="python" no matter what the dataset said, so every Node/Rust/Java repo's _meta.json
# recorded "language":"python" and harvest -> get_language() ran PythonLanguage on it (0 tests
# collected, silent EBSR-0). Without the fix this call raises TypeError (predict() didn't
# accept a `language` kwarg at all) -- the strongest possible RED.
def test_producer_predict_threads_language_into_meta(tmp_path, monkeypatch):
    agent_root = tmp_path / "agent_lang"
    _write_fake_agent_root(str(agent_root), _CLONE_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_lang"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    out = m.predict("o/r", language="JavaScript")
    assert out["status"] == "success"

    meta = json.loads((run_dir / "output" / "o" / "r" / "_meta.json").read_text())
    # dataset's capitalized "JavaScript" must reach _meta.json lower-cased, so downstream
    # harvest -> get_language("javascript") resolves to NodeLanguage, not the Python default.
    assert meta["language"] == "javascript"


def test_producer_predict_default_language_is_python(tmp_path, monkeypatch):
    # predict() with no language keeps the historical default -- Python _meta.json/MeasureRow
    # stay byte-identical to pre-fix behavior (datasets/rat_python50.json rows, or any row
    # lacking a "language" key, both resolve to "python" here exactly as before).
    agent_root = tmp_path / "agent_lang_default"
    _write_fake_agent_root(str(agent_root), _CLONE_ADAPTER)
    monkeypatch.setenv("DOCKERAGENT_ROOT", str(agent_root))

    run_dir = tmp_path / "run_lang_default"
    m = rrb._make_model("dockeragent", root_path=str(run_dir), timeout=60, llm="x/y", num_turn=3)
    out = m.predict("o/r")
    assert out["status"] == "success"

    meta = json.loads((run_dir / "output" / "o" / "r" / "_meta.json").read_text())
    assert meta["language"] == "python"


# ── 5. _run_one forwards `language` through to model.predict (the dataset-row -> predict seam) ──
# Covers the OTHER half of the threading chain that test 4 doesn't reach: _run_one's own
# `language` param and its single `model.predict(...)` call site. A stub model records the
# kwargs it was called with -- no docker, no producer registry.
class _RecordingModel:
    llm = "x/y"

    def __init__(self):
        self.calls = []

    def predict(self, full_name, commit=None, language=None):
        self.calls.append({"full_name": full_name, "commit": commit, "language": language})
        return {"status": "success", "root_path": "/tmp/unused", "full_name": full_name}


def test_run_one_forwards_language_to_predict(tmp_path):
    stub = _RecordingModel()
    rrb._run_one("o/r", stub, str(tmp_path), "cat", language="JavaScript")

    assert len(stub.calls) == 1
    assert stub.calls[0]["language"] == "JavaScript"
