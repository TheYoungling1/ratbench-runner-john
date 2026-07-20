#!/usr/bin/env python3
"""
search_repo tool implementation.

Purpose: search a repository for relevant code snippets.
Input: a query/problem statement.
Output: matching code snippets and their locations.
"""

import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
import argparse
import re
import sys
import json
import os
from pathlib import Path
from collections import defaultdict

try:
    from llm import LLMChat
except ImportError:

    class FallbackLLMChat:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return ("LLM is not available", None)

    LLMChat = FallbackLLMChat


import ast


class CodeSearcher:
    """Code searcher."""

    def __init__(self, repo_path="/repo", llm_name="deepseek-chat"):
        self.repo_path = Path(repo_path)
        self.results = []

    def extract_keywords(self, query):
        """Extract keywords from a query."""
        # Remove common stop words
        stop_words = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "from",
            "up",
            "about",
            "into",
            "through",
            "during",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "should",
            "could",
            "may",
            "might",
            "must",
            "can",
            "how",
            "what",
            "where",
            "when",
            "why",
            "which",
            "who",
            # English equivalents of common question/stop words
            "how",
            "what",
            "where",
            "why",
            "which",
            "whether",
            "can",
            "could",
            "should",
            "is",
        }
        words = re.findall(r"\b\w+\b", query.lower())
        keywords = [w for w in words if w not in stop_words and len(w) > 2]
        return keywords

    def search_files_by_keyword(self, keywords, max_files=50):
        """Search files by keywords."""
        file_scores = defaultdict(int)
        for py_file in self.repo_path.rglob("*.py"):
            # Skip virtualenv and cache directories
            if any(
                part in str(py_file)
                for part in [".venv", "venv", "__pycache__", ".git", ".pytest_cache"]
            ):
                continue
            try:
                with open(py_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().lower()
                    for keyword in keywords:
                        count = content.count(keyword)
                        if count > 0:
                            file_scores[py_file] += count
            except Exception:
                continue
        sorted_files = sorted(file_scores.items(), key=lambda x: x[1], reverse=True)
        return [f[0] for f in sorted_files[:max_files]]

    def extract_code_elements(self, file_path):
        """Extract function/class definitions from code."""
        elements = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                tree = ast.parse(content, filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    # Function definition
                    docstring = ast.get_docstring(node) or ""
                    elements.append(
                        {
                            "type": "function",
                            "name": node.name,
                            "lineno": node.lineno,
                            "end_lineno": getattr(node, "end_lineno", node.lineno + 10),
                            "docstring": docstring,
                            "file": str(file_path),
                        }
                    )

                elif isinstance(node, ast.ClassDef):
                    # Class definition
                    docstring = ast.get_docstring(node) or ""
                    elements.append(
                        {
                            "type": "class",
                            "name": node.name,
                            "lineno": node.lineno,
                            "end_lineno": getattr(node, "end_lineno", node.lineno + 20),
                            "docstring": docstring,
                            "file": str(file_path),
                        }
                    )

        except Exception as e:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                for i, line in enumerate(lines, 1):
                    # Find function definitions
                    func_match = re.match(r"^\s*def\s+(\w+)\s*\(", line)
                    if func_match:
                        elements.append(
                            {
                                "type": "function",
                                "name": func_match.group(1),
                                "lineno": i,
                                "end_lineno": min(i + 20, len(lines)),
                                "docstring": "",
                                "file": str(file_path),
                            }
                        )

                    # Find class definitions
                    class_match = re.match(r"^\s*class\s+(\w+)\s*[:\(]", line)
                    if class_match:
                        elements.append(
                            {
                                "type": "class",
                                "name": class_match.group(1),
                                "lineno": i,
                                "end_lineno": min(i + 30, len(lines)),
                                "docstring": "",
                                "file": str(file_path),
                            }
                        )

            except Exception:
                pass

        return elements

    def score_element(self, element, keywords, query):
        """Score a code element."""
        score = 0

        # Name match
        name_lower = element["name"].lower()
        for keyword in keywords:
            if keyword in name_lower:
                score += 10
        docstring_lower = element["docstring"].lower()
        for keyword in keywords:
            if keyword in docstring_lower:
                score += 5
        if query.lower() in name_lower:
            score += 20
        if query.lower() in docstring_lower:
            score += 15

        return score

    def extract_code_snippet(self, file_path, start_line, end_line, context=3):
        """Extract a code snippet with surrounding context."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            actual_start = max(0, start_line - 1 - context)
            actual_end = min(len(lines), end_line + context)
            snippet_lines = []
            for i in range(actual_start, actual_end):
                line_num = i + 1
                prefix = "→" if start_line <= line_num <= end_line else " "
                snippet_lines.append(f"{prefix} {line_num:4d} | {lines[i].rstrip()}")
            return "\n".join(snippet_lines)
        except Exception as e:
            return f"Error reading file: {e}"

    def concat_repo_content(self, extensions=None, max_chars=None):
        """
        Walk the repository and concatenate file paths and contents into a long string.
        Used to build LLM context.

        Args:
            extensions: File extensions to include, e.g. ['.py', '.md'].
                Defaults to None (read all text files).
            max_chars: Max character limit to prevent context/token overflow.
                Defaults to None.
        """
        context_parts = []
        total_chars = 0

        # Directories/files to ignore
        ignore_dirs = {
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".idea",
            ".vscode",
            "node_modules",
            "dist",
            "build",
        }
        ignore_files = {"package-lock.json", "yarn.lock", ".DS_Store"}
        binary_extensions = {
            ".pyc",
            ".pyo",
            ".pyd",
            ".so",
            ".dll",
            ".exe",
            ".bin",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".ico",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
        }
        print(f"📦 Packing repository content: {self.repo_path}")
        # Use os.walk (easier than rglob to control ignored dirs)
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for file in files:
                if file in ignore_files:
                    continue
                file_path = Path(root) / file
                if file_path.suffix.lower() in binary_extensions:
                    continue
                if extensions and file_path.suffix not in extensions:
                    continue
                try:
                    rel_path = file_path.relative_to(self.repo_path)
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        if not content.strip():
                            continue
                        file_block = f"File: {rel_path}\n```\n{content}\n```\n\n"
                        if max_chars and (total_chars + len(file_block) > max_chars):
                            context_parts.append(
                                f"... (Stopped due to length limit: {max_chars} chars) ..."
                            )
                            print(
                                f"⚠️ Reached max character limit ({max_chars}); stopping."
                            )
                            return "".join(context_parts)
                        context_parts.append(file_block)
                        total_chars += len(file_block)
                except Exception as e:
                    print(f"⚠️ Error reading file {file_path}: {e}")
                    continue
        final_context = "".join(context_parts)
        print(f"✅ Packed {len(context_parts)} files, {total_chars} characters total.")
        return final_context

    def search(self, query, max_results=10, is_gpt=True):
        """Run the search."""
        print(f"🔍 Search query: {query}")
        print()
        # 1. Extract keywords
        keywords = self.extract_keywords(query)
        print(f"📌 Extracted keywords: {', '.join(keywords)}")
        print()
        if not keywords:
            print("⚠️  No valid keywords extracted; falling back to raw query")
            keywords = [query.lower()]
        print("📂 Searching files containing keywords...")
        relevant_files = self.search_files_by_keyword(keywords)
        if not relevant_files:
            print("❌ No relevant files found")
            return []
        print(f"✅ Found {len(relevant_files)} relevant files")
        print()
        print("📝 Analyzing code structure...")
        all_elements = []

        for file_path in relevant_files:
            elements = self.extract_code_elements(file_path)
            all_elements.extend(elements)

        print(f"✅ Found {len(all_elements)} code elements (functions/classes)")
        print()

        # 4. Score and sort
        scored_elements = []
        for element in all_elements:
            score = self.score_element(element, keywords, query)
            if score > 0:
                element["score"] = score
                scored_elements.append(element)

        scored_elements.sort(key=lambda x: x["score"], reverse=True)

        # 5. Return top N results
        results = []
        print(f"📊 Top {min(max_results, len(scored_elements))} most relevant results:")
        print("=" * 70)
        for i, element in enumerate(scored_elements[:max_results], 1):
            try:
                relative_path = Path(element["file"]).relative_to(self.repo_path)
            except ValueError:
                relative_path = element["file"]

            # Extract snippet
            snippet = self.extract_code_snippet(
                element["file"], element["lineno"], element["end_lineno"], context=2
            )

            result = {
                "rank": i,
                "score": element["score"],
                "type": element["type"],
                "name": element["name"],
                "file": str(relative_path),
                "lineno": element["lineno"],
                "snippet": snippet,
                "docstring": element["docstring"][:200] if element["docstring"] else "",
            }

            results.append(result)

            # Print result
            print(f"\n{i}. [{element['type'].upper()}] {element['name']}")
            print(f"   File: {relative_path}:{element['lineno']}")
            print(f"   Relevance: {element['score']}")
            if element["docstring"]:
                doc_preview = element["docstring"][:100].replace("\n", " ")
                print(f"   Doc: {doc_preview}...")
            print("\n   Code snippet:")
            print(f"   {'-' * 66}")
            for line in snippet.split("\n")[:15]:  # Show up to 15 lines
                print(f"   {line}")
            if len(snippet.split("\n")) > 15:
                line_cnt = len(snippet.split("\n")) - 15
                print(f"   ... ({line_cnt} more lines)")
            print(f"   {'-' * 66}")

        print("\n" + "=" * 70)

        return results

    def search_simple(self, query, max_results=5):
        """Simplified search for quick lookups."""
        keywords = self.extract_keywords(query)
        if not keywords:
            keywords = [query.lower()]
        relevant_files = self.search_files_by_keyword(keywords, max_files=20)
        results = []
        for file_path in relevant_files[:max_results]:
            try:
                relative_path = Path(file_path).relative_to(self.repo_path)
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                # Find lines containing keywords
                matching_lines = []
                for i, line in enumerate(lines, 1):
                    if any(kw in line.lower() for kw in keywords):
                        matching_lines.append((i, line.rstrip()))
                if matching_lines:
                    results.append(
                        {
                            "file": str(relative_path),
                            "matches": matching_lines[:10],  # Up to 10 matches
                        }
                    )
            except Exception:
                continue

        return results


def search_repo_main(query, repo_path="/repo", mode="detailed", max_results=10):
    """
    Main entry point: search for relevant code in a repository.

    Args:
        query: Search query.
        repo_path: Repository path.
        mode: Search mode ('detailed', 'simple', or 'llm').
        max_results: Maximum number of results.
    """
    print("=" * 70)
    print("🔍 Search Repo - Search repository code")
    print("=" * 70)
    print()

    searcher = CodeSearcher(repo_path)

    if mode == "simple":
        results = searcher.search_simple(query, max_results)

        print(f"📋 Simple search results (query: {query}):")
        print("-" * 70)

        for i, result in enumerate(results, 1):
            print(f"\n{i}. {result['file']}")
            for lineno, line in result["matches"][:5]:
                print(f"   {lineno:4d} | {line}")

        print("\n" + "=" * 70)
    elif mode == "llm":
        full_context = searcher.concat_repo_content(extensions=[".py"])
        final_prompt = f"""
            ### Role
            You are a precise code extraction API. Analyze the codebase and extract snippets relevant to the query.

            ### Input Context
            {full_context}

            ### User Query
            {query}

            ### Output Rules
            1. Return ONLY a raw JSON list. No Markdown, no explanation.
            2. Format: [{{"file": "path", "line_start": int, "code": "snippet string", "summary": "what this does"}}]
            3. If code is spread across multiple files, return multiple objects.
            """
        llm = LLMChat("deepseek-chat")
        llm_response, _ = llm.chat(
            [
                {"role": "system", "content": "You are a JSON-only response bot."},
                {"role": "user", "content": final_prompt},
            ],
            temperature=0.0,
        )
        try:
            if not llm_response:
                raise ValueError("LLM returned an empty response")
            results = json.loads(llm_response)
        except Exception as e:
            print("Failed to parse JSON:", e)
            results = []
    else:
        results = searcher.search(query, max_results)

    # Save results
    result_file = Path("/tmp/search_repo_result.json")
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Results saved to: {result_file}")
    except Exception as e:
        print(f"⚠️  Failed to save results: {e}")

    # Return summary
    if results:
        print(f"\n✅ Search complete; found {len(results)} relevant snippets")
        return 0
    else:
        print("\n⚠️  No relevant snippets found")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Search repository for relevant code snippets"
    )
    parser.add_argument(
        "-q", "--query", type=str, help="Search query or problem description"
    )
    parser.add_argument("--repo", type=str, default="/repo", help="Repository path")
    parser.add_argument(
        "--mode",
        type=str,
        default="llm",
        choices=["detailed", "simple", "llm"],
        help="Search mode (detailed or simple)",
    )
    parser.add_argument("--max", type=int, default=10, help="Maximum number of results")
    args = parser.parse_args()
    sys.exit(search_repo_main(args.query, args.repo, args.mode, args.max))
