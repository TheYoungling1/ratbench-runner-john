# tests/bench/test_docker_client_decoding.py
"""Docker's output is not guaranteed to be valid UTF-8.

BuildKit truncates long log lines, and a cut through a multi-byte character leaves an invalid
continuation byte. Under strict decoding that raises UnicodeDecodeError out of subprocess.run,
which unified_bench.run_one catches and records as `measure_error` -- a FABRICATED failure for a
build that was fine, and one biased toward repos whose logs carry non-ASCII text.
"""
import subprocess

from bench.docker_client import SubprocessDocker

# A valid UTF-8 prefix, then a lone continuation byte: exactly what a mid-character truncation
# leaves behind. `b"\xe6\x96\xb0"` is 新; cutting it after one byte yields the bad sequence.
TRUNCATED = b"building 3 [6/9] RUN setup\n#7 HEAD is now at feat: \xe6\x96"


def test_build_survives_a_truncated_multibyte_character(monkeypatch):
    def fake(argv, **kw):
        enc, errs = kw.get("encoding", "utf-8"), kw.get("errors", "strict")
        return subprocess.CompletedProcess(argv, 0, TRUNCATED.decode(enc, errs), "")
    monkeypatch.setattr(subprocess, "run", fake)
    rc, log = SubprocessDocker().build("t", "/ctx")
    assert rc == 0
    assert "RUN setup" in log            # the readable part survives
    assert "�" in log               # the bad byte became a replacement char, not an exception


def test_exec_survives_non_utf8_pytest_output(monkeypatch):
    # exec() decodes the pytest RUN; a crash there discards a measurement already paid for.
    def fake(argv, **kw):
        enc, errs = kw.get("encoding", "utf-8"), kw.get("errors", "strict")
        return subprocess.CompletedProcess(argv, 1, TRUNCATED.decode(enc, errs), "")
    monkeypatch.setattr(subprocess, "run", fake)
    rc, out, timed_out = SubprocessDocker().exec("c", ["bash", "-c", "pytest"])
    assert (rc, timed_out) == (1, False)
    assert "RUN setup" in out


def test_every_text_mode_call_decodes_leniently():
    # A future call site that forgets errors="replace" reintroduces the fabricated failure.
    import inspect
    src = inspect.getsource(SubprocessDocker)
    assert "text=True" not in src, "text-mode calls must go through _TEXT (encoding + errors)"
