#!/usr/bin/env python3
"""
edit_file tool implementation - GitHub-style diff preview
Purpose: edit/modify file contents and show a GitHub-like add/remove diff
Input: file path, edit mode, and change details
Output: diff-style preview and result
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Optional, List, Tuple
import re
import difflib

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
from llm import LLMChat

# ANSI color codes
RED = "\033[31m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


class FileEditor:
    """File editor with GitHub-style diff preview."""

    def __init__(self, llm_name="deepseek-chat", use_color=True):
        self.llm = LLMChat(llm_name)
        self.max_file_size = 100000
        self.use_color = use_color
        self.context_lines = 3  # Context lines for diffs

    def colorize(self, text: str, color: str) -> str:
        """Apply ANSI color to text."""
        if not self.use_color:
            return text
        return f"{color}{text}{RESET}"

    def read_file(self, file_path: Path) -> Optional[str]:
        """Read file content; if missing, return empty string."""
        try:
            if not file_path.exists():
                print(
                    self.colorize(
                        f"⚠️ File does not exist: {file_path}; will create", YELLOW
                    )
                )
                return ""

            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            if len(content) > self.max_file_size:
                print(
                    self.colorize(
                        f"⚠️ File is large ({len(content)} chars); consider editing in chunks",
                        YELLOW,
                    )
                )
                return None

            return content
        except Exception as e:
            print(self.colorize(f"❌ Failed to read file: {e}", RED))
            return None

    def show_diff(
        self, file_path: Path, old_content: str, new_content: str, title: str = ""
    ):
        """Show a GitHub-style unified diff."""
        print(f"\n{self.colorize('=' * 70, CYAN)}")
        if title:
            print(self.colorize(f"📝 {title}", BOLD + CYAN))
        print(self.colorize(f"File: {file_path}", BLUE))
        print(self.colorize("=" * 70, CYAN))

        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()

        # Use difflib to generate unified diff
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{file_path.name}",
            tofile=f"b/{file_path.name}",
            lineterm="",
        )

        diff_lines = list(diff)

        if not diff_lines:
            print(self.colorize("ℹ️ No changes", YELLOW))
            return

        # Count changes
        additions = sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )

        print(
            self.colorize("Changes: ", BOLD)
            + self.colorize(f"+{additions} lines", GREEN)
            + " "
            + self.colorize(f"-{deletions} lines", RED)
        )
        print(self.colorize("-" * 70, DIM))

        # Print diff
        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---"):
                # File header
                print(self.colorize(line, BOLD))
            elif line.startswith("@@"):
                # Hunk header
                print(self.colorize(line, CYAN))
            elif line.startswith("+"):
                # Added line
                print(self.colorize(line, GREEN))
            elif line.startswith("-"):
                # Deleted line
                print(self.colorize(line, RED))
            else:
                # Context line
                print(self.colorize(line, DIM))

        print(self.colorize("=" * 70, CYAN))

    def write_file(
        self,
        file_path: Path,
        old_content: str,
        new_content: str,
        show_preview: bool = True,
    ) -> bool:
        """Write file content, optionally showing a diff preview."""
        try:
            # Preview diff
            if show_preview:
                self.show_diff(file_path, old_content, new_content, "Edit preview")

            # Create backup
            if file_path.exists():
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                with open(file_path, "r", encoding="utf-8") as f:
                    backup_content = f.read()
                with open(backup_path, "w", encoding="utf-8") as f:
                    f.write(backup_content)
                # print(self.colorize(f"✓ Backup created: {backup_path}", GREEN))

            # Write new content
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            print(self.colorize(f"✓ File written: {file_path}", GREEN))
            return True
        except Exception as e:
            print(self.colorize(f"❌ Failed to write file: {e}", RED))
            return False

    def replace_lines(
        self, file_path: Path, start_line: int, end_line: int, new_content: str
    ) -> bool:
        """Replace a range of lines."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False

        lines = old_content.split("\n")
        total_lines = len(lines)

        # Validate line numbers
        if start_line < 1 or end_line > total_lines or start_line > end_line:
            print(
                self.colorize(
                    f"❌ Invalid line range: {start_line}-{end_line} (file has {total_lines} lines)",
                    RED,
                )
            )
            return False

        # Replace content (1-based line numbers)
        new_lines = new_content.split("\n")
        modified_lines = lines[: start_line - 1] + new_lines + lines[end_line:]
        new_file_content = "\n".join(modified_lines)

        return self.write_file(file_path, old_content, new_file_content)

    def insert_at_line(self, file_path: Path, line_number: int, content: str) -> bool:
        """Insert content after a given line number."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False

        lines = old_content.split("\n")
        total_lines = len(lines)

        # Validate line number
        if line_number < 0 or line_number > total_lines:
            print(
                self.colorize(
                    f"❌ Invalid line number: {line_number} (file has {total_lines} lines)",
                    RED,
                )
            )
            return False

        # Insert
        new_lines = content.split("\n")
        modified_lines = lines[:line_number] + new_lines + lines[line_number:]
        new_file_content = "\n".join(modified_lines)

        return self.write_file(file_path, old_content, new_file_content)

    def delete_lines(self, file_path: Path, start_line: int, end_line: int) -> bool:
        """Delete a range of lines."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False

        lines = old_content.split("\n")
        total_lines = len(lines)

        # Validate line numbers
        if start_line < 1 or end_line > total_lines or start_line > end_line:
            print(
                self.colorize(
                    f"❌ Invalid line range: {start_line}-{end_line} (file has {total_lines} lines)",
                    RED,
                )
            )
            return False

        # Delete content (1-based line numbers)
        modified_lines = lines[: start_line - 1] + lines[end_line:]
        new_file_content = "\n".join(modified_lines)

        return self.write_file(file_path, old_content, new_file_content)

    def search_and_replace(
        self,
        file_path: Path,
        search_text: str,
        replace_text: str,
        regex: bool = False,
        count: int = -1,
    ) -> bool:
        """Search and replace text."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False

        try:
            if regex:
                # Regex replace
                new_content = re.sub(
                    search_text,
                    replace_text,
                    old_content,
                    count=count if count > 0 else 0,
                )
                matches = len(re.findall(search_text, old_content))
            else:
                # Plain string replace
                if count > 0:
                    new_content = old_content.replace(search_text, replace_text, count)
                else:
                    new_content = old_content.replace(search_text, replace_text)
                matches = old_content.count(search_text)

            if matches == 0:
                print(
                    self.colorize(f"⚠️ No matching text found: '{search_text}'", YELLOW)
                )
                return False

            actual_replacements = min(matches, count) if count > 0 else matches
            print(
                self.colorize(
                    f"ℹ️ Found {matches} matches; replacing {actual_replacements}", BLUE
                )
            )

            return self.write_file(file_path, old_content, new_content)
        except re.error as e:
            print(self.colorize(f"❌ Regex error: {e}", RED))
            return False
        except Exception as e:
            print(self.colorize(f"❌ Replace failed: {e}", RED))
            return False

    def llm_edit(self, file_path: Path, instruction: str) -> bool:
        """Edit a file with LLM."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False
        print(self.colorize(f"Instruction: {instruction}", BLUE))
        print(self.colorize("-" * 70, DIM))

        # Build LLM prompt
        prompt = f"""You are a code editing assistant. Modify the file according to the instruction.
File path: {file_path}
File content:
```
{old_content}
```
Instruction: {instruction}

Output the full modified file content only; do not add explanations or Markdown code fences.
"""

        try:
            messages = [{"role": "user", "content": prompt}]
            new_content, usage = self.llm.chat(messages)

            if not new_content:
                print(self.colorize("❌ LLM returned empty content", RED))
                return False

            new_content = str(new_content)

            # Strip potential Markdown code fences
            new_content = re.sub(r"^```[\w]*\n", "", new_content)
            new_content = re.sub(r"\n```$", "", new_content)

            print(
                self.colorize(
                    f"✓ LLM finished (Tokens: {usage.completion_tokens if usage else 'N/A'})",
                    GREEN,
                )
            )

            return self.write_file(file_path, old_content, new_content)
        except Exception as e:
            print(self.colorize(f"❌ LLM edit failed: {e}", RED))
            return False

    def append_content(self, file_path: Path, content: str) -> bool:
        """Append content to the end of a file."""
        old_content = self.read_file(file_path)
        if old_content is None:
            # If file does not exist, create it
            print(self.colorize("ℹ️ File does not exist; will create", BLUE))
            old_content = ""

        new_content = old_content + "\n" + content if old_content else content

        return self.write_file(file_path, old_content, new_content)


def main():
    parser = argparse.ArgumentParser(
        description="File editor - GitHub-style diff preview"
    )
    parser.add_argument("file_path", type=str, help="Path of the file to edit")
    parser.add_argument(
        "--mode",
        type=str,
        default="llm",
        choices=["replace", "insert", "delete", "search", "llm", "append"],
        help="Edit mode",
    )
    # replace mode args
    parser.add_argument("--start-line", type=int, help="Start line (1-based)")
    parser.add_argument("--end-line", type=int, help="End line")
    parser.add_argument("--content", type=str, help="New content / inserted content")

    # insert mode args
    parser.add_argument("--at-line", type=int, help="Insert after this line")

    # search/replace args
    parser.add_argument("--search", type=str, help="Search text")
    parser.add_argument("--replace", type=str, help="Replacement text")
    parser.add_argument("--regex", action="store_true", help="Use regular expression")
    parser.add_argument(
        "--count", type=int, default=-1, help="Replacement count (-1 = all)"
    )

    # LLM edit args
    parser.add_argument("--instruction", type=str, help="LLM edit instruction")

    # other args
    parser.add_argument(
        "--llm", type=str, default="deepseek-chat", help="LLM model name"
    )
    parser.add_argument("--no-color", action="store_true", help="Disable color output")

    args = parser.parse_args()

    # Create editor
    editor = FileEditor(llm_name=args.llm, use_color=not args.no_color)
    file_path = Path(args.file_path)

    print(editor.colorize("=" * 70, CYAN))
    print(editor.colorize(f"📄 File editor - {args.mode.upper()} mode", BOLD + CYAN))
    print(editor.colorize("=" * 70, CYAN))

    success = False

    if args.mode == "replace":
        if not args.start_line or not args.end_line or not args.content:
            print(
                editor.colorize(
                    "❌ replace mode requires: --start-line, --end-line, --content",
                    RED,
                )
            )
            return 1
        success = editor.replace_lines(
            file_path, args.start_line, args.end_line, args.content
        )

    elif args.mode == "insert":
        if args.at_line is None or not args.content:
            print(editor.colorize("❌ insert mode requires: --at-line, --content", RED))
            return 1
        success = editor.insert_at_line(file_path, args.at_line, args.content)

    elif args.mode == "delete":
        if not args.start_line or not args.end_line:
            print(
                editor.colorize(
                    "❌ delete mode requires: --start-line, --end-line", RED
                )
            )
            return 1
        success = editor.delete_lines(file_path, args.start_line, args.end_line)

    elif args.mode == "search":
        if not args.search or args.replace is None:
            print(editor.colorize("❌ search mode requires: --search, --replace", RED))
            return 1
        success = editor.search_and_replace(
            file_path, args.search, args.replace, args.regex, args.count
        )

    elif args.mode == "llm":
        if not args.instruction:
            print(editor.colorize("❌ llm mode requires: --instruction", RED))
            return 1
        success = editor.llm_edit(file_path, args.instruction)

    elif args.mode == "append":
        if not args.content:
            print(editor.colorize("❌ append mode requires: --content", RED))
            return 1
        success = editor.append_content(file_path, args.content)

    print(editor.colorize("=" * 70, CYAN))
    if success:
        print(editor.colorize("✅ Edit complete!", GREEN))
        return 0
    else:
        print(editor.colorize("❌ Edit failed!", RED))
        return 1


if __name__ == "__main__":
    sys.exit(main())
