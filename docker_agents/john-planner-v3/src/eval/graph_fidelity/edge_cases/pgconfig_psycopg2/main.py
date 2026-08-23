"""Triggering import for the pgconfig_psycopg2 edge case.

psycopg2 (the source distribution -- NOT psycopg2-binary) compiles a C
extension against libpq at build time and needs the pg_config binary on
PATH to locate PostgreSQL's headers and libraries. Without the apt
package libpq-dev (which provides both), `pip install psycopg2` fails
during the build step with "Error: pg_config executable not found."
"""

import psycopg2

print(psycopg2.__version__)
