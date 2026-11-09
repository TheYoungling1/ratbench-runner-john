# tests/bench/test_only_filter.py
"""--only FILE restricts a measure pass to the listed repos (a targeted re-measure)."""
from bench import unified_bench as ub
from bench.schema import HarvestedEnv, RepoSpec


def _env(repo):
    return HarvestedEnv("a", RepoSpec(repo, f"https://github.com/{repo}"), "FROM x", base_image=None)


def test_only_measures_listed_repos_case_insensitively(tmp_path, monkeypatch):
    envs = [_env("O/Keep"), _env("o/drop"), _env("x/also")]
    monkeypatch.setattr(ub, "discover", lambda roots: envs)
    monkeypatch.setattr(ub, "SubprocessDocker", lambda: None)
    seen = []
    monkeypatch.setattr(ub, "run_one", lambda e, out, docker: seen.append(e.repo.full_name))
    (tmp_path / "out").mkdir()
    only = tmp_path / "only.txt"
    only.write_text("o/keep\n\n  x/also  \n# comment\n")
    ub.main(["--harvest", "a=/nowhere", "--out", str(tmp_path / "out"), "--only", str(only)])
    assert sorted(seen) == ["O/Keep", "x/also"]
