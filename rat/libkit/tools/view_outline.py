#!/usr/bin/env python3
"""
view_outline tool implementation.
Purpose: show an outline of code files (classes/functions/definitions only; no bodies).
Input: file path or directory path.
Output: code structure outline (signatures of classes/functions/methods).
"""

import argparse
import ast
import sys
import re
from pathlib import Path
from typing import List, Dict, Optional, Union


class OutlineExtractor:
    def __init__(self, repo_path: Union[str, Path] = "/repo"):
        self.repo_path = Path(repo_path)
        self.outline = []

    def extract_file_outline(self, file_path: Path) -> Dict:
        """Extract outline for a single file."""
        result = {"file": str(file_path), "language": file_path.suffix, "elements": []}

        if file_path.suffix == ".py":
            result["elements"] = self._extract_python_outline(file_path)
        elif file_path.suffix in [".js", ".ts", ".jsx", ".tsx"]:
            result["elements"] = self._extract_javascript_outline(file_path)
        elif file_path.suffix in [".java"]:
            result["elements"] = self._extract_java_outline(file_path)
        else:
            # Generic extraction (regex-based)
            result["elements"] = self._extract_generic_outline(file_path)

        return result

    def _extract_python_outline(self, file_path: Path) -> List[Dict]:
        """Extract outline for a Python file."""
        elements = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                tree = ast.parse(content, filename=str(file_path))
            imports = []
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = ", ".join([alias.name for alias in node.names])
                    imports.append(f"from {module} import {names}")
            if imports:
                elements.append({"type": "imports", "content": imports, "lineno": 1})
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    class_info = self._extract_class_info(node)
                    elements.append(class_info)
                elif isinstance(node, ast.FunctionDef) or isinstance(
                    node, ast.AsyncFunctionDef
                ):
                    # Only extract module-level functions (not inside classes)
                    func_info = self._extract_function_info(node)
                    elements.append(func_info)

        except SyntaxError as e:
            # If AST parsing fails, fall back to regex
            print(f"⚠️ AST parsing failed; using regex: {file_path}")
            return self._extract_python_outline_regex(file_path)
        except Exception as e:
            print(f"⚠️ Error extracting outline for {file_path}: {e}")
            return []

        return elements

    def _extract_class_info(self, node: ast.ClassDef) -> Dict:
        """Extract class info."""
        # Base classes
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                if isinstance(base.value, ast.Name):
                    bases.append(f"{base.value.id}.{base.attr}")
                else:
                    bases.append(base.attr)

        bases_str = f"({', '.join(bases)})" if bases else ""

        # Docstring
        docstring = ast.get_docstring(node) or ""

        # Methods
        methods = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_info = self._extract_function_info(item, is_method=True)
                methods.append(method_info)

        return {
            "type": "class",
            "name": node.name,
            "bases": bases_str,
            "signature": f"class {node.name}{bases_str}:",
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "docstring": docstring.split("\n")[0]
            if docstring
            else "",  # First line only
            "methods": methods,
        }

    def _extract_function_info(self, node, is_method=False) -> Dict:
        """Extract function/method info."""
        # Build signature
        args_list = []

        # Args
        for arg in node.args.args:
            arg_str = arg.arg
            # Type annotations (if present)
            if arg.annotation:
                if isinstance(arg.annotation, ast.Name):
                    arg_str += f": {arg.annotation.id}"
                elif isinstance(arg.annotation, ast.Attribute):
                    arg_str += f": {arg.annotation.attr}"
            args_list.append(arg_str)

        # *args
        if node.args.vararg:
            args_list.append(f"*{node.args.vararg.arg}")

        # **kwargs
        if node.args.kwarg:
            args_list.append(f"**{node.args.kwarg.arg}")

        args_str = ", ".join(args_list)

        # Return annotation
        return_type = ""
        if node.returns:
            if isinstance(node.returns, ast.Name):
                return_type = f" -> {node.returns.id}"
            elif isinstance(node.returns, ast.Attribute):
                return_type = f" -> {node.returns.attr}"

        # Async
        async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""

        signature = f"{async_prefix}def {node.name}({args_str}){return_type}:"

        # Docstring
        docstring = ast.get_docstring(node) or ""

        # Decorators
        decorators = []
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name):
                decorators.append(f"@{decorator.id}")
            elif isinstance(decorator, ast.Call) and isinstance(
                decorator.func, ast.Name
            ):
                decorators.append(f"@{decorator.func.id}")

        return {
            "type": "method" if is_method else "function",
            "name": node.name,
            "signature": signature,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "docstring": docstring.split("\n")[0] if docstring else "",
            "decorators": decorators,
        }

    def _extract_python_outline_regex(self, file_path: Path) -> List[Dict]:
        """Extract Python outline using regex (fallback)."""
        elements = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            current_class = None
            indent_stack = []

            for i, line in enumerate(lines, 1):
                stripped = line.lstrip()
                if not stripped or stripped.startswith("#"):
                    continue

                indent = len(line) - len(stripped)

                # Match class definitions
                class_match = re.match(r"^class\s+(\w+)(\([^)]*\))?:", stripped)
                if class_match:
                    class_name = class_match.group(1)
                    bases = class_match.group(2) or ""
                    current_class = {
                        "type": "class",
                        "name": class_name,
                        "signature": f"class {class_name}{bases}:",
                        "lineno": i,
                        "methods": [],
                    }
                    elements.append(current_class)
                    indent_stack = [indent]
                    continue

                # Match function/method definitions
                func_match = re.match(
                    r"^(async\s+)?def\s+(\w+)\s*\((.*?)\)(\s*->\s*[^:]+)?:", stripped
                )
                if func_match:
                    is_async = func_match.group(1) is not None
                    func_name = func_match.group(2)
                    args = func_match.group(3)
                    return_type = func_match.group(4) or ""
                    async_prefix = "async " if is_async else ""
                    signature = f"{async_prefix}def {func_name}({args}){return_type}:"

                    func_info = {
                        "type": "function",
                        "name": func_name,
                        "signature": signature,
                        "lineno": i,
                    }

                    # Determine whether it's a method (inside a class)
                    if current_class and indent > indent_stack[0]:
                        func_info["type"] = "method"
                        current_class["methods"].append(func_info)
                    else:
                        elements.append(func_info)
                        current_class = None

        except Exception as e:
            print(f"⚠️ Regex extraction failed: {e}")

        return elements

    def _extract_javascript_outline(self, file_path: Path) -> List[Dict]:
        """Extract outline for a JavaScript/TypeScript file."""
        elements = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue

                # Match class definitions
                class_match = re.match(
                    r"^(export\s+)?(default\s+)?class\s+(\w+)(\s+extends\s+\w+)?",
                    stripped,
                )
                if class_match:
                    class_name = class_match.group(3)
                    extends = class_match.group(4) or ""
                    elements.append(
                        {
                            "type": "class",
                            "name": class_name,
                            "signature": f"class {class_name}{extends}",
                            "lineno": i,
                        }
                    )

                # Match function definitions
                func_patterns = [
                    r"^(export\s+)?(async\s+)?function\s+(\w+)\s*\((.*?)\)",  # function foo()
                    r"^(export\s+)?const\s+(\w+)\s*=\s*(async\s+)?\((.*?)\)\s*=>",  # const foo = () =>
                    r"^(\w+)\s*\((.*?)\)\s*\{",  # method()
                ]

                for pattern in func_patterns:
                    match = re.match(pattern, stripped)
                    if match:
                        if pattern == func_patterns[0]:
                            func_name = match.group(3)
                        elif pattern == func_patterns[1]:
                            func_name = match.group(2)
                        else:
                            func_name = match.group(1)
                        if func_name and func_name not in [
                            "if",
                            "for",
                            "while",
                            "switch",
                        ]:
                            elements.append(
                                {
                                    "type": "function",
                                    "name": func_name,
                                    "signature": stripped,
                                    "lineno": i,
                                }
                            )
                            break

        except Exception as e:
            print(f"⚠️ Failed to extract JavaScript outline: {e}")

        return elements

    def _extract_java_outline(self, file_path: Path) -> List[Dict]:
        """Extract outline for a Java file."""
        elements = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                # Match class definitions
                class_match = re.match(
                    r"^(public|private|protected)?\s*(abstract|final)?\s*class\s+(\w+)(\s+extends\s+\w+)?(\s+implements\s+[\w,\s]+)?",
                    stripped,
                )
                if class_match:
                    class_name = class_match.group(3)
                    elements.append(
                        {
                            "type": "class",
                            "name": class_name,
                            "signature": stripped,
                            "lineno": i,
                        }
                    )
                # Match method definitions
                method_match = re.match(
                    r"^(public|private|protected)?\s*(static)?\s*([\w<>\[\]]+)\s+(\w+)\s*\(",
                    stripped,
                )
                if method_match and not stripped.startswith("class"):
                    method_name = method_match.group(4)
                    elements.append(
                        {
                            "type": "method",
                            "name": method_name,
                            "signature": stripped,
                            "lineno": i,
                        }
                    )

        except Exception as e:
            print(f"⚠️ Failed to extract Java outline: {e}")

        return elements

    def _extract_generic_outline(self, file_path: Path) -> List[Dict]:
        """Generic outline extraction (for other languages)."""
        elements = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # Generic class/function patterns
            patterns = [
                (r"^class\s+(\w+)", "class"),
                (r"^def\s+(\w+)", "function"),
                (r"^function\s+(\w+)", "function"),
                (r"^(public|private|protected)\s+class\s+(\w+)", "class"),
            ]

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                for pattern, elem_type in patterns:
                    match = re.match(pattern, stripped)
                    if match:
                        name = (
                            match.group(1)
                            if elem_type != "class" or "public" not in pattern
                            else match.group(2)
                        )
                        elements.append(
                            {
                                "type": elem_type,
                                "name": name,
                                "signature": stripped,
                                "lineno": i,
                            }
                        )
                        break

        except Exception as e:
            print(f"⚠️ Generic extraction failed: {e}")

        return elements

    def format_outline(self, outline_data: Dict, show_line_numbers: bool = True) -> str:
        """Format outline output."""
        output = []

        try:
            relative_path = Path(outline_data["file"]).relative_to(self.repo_path)
        except ValueError:
            relative_path = outline_data["file"]

        output.append(f"\n{relative_path}")

        if not outline_data["elements"]:
            return "\n".join(output)

        for element in outline_data["elements"]:
            elem_type = element["type"]

            if elem_type == "imports":
                # Skip imports
                continue

            elif elem_type == "class":
                line_info = f":L{element['lineno']}" if show_line_numbers else ""
                output.append(f"  class {element['name']}{line_info}")

                # Methods
                methods = element.get("methods", [])
                for method in methods:
                    method_line = f":L{method['lineno']}" if show_line_numbers else ""
                    decorators = method.get("decorators", [])
                    decorator_str = " ".join(decorators) + " " if decorators else ""
                    output.append(
                        f"    {decorator_str}{method['signature']}{method_line}"
                    )

            elif elem_type == "function":
                line_info = f":L{element['lineno']}" if show_line_numbers else ""
                decorators = element.get("decorators", [])
                decorator_str = " ".join(decorators) + " " if decorators else ""
                output.append(f"  {decorator_str}{element['signature']}{line_info}")

        return "\n".join(output)


def view_outline_main(
    target_path: str,
    recursive: bool = False,
    show_line_numbers: bool = True,
    file_extensions: Optional[List[str]] = None,
):
    """
    Main entry point: view code outline.

    Args:
        target_path: Target file or directory.
        recursive: Whether to process directories recursively.
        show_line_numbers: Whether to show line numbers.
        file_extensions: List of file extensions to include.

    Returns:
        0: success
        1: failure
    """
    target = Path(target_path)

    # Use the target itself as repo_path (for relative paths)
    if target.is_file():
        repo_path = target.parent
    else:
        repo_path = target

    extractor = OutlineExtractor(repo_path)

    if not target.exists():
        print(f"Error: Path not found: {target_path}")
        return 1

    # Determine files to process
    files_to_process = []
    if target.is_file():
        files_to_process.append(target)
    elif target.is_dir():
        if recursive:
            pattern = "**/*"
        else:
            pattern = "*"

        for file_path in target.glob(pattern):
            if file_path.is_file():
                # Filter by extension
                if file_extensions and file_path.suffix not in file_extensions:
                    continue
                # Skip common non-code files
                if file_path.suffix in [".pyc", ".pyo", ".so", ".dll", ".exe"]:
                    continue
                files_to_process.append(file_path)

    if not files_to_process:
        print("No files to process")
        return 1

    # Process each file
    for file_path in files_to_process:
        outline_data = extractor.extract_file_outline(file_path)
        formatted_output = extractor.format_outline(outline_data, show_line_numbers)
        print(formatted_output)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="View code outline - extract class and function definitions"
    )
    parser.add_argument(
        "path",
        type=str,
        nargs="?",
        default=".",
        help="File or directory path to scan (default: current directory)",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="Recursively process directories"
    )
    parser.add_argument(
        "--no-line-numbers", action="store_true", help="Do not show line numbers"
    )
    parser.add_argument(
        "-e",
        "--extensions",
        type=str,
        nargs="+",
        help="File extensions to process (e.g., .py .js .java)",
    )

    args = parser.parse_args()

    sys.exit(
        view_outline_main(
            args.path, args.recursive, not args.no_line_numbers, args.extensions
        )
    )
