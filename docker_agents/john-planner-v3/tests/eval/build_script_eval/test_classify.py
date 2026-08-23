from src.eval.build_script_eval.classify import (
    classify_tool_failures, merge_gaps, real_first_failure,
)


def test_classify_tool_failures_finds_command_failed_gcc():
    blob = "error: command 'gcc' failed with exit status 1\n"
    gaps = classify_tool_failures(blob)
    assert {(g["tier"], g["id"]) for g in gaps} == {("TOOL", "gcc")}


def test_pygraphviz_blob_finds_gcc_and_real_first_failure_ignores_pip_notice():
    blob = (
        "running build_ext\n"
        "building 'pygraphviz._graphviz' extension\n"
        "gcc -pthread -fno-strict-aliasing -I/usr/include/graphviz -c graphviz_wrap.c\n"
        "unable to execute 'gcc': No such file or directory\n"
        "error: command 'gcc' failed: No such file or directory\n"
        "[notice] A new release of pip is available: 23.0 -> 24.0\n"
        "[notice] To update, run: pip install --upgrade pip\n"
    )
    gaps = classify_tool_failures(blob)
    assert {(g["tier"], g["id"]) for g in gaps} == {("TOOL", "gcc")}

    result = real_first_failure(blob)
    assert "gcc" in result["command"]
    assert "error:" in result["command"].lower()
    assert "[notice]" not in result["command"]
    assert "pip install --upgrade pip" not in result["command"]


def test_git_python_refresh_blob_is_git_tool_gap():
    blob = (
        "ImportError: Bad git executable.\n"
        "The git executable must be specified in one of the following ways:\n"
        "    - be included in your $PATH\n"
        "    - be set via $GIT_PYTHON_GIT_EXECUTABLE\n"
        "All git commands will error until this is rectified.\n"
        "This initial warning can be silenced or aggravated in the future by setting the\n"
        "$GIT_PYTHON_REFRESH environment variable.\n"
    )
    gaps = classify_tool_failures(blob)
    assert {(g["tier"], g["id"]) for g in gaps} == {("TOOL", "git")}


def test_merge_gaps_dedupes_duplicate_tool_and_keeps_distinct_system_lib():
    base = ({"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": "cannot open"},
             {"tier": "TOOL", "id": "gcc", "evidence": "base evidence"})
    extra = ({"tier": "TOOL", "id": "gcc", "evidence": "extra evidence"},)
    merged = merge_gaps(base, extra)
    assert merged == base
    assert len({(g["tier"], g["id"]) for g in merged}) == 2


def test_real_first_failure_falls_back_to_last_non_noise_line_never_notice():
    blob = (
        "[notice] A new release of pip is available: 23.0 -> 24.0\n"
        "[notice] To update, run: pip install --upgrade pip\n"
    )
    result = real_first_failure(blob)
    assert result["command"] is None or "[notice]" not in result["command"]
