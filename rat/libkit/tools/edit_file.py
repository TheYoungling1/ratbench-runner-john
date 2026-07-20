#!/usr/bin/env python3
"""
edit_file tool implementation - GitHub-style diff preview
Purpose: edit/modify file contents and show a GitHub-like add/remove diff
Input: file path, edit mode, and change details
Output: diff-style preview and result
"""

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
        if start_line < 1:
            print(
                self.colorize(
                    f"❌ Invalid start line: {start_line} (must be >= 1)", RED
                )
            )
            return False
        if start_line > end_line:
            print(
                self.colorize(
                    f"❌ Invalid line range: {start_line}-{end_line} (start must be <= end)",
                    RED,
                )
            )
            return False

        # Warn if line range is out of bounds
        if start_line > total_lines:
            print(
                self.colorize(
                    f"⚠️ Start line {start_line} is beyond file length ({total_lines} lines); will replace/append at end",
                    YELLOW,
                )
            )
        elif end_line > total_lines:
            print(
                self.colorize(
                    f"⚠️ End line {end_line} is beyond file length ({total_lines} lines); replacing to end",
                    YELLOW,
                )
            )

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
        use_llm_fallback: bool = True,
    ) -> bool:
        """Search and replace text; optionally fall back to LLM if nothing matches."""
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

                # LLM fallback
                if use_llm_fallback:
                    print(self.colorize("🤖 Trying LLM-based smart replace...", CYAN))
                    instruction = f"""Find text in the file that is similar to the following content and replace it.

Text to find (may not match exactly):
```
{search_text}
```

Replace with:
```
{replace_text}
```

Use semantics and surrounding context to find the closest match and replace it. If there are multiple matches, {f"only replace the first {count}" if count > 0 else "replace all matches"}.
Output the full modified file content only; do not add any explanation."""

                    return self.llm_edit_with_instruction(
                        file_path, old_content, instruction
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

    def extract_context(
        self, lines: List[str], start_line: int, end_line: int, context_size: int = 50
    ) -> Dict:
        """Extract a target region and its surrounding context."""
        total_lines = len(lines)

        # Compute context range
        context_start = max(0, start_line - 1 - context_size)
        context_end = min(total_lines, end_line + context_size)

        # Slice sections
        before_context = lines[context_start : start_line - 1]
        target_lines = lines[start_line - 1 : end_line]
        after_context = lines[end_line:context_end]

        return {
            "context_start": context_start + 1,
            "target_start": start_line,
            "target_end": end_line,
            "before_context": before_context,
            "target_lines": target_lines,
            "after_context": after_context,
        }

    def llm_locate_edit_region(
        self, file_path: Path, content: str, instruction: str
    ) -> Optional[List[Dict]]:
        """Use an LLM to locate the region(s) to edit."""
        lines = content.split("\n")

        # Add line numbers (only show 1 out of every 10 lines)
        numbered_lines = []
        for i, line in enumerate(lines, 1):
            if i % 10 == 1 or i == len(lines):
                numbered_lines.append(f"{i:5d}| {line}")

        numbered_content = "\n".join(numbered_lines)

        prompt = f"""Analyze the file and find code regions that should be modified according to the instruction.

File: {file_path.name}
Instruction: {instruction}

File content (showing one line every 10 lines):
{numbered_content}

Return a JSON list of regions to edit. Return JSON only (no explanation):
[{"start_line": <int>, "end_line": <int>, "reason": "why change"}]

If multiple regions are needed, return multiple objects. If unsure, return the whole related function/class region."""

        try:
            messages = [{"role": "user", "content": prompt}]
            response, usage = self.llm.chat(messages, max_tokens=1000)

            if not response:
                return None

            # Extract JSON
            json_match = re.search(r"\[.*\]", response, re.DOTALL)
            if json_match:
                regions = json.loads(json_match.group())
                print(
                    self.colorize(f"✓ Located {len(regions)} region(s) to edit", GREEN)
                )
                for r in regions:
                    print(
                        self.colorize(
                            f"  Lines {r['start_line']}-{r['end_line']}: {r['reason']}",
                            BLUE,
                        )
                    )
                return regions
            else:
                print(
                    self.colorize(
                        "⚠️ Failed to parse regions; falling back to full-file edit",
                        YELLOW,
                    )
                )
                return None

        except Exception as e:
            print(
                self.colorize(
                    f"⚠️ Region detection failed: {e}; falling back to full-file edit",
                    YELLOW,
                )
            )
            return None

    def validate_edit_integrity(
        self, old_lines: List[str], new_lines: List[str]
    ) -> Tuple[bool, str]:
        """Validate the integrity of edited content."""
        old_count = len(old_lines)
        new_count = len(new_lines)

        # 1) Line-count sanity check (must not differ by > 3x)
        if new_count == 0:
            return False, "Edited content is empty"

        if old_count > 0:
            ratio = new_count / old_count
            if ratio > 3 or ratio < 0.3:
                return (
                    False,
                    f"Line count changed too much ({old_count} -> {new_count}); possible truncation",
                )

        # 2) Structural integrity check
        new_content = "\n".join(new_lines)

        # Check bracket matching
        brackets = {"(": ")", "[": "]", "{": "}"}
        stack = []
        for char in new_content:
            if char in brackets:
                stack.append(brackets[char])
            elif char in brackets.values():
                if not stack or stack[-1] != char:
                    return False, "Bracket mismatch; possible truncation"
                stack.pop()

        if stack:
            return False, "Bracket mismatch (unclosed); possible truncation"

        # 3) Check last line completeness
        if new_lines:
            last_line = new_lines[-1].strip()
            if last_line.endswith("...") or last_line.endswith("…"):
                return False, "Truncation marker detected"

        return True, "OK"

    def llm_local_edit(
        self, file_path: Path, old_content: str, instruction: str
    ) -> bool:
        """Local-edit strategy (core method)."""
        lines = old_content.split("\n")
        total_lines = len(lines)

        print(self.colorize(f"📄 Total lines: {total_lines}", BLUE))

        # For small files, do a full-file edit
        if total_lines <= 200:
            print(self.colorize("ℹ️ Small file; using full-file edit", BLUE))
            return self.llm_edit_full_file(file_path, old_content, instruction)

        print(self.colorize("🎯 Using local-edit mode", CYAN))

        # Step 1: detect regions
        regions = self.llm_locate_edit_region(file_path, old_content, instruction)

        if not regions:
            # Fall back to full-file edit
            print(
                self.colorize(
                    "⚠️ Failed to locate regions; using full-file edit", YELLOW
                )
            )
            return self.llm_edit_full_file(file_path, old_content, instruction)

        # Step 2: process each region sequentially
        current_content = old_content
        line_offset = 0  # Track line offset

        for idx, region in enumerate(regions, 1):
            print(self.colorize(f"\n{'=' * 70}", CYAN))
            print(
                self.colorize(
                    f"🔧 Editing region {idx}/{len(regions)}: {region['reason']}",
                    BOLD + CYAN,
                )
            )

            # Adjust line numbers for prior edits
            start_line = region["start_line"] + line_offset
            end_line = region["end_line"] + line_offset

            # Re-split current content
            current_lines = current_content.split("\n")

            # Validate line range
            if start_line < 1:
                start_line = 1
            if end_line > len(current_lines):
                end_line = len(current_lines)

            # Step 3: extract context
            context = self.extract_context(current_lines, start_line, end_line)

            # Step 4: build local-edit prompt
            before_text = "\n".join(context["before_context"])
            target_text = "\n".join(context["target_lines"])
            after_text = "\n".join(context["after_context"])

            prompt = f"""Edit the following code snippet according to the instruction.

Context before (for reference only; do not modify):
```
{before_text}
```

【Code to edit】:
```
{target_text}
```

Context after (for reference only; do not modify):
```
{after_text}
```

Instruction: {instruction}
Specific task: {region["reason"]}

Return ONLY the edited 【Code to edit】 section, preserving indentation and formatting.
Do not include any context, do not add ``` fences; output code only."""

            try:
                messages = [{"role": "user", "content": prompt}]
                new_target_content, usage = self.llm.chat(messages, max_tokens=4096)

                if not new_target_content:
                    print(self.colorize("❌ LLM returned empty content", RED))
                    continue

                # Strip potential code fences
                new_target_content = re.sub(r"^```[\w]*\n?", "", new_target_content)
                new_target_content = re.sub(r"\n?```$", "", new_target_content)
                new_target_content = new_target_content.strip()

                new_target_lines = new_target_content.split("\n")

                # Step 5: validate integrity
                is_valid, msg = self.validate_edit_integrity(
                    context["target_lines"], new_target_lines
                )

                if not is_valid:
                    print(self.colorize(f"⚠️ Validation failed: {msg}", YELLOW))
                    print(
                        self.colorize(
                            "Continuing anyway; please review the result", YELLOW
                        )
                    )
                    # Continue anyway, but warn

                # Step 6: apply change
                modified_lines = (
                    current_lines[: start_line - 1]
                    + new_target_lines
                    + current_lines[end_line:]
                )

                new_content = "\n".join(modified_lines)

                # Track line offset
                old_region_lines = end_line - start_line + 1
                new_region_lines = len(new_target_lines)
                line_offset += new_region_lines - old_region_lines

                print(
                    self.colorize(
                        f"✓ Region updated (Tokens: {usage.completion_tokens if usage else 'N/A'})",
                        GREEN,
                    )
                )
                print(
                    self.colorize(
                        f"  Line count change: {old_region_lines} -> {new_region_lines}",
                        BLUE,
                    )
                )

                # Update current content
                current_content = new_content

            except Exception as e:
                print(self.colorize(f"❌ Failed to edit region: {e}", RED))
                continue

        # Final write
        print(self.colorize(f"\n{'=' * 70}", CYAN))
        return self.write_file(file_path, old_content, current_content)

    def llm_edit_full_file(
        self, file_path: Path, old_content: str, instruction: str
    ) -> bool:
        """Full-file edit mode."""
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
            new_content, usage = self.llm.chat(messages, max_tokens=4096)

            if not new_content:
                print(self.colorize("❌ LLM returned empty content", RED))
                return False

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

    def llm_edit_with_instruction(
        self, file_path: Path, old_content: str, instruction: str
    ) -> bool:
        """Edit a file with an instruction (auto local/full strategy)."""
        print(self.colorize(f"Instruction: {instruction}", BLUE))
        print(self.colorize("-" * 70, DIM))

        # Use the local-edit strategy
        return self.llm_local_edit(file_path, old_content, instruction)

    def llm_edit(self, file_path: Path, instruction: str) -> bool:
        """Edit a file with LLM."""
        old_content = self.read_file(file_path)
        if old_content is None:
            return False

        return self.llm_edit_with_instruction(file_path, old_content, instruction)

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
    parser.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="Disable LLM fallback (do not use LLM when search finds no matches)",
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
                    "❌ replace mode requires: --start-line, --end-line, --content", RED
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
            file_path,
            args.search,
            args.replace,
            args.regex,
            args.count,
            use_llm_fallback=not args.no_llm_fallback,
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
