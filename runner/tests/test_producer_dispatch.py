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
