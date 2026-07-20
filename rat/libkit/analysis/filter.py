import re
from bs4 import BeautifulSoup
import re


def long_line_filter(content, max_line_len=100000, max_lines=1000):
    # print("long line filter")
    lines = content.splitlines()
    if any(len(line) > max_line_len for line in lines):
        return None
    return content


def alphabet_ratio_filter(content, threshold=0.25):
    letters = sum(c.isalpha() for c in content)
    total = len(content)
    # print("alphabet ratio filter")
    if total == 0:
        return None
    if letters / total < threshold:
        return None
    return content


def html_filter(content, min_text=100, min_ratio=0.2):
    # print("html filter")
    if "<html" not in content.lower():
        return content  # Not HTML; keep as-is
    soup = BeautifulSoup(content, "html.parser")
    text = soup.get_text()
    print("html filter")
    if len(text) < min_text:
        return None
    if len(text) / len(content) < min_ratio:
        return None
    return content


EXCLUDE_NAMES = ["readme", "notes", "todo", "description", "cmakelists"]


def filename_filter(filename, content, max_lines=5000):
    # print("filename filter")
    if any(name in filename.lower() for name in EXCLUDE_NAMES):
        return None
    if content.count("\n") > max_lines:
        return None
    print("filename filter")
    return content


def encoding_filter(content, max_len=1024):
    # print("encoding filter")
    # Base64-like
    if re.search(r"[A-Za-z0-9+/=]{%d,}" % max_len, content):
        return None
    # Hex-like
    if re.search(r"(?:[0-9a-fA-F]{2}){%d,}" % (max_len // 2), content):
        return None
    # Unicode escape
    if re.search(r"(?:\\u[0-9a-fA-F]{4}){%d,}" % (max_len // 6), content):
        return None
    return content


EXCLUDE_EXTS = [".exe", ".dll", ".bin", ".png", ".jpg", ".gif", ".zip", ".pdf"]


def extension_filter(filename, content):
    # print("extension filter")
    if any(filename.lower().endswith(ext) for ext in EXCLUDE_EXTS):
        return None
    return content


import ast


def syntax_filter_python(content):
    try:
        ast.parse(content)
        return content
    except SyntaxError:
        return None


def clean_code_file(filename, content):
    filters = [
        lambda c: long_line_filter(c),
        lambda c: alphabet_ratio_filter(c),
        lambda c: html_filter(c),
        lambda c: filename_filter(filename, c),
        lambda c: encoding_filter(c),
        lambda c: extension_filter(filename, c),
        lambda c: syntax_filter_python(c) if filename.endswith(".py") else c,
    ]
    for f in filters:
        if content is None or content == "":
            print("Out")
            return ""
        before_len = len(content)
        content = f(content)
        if content is None or content == "":
            print("Out")
            return ""
        after_len = len(content)
        # print("Reduce:",after_len/before_len)
    return content
