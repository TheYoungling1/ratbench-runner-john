#!/usr/bin/env python3
"""
Purpose: show a high-level repository structure.
Input: repository path.
Output: directory tree.
"""

import argparse
import sys
from pathlib import Path
from collections import defaultdict


class RepositoryStructure:
    """Repository structure analyzer."""

    def __init__(self, repo_path="/repo"):
        self.repo_path = Path(repo_path)
        self.ignore_dirs = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".idea",
            ".vscode",
            "node_modules",
            "dist",
            "build",
            ".pytest_cache",
            "env",
            ".eggs",
            "*.egg-info",
            ".tox",
            "htmlcov",
            ".coverage",
            ".mypy_cache",
            ".ruff_cache",
            "site-packages",
            "logs",
            "tmp",
            "temp",
            "cache",
            "coverage",
            ".next",
            ".nuxt",
            "target",
            "out",
        }
        self.ignore_files = {
            ".DS_Store",
            "Thumbs.db",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".gitignore",
            ".dockerignore",
            "*.log",
            "*.lock",
            "*.swp",
            "*.swo",
            "*.bak",
            "*.tmp",
            "*.cache",
            ".editorconfig",
            ".prettierrc",
            ".eslintrc",
            ".pylintrc",
            ".flake8",
        }

    def should_ignore(self, path):
        """Return True if this path should be ignored."""
        name = path.name
        # Check directories
        if path.is_dir() and name in self.ignore_dirs:
            return True
        # Check files
        if path.is_file() and name in self.ignore_files:
            return True
        # Hide dotfiles (except env files)
        if name.startswith(".") and name not in {".env", ".env.example"}:
            return True
        return False

    def get_structure(self, path=None, prefix="", max_depth=5, current_depth=0):
        """
        Recursively build directory structure.

        Args:
            path: Path to analyze.
            prefix: Prefix (indentation).
            max_depth: Maximum recursion depth.
            current_depth: Current recursion depth.
        """
        if path is None:
            path = self.repo_path
        if current_depth > max_depth:
            return []

        lines = []
        try:
            items = sorted(
                path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())
            )
        except PermissionError:
            return [f"{prefix}[Permission Denied]"]

        # Filter ignored items
        items = [item for item in items if not self.should_ignore(item)]
        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            current_prefix = "└─ " if is_last else "├─ "
            next_prefix = "    " if is_last else "│   "

            # Render line
            if item.is_dir():
                lines.append(f"{prefix}{current_prefix}{item.name}/")
                # Recurse into subdirectory
                sub_lines = self.get_structure(
                    item, prefix + next_prefix, max_depth, current_depth + 1
                )
                lines.extend(sub_lines)
            else:
                try:
                    size = item.stat().st_size
                    size_str = self.format_size(size)
                    lines.append(f"{prefix}{current_prefix}{item.name} ({size_str})")
                except:
                    lines.append(f"{prefix}{current_prefix}{item.name}")

        return lines

    def format_size(self, size):
        """Format file size."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}TB"

    def get_statistics(self):
        """Get repository statistics."""
        stats = {
            "total_files": 0,
            "total_dirs": 0,
            "file_types": defaultdict(int),
            "total_size": 0,
            "python_files": 0,
            "test_files": 0,
            "config_files": 0,
        }

        for item in self.repo_path.rglob("*"):
            if self.should_ignore(item):
                continue

            if item.is_file():
                stats["total_files"] += 1
                try:
                    stats["total_size"] += item.stat().st_size
                except:
                    pass

                # File type stats
                suffix = item.suffix.lower()
                if suffix:
                    stats["file_types"][suffix] += 1

                # Special file counters
                if suffix == ".py":
                    stats["python_files"] += 1
                    if "test" in item.name.lower():
                        stats["test_files"] += 1
                elif item.name in [
                    "setup.py",
                    "setup.cfg",
                    "pyproject.toml",
                    "requirements.txt",
                    "Dockerfile",
                    "docker-compose.yml",
                    ".env",
                    ".env.example",
                    "config.py",
                    "settings.py",
                ]:
                    stats["config_files"] += 1

            elif item.is_dir():
                stats["total_dirs"] += 1

        return stats

    def find_important_files(self):
        """Find important files."""
        important_patterns = [
            "README.md",
            "README.rst",
            "README.txt",
            "setup.py",
            "setup.cfg",
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "Dockerfile",
            "docker-compose.yml",
            "Makefile",
            ".gitignore",
            "LICENSE",
            "LICENSE.txt",
            "MANIFEST.in",
            "tox.ini",
            ".env.example",
            "config.py",
            "settings.py",
        ]

        found_files = []
        for pattern in important_patterns:
            matches = list(self.repo_path.rglob(pattern))
            for match in matches:
                if not self.should_ignore(match):
                    try:
                        rel_path = match.relative_to(self.repo_path)
                        found_files.append(str(rel_path))
                    except:
                        pass

        return found_files


def ls_structure_main(repo_path="/repo", max_depth=5):
    """
    Main: show repository structure.
    Args:
        repo_path: Repository path.
        max_depth: Max recursion depth.
        show_stats: Whether to show stats.
    Returns:
        0: Success
        1: Failure
    """
    print("=" * 70)
    print("LS Structure:")
    # print("=" * 70)
    analyzer = RepositoryStructure(repo_path)
    # Validate path
    if not analyzer.repo_path.exists():
        print(f"❌ Path does not exist: {repo_path}")
        return 1
    if not analyzer.repo_path.is_dir():
        print(f"❌ Path is not a directory: {repo_path}")
        return 1

    # 2. Important files
    important_files = analyzer.find_important_files()
    if important_files:
        print("Important files:")
        for file in important_files:
            print(f"   • {file}")
        print()

    # 3. Directory tree
    print(f"Directory tree (max depth: {max_depth}):")
    print(f"{analyzer.repo_path.name}/")
    structure_lines = analyzer.get_structure(max_depth=max_depth)
    for line in structure_lines:
        print(line)
    print()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Display repository structure with global view"
    )
    parser.add_argument(
        "--repo", type=str, default="/repo", help="Repository path (default: /repo)"
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=5,
        help="Maximum depth for directory tree (default: 5)",
    )

    args = parser.parse_args()
    sys.exit(ls_structure_main(args.repo, args.depth))
