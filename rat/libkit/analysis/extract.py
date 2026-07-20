import os
from collections import deque
from libkit.filter import clean_code_file


def read_all_code(repo_dir, exts=(".py", ".js", ".java", ".cpp", ".c", ".ts")):
    code_texts = []
    for root, _, files in os.walk(repo_dir):
        for f in files:
            if f.endswith(exts):  # Code files only
                file_path = os.path.join(root, f)
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
                        code_texts.append(fp.read())
                except Exception as e:
                    print(f"Read failed {file_path}: {e}")
    return "\n\n".join(code_texts)


def read_code_dfs(repo_dir, max_files=10, exts=(".py",)):
    code_texts = []
    stack = [repo_dir]
    count = 0
    while stack and count < max_files:
        cur = stack.pop()
        if os.path.isdir(cur):
            # Depth-first: enqueue children first
            for child in os.listdir(cur):
                stack.append(os.path.join(cur, child))
        else:
            if cur.endswith(exts):
                try:
                    with open(cur, "r", encoding="utf-8", errors="ignore") as fp:
                        code_texts.append(fp.read())
                        count += 1
                except Exception as e:
                    print(f"Read failed {cur}: {e}")
    return "\n\n".join(code_texts)


def read_code_bfs(repo_dir, max_files=10, exts=(".py",)):
    code_texts = []
    q = deque([repo_dir])
    count = 0
    while q and count < max_files:
        cur = q.popleft()
        if os.path.isdir(cur):
            for child in os.listdir(cur):
                q.append(os.path.join(cur, child))
        else:
            if cur.endswith(exts):
                try:
                    with open(cur, "r", encoding="utf-8", errors="ignore") as fp:
                        code_texts.append(fp.read())
                        count += 1
                except Exception as e:
                    print(f"Read failed {cur}: {e}")
    return "\n\n".join(code_texts)


def read_repo_code(
    repo_dir,
    mode="all",
    exts=(".py", ".js", ".java", ".cpp", ".c", ".ts"),
    max_files=5,
    lang="python",
    use_filters=False,
):
    """
    Read code from a repo directory.
    mode: "all" | "dfs" | "bfs" | "clean"
    use_filters: whether to apply clean_code_file
    """
    code_texts = []

    def process_file(path):
        """Read (and optionally clean)."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read()
            if use_filters:
                content = clean_code_file(path, content)
            return content
        except Exception as e:
            print(f"Read failed {path}: {e}")
            return ""

    # --- 1. all mode ---
    if mode == "all":
        for root, _, files in os.walk(repo_dir):
            for f in files:
                if f.endswith(exts):
                    code_texts.append(process_file(os.path.join(root, f)))

    # --- 2. dfs mode ---
    elif mode == "dfs":
        stack = [repo_dir]
        count = 0
        while stack and count < max_files:
            cur = stack.pop()
            if os.path.isdir(cur):
                for child in os.listdir(cur):
                    stack.append(os.path.join(cur, child))
            else:
                if cur.endswith(exts):
                    code_texts.append(process_file(cur))
                    count += 1

    # --- 3. bfs mode ---
    elif mode == "bfs":
        q = deque([repo_dir])
        count = 0
        while q and count < max_files:
            cur = q.popleft()
            if os.path.isdir(cur):
                for child in os.listdir(cur):
                    q.append(os.path.join(cur, child))
            else:
                if cur.endswith(exts):
                    code_texts.append(process_file(cur))
                    count += 1

    else:
        raise ValueError("Unknown mode; supported: all / dfs / bfs / clean")

    return "\n\n".join([c for c in code_texts if c.strip()])
