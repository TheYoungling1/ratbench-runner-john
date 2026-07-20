import subprocess, json, os, re
import networkx as nx
import base64
from libkit.analysis.filter import clean_code_file
from collections import defaultdict
import tiktoken

enc = tiktoken.get_encoding("cl100k_base")


def parse_radon_output(radon_text):
    """Parse output text from `radon cc`."""
    # Regex patterns for file paths and functions
    file_pattern = re.compile(r"^(/.*?\.py)$", re.MULTILINE)
    func_pattern = re.compile(
        r"^\s+[FM] (\d+):(\d+) (\w+) - ([A-F]) \((\d+)\)$", re.MULTILINE
    )

    # Parsed result
    result = {}
    current_file = None

    # Process line by line
    for line in radon_text.split("\n"):
        file_match = file_pattern.match(line.strip())
        if file_match:
            current_file = file_match.group(1)
            result[current_file] = []
            continue

        if current_file:
            func_match = func_pattern.match(line)
            if func_match:
                line_num = func_match.group(1)
                func_name = func_match.group(3)
                rating = func_match.group(4)
                complexity = int(func_match.group(5))

                result[current_file].append(
                    {
                        "function": func_name,
                        "line": line_num,
                        "rating": rating,
                        "complexity": complexity,
                    }
                )

    return result


def generate_report(parsed_data):
    """Generate an analysis report."""
    # Per-file stats
    file_stats = {}
    # Per-rating stats
    rating_stats = defaultdict(int)
    # Totals
    total_complexity = 0
    total_functions = 0

    # Collect stats
    for file_path, functions in parsed_data.items():
        file_complexity = sum(f["complexity"] for f in functions)
        file_func_count = len(functions)

        file_stats[file_path] = {
            "total_complexity": file_complexity,
            "function_count": file_func_count,
            "avg_complexity": round(file_complexity / file_func_count, 2)
            if file_func_count > 0
            else 0,
            "highest_rating": max(f["rating"] for f in functions) if functions else "A",
        }

        # Update totals
        total_complexity += file_complexity
        total_functions += file_func_count

        # Update rating stats
        for func in functions:
            rating_stats[func["rating"]] += 1

    # Render report
    report = []
    report.append("=== Cyclomatic Complexity Report ===")
    report.append(f"Total functions: {total_functions}")
    report.append(
        f"Average complexity: {round(total_complexity / total_functions, 2) if total_functions > 0 else 0}"
    )
    report.append("\nRating distribution:")
    for rating in ["A", "B", "C", "D", "E", "F"]:
        report.append(f"  {rating}: {rating_stats[rating]} functions")

    # report.append("\nFile details:")
    # for file_path, stats in sorted(file_stats.items()):
    #     report.append(f"\n{file_path}")
    #     report.append(f"  Function count: {stats['function_count']}")
    #     report.append(f"  Total complexity: {stats['total_complexity']}")
    #     report.append(f"  Avg complexity: {stats['avg_complexity']}")
    #     report.append(f"  Highest rating: {stats['highest_rating']}")

    # Functions that likely need refactoring
    critical_functions = []
    for file_path, functions in parsed_data.items():
        for func in functions:
            if func["rating"] in ["D", "E", "F"]:
                critical_functions.append(
                    {
                        "file": file_path,
                        "function": func["function"],
                        "rating": func["rating"],
                        "complexity": func["complexity"],
                        "line": func["line"],
                    }
                )

    if critical_functions:
        report.append("\nFunctions to refactor:")
        for func in sorted(critical_functions, key=lambda x: x["rating"], reverse=True):
            report.append(
                f"  {func['file']}:{func['line']} {func['function']} - rating {func['rating']} (complexity {func['complexity']})"
            )

    return "\n".join(report)


def cyclomatic_complexity(path="."):
    """Compute cyclomatic complexity using radon."""
    result = subprocess.getoutput(f"radon cc -s {path}")
    parsed_data = parse_radon_output(result)
    report = generate_report(parsed_data)
    return report


def maintainability_complexity(path="."):
    """Compute maintainability index using radon."""
    return subprocess.getoutput(f"radon mi -s {path}")


def maintainability_complexity_average(path="."):
    """Compute average maintainability index using radon."""
    output = subprocess.getoutput(f"radon mi -s {path}")
    pattern = r"\((\d+\.\d+)\)"
    matches = re.findall(pattern, output)
    if not matches:
        return 0.0  # No valid data
    scores = [float(score) for score in matches]
    average = sum(scores) / len(scores)
    return round(average, 2)


def extract_format_and_content(output_text):
    lines = output_text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if "Format" in line:
            start_idx = i
            break

    if start_idx is not None:
        for i in range(start_idx, len(lines)):
            if lines[i].startswith("└"):
                end_idx = i
                break

    if start_idx is not None:
        return "\n".join(lines[start_idx - 1 :])
    else:
        return "Table content containing 'Format' not found"


def extract_rating(output_text):
    lines = output_text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if "---------" in line:
            start_idx = i
            break

    if start_idx is not None:
        return "\n".join(lines[start_idx + 1 :])
    else:
        return "Rating content not found"


def count_tokens_in_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        tokens = enc.encode(text, disallowed_special=())  # Allow special tokens
        return len(tokens)
    except Exception as e:
        print(f"❌ Error reading {filepath}: {e}")
        return 0


def analyze_repo_token(path="."):
    total_tokens = 0
    file_count = 0

    for root, _, files in os.walk(path):
        for file in files:
            filepath = os.path.join(root, file)
            # Only analyze source/text files
            if file.endswith(
                (
                    ".py",
                    ".java",
                    ".js",
                    ".ts",
                    ".cpp",
                    ".c",
                    ".go",
                    ".rs",
                    ".md",
                    ".txt",
                )
            ):
                tokens = count_tokens_in_file(filepath)
                total_tokens += tokens
                file_count += 1

    avg_tokens = total_tokens / file_count if file_count > 0 else 0
    return file_count, total_tokens, avg_tokens


def code_duplication(path=".", only_table=False):
    """Detect code duplication using jscpd."""
    result = subprocess.getoutput(f"npx jscpd --min-lines 5 --reporters console {path}")
    table = extract_format_and_content(result)

    if only_table:
        return table
    return result


def lint_warnings(path="."):
    """Count flake8 lint warnings."""
    output = subprocess.getoutput(f"flake8 {path}")
    return len(output.splitlines())


def unused_imports(path=".", only_rate=False):
    """Detect unused imports using pylint."""
    output = subprocess.getoutput(f"pylint --disable=all --enable=unused-import {path}")
    # print(output)
    if only_rate:
        return extract_rating(output)
    return output


def comment_density(path="."):
    """Comment density = comment lines / total lines."""
    total, comments = 0, 0
    for root, _, files in os.walk(path):
        for f in files:
            if f.endswith(".py"):
                with open(
                    os.path.join(root, f), "r", encoding="utf-8", errors="ignore"
                ) as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        total += 1
                        if line.startswith("#"):
                            comments += 1
    return comments / total if total else 0


def project_structure(path="."):
    """Compute max directory depth and module count."""
    max_depth = 0
    module_count = 0
    for root, _, files in os.walk(path):
        depth = root[len(path) :].count(os.sep)
        max_depth = max(max_depth, depth)
        module_count += sum(1 for f in files if f.endswith((".py", ".js")))
    return {"max_depth": max_depth, "module_count": module_count}


def build_dependency_graph(repo_path, threshold=10):
    G = nx.DiGraph()
    for root, _, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".py"):
                filepath = os.path.join(root, f)
                with open(filepath, "r", errors="ignore") as fh:
                    content = fh.read()
                imports = re.findall(r"import (\S+)|from (\S+) import", content)
                for imp in imports:
                    target = imp[0] or imp[1]
                    G.add_edge(f, target)
    cbo = {n: G.out_degree(n) for n in G.nodes}
    return G, cbo, [n for n, d in cbo.items() if d > threshold]


def get_topk_codefile(repo_path, topk=5):
    content_list = ""
    G = nx.DiGraph()
    for root, _, files in os.walk(repo_path):
        for f in files:
            if f.endswith(".py"):
                filepath = os.path.join(root, f)
                with open(filepath, "r", errors="ignore") as fh:
                    content = fh.read()
                imports = re.findall(r"import (\S+)|from (\S+) import", content)
                for imp in imports:
                    target = imp[0] or imp[1]
                    G.add_edge(f, target)
    cbo = {n: G.out_degree(n) for n in G.nodes}
    sorted_cbo = sorted(cbo.items(), key=lambda x: x[1], reverse=True)
    top5_high_degree_files = [file for file, degree in sorted_cbo[:topk]]
    for root, _, files in os.walk(repo_path):
        for f in files:
            if any(f.endswith(top_file) for top_file in top5_high_degree_files):
                filepath = os.path.join(root, f)
                with open(filepath, "r", errors="ignore") as fh:
                    content = fh.read()
                    # print(f)
                    # print(content)
                    content = clean_code_file(f, content)
                    # content=base64.b64decode(content[:2000])
                content_list += content[:1000]
                # content_list+=content
    return content_list


def get_readme_content(repo_dir, max_length=5000):
    """
    Read README content from a repo directory.
    Returns "[]" if missing; truncates if too long.

    Args:
        repo_dir: Repository directory.
        max_length: Max length; truncate if exceeded.

    Returns:
        Processed README content (string) or "[]".
    """
    # Common README filenames (in priority order)
    readme_filenames = [
        "README.md",
        "README",
        "readme.md",
        "readme",
        "README.rst",
        "readme.rst",
    ]

    # Find a README
    readme_path = None
    for filename in readme_filenames:
        path = os.path.join(repo_dir, filename)
        if os.path.isfile(path):
            readme_path = path
            break

    # No README found
    if not readme_path:
        return "[]"

    # Read content
    try:
        with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        # Read failed (e.g., permissions)
        return "[]"

    # Truncate if too long
    if len(content) > max_length:
        content = content[:max_length] + "\n\n...(content too long; truncated)"

    return content
