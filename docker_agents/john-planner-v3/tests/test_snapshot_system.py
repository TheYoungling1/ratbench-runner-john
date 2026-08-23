from src.envstate.snapshot import probe_env, EnvSnapshot


def _exec(table):
    def run(cmd):
        for k, v in table.items():
            if k in cmd:
                return v
        return (1, "")
    return run


def test_probe_collects_system_installed_and_compact_env():
    table = {
        "pip list --format=freeze": (0, "flask==3.0.0\n"),
        "uname -m": (0, "x86_64"),
        "dpkg -l": (0, "libpq-dev\nbuild-essential\n"),
        "pkg-config --list-all": (0, "libxml-2.0 libXML\nzlib zlib\n"),
        "command -v": (0, "gcc\npg_config\n"),        # system_tools loop
        "/etc/os-release": (0, "ID=debian\nVERSION_CODENAME=bookworm\n"),
    }
    snap = probe_env(_exec(table))
    sysnames = {f.name.lower() for f in snap.system_installed}
    assert {"libpq-dev", "build-essential", "gcc", "pg_config"} <= sysnames
    assert "libxml-2.0" in sysnames                     # pkg-config module name
    assert snap.env["build_tools"] == "gcc,pg_config"   # compact, prompt-friendly
    assert "debian" in snap.env["os_release"]
    # bulky lists are NOT dumped into env
    assert "dpkg_packages" not in snap.env and "pkg_config_modules" not in snap.env


def test_total_failure_empty_snapshot():
    assert probe_env(lambda cmd: (1, "")) == EnvSnapshot()
