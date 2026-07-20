import argparse
import warnings
import sys
import re
import json
import ast
from pathlib import Path
import subprocess
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
try:
    from llm import LLMChat
except ImportError:

    class FallbackLLMChat:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return ("LLM is not available", None)

    LLMChat = FallbackLLMChat


warnings.simplefilter("ignore", FutureWarning)


def check_pytest():
    result = subprocess.run(
        "pytest --version", shell=True, text=True, capture_output=True
    )
    if result.returncode == 0:
        return True
    else:
        return False


def extract_pytest_functions(file_path):
    """
    Extract pytest test functions from a test file.

    Returns a list of test functions, each including name, line, docstring, etc.
    """
    test_functions = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                # Find all function definitions
                if isinstance(node, ast.FunctionDef):
                    func_name = node.name
                    # pytest test functions typically start with `test_`
                    if func_name.startswith("test_"):
                        # Extract docstring
                        docstring = ast.get_docstring(node) or ""

                        # Extract decorators
                        decorators = []
                        for decorator in node.decorator_list:
                            if isinstance(decorator, ast.Name):
                                decorators.append(decorator.id)
                            elif isinstance(decorator, ast.Attribute):
                                decorators.append(decorator.attr)
                            elif isinstance(decorator, ast.Call):
                                if isinstance(decorator.func, ast.Name):
                                    decorators.append(decorator.func.id)
                                elif isinstance(decorator.func, ast.Attribute):
                                    decorators.append(decorator.func.attr)

                        # Extract args (for fixture detection)
                        args = [arg.arg for arg in node.args.args]

                        test_functions.append(
                            {
                                "name": func_name,
                                "line": node.lineno,
                                "docstring": docstring,
                                "decorators": decorators,
                                "args": args,
                                "file": str(file_path),
                            }
                        )
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")

    return test_functions


def is_valid_repo(repo_path):
    """
    Check whether a path looks like a valid repository.

    Uses common indicator files/dirs.
    """
    repo_path = Path(repo_path)

    # Common repository indicators
    indicators = [
        ".git",  # Git repo
        "setup.py",
        "pyproject.toml",
        "requirements.txt",  # Python project
        "package.json",  # Node.js project
        "pom.xml",
        "build.gradle",  # Java project
        "Cargo.toml",  # Rust project
        "go.mod",  # Go project
        "CMakeLists.txt",
        "Makefile",  # C/C++ project
        "README.md",
        "README.rst",
        "README.txt",  # docs
    ]

    for indicator in indicators:
        if (repo_path / indicator).exists():
            return True

    # Check for source files
    code_files = (
        list(repo_path.glob("*.py"))
        + list(repo_path.glob("*.js"))
        + list(repo_path.glob("*.java"))
        + list(repo_path.glob("*.go"))
    )

    return len(code_files) > 0


def find_repos_in_directory(base_path, max_depth=3):
    """
    Recursively find repositories in a directory.

    Returns a list of repository paths.
    """
    repos = []
    base_path = Path(base_path)

    def search_repos(path, depth=0):
        if depth > max_depth:
            return

        # Check if current path is a repo
        if is_valid_repo(path):
            # Skip pure test directories (no other code)
            if path.name in ["tests", "test", "__tests__", "testing"]:
                # Check whether it only contains test files
                has_non_test_files = False
                try:
                    for py_file in path.glob("*.py"):
                        if not (
                            py_file.name.startswith("test_")
                            or py_file.name.endswith("_test.py")
                            or py_file.name == "__init__.py"
                        ):
                            has_non_test_files = True
                            break
                    if not has_non_test_files:
                        # Pure test directory; skip
                        return
                except Exception:
                    pass

            repos.append(str(path))
            return  # Stop descending once a repo is found

        # Recurse into subdirectories
        try:
            for item in path.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    # Skip common non-repo dirs
                    skip_dirs = {
                        "node_modules",
                        "venv",
                        ".venv",
                        "__pycache__",
                        "build",
                        "dist",
                    }
                    if item.name not in skip_dirs:
                        search_repos(item, depth + 1)
        except PermissionError:
            pass

    search_repos(base_path)
    return repos


def find_entry_points(repo_path="/repo"):
    """
    Find program entry points in a repository (multi-language).

    Returns a list of entry points including file path, type, and language.
    """
    entry_points = []
    # ==================== Python ====================
    # 1. Find setup.py
    setup_py = Path(repo_path) / "setup.py"
    if setup_py.exists():
        try:
            with open(setup_py, "r", encoding="utf-8") as f:
                content = f.read()
                entry_pattern = r"entry_points\s*=\s*{([^}]+)}"
                matches = re.findall(entry_pattern, content, re.DOTALL)
                if matches:
                    entry_points.append(
                        {
                            "type": "setup.py",
                            "file": str(setup_py),
                            "content": matches[0],
                            "language": "python",
                        }
                    )
        except Exception as e:
            print(f"Error reading setup.py: {e}")

    # 2. Find scripts in pyproject.toml
    pyproject = Path(repo_path) / "pyproject.toml"
    if pyproject.exists():
        try:
            with open(pyproject, "r", encoding="utf-8") as f:
                content = f.read()
                if "[tool.poetry.scripts]" in content or "[project.scripts]" in content:
                    entry_points.append(
                        {
                            "type": "pyproject.toml",
                            "file": str(pyproject),
                            "has_scripts": True,
                            "language": "python",
                        }
                    )
        except Exception as e:
            print(f"Error reading pyproject.toml: {e}")

    # 3. Common Python entry files (including web frameworks)
    python_mains = [
        "__main__.py",
        "main.py",
        "app.py",
        "run.py",
        "cli.py",
        "streamlit_app.py",
        "manage.py",  # Streamlit / Django
        "wsgi.py",
        "asgi.py",  # WSGI/ASGI apps
    ]
    for main_file in python_mains:
        main_path = Path(repo_path) / main_file
        if main_path.exists():
            entry_points.append(
                {
                    "type": "main_file",
                    "file": str(main_path),
                    "name": main_file,
                    "language": "python",
                }
            )
        else:
            for path in Path(repo_path).rglob(main_file):
                if ".venv" not in str(path) and "venv" not in str(path):
                    entry_points.append(
                        {
                            "type": "main_file",
                            "file": str(path),
                            "name": main_file,
                            "language": "python",
                        }
                    )
                    break

    # 4. Find files with `if __name__ == "__main__"`
    for py_file in Path(repo_path).rglob("*.py"):
        if (
            ".venv" in str(py_file)
            or "venv" in str(py_file)
            or "test" in str(py_file).lower()
        ):
            continue
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()
                if re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', content):
                    entry_points.append(
                        {
                            "type": "__main__",
                            "file": str(py_file),
                            "relative": str(py_file.relative_to(repo_path)),
                            "language": "python",
                        }
                    )
        except Exception:
            continue

    # ==================== Java ====================
    # Java main method
    for java_file in Path(repo_path).rglob("*.java"):
        if "test" in str(java_file).lower() or "target" in str(java_file):
            continue
        try:
            with open(java_file, "r", encoding="utf-8") as f:
                content = f.read()
                # Find public static void main(String[] args)
                if re.search(
                    r"public\s+static\s+void\s+main\s*\(\s*String\s*\[\s*\]\s+\w+\s*\)",
                    content,
                ):
                    entry_points.append(
                        {
                            "type": "main_method",
                            "file": str(java_file),
                            "relative": str(java_file.relative_to(repo_path)),
                            "language": "java",
                        }
                    )
        except Exception:
            continue

    # Maven/Gradle config
    pom_xml = Path(repo_path) / "pom.xml"
    if pom_xml.exists():
        entry_points.append(
            {
                "type": "build_config",
                "file": str(pom_xml),
                "name": "pom.xml",
                "language": "java",
            }
        )

    build_gradle = Path(repo_path) / "build.gradle"
    if build_gradle.exists():
        entry_points.append(
            {
                "type": "build_config",
                "file": str(build_gradle),
                "name": "build.gradle",
                "language": "java",
            }
        )

    # ==================== JavaScript/TypeScript ====================
    # package.json
    package_json = Path(repo_path) / "package.json"
    if package_json.exists():
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                package_data = json.load(f)
                if "main" in package_data or "scripts" in package_data:
                    entry_points.append(
                        {
                            "type": "package.json",
                            "file": str(package_json),
                            "main": package_data.get("main", ""),
                            "scripts": package_data.get("scripts", {}),
                            "language": "javascript",
                        }
                    )
        except Exception as e:
            print(f"Error reading package.json: {e}")

    # Common JS/TS entry files
    js_mains = [
        "index.js",
        "main.js",
        "app.js",
        "server.js",
        "index.ts",
        "main.ts",
        "app.ts",
        "server.ts",
    ]
    for main_file in js_mains:
        main_path = Path(repo_path) / main_file
        if main_path.exists():
            entry_points.append(
                {
                    "type": "main_file",
                    "file": str(main_path),
                    "name": main_file,
                    "language": "javascript"
                    if main_file.endswith(".js")
                    else "typescript",
                }
            )

    # ==================== Go ====================
    # Go main package
    for go_file in Path(repo_path).rglob("*.go"):
        if "vendor" in str(go_file) or "test" in str(go_file).lower():
            continue
        try:
            with open(go_file, "r", encoding="utf-8") as f:
                content = f.read()
                # Find `package main` and `func main()`
                if re.search(r"package\s+main", content) and re.search(
                    r"func\s+main\s*\(\s*\)", content
                ):
                    entry_points.append(
                        {
                            "type": "main_package",
                            "file": str(go_file),
                            "relative": str(go_file.relative_to(repo_path)),
                            "language": "go",
                        }
                    )
        except Exception:
            continue

    # go.mod
    go_mod = Path(repo_path) / "go.mod"
    if go_mod.exists():
        entry_points.append(
            {
                "type": "build_config",
                "file": str(go_mod),
                "name": "go.mod",
                "language": "go",
            }
        )

    # ==================== Rust ====================
    # Cargo.toml
    cargo_toml = Path(repo_path) / "Cargo.toml"
    if cargo_toml.exists():
        try:
            with open(cargo_toml, "r", encoding="utf-8") as f:
                content = f.read()
                entry_points.append(
                    {
                        "type": "build_config",
                        "file": str(cargo_toml),
                        "name": "Cargo.toml",
                        "language": "rust",
                    }
                )
        except Exception as e:
            print(f"Error reading Cargo.toml: {e}")

    # main.rs
    for main_rs in Path(repo_path).rglob("main.rs"):
        if "target" not in str(main_rs):
            entry_points.append(
                {
                    "type": "main_file",
                    "file": str(main_rs),
                    "relative": str(main_rs.relative_to(repo_path)),
                    "language": "rust",
                }
            )

    # ==================== C/C++ ====================
    # CMakeLists.txt
    cmake = Path(repo_path) / "CMakeLists.txt"
    if cmake.exists():
        entry_points.append(
            {
                "type": "build_config",
                "file": str(cmake),
                "name": "CMakeLists.txt",
                "language": "cpp",
            }
        )

    # Makefile
    makefile = Path(repo_path) / "Makefile"
    if makefile.exists():
        entry_points.append(
            {
                "type": "build_config",
                "file": str(makefile),
                "name": "Makefile",
                "language": "cpp",
            }
        )

    # main.c/main.cpp
    for main_file in ["main.c", "main.cpp", "main.cc"]:
        for main_path in Path(repo_path).rglob(main_file):
            if "build" not in str(main_path) and "test" not in str(main_path).lower():
                entry_points.append(
                    {
                        "type": "main_file",
                        "file": str(main_path),
                        "relative": str(main_path.relative_to(repo_path)),
                        "language": "cpp",
                    }
                )
                break

    return entry_points


def extract_run_commands_from_docs(repo_path="/repo"):
    """
    Extract run commands from README/docs.

    Returns a dict containing doc path and content.
    """
    doc_files = []
    doc_patterns = [
        "README.md",
        "README.rst",
        "README.txt",
        "README",
        "INSTALL.md",
        "INSTALL.rst",
        "INSTALL.txt",
        "GETTING_STARTED.md",
        "QUICKSTART.md",
        "docs/index.md",
        "docs/README.md",
        "USAGE.md",
        "RUNNING.md",
    ]
    # Find documentation files
    for pattern in doc_patterns:
        doc_path = Path(repo_path) / pattern
        if doc_path.exists():
            try:
                with open(doc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    # Keep up to 5000 chars to avoid overlong input
                    doc_files.append(
                        {
                            "path": str(doc_path),
                            "name": pattern,
                            "content": content[:5000],
                        }
                    )
            except Exception as e:
                print(f"Error reading {doc_path}: {e}")
    # Also look in docs/
    docs_dir = Path(repo_path) / "docs"
    if docs_dir.exists() and docs_dir.is_dir():
        for doc_file in docs_dir.glob("*.md"):
            if len(doc_files) >= 5:  # Read up to 5 docs
                break
            try:
                with open(doc_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    doc_files.append(
                        {
                            "path": str(doc_file),
                            "name": doc_file.name,
                            "content": content[:5000],
                        }
                    )
            except Exception:
                continue

    return doc_files


def parse_run_commands_from_docs_with_llm(
    doc_files, repo_path="/repo", model="deepseek-chat"
):
    """
    Use an LLM to extract run/install commands from docs.

    Returns: parsed commands and notes.
    """
    if not doc_files:
        return {"run_commands": [], "installation_steps": [], "dependencies": []}

    # Build prompt
    prompt = f"""Analyze the project documentation and extract how to run the project and how to install dependencies.
Repository path: {repo_path}
Documentation:
"""

    for doc in doc_files[:3]:  # Analyze up to 3 docs
        prompt += f"\n{'=' * 60}\nFile: {doc['name']}\n{'=' * 60}\n{doc['content']}\n"

    prompt += """

Extract the following:
1. How to run the project (concrete commands)
2. Installation steps (dependency install, environment setup)
3. Frameworks/tools used (streamlit, flask, django, fastapi, ...)
4. Prerequisites before running

Return JSON with the following fields:
{
  "framework": "primary framework/tool (e.g. streamlit, flask, django)",
  "run_commands": [
    {"command": "run command 1", "description": "what it does"},
    {"command": "run command 2", "description": "what it does"}
  ],
  "installation_steps": [
    {"step": "step 1", "command": "command (if any)"},
    {"step": "step 2", "command": "command (if any)"}
  ],
  "dependencies": ["dep1", "dep2"],
  "notes": "other important notes"
}

Important: ensure run commands are complete and directly executable (include framework CLIs like `streamlit run`, `flask run`, etc.)
"""

    try:
        llm = LLMChat(model=model)
        messages = [{"role": "user", "content": prompt}]
        response, usage = llm.chat(messages, temperature=0.0, max_tokens=2048)
        if response:
            # Try to parse JSON
            try:
                # Extract JSON with a flexible regex
                json_match = re.search(r'\{[\s\S]*"framework"[\s\S]*\}', response)
                if json_match:
                    result = json.loads(json_match.group())
                    return result
                else:
                    # If JSON parsing fails, return raw response
                    return {
                        "run_commands": [],
                        "installation_steps": [],
                        "dependencies": [],
                        "raw_response": response,
                    }
            except json.JSONDecodeError:
                return {
                    "run_commands": [],
                    "installation_steps": [],
                    "dependencies": [],
                    "raw_response": response,
                }
        else:
            return {"run_commands": [], "installation_steps": [], "dependencies": []}
    except Exception as e:
        print(f"LLM failed to parse docs: {e}")
        return {
            "run_commands": [],
            "installation_steps": [],
            "dependencies": [],
            "error": str(e),
        }


def analyze_entry_points_with_llm(
    entry_points, repo_path="/repo", model="deepseek-chat"
):
    """
    Use an LLM to analyze entry points and provide recommendations.

    Returns: LLM analysis result.
    """
    if not entry_points:
        return {
            "analysis": "No entry points found",
            "recommendations": [],
            "main_entry": None,
        }

    # Prepare data for LLM
    entry_summary = []
    for i, entry in enumerate(entry_points[:10], 1):  # Limit to 10 entry points
        summary = (
            f"{i}. Language: {entry.get('language', 'unknown')}, Type: {entry['type']}"
        )
        if "name" in entry:
            summary += f", File: {entry['name']}"
        elif "relative" in entry:
            summary += f", Path: {entry['relative']}"
        entry_summary.append(summary)

    # Read excerpts of key files
    file_contents = []
    for entry in entry_points[:5]:
        if entry["type"] in ["main_file", "__main__", "main_method", "main_package"]:
            try:
                with open(entry["file"], "r", encoding="utf-8") as f:
                    content = f.read()[:1000]  # Read up to 1000 chars
                    file_contents.append(
                        {
                            "file": entry.get(
                                "name", entry.get("relative", entry["file"])
                            ),
                            "language": entry.get("language", "unknown"),
                            "content": content,
                        }
                    )
            except Exception:
                continue

    # Build LLM prompt
    prompt = f"""Analyze the repository entry points and provide professional recommendations.

Repository path: {repo_path}

Detected entry points:
{chr(10).join(entry_summary)}

Selected key file excerpts:
"""
    for fc in file_contents[:3]:
        prompt += f"\n--- {fc['file']} ({fc['language']}) ---\n{fc['content']}\n"

    prompt += """

Please analyze:
1. The primary language and project type (web app, CLI, library, ...)
2. The most likely main entry point and why
3. How to run the project (give concrete commands)
4. High-level architecture notes

Return JSON with the following fields:
{
  "main_language": "primary language",
  "project_type": "project type",
  "main_entry": "main entry point path",
  "run_command": "run command",
  "architecture": "architecture notes",
  "recommendations": ["rec1", "rec2"]
}
"""

    try:
        llm = LLMChat(model=model)
        messages = [{"role": "user", "content": prompt}]
        response, usage = llm.chat(messages, temperature=0.0, max_tokens=2048)

        if response:
            # Try to parse JSON
            try:
                # Extract JSON
                json_match = re.search(
                    r'\{[^}]*"main_language"[^}]*\}', response, re.DOTALL
                )
                if json_match:
                    analysis_result = json.loads(json_match.group())
                    return analysis_result
                else:
                    return {
                        "analysis": response,
                        "recommendations": [],
                        "main_entry": None,
                        "raw_response": response,
                    }
            except json.JSONDecodeError:
                return {
                    "analysis": response,
                    "recommendations": [],
                    "main_entry": None,
                    "raw_response": response,
                }
        else:
            return {
                "analysis": "LLM analysis failed",
                "recommendations": [],
                "main_entry": None,
            }
    except Exception as e:
        print(f"LLM analysis error: {e}")
        return {
            "analysis": f"LLM analysis error: {str(e)}",
            "recommendations": [],
            "main_entry": None,
        }


def find_existing_tests(repo_path="/repo"):
    """
    Find existing tests and test directories.

    Returns a test info dict.
    """
    test_info = {
        "has_tests": False,
        "test_dirs": [],
        "test_files": [],
        "test_functions": [],  # Extracted test functions
        "test_framework": None,  # pytest, unittest, nose, ...
    }
    # Find test directories
    test_dir_patterns = ["tests", "test", "testing", "__tests__"]
    for pattern in test_dir_patterns:
        for path in Path(repo_path).glob(f"**/{pattern}"):
            if path.is_dir():
                test_info["test_dirs"].append(str(path))
                test_info["has_tests"] = True

    # Find test files
    for py_file in Path(repo_path).rglob("test_*.py"):
        if ".venv" not in str(py_file) and "venv" not in str(py_file):
            test_info["test_files"].append(str(py_file))
            test_info["has_tests"] = True

    for py_file in Path(repo_path).rglob("*_test.py"):
        if ".venv" not in str(py_file) and "venv" not in str(py_file):
            if str(py_file) not in test_info["test_files"]:  # Avoid duplicates
                test_info["test_files"].append(str(py_file))
                test_info["has_tests"] = True

    # Detect test framework
    if test_info["has_tests"]:
        # Check pytest usage
        for test_file in test_info["test_files"][:5]:  # Only check first 5 files
            try:
                with open(test_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    if "import pytest" in content or "from pytest" in content:
                        test_info["test_framework"] = "pytest"
                        break
                    elif "import unittest" in content or "from unittest" in content:
                        test_info["test_framework"] = "unittest"
            except Exception:
                continue

        # Check config files
        if (
            Path(repo_path, "pytest.ini").exists()
            or Path(repo_path, "setup.cfg").exists()
        ):
            test_info["test_framework"] = "pytest"
        elif Path(repo_path, "tox.ini").exists():
            test_info["test_framework"] = "pytest"  # tox commonly uses pytest

    # Extract pytest test functions
    if test_info["test_framework"] == "pytest":
        print("  Extracting pytest test functions...")
        for test_file in test_info["test_files"]:
            functions = extract_pytest_functions(test_file)
            test_info["test_functions"].extend(functions)

    return test_info


def generate_simple_test(repo_path="/repo", entry_points=None):
    """
    If there are no tests, generate a simple test case.
    """
    if not entry_points:
        return None

    test_dir = Path(repo_path) / "tests"
    test_dir.mkdir(exist_ok=True)

    # Create __init__.py
    init_file = test_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("")

    # Generate a basic import test
    test_file = test_dir / "test_basic.py"

    test_content = '''"""
Auto-generated basic tests.

Used to validate basic imports and environment setup.
"""
import pytest
import sys
from pathlib import Path

def test_python_version():
    """Test Python version."""
    assert sys.version_info >= (3, 6), "Python version should be >= 3.6"

def test_repo_structure():
    """Test repository structure."""
    repo_path = Path(__file__).parent.parent
    assert repo_path.exists(), "Repository path should exist"
'''

    # Add import checks for up to 3 entry points
    for i, entry in enumerate(entry_points[:3]):
        if entry["type"] == "main_file":
            file_path = Path(entry["file"])
            try:
                relative_path = file_path.relative_to(repo_path)
                module_path = str(relative_path).replace("/", ".").replace(".py", "")
                test_content += f'''
def test_import_entry_{i}():
    """Test importing entry point: {entry.get("name", "unknown")}"""
    try:
        import {module_path.split(".")[0]}
        assert True
    except ImportError as e:
        pytest.skip(f"Cannot import module: {{e}}")
'''
            except Exception:
                continue

    test_file.write_text(test_content)
    return str(test_file)


def suggest_test_commands(
    test_info, entry_points, repo_path="/repo", doc_analysis=None
):
    """
    Suggest test/run commands based on analysis.

    Prefer run commands extracted from documentation.
    """
    commands = []

    # 0. Prefer run commands extracted from docs
    if doc_analysis and doc_analysis.get("run_commands"):
        for cmd_info in doc_analysis["run_commands"]:
            commands.append(
                {
                    "command": cmd_info["command"],
                    "description": cmd_info.get(
                        "description", "Run command extracted from documentation"
                    ),
                    "type": "run",
                    "source": "documentation",
                }
            )

    # 1. If tests exist
    if test_info["has_tests"]:
        if test_info["test_framework"] == "pytest":
            commands.append(
                {
                    "command": "pytest",
                    "description": "Run all pytest tests",
                    "type": "test",
                }
            )
            commands.append(
                {
                    "command": "pytest -v",
                    "description": "Run pytest in verbose mode",
                    "type": "test",
                }
            )
            commands.append(
                {
                    "command": "pytest --collect-only",
                    "description": "Collect all tests (do not run)",
                    "type": "collect",
                }
            )
        elif test_info["test_framework"] == "unittest":
            commands.append(
                {
                    "command": "python -m unittest discover",
                    "description": "Run all unittest tests",
                    "type": "test",
                }
            )
        else:
            # Default to pytest
            commands.append(
                {
                    "command": "pytest",
                    "description": "Try running tests with pytest",
                    "type": "test",
                }
            )

    # 2. Check config files for test configuration
    for config_file in ["pyproject.toml", "setup.cfg", "tox.ini"]:
        config_path = Path(repo_path) / config_file
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if "pytest" in content:
                        commands.append(
                            {
                                "command": "pytest",
                                "description": f"Detected pytest config in {config_file}",
                                "type": "test",
                            }
                        )
                        break
            except Exception:
                continue

    # 3. Poetry projects
    if Path(repo_path, "poetry.lock").exists():
        commands.append(
            {
                "command": "poetry run pytest",
                "description": "Run pytest via Poetry",
                "type": "test",
            }
        )

    # 4. Suggest run commands for entry points
    for entry in entry_points[:3]:
        if entry["type"] == "main_file":
            file_path = Path(entry["file"])
            try:
                relative_path = file_path.relative_to(repo_path)
                commands.append(
                    {
                        "command": f"python {relative_path}",
                        "description": f"Run main program: {entry.get('name', relative_path)}",
                        "type": "run",
                    }
                )
            except Exception:
                continue

    return commands


def construct_test_main(repo_path="/repo", mode="llm"):
    """
    Main entry point: analyze a repository and suggest tests.
    """
    print("=" * 60)
    print("🔍 Construct Test - Analyze repo and suggest tests")
    print("=" * 60)
    result = {
        "entry_points": [],
        "test_info": {},
        "suggested_commands": [],
        "created_test": None,
    }

    # 0. Try finding nested repos first
    print("\n📌 Checking directory structure...")
    repos = find_repos_in_directory(repo_path, max_depth=3)
    if repos:
        print(f"✅ Found {len(repos)} repo(s):")
        for i, repo in enumerate(repos, 1):
            print(f"  {i}. {repo}")

        # If multiple repos are found, analyze the first
        if len(repos) > 1:
            print(f"\n⚠️  Multiple repos detected; analyzing first: {repos[0]}")
            repo_path = repos[0]
        elif len(repos) == 1:
            print(f"\n✅ Analyzing repo: {repos[0]}")
            repo_path = repos[0]
    else:
        print(f"✅ Analysis path: {repo_path}")

    # 1. Find entry points
    print("\n📌 Finding entry points...")
    entry_points = find_entry_points(repo_path)
    result["entry_points"] = entry_points
    if entry_points:
        print(f"✅ Found {len(entry_points)} entry point(s):")
        # Group by language
        by_language = {}
        for entry in entry_points:
            lang = entry.get("language", "unknown")
            if lang not in by_language:
                by_language[lang] = []
            by_language[lang].append(entry)

        for lang, entries in by_language.items():
            print(f"\n  [{lang.upper()}] - {len(entries)} entry point(s):")
            for i, entry in enumerate(entries[:3], 1):
                if entry["type"] == "main_file":
                    print(f"    {i}. Main file: {entry.get('name', 'unknown')}")
                elif entry["type"] == "__main__":
                    print(f"    {i}. __main__: {entry.get('relative', 'unknown')}")
                elif entry["type"] == "main_method":
                    print(f"    {i}. main method: {entry.get('relative', 'unknown')}")
                elif entry["type"] == "main_package":
                    print(f"    {i}. main package: {entry.get('relative', 'unknown')}")
                else:
                    print(f"    {i}. {entry['type']}: {entry.get('name', 'unknown')}")
    else:
        print("⚠️  No clear entry points found")

    # 2. Find existing tests
    print("\n📌 Finding existing tests...")
    test_info = find_existing_tests(repo_path)
    result["test_info"] = test_info

    if test_info["has_tests"]:
        print("✅ Existing tests found:")
        print(f"  - Test dirs: {len(test_info['test_dirs'])}")
        print(f"  - Test files: {len(test_info['test_files'])}")
        if test_info["test_framework"]:
            print(f"  - Test framework: {test_info['test_framework']}")

        # Show test functions
        if test_info.get("test_functions"):
            print(f"  - Test functions: {len(test_info['test_functions'])}")
            print("\n  Test function list (top 10):")
            for i, func in enumerate(test_info["test_functions"][:10], 1):
                print(f"    {i}. {func['name']} (line {func['line']})")
                if func["docstring"]:
                    # Only show the first docstring line
                    doc_first_line = func["docstring"].split("\n")[0].strip()
                    if doc_first_line:
                        print(f"       Doc: {doc_first_line}")
                if func["decorators"]:
                    print(f"       Decorators: {', '.join(func['decorators'])}")
    else:
        print("⚠️  No existing tests found")
        # Do not auto-create test_basic.py; use suggested commands instead
        # created_test = generate_simple_test(repo_path, entry_points)
        # if created_test:
        #     result['created_test'] = created_test
        #     print(f"✅ Created basic test file: {created_test}")
        #     test_info = find_existing_tests(repo_path)
        #     result['test_info'] = test_info

    # 3. Extract run instructions from docs (LLM)
    doc_analysis = None
    if mode == "llm":
        print("\n📌 Extracting how-to-run from docs...")
        doc_files = extract_run_commands_from_docs(repo_path)
        if doc_files:
            print(f"✅ Found {len(doc_files)} doc file(s):")
            for doc in doc_files:
                print(f"  - {doc['name']}")

            print("\n  Using LLM to parse docs...")
            doc_analysis = parse_run_commands_from_docs_with_llm(doc_files, repo_path)
            result["doc_analysis"] = doc_analysis

            if doc_analysis.get("framework"):
                print("\n✅ Doc analysis:")
                print(f"  - Framework: {doc_analysis.get('framework', 'N/A')}")

                if doc_analysis.get("run_commands"):
                    print("\n  Run commands:")
                    for i, cmd in enumerate(doc_analysis["run_commands"], 1):
                        print(f"    {i}. {cmd['command']}")
                        if cmd.get("description"):
                            print(f"       Note: {cmd['description']}")

                if doc_analysis.get("installation_steps"):
                    print("\n  Install steps:")
                    for i, step in enumerate(doc_analysis["installation_steps"][:5], 1):
                        print(f"    {i}. {step['step']}")
                        if step.get("command"):
                            print(f"       Command: {step['command']}")

                if doc_analysis.get("dependencies"):
                    deps = doc_analysis["dependencies"]
                    if deps:
                        print(f"\n  Dependencies: {', '.join(deps[:5])}")

                if doc_analysis.get("notes"):
                    print(f"\n  Notes: {doc_analysis['notes']}")

            elif doc_analysis.get("raw_response"):
                print("\n✅ Doc parsing:")
                print(f"  {doc_analysis['raw_response'][:300]}")
        else:
            print("⚠️  No README or docs found")

    # 4. LLM entry-point analysis (llm mode)
    if mode == "llm" and entry_points:
        print("\n📌 LLM analyzing entry points...")
        llm_analysis = analyze_entry_points_with_llm(entry_points, repo_path)
        result["llm_analysis"] = llm_analysis

        if llm_analysis.get("main_language"):
            print("LLM analysis:")
            print(f"  - Main language: {llm_analysis.get('main_language', 'N/A')}")
            print(f"  - Project type: {llm_analysis.get('project_type', 'N/A')}")
            print(f"  - Main entry: {llm_analysis.get('main_entry', 'N/A')}")
            print(f"  - Run command: {llm_analysis.get('run_command', 'N/A')}")
            if llm_analysis.get("architecture"):
                print(f"  - Architecture: {llm_analysis['architecture']}")
            if llm_analysis.get("recommendations"):
                print("\n  Recommendations:")
                for i, rec in enumerate(llm_analysis["recommendations"], 1):
                    print(f"    {i}. {rec}")
        elif llm_analysis.get("raw_response"):
            print("LLM analysis (raw):")
            print(f"  {llm_analysis['raw_response'][:500]}")

    # 5. Suggest commands
    print("\nSuggested commands:")
    commands = suggest_test_commands(test_info, entry_points, repo_path, doc_analysis)
    result["suggested_commands"] = commands

    if commands:
        for i, cmd in enumerate(commands, 1):
            print(f"  {i}. {cmd['command']}")
            print(f"     Description: {cmd['description']}")
            print(f"     Type: {cmd['type']}")
    else:
        print("⚠️  Could not suggest commands automatically; please configure manually")

    # # 6. Print summary
    # print("\n" + "=" * 60)
    # print("Summary:")
    # print(f"  - Entry points: {len(entry_points)}")
    # print(f"  - Has tests: {'yes' if test_info['has_tests'] else 'no'}")
    # print(f"  - Suggested commands: {len(commands)}")
    # if result['created_test']:
    #     print(f"  - Created test: {result['created_test']}")
    # print("=" * 60)

    # 7. Save results to file (/repo/logs)
    logs_dir = Path(repo_path) / "logs"
    try:
        logs_dir.mkdir(exist_ok=True)  # Ensure logs/ exists
    except Exception as e:
        print(f"⚠️  Failed to create logs directory: {e}")

    result_file = logs_dir / "construct_test_result.json"
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Results saved to: {result_file}")
    except Exception as e:
        print(f"⚠️  Failed to save results: {e}")
        import traceback

        traceback.print_exc()

    # 8. Return primary recommended command
    if commands:
        primary_cmd = commands[0]["command"]
        print(f"\nRecommended first command: {primary_cmd}")
        return 0
    else:
        return 1


def construct_test_main_quiet(repo_path="/repo", mode="llm"):
    """
    Quiet mode: only print the recommended run command.
    """
    # 1. Extract run commands from docs
    doc_analysis = None
    if mode == "llm":
        doc_files = extract_run_commands_from_docs(repo_path)
        if doc_files:
            doc_analysis = parse_run_commands_from_docs_with_llm(doc_files, repo_path)

            # Prefer run commands from docs
            if doc_analysis and doc_analysis.get("run_commands"):
                for cmd in doc_analysis["run_commands"]:
                    print(cmd["command"])
                return 0

    # 2. If docs have nothing, infer from entry points
    entry_points = find_entry_points(repo_path)
    test_info = find_existing_tests(repo_path)

    commands = suggest_test_commands(test_info, entry_points, repo_path, doc_analysis)

    if commands:
        # Only print the first recommended command
        print(commands[0]["command"])
        return 0
    else:
        print("# No run command found")
        return 1


def test_by_pytest():
    filepath = "<omitted>"
    success = check_pytest()
    if not success:
        print(
            "Pytest is not installed in your environment. Please install the latest version of pytest using `pip install pytest`."
        )
        sys.exit(100)
    # if not os.path.exists('/home/tools/.test_func'):
    try:
        # with open('/home/tools/.test_func', 'w') as file:、
        with open(f"{filepath}", "w") as file:
            # Use subprocess.run and write stdout/stderr to file
            result = subprocess.run(
                # ['pytest', '/repo', '--collect-only', '-q', '--disable-warnings'],
                ["pytest", "--collect-only", "-q", "--disable-warnings"],
                cwd="<omitted>",
                stdout=file,
                stderr=subprocess.STDOUT,
            )
        if result.returncode == 5:
            print(
                "No unit tests were detected in this repository, so it passes. Congratulations, you have successfully configured the environment!"
            )
            sys.exit(5)
        if result.returncode != 0:
            print(
                "Error: Please modify the configuration according to the error messages below. Once all issues are resolved, rerun the tests."
            )
            subprocess.run(f"cat {filepath}", shell=True)
            sys.exit(result.returncode)
        else:
            print("Congratulations, you have successfully configured the environment!")
            subprocess.run(f"cat {filepath}", shell=True)
            sys.exit(0)
    except Exception as e:
        print(e)
        subprocess.run(f"rm -rf {filepath}", shell=True)
        print(
            "Error: Please modify the configuration according to the error messages below. Once all issues are resolved, rerun the tests."
        )
        sys.exit(200)
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Construct test cases and find entry points"
    )
    parser.add_argument("--repo", type=str, default="/repo", help="Repository path")
    parser.add_argument("--mode", type=str, default="llm", help="llm or pytest")
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only output the recommended run command",
    )
    args = parser.parse_args()
    if args.mode == "pytest":
        # pytest mode: only run pytest checks
        test_by_pytest()
    else:
        # llm/other mode: run full analysis
        if args.quiet:
            # Quiet mode: only output the recommended command
            result = construct_test_main_quiet(args.repo, args.mode)
            sys.exit(result)
        else:
            sys.exit(construct_test_main(args.repo, args.mode))
