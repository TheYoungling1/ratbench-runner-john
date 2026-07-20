#!/usr/bin/env python3
"""Color utilities for terminal output."""

import sys


class Colors:
    """ANSI color codes."""

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # Styles
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    BLINK = "\033[5m"
    REVERSE = "\033[7m"

    # Reset
    RESET = "\033[0m"

    # Tool-specific colors
    TOOL_SEARCH = CYAN  # search-repo
    TOOL_TEST = GREEN  # construct-test
    TOOL_READ = BLUE  # read-file
    TOOL_IMAGE = MAGENTA  # retrieve-image
    TOOL_WEB = YELLOW  # search-web
    TOOL_ISSUE = RED  # retrieve-issue
    TOOL_TRAJECTORY = BRIGHT_MAGENTA  # retrieve-trajectory
    TOOL_OUTLINE = BRIGHT_BLUE  # view-outline
    TOOL_STRUCTURE = BRIGHT_CYAN  # ls-structure
    TOOL_STOP = BRIGHT_RED  # stop

    @staticmethod
    def disable():
        """Disable colored output."""
        Colors.BLACK = ""
        Colors.RED = ""
        Colors.GREEN = ""
        Colors.YELLOW = ""
        Colors.BLUE = ""
        Colors.MAGENTA = ""
        Colors.CYAN = ""
        Colors.WHITE = ""
        Colors.BRIGHT_BLACK = ""
        Colors.BRIGHT_RED = ""
        Colors.BRIGHT_GREEN = ""
        Colors.BRIGHT_YELLOW = ""
        Colors.BRIGHT_BLUE = ""
        Colors.BRIGHT_MAGENTA = ""
        Colors.BRIGHT_CYAN = ""
        Colors.BRIGHT_WHITE = ""
        Colors.BOLD = ""
        Colors.DIM = ""
        Colors.ITALIC = ""
        Colors.UNDERLINE = ""
        Colors.BLINK = ""
        Colors.REVERSE = ""
        Colors.RESET = ""


def colorize(text: str, color: str, bold: bool = False) -> str:
    """
    Add ANSI color codes to text.

    Args:
        text: Text to colorize
        color: ANSI color code
        bold: Whether to render in bold

    Returns:
        Colorized text
    """
    prefix = Colors.BOLD if bold else ""
    return f"{prefix}{color}{text}{Colors.RESET}"


def print_colored(text: str, color: str, bold: bool = False, file=sys.stdout):
    """
    Print colored text.

    Args:
        text: Text to print
        color: ANSI color code
        bold: Whether to render in bold
        file: Output stream
    """
    print(colorize(text, color, bold), file=file)


# Tool-specific color mapping
TOOL_COLORS = {
    "search-repo": Colors.TOOL_SEARCH,
    "construct-test": Colors.TOOL_TEST,
    "run-test": Colors.TOOL_TEST,  # Same color as construct-test
    "read-file": Colors.TOOL_READ,
    "retrieve-image": Colors.TOOL_IMAGE,
    "search-web": Colors.TOOL_WEB,
    "retrieve-issue": Colors.TOOL_ISSUE,
    "retrieve-trajectory": Colors.TOOL_TRAJECTORY,
    "view-outline": Colors.TOOL_OUTLINE,
    "ls-structure": Colors.TOOL_STRUCTURE,
    "stop": Colors.TOOL_STOP,
}


def get_tool_color(tool_name: str) -> str:
    """
    Get the color associated with a tool.

    Args:
        tool_name: Tool name

    Returns:
        ANSI color code
    """
    return TOOL_COLORS.get(tool_name, Colors.WHITE)


if __name__ == "__main__":
    # Test color output
    print("Color test:")
    print_colored("■ search-repo", Colors.TOOL_SEARCH, bold=True)
    print_colored("■ construct-test", Colors.TOOL_TEST, bold=True)
    print_colored("■ read-file", Colors.TOOL_READ, bold=True)
    print_colored("■ retrieve-image", Colors.TOOL_IMAGE, bold=True)
    print_colored("■ search-web", Colors.TOOL_WEB, bold=True)
    print_colored("■ retrieve-issue", Colors.TOOL_ISSUE, bold=True)
    print_colored("■ retrieve-trajectory", Colors.TOOL_TRAJECTORY, bold=True)
    print_colored("■ view-outline", Colors.TOOL_OUTLINE, bold=True)
    print_colored("■ ls-structure", Colors.TOOL_STRUCTURE, bold=True)
    print_colored("■ stop", Colors.TOOL_STOP, bold=True)
