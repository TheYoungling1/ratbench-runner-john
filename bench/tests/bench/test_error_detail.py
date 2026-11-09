# tests/bench/test_error_detail.py
#
# When measure() raises, run_one records the failure as `meta.error` and the row becomes
# measure_error -- a DISQUALIFIED status, so the repo scores EBSR 0. That row is the ONLY evidence
# of why, which makes swallowing the message expensive.
#
# It was being swallowed. docker_client.run_detached runs with check=True, capture_output=True, so
# Docker's explanation lands in CalledProcessError.stderr -- but the handler recorded repr(e), and
# repr(CalledProcessError) prints only the exit code and argv. Ten repo2run repos failed with
# `CalledProcessError(125, ['docker','run',...])` and nothing else, and the cause could not be
# determined from the rows at all.
import subprocess

from bench.unified_bench import _error_detail


def test_a_called_process_error_keeps_its_stderr():
    e = subprocess.CalledProcessError(125, ["docker", "run", "-d", "x"],
                                      stderr=b"Error response from daemon: no such image")
    out = _error_detail(e)
    assert "125" in out
    assert "no such image" in out, "the daemon's reason must survive into the row"


def test_stdout_is_kept_when_stderr_is_empty():
    e = subprocess.CalledProcessError(1, ["docker", "run"], output=b"something on stdout")
    assert "something on stdout" in _error_detail(e)


def test_a_plain_exception_still_reports_its_type_and_message():
    assert "ValueError" in _error_detail(ValueError("boom"))
    assert "boom" in _error_detail(ValueError("boom"))


def test_text_mode_stderr_is_handled_as_well_as_bytes():
    e = subprocess.CalledProcessError(2, ["x"], stderr="already a str")
    assert "already a str" in _error_detail(e)


def test_undecodable_bytes_do_not_raise():
    # Docker output is not guaranteed valid UTF-8; the error path must never itself explode.
    e = subprocess.CalledProcessError(3, ["x"], stderr=b"\xff\xfe bad bytes")
    assert "3" in _error_detail(e)


def test_the_detail_is_truncated_so_a_huge_log_cannot_bloat_every_row():
    e = subprocess.CalledProcessError(4, ["x"], stderr=b"y" * 50_000)
    assert len(_error_detail(e)) < 3000
