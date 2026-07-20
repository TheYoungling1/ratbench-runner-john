"""A src-layout package: importable only after `pip install -e .` registers the
src/ tree. A graph that installs the resolved closure but never the repo itself
(finding A) leaves `import srclayout_editable` failing with ModuleNotFoundError.
"""
import click  # a real declared dependency — must appear in the resolved closure

__all__ = ["hello"]


def hello() -> str:
    return click.style("hello", fg="green")
