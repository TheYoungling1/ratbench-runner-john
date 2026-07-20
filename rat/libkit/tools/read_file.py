#!/usr/bin/env python3
"""read_file tool implementation.

Purpose: deeply understand and analyze a file.
Input: a file path and an optional analysis question.
Output: analysis, summary, key points, etc.
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Dict, Optional
import ast
import re

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
from llm import LLMChat


class FileAnalyzer:
    """File analyzer."""

    def __init__(self, llm_name="deepseek-chat"):
        self.llm = LLMChat(llm_name)
        self.max_file_size = 100000

    def read_file_content(self, file_path: Path) -> Optional[str]:
        """Read file content."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            if len(content) > self.max_file_size:
                print(
                    f"⚠️ File too large ({len(content)} chars); truncating to first {self.max_file_size} chars"
                )
                content = content[: self.max_file_size]

            return content
        except Exception as e:
            print(f"❌ Failed to read file: {e}")
            return None

    def extract_file_metadata(self, file_path: Path, content: str) -> Dict:
        """Extract file metadata."""
        metadata = {
            "file_name": file_path.name,
            "file_path": str(file_path),
            "file_extension": file_path.suffix,
            "file_size": len(content),
            "line_count": len(content.split("\n")),
            "language": self._detect_language(file_path.suffix),
        }

        # Extra info for Python files
        if file_path.suffix == ".py":
            metadata["python_info"] = self._analyze_python_metadata(content)
        return metadata

    def _detect_language(self, extension: str) -> str:
        """Detect language by file extension."""
        language_map = {
            ".py": "Python",
            ".js": "JavaScript",
            ".ts": "TypeScript",
            ".jsx": "React/JSX",
            ".tsx": "React/TypeScript",
            ".java": "Java",
            ".cpp": "C++",
            ".c": "C",
            ".go": "Go",
            ".rs": "Rust",
            ".rb": "Ruby",
            ".php": "PHP",
            ".swift": "Swift",
            ".kt": "Kotlin",
            ".md": "Markdown",
            ".json": "JSON",
            ".yaml": "YAML",
            ".yml": "YAML",
            ".toml": "TOML",
            ".xml": "XML",
            ".html": "HTML",
            ".css": "CSS",
            ".sh": "Shell",
            ".sql": "SQL",
        }
        return language_map.get(extension.lower(), "Unknown")

    def _analyze_python_metadata(self, content: str) -> Dict:
        """Analyze Python file metadata."""
        info = {
            "imports": [],
            "classes": [],
            "functions": [],
            "has_main": False,
            "docstring": None,
        }

        try:
            tree = ast.parse(content)

            # Extract module docstring
            info["docstring"] = ast.get_docstring(tree)

            # Walk AST
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        info["imports"].append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    info["imports"].append(module)
                elif isinstance(node, ast.ClassDef):
                    info["classes"].append(node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info["functions"].append(node.name)
                    if node.name == "__main__":
                        info["has_main"] = True

            info["imports"] = list(set(info["imports"]))
            info["classes"] = list(set(info["classes"]))
            info["functions"] = list(set(info["functions"]))

        except Exception as e:
            print(f"⚠️ Python AST analysis failed: {e}")

        return info

    def analyze_with_llm(
        self, content: str, metadata: Dict, question: Optional[str] = None
    ) -> Dict:
        """Analyze file content with an LLM."""
        # Build analysis prompt
        prompt = self._build_analysis_prompt(content, metadata, question)

        # Call LLM
        try:
            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a code analysis assistant. Provide concise, structured analysis optimized for LLM agents to understand and use.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            # Parse response
            analysis = self._parse_llm_response(response)
            return analysis

        except Exception as e:
            print(f"Error: LLM analysis failed: {e}")
            return {"error": str(e)}

    def _build_analysis_prompt(
        self, content: str, metadata: Dict, question: Optional[str] = None
    ) -> str:
        """Build LLM analysis prompt."""
        prompt_parts = []
        prompt_parts.append(f"## File Information")
        prompt_parts.append(f"- File: {metadata['file_name']}")
        prompt_parts.append(f"- Language: {metadata['language']}")
        prompt_parts.append(f"- Size: {metadata['line_count']} lines")

        # Add Python-specific info
        if "python_info" in metadata:
            py_info = metadata["python_info"]
            if py_info["classes"]:
                prompt_parts.append(f"- Classes: {', '.join(py_info['classes'][:10])}")
            if py_info["functions"]:
                prompt_parts.append(f"- Functions: {len(py_info['functions'])}")

        prompt_parts.append("")
        prompt_parts.append("## File Content")
        prompt_parts.append("```")
        prompt_parts.append(content)
        prompt_parts.append("```")
        prompt_parts.append("")

        # Analysis task
        if question:
            # User question
            prompt_parts.append(f"## Analysis Task")
            prompt_parts.append(question)
        else:
            # Default concise analysis
            prompt_parts.append("## Analysis Task")
            prompt_parts.append("Analyze this file and provide:")
            prompt_parts.append("1. Purpose: What this file does (1-2 sentences)")
            prompt_parts.append("2. Key components: Main classes/functions")
            prompt_parts.append("3. Dependencies: Important external dependencies")
            prompt_parts.append("4. Notes: Any critical logic or patterns")
            prompt_parts.append("")
            prompt_parts.append(
                "Keep the analysis concise and structured for LLM consumption."
            )

        return "\n".join(prompt_parts)

    def _parse_llm_response(self, response: str) -> Dict:
        """Parse LLM response."""
        # Wrap response in a dict
        return {"analysis": response, "parsed": self._extract_sections(response)}

    def _extract_sections(self, text: str) -> Dict:
        """Extract structured sections from text."""
        sections = {}

        # Try extracting headings and content
        section_pattern = r"##?\s*\*?\*?([^*\n]+)\*?\*?:?\s*\n((?:(?!##)[\s\S])*)"
        matches = re.findall(section_pattern, text)

        for title, content in matches:
            clean_title = title.strip().lower()
            clean_content = content.strip()
            sections[clean_title] = clean_content

        return sections

    def generate_simple_analysis(
        self, content: str, metadata: Dict, max_lines: int = 100
    ) -> Dict:
        """Generate a simple analysis (no LLM; returns file content)."""
        lines = content.split("\n")
        total_lines = len(lines)

        # Truncate long files
        if total_lines > max_lines:
            truncated_content = "\n".join(lines[:max_lines])
            truncated_content += (
                f"\n\n... (truncated, showing {max_lines}/{total_lines} lines)"
            )
        else:
            truncated_content = content

        analysis = {
            "content": truncated_content,
            "total_lines": total_lines,
            "truncated": total_lines > max_lines,
            "statistics": {
                "lines": metadata["line_count"],
                "size": metadata["file_size"],
            },
        }

        # Python-specific details
        if "python_info" in metadata:
            py_info = metadata["python_info"]
            analysis["python_details"] = {
                "import_count": len(py_info["imports"]),
                "class_count": len(py_info["classes"]),
                "function_count": len(py_info["functions"]),
                "has_main": py_info["has_main"],
                "docstring": py_info["docstring"],
            }

        return analysis

    def format_analysis(
        self, metadata: Dict, analysis: Dict, compact: bool = True
    ) -> str:
        """Format analysis output."""
        if compact:
            # Compact mode (for agent usage)
            output = []
            output.append(
                f"\nFile: {metadata['file_name']} ({metadata['language']}, {metadata['line_count']} lines)"
            )

            # Python-specific info
            if "python_info" in metadata:
                py_info = metadata["python_info"]
                if py_info["classes"]:
                    output.append(f"Classes: {', '.join(py_info['classes'][:5])}")
                if py_info["functions"]:
                    output.append(f"Functions: {len(py_info['functions'])}")

            # LLM analysis
            if "analysis" in analysis:
                output.append(f"\nAnalysis:")
                output.append(analysis["analysis"])
            # Raw file content (non-LLM mode)
            elif "content" in analysis:
                output.append(f"\nContent:")
                if analysis.get("truncated"):
                    output.append(f"(Showing first {analysis['total_lines']} lines)")
                output.append(analysis["content"])

            return "\n".join(output)
        else:
            # Detailed mode
            output = []
            output.append("\n" + "=" * 70)
            output.append("File Analysis")
            output.append("=" * 70)

            # File info
            output.append(f"\nFile: {metadata['file_name']}")
            output.append(f"Path: {metadata['file_path']}")
            output.append(f"Language: {metadata['language']}")
            output.append(
                f"Size: {metadata['file_size']} chars, {metadata['line_count']} lines"
            )

            # Python-specific info
            if "python_info" in metadata:
                py_info = metadata["python_info"]
                output.append(f"\nPython Info:")
                output.append(f"  Imports: {len(py_info['imports'])}")
                if py_info["imports"][:5]:
                    output.append(f"    {', '.join(py_info['imports'][:5])}")
                output.append(f"  Classes: {len(py_info['classes'])}")
                if py_info["classes"]:
                    output.append(f"    {', '.join(py_info['classes'])}")
                output.append(f"  Functions: {len(py_info['functions'])}")
                if py_info["docstring"]:
                    output.append(f"  Docstring: {py_info['docstring'][:100]}...")

            # LLM analysis or raw file content
            if "analysis" in analysis:
                output.append(f"\nAnalysis:")
                output.append("-" * 70)
                output.append(analysis["analysis"])
                output.append("-" * 70)
            elif "content" in analysis:
                output.append(f"\nContent:")
                output.append("-" * 70)
                if analysis.get("truncated"):
                    output.append(f"(Showing first {analysis['total_lines']} lines)")
                output.append(analysis["content"])
                output.append("-" * 70)

            output.append("\n" + "=" * 70)
            return "\n".join(output)


def read_file_main(
    file_path: str,
    question: Optional[str] = None,
    use_llm: bool = True,
    llm_name: str = "deepseek-chat",
    compact: bool = True,
):
    """
    Main: deeply analyze a file.

    Args:
        file_path: File path.
        question: Optional analysis question.
        use_llm: Whether to use an LLM for deeper analysis.
        llm_name: LLM model name.
        compact: Whether to use compact output.

    Returns:
        0: Success
        1: Failure
    """
    analyzer = FileAnalyzer(llm_name)
    file_path_obj = Path(file_path)

    # Validate file path
    if not file_path_obj.exists():
        print(f"Error: File not found: {file_path}")
        return 1

    if not file_path_obj.is_file():
        print(f"Error: Not a file: {file_path}")
        return 1

    # 1. Read file content
    content = analyzer.read_file_content(file_path_obj)
    if content is None:
        return 1
    metadata = analyzer.extract_file_metadata(file_path_obj, content)

    # Heuristic: if no question and LLM allowed, decide based on file size
    if use_llm and question is None:
        # Small-file thresholds: 200 lines or 10KB
        is_small_file = metadata["line_count"] <= 300 or metadata["file_size"] <= 10000
        if is_small_file:
            # Small file: no LLM
            analysis = analyzer.generate_simple_analysis(
                content, metadata, max_lines=200
            )
        else:
            # Large file: use LLM
            analysis = analyzer.analyze_with_llm(content, metadata, question)
    elif use_llm:
        # If question exists, always use LLM
        analysis = analyzer.analyze_with_llm(content, metadata, question)
    else:
        # If --no-llm, do basic analysis only
        analysis = analyzer.generate_simple_analysis(content, metadata, max_lines=100)

    output = analyzer.format_analysis(metadata, analysis, compact)
    print(output)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze and understand code files")
    parser.add_argument("file", type=str, help="File path to analyze")
    parser.add_argument(
        "-q", "--question", type=str, help="Custom analysis question (optional)"
    )
    parser.add_argument(
        "--no-llm", action="store_true", help="Skip LLM analysis (basic analysis only)"
    )
    parser.add_argument(
        "--llm",
        type=str,
        default="deepseek-chat",
        help="LLM model name (default: deepseek-chat)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output mode (default: compact mode for agent use)",
    )

    args = parser.parse_args()

    sys.exit(
        read_file_main(
            args.file,
            args.question,
            not args.no_llm,
            args.llm,
            not args.verbose,  # compact mode by default
        )
    )
