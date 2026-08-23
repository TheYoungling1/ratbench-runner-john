from python_deps.depgraph.patch_gate import is_read_only


def test_dev_null_redirect_is_read_only():
    assert is_read_only("pkg-config --exists libplacebo >/dev/null 2>&1") is True


def test_version_probe_is_read_only():
    assert is_read_only("ffmpeg -version") is True
    assert is_read_only('python -c "import lxml"') is True


def test_file_write_is_mutation():
    assert is_read_only("echo hi > /etc/profile.d/x.sh") is False
    assert is_read_only("echo hi >> /etc/profile.d/x.sh") is False   # append to real path


def test_widened_mutators_caught():
    for cmd in ("tee /tmp/x", "dd if=/dev/zero of=/tmp/x", "mv a b", "cp a b",
                "curl http://x -o y", "wget http://x",
                "apt-get install -y libpq-dev", "pip3 install lxml"):
        assert is_read_only(cmd) is False, cmd
