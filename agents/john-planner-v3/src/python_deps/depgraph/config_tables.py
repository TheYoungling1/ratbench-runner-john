"""Curated `package -> config-obligation` table (this is a config-tier table,
unrelated to native/system deps — see construction-enrichment cluster 1a for
why the analogous native-deps table was deleted).  A distribution that, once installed, reads
an env var to function induces a Config *need*: ``django`` reads
``DJANGO_SETTINGS_MODULE``, ``celery`` reads a broker URL, etc.  Keyed by PyPI
distribution name; lookups are normalized so case/separators don't matter.

Each obligation is ``(env_var_name, default_value_or_None)``.  A default is given
only when a universally-safe test-time value exists; otherwise ``None`` (the
agent must supply it — see design §7.4 placeholder fix).
"""

from __future__ import annotations

from python_deps.import_mapping import normalize_package_name

PACKAGE_TO_CONFIG: dict[str, list[tuple[str, str | None]]] = {
    "django": [("DJANGO_SETTINGS_MODULE", None)],
    "celery": [("CELERY_BROKER_URL", None)],
    "boto3": [("AWS_ACCESS_KEY_ID", None), ("AWS_SECRET_ACCESS_KEY", None),
              ("AWS_DEFAULT_REGION", "us-east-1")],
}

_NORMALIZED: dict[str, list[tuple[str, str | None]]] = {
    normalize_package_name(name): obligations
    for name, obligations in PACKAGE_TO_CONFIG.items()
}


def config_obligations_for_package(name: str) -> list[tuple[str, str | None]]:
    """Env vars a distribution induces, or ``[]`` if unknown (fresh list)."""
    return list(_NORMALIZED.get(normalize_package_name(name), ()))
