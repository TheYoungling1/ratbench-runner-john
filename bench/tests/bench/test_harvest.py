# tests/bench/test_harvest.py
import json
from bench.harvest import discover


def _write(base, owner_repo, dockerfile=None, meta=None, subdir="", scripts=None):
    d = base / owner_repo
    (d / subdir).mkdir(parents=True, exist_ok=True) if subdir else d.mkdir(parents=True, exist_ok=True)
    tgt = d / subdir if subdir else d
    if dockerfile is not None:
        (tgt / "Dockerfile").write_text(dockerfile)
    for name, content in (scripts or {}).items():
        (tgt / name).write_text(content)
    if meta is not None:
        (d / "bench_meta.json").write_text(json.dumps(meta))


def test_discovers_dockerfile_and_meta(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r1", dockerfile="FROM x\nCOPY setup.sh /tmp/s\nRUN bash /tmp/s",
           meta={"tokens_in": 10, "tokens_out": 20, "base_image": "python:3.13-slim"},
           scripts={"setup.sh": "echo hi"})
    envs = discover({"v3": str(root)})
    assert len(envs) == 1
    e = envs[0]
    assert e.agent == "v3" and e.repo.full_name == "o/r1" and e.status == "ok"
    assert e.dockerfile.startswith("FROM x") and e.setup_scripts["setup.sh"] == "echo hi"
    assert e.base_image == "python:3.13-slim" and e.meta["tokens_in"] == 10


def test_eval_build_subdir_layout(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r2", dockerfile="FROM y", subdir="eval_build",
           meta={"base_image": "python:3.12-slim"})
    envs = discover({"v3": str(root)})
    assert len(envs) == 1 and envs[0].dockerfile == "FROM y" and envs[0].status == "ok"


def test_missing_dockerfile_is_status_missing(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r3", dockerfile=None, meta={"tokens_in": 5})
    envs = discover({"v3": str(root)})
    assert len(envs) == 1 and envs[0].status == "missing" and envs[0].dockerfile is None
    assert envs[0].meta["tokens_in"] == 5


def test_no_meta_gives_empty_meta(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r4", dockerfile="FROM z", meta=None)
    envs = discover({"v3": str(root)})
    assert envs[0].meta == {} and envs[0].status == "ok"


def test_copy_with_chown_and_multisource(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r5",
           dockerfile='FROM x\nCOPY --chown=1000:1000 setup.sh /tmp/s\nCOPY a.sh b.sh /tmp/',
           scripts={"setup.sh": "echo hi", "a.sh": "echo a", "b.sh": "echo b"})
    e = discover({"v3": str(root)})[0]
    assert e.setup_scripts["setup.sh"] == "echo hi"
    assert e.setup_scripts["a.sh"] == "echo a" and e.setup_scripts["b.sh"] == "echo b"


def test_copy_json_array_form(tmp_path):
    root = tmp_path / "v3run"
    _write(root, "o/r6", dockerfile='FROM x\nCOPY ["run.sh", "/tmp/run.sh"]',
           scripts={"run.sh": "echo run"})
    e = discover({"v3": str(root)})[0]
    assert e.setup_scripts["run.sh"] == "echo run"
