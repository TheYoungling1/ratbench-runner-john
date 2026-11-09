# tools/tests/test_dockerfile_quality.py
#
# inline_scripts exists so a build-time script hidden behind `COPY x.sh` + `RUN bash x.sh` gets
# linted like commands written inline. Each test pins one way it could misreport: skip the
# script, touch a script that never runs at build time, or silently leave one it can't inline.
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dockerfile_quality import inline_scripts


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text)


def test_graph2env_pattern_becomes_heredoc(tmp_path):
    _write(tmp_path, "setup.sh", "#!/bin/bash\npip install requests\n")
    df = ("FROM python:3.11-slim\nCOPY setup.sh /tmp/setup.sh\n"
          "RUN chmod +x /tmp/setup.sh && cd /app && bash /tmp/setup.sh\nWORKDIR /testbed\n")
    out = inline_scripts(df, str(tmp_path))
    assert "COPY setup.sh" not in out
    assert "bash /tmp/setup.sh" not in out
    assert "RUN <<" in out and "pip install requests" in out
    assert "cd /app" in out            # the RUN's own prefix is still linted
    assert "INLINED_SCRIPT_EOF\n\n" in out   # hadolint 2.12 needs a blank line after it
    assert out.endswith("WORKDIR /testbed\n")


def test_setupx_pattern_becomes_heredoc(tmp_path):
    _write(tmp_path, "setupx_replay.sh", "apt-get install -y gcc\n")
    df = "FROM ubuntu\nCOPY setupx_replay.sh /tmp/setupx_replay.sh\nRUN bash /tmp/setupx_replay.sh\n"
    out = inline_scripts(df, str(tmp_path))
    assert "COPY" not in out and "apt-get install -y gcc" in out


def test_entrypoint_script_left_alone(tmp_path):
    _write(tmp_path, "start.sh", "exec \"$@\"\n")
    df = 'FROM python\nCOPY start.sh /start.sh\nRUN chmod +x /start.sh\nENTRYPOINT ["/start.sh"]\n'
    assert inline_scripts(df, str(tmp_path)) == df


def test_copied_script_missing_from_context_left_alone(tmp_path):
    df = "FROM python\nCOPY setup.sh /tmp/setup.sh\nRUN bash /tmp/setup.sh\n"
    assert inline_scripts(df, str(tmp_path)) == df


def test_unhandled_invocation_fails_loudly(tmp_path):
    _write(tmp_path, "setup.sh", "true\n")
    df = "FROM python\nCOPY setup.sh /tmp/setup.sh\nRUN echo hi && \\\n    bash /tmp/setup.sh\n"
    with pytest.raises(ValueError):
        inline_scripts(df, str(tmp_path))


def test_bash_c_wrappers_unwrapped_so_hadolint_sees_them():
    # hadolint/ShellCheck treat `bash -c '...'` as one opaque string (setupx's replay format)
    from dockerfile_quality import unwrap_bash_c
    script = ("set +e\n"
              "bash -c 'cd /r && pip install x'\n"
              "bash -c 'python - <<'\"'\"'PY'\"'\"'\nimport os\nPY'\n"
              "exit 0\n")
    assert unwrap_bash_c(script) == ("set +e\ncd /r && pip install x\n"
                                      "python - <<'PY'\nimport os\nPY\nexit 0\n")


def test_unbalanced_bash_c_left_as_is():
    from dockerfile_quality import unwrap_bash_c
    assert unwrap_bash_c("bash -c 'oops\n") == "bash -c 'oops\n"


def test_heredoc_terminators_get_a_blank_line_after():
    # hadolint 2.12 can't parse a heredoc terminator followed directly by an instruction (DL1000)
    from dockerfile_quality import pad_heredoc_terminators
    df = ("FROM python\nRUN cat > /a.py <<'PYEOF'\nx = 1\nPYEOF\nRUN pip install -r r.txt\n"
          "RUN cat > r.txt <<REQ\nx==1\nREQ\n\nRUN true\n")
    assert pad_heredoc_terminators(df) == (
        "FROM python\nRUN cat > /a.py <<'PYEOF'\nx = 1\nPYEOF\n\nRUN pip install -r r.txt\n"
        "RUN cat > r.txt <<REQ\nx==1\nREQ\n\nRUN true\n")


def test_heredoc_body_lines_never_treated_as_openers():
    from dockerfile_quality import pad_heredoc_terminators
    df = "RUN cat > a.sh <<EOF\necho <<NOTADELIM\nEOF\nRUN true\n"
    assert pad_heredoc_terminators(df) == "RUN cat > a.sh <<EOF\necho <<NOTADELIM\nEOF\n\nRUN true\n"


def test_lint_failure_is_excluded_not_scored_clean(tmp_path, monkeypatch):
    # a hadolint timeout/crash must never read as "0 findings" (inlined scripts can take 30s+)
    import dockerfile_quality as dq
    df = tmp_path / "Dockerfile"
    df.write_text("FROM python\n")
    monkeypatch.setattr(dq, "run_hadolint", lambda _: None)
    row = dq.score_repo("graph2env", str(tmp_path), "o/r", str(df))
    assert "unparseable" in row and "hadolint_total" not in row
