# tests/bench/test_copy_dirs.py
#
# A COPY whose source is a DIRECTORY must reach the build context byte-for-byte.
#
# The first attempt at this inlined directory contents into setup_scripts as TEXT (read and written
# with errors="replace") and capped the total at 32MB. That works for Repo2Run's `COPY patch`
# (unified diffs, 372KB across 40 dirs) and fails badly for RAT's `COPY repo`, a real git checkout:
#   * errors="replace" corrupts .git/index and pack files, so `git rev-parse --show-toplevel`
#     fails inside the image. contract._probe_script then reports reason=not_git ->
#     status non_conforming -> DISQUALIFIED -> EBSR 0. Observed on 6 of the first 14 rat50new rows.
#   * the 32MB cap silently returned a PARTIAL context for 21 of 48 rat50 and 15 of 41 rat50new
#     checkouts (huggingface/transformers alone is 132MB).
#
# Directories are therefore carried by PATH and copied on disk at measure time, never inlined.
import os

from bench.harvest import _copy_dirs, _sibling_scripts


def _mk(tmp_path, dockerfile, files=(), dirs=()):
    d = tmp_path / "repo_dir"
    d.mkdir(exist_ok=True)
    (d / "Dockerfile").write_text(dockerfile)
    for name, content in files:
        (d / name).write_text(content)
    for name, inner in dirs:
        p = d / name
        p.mkdir(parents=True, exist_ok=True)
        for fn, data in inner:
            fp = p / fn
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(data) if isinstance(data, bytes) else fp.write_text(data)
    return str(d)


def test_a_directory_copy_is_reported_as_a_path_not_inlined(tmp_path):
    d = _mk(tmp_path, "FROM x\nCOPY repo /repo\n", dirs=[("repo", [("a.py", "print(1)")])])
    dirs = _copy_dirs(d, open(os.path.join(d, "Dockerfile")).read())
    assert dirs == {"repo": os.path.join(d, "repo")}


def test_directories_are_absent_from_setup_scripts(tmp_path):
    # Inlining is what corrupted them; setup_scripts must carry FILES only.
    d = _mk(tmp_path, "FROM x\nCOPY repo /repo\nCOPY s.sh /s\n",
            files=[("s.sh", "echo hi")], dirs=[("repo", [("a.py", "x")])])
    s = _sibling_scripts(d, open(os.path.join(d, "Dockerfile")).read())
    assert s == {"s.sh": "echo hi"}
    assert not any(k.startswith("repo/") for k in s)


def test_a_binary_file_in_the_directory_is_not_mangled(tmp_path):
    # .git/index is binary; errors="replace" silently rewrote it and broke the repository.
    raw = b"DIRC\x00\x00\x00\x02\xff\xfe binary"
    d = _mk(tmp_path, "FROM x\nCOPY repo /repo\n", dirs=[("repo", [(".git/index", raw)])])
    dirs = _copy_dirs(d, open(os.path.join(d, "Dockerfile")).read())
    with open(os.path.join(dirs["repo"], ".git", "index"), "rb") as fh:
        assert fh.read() == raw, "bytes must survive; the path is copied, not re-encoded"


def test_a_large_directory_is_not_dropped(tmp_path):
    # The 32MB cap silently returned a partial context. Carrying a path has no size limit.
    big = b"y" * (2 * 1024 * 1024)
    d = _mk(tmp_path, "FROM x\nCOPY repo /repo\n",
            dirs=[("repo", [(f"f{i}.bin", big) for i in range(3)])])
    dirs = _copy_dirs(d, open(os.path.join(d, "Dockerfile")).read())
    assert "repo" in dirs
    assert len(os.listdir(dirs["repo"])) == 3


def test_a_file_copy_source_is_not_reported_as_a_directory(tmp_path):
    d = _mk(tmp_path, "FROM x\nCOPY s.sh /s\n", files=[("s.sh", "echo")])
    assert _copy_dirs(d, open(os.path.join(d, "Dockerfile")).read()) == {}


def test_an_absent_copy_source_is_not_reported(tmp_path):
    d = _mk(tmp_path, "FROM x\nCOPY nope /nope\n")
    assert _copy_dirs(d, open(os.path.join(d, "Dockerfile")).read()) == {}


# ── symlinks inside a copied directory ────────────────────────────────────────────────────
#
# copytree(symlinks=False) DEREFERENCES links, so a DANGLING one raises and the whole repo is lost:
# baserow/baserow, polarsource/polar and Checkmk/checkmk all carry dangling node_modules links and
# came back measure_error. Docker's own build context preserves symlinks rather than following
# them, so preserving is both the fix and the faithful behaviour.

def test_a_dangling_symlink_does_not_abort_the_copy(tmp_path):
    import shutil
    from bench.measure import _stage_copy_dirs
    src = tmp_path / "repo"; (src / "sub").mkdir(parents=True)
    (src / "real.txt").write_text("kept")
    (src / "sub" / "dangling").symlink_to("/nonexistent/target")
    ctx = tmp_path / "ctx"; ctx.mkdir()
    _stage_copy_dirs(str(ctx), {"repo": str(src)})
    assert (ctx / "repo" / "real.txt").read_text() == "kept"
    assert (ctx / "repo" / "sub" / "dangling").is_symlink(), "preserved, not dereferenced"


def test_a_live_symlink_is_preserved_as_a_link(tmp_path):
    from bench.measure import _stage_copy_dirs
    src = tmp_path / "repo"; src.mkdir()
    (src / "target.txt").write_text("data")
    (src / "link.txt").symlink_to("target.txt")
    ctx = tmp_path / "ctx"; ctx.mkdir()
    _stage_copy_dirs(str(ctx), {"repo": str(src)})
    assert (ctx / "repo" / "link.txt").is_symlink()
    assert (ctx / "repo" / "target.txt").read_text() == "data"
