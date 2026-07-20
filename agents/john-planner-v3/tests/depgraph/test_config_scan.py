import os
import textwrap
from python_deps.depgraph.config_scan import scan_env_reads
from python_deps.depgraph.config_scan import scan_framework_config_reads
from python_deps.depgraph.config_scan import parse_env_example, configured_vars
from python_deps.depgraph.config_scan import scan_env_defaults


def _write(tmp_path, rel, src):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))
    return p


def test_scan_finds_os_environ_subscript(tmp_path):
    _write(tmp_path, "app/settings.py", """
        import os
        SECRET_KEY = os.environ['SECRET_KEY']
        DEBUG = os.getenv('DEBUG')
        DB = os.environ.get('DATABASE_URL')
    """)
    found = scan_env_reads(str(tmp_path))
    assert set(found) == {"SECRET_KEY", "DEBUG", "DATABASE_URL"}
    assert "settings.py" in found["SECRET_KEY"]


def test_scan_skips_excluded_dirs(tmp_path):
    _write(tmp_path, "examples/demo.py", "import os\nX = os.environ['SHOULD_BE_IGNORED']\n")
    found = scan_env_reads(str(tmp_path))
    assert "SHOULD_BE_IGNORED" not in found


def test_scan_handles_unparseable_file(tmp_path):
    _write(tmp_path, "app/ok.py", "import os\nA = os.getenv('A')\n")
    _write(tmp_path, "app/broken.py", "def (:\n")  # syntax error
    found = scan_env_reads(str(tmp_path))
    assert "A" in found  # broken file skipped, good file still scanned


def test_scan_decouple_and_environs(tmp_path):
    _write(tmp_path, "app/conf.py", """
        from decouple import config
        from environs import Env
        env = Env()
        SECRET = config('SECRET_KEY')
        PORT = env.int('PORT')
    """)
    found = scan_framework_config_reads(str(tmp_path))
    assert "SECRET_KEY" in found
    assert "PORT" in found


def test_scan_pydantic_basesettings_fields(tmp_path):
    _write(tmp_path, "app/settings.py", """
        from pydantic_settings import BaseSettings
        class Settings(BaseSettings):
            database_url: str
            redis_url: str = "redis://localhost"
    """)
    found = scan_framework_config_reads(str(tmp_path))
    assert "DATABASE_URL" in found
    assert "REDIS_URL" in found


def test_parse_env_example_values(tmp_path):
    _write(tmp_path, ".env.example", "DEBUG=True\nDATABASE_URL=postgres://localhost/db\n# comment\nEMPTY=\n")
    vals = parse_env_example(str(tmp_path))
    assert vals["DEBUG"] == "True"
    assert vals["DATABASE_URL"] == "postgres://localhost/db"
    assert vals.get("EMPTY", "") == ""


def test_configured_vars_from_real_dotenv_and_pytest_ini(tmp_path):
    _write(tmp_path, ".env", "ALREADY_SET=1\n")
    _write(tmp_path, "pytest.ini", "[pytest]\nenv =\n    DJANGO_SETTINGS_MODULE=app.settings\n")
    provided = configured_vars(str(tmp_path))
    assert "ALREADY_SET" in provided
    assert "DJANGO_SETTINGS_MODULE" in provided


def test_plain_baseconfig_class_not_treated_as_settings(tmp_path):
    # A plain (non-pydantic) `class BaseConfig` and its subclass must NOT have their
    # annotated attrs harvested as env vars (gap ④ false-positive regression test).
    _write(tmp_path, "app/config.py", """
        import os
        class BaseConfig:
            DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///x")
            DATABASE_CONNECT_DICT: dict = {}
        class DevelopmentConfig(BaseConfig):
            DATABASE_CONNECT_DICT: dict = {"pool_pre_ping": True}
    """)
    found = scan_framework_config_reads(str(tmp_path))
    # DATABASE_CONNECT_DICT is a plain dict attr, never read from env -> must be absent.
    assert "DATABASE_CONNECT_DICT" not in found


def test_scan_env_defaults_captures_string_literal(tmp_path):
    _write(tmp_path, "app/config.py", """
        import os
        BROKER = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
        FLAG = os.getenv("FEATURE_FLAG", "on")
        DB = os.environ.get("DATABASE_URL", f"sqlite:///{x}")
        BARE = os.environ.get("NO_DEFAULT")
    """)
    d = scan_env_defaults(str(tmp_path))
    assert d["CELERY_BROKER_URL"] == "redis://127.0.0.1:6379/0"
    assert d["FEATURE_FLAG"] == "on"
    assert "DATABASE_URL" not in d      # f-string default: not statically resolvable
    assert "NO_DEFAULT" not in d        # no default argument
