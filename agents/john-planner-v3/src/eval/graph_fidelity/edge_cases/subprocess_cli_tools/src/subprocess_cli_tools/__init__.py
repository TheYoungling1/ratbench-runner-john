"""Shells out to external CLI tools (adb, git, sqlite3) that are neither Python
packages, linked shared libraries (ldd), nor pip build tools — the finding-C
coverage class a graph must discover by scanning subprocess invocations.
"""
import subprocess


def devices() -> str:
    return subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout


def clone(url: str) -> None:
    subprocess.run(["git", "clone", url])


def query(db: str, sql: str) -> str:
    return subprocess.check_output(["sqlite3", db, sql], text=True)
