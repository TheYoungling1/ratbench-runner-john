import os
from openai import OpenAI
from datetime import datetime
import json
import re
from libkit.config import config as env_config

IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
EXTS = {
    ".py",
    ".js",
    ".ts",
    ".java",
    ".cpp",
    ".c",
    ".go",
    ".rs",
    ".html",
    ".css",
}  # Extensible

# model='deepseek'
model = "glm-4-flash"
max_tokens = None
temperature = 0.0

model_mapping = {
    "gpt": "gpt-3.5-turbo",
    "tongyi": "qwen-turbo",
    "deepseek": "deepseek-chat",
    "glm-4-flash": "glm-4-flash",
}

model_type = model_mapping.get(model)
llm_config = env_config.get_llm_config(model)
client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["key"])


def read_repository(path="."):
    code_files = {}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            ext = os.path.splitext(f)[-1]
            if ext in EXTS:
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as fp:
                        code_files[path] = fp.read()
                except Exception as e:
                    print(f"Skipping {path}: {e}")
    return code_files


def analyze_from_report(owner, repo, report_content):
    global_prompt = f"""
Please analyze the following code quality report for the project "{owner} {repo}" 
and provide a comprehensive assessment covering code quality, potential risks, maintainability, 
and improvement priorities. Focus on strengths, critical issues, and actionable recommendations.  

Key analysis results include:
{report_content}

In addition, please structure your summary to cover:
1. Overall project functionality and architecture.
2. Code quality (style, maintainability, potential bugs).
3. Improvement suggestions.
    """
    response = client.chat.completions.create(
        model=model_type,
        messages=[{"role": "user", "content": global_prompt}],
        stream=False,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content


def analyze_repository(path="."):
    code_files = read_repository(path)
    all_summaries = []
    for path, content in code_files.items():
        print(f"Analyzing {path} ...")
        summary = analyze_file(path, content)
        print(summary)
        all_summaries.append(f"### {path}\n{summary}\n")
    # Global summary
    global_prompt = f"""
Below are per-file analysis results. Please provide an overall summary:
1. Overall project functionality and architecture.
2. Code quality (style, maintainability, potential bugs).
3. Improvement suggestions.

{"".join(all_summaries)}
    """
    response = client.chat.completions.create(
        model=model_type,
        messages=[{"role": "user", "content": global_prompt}],
        stream=False,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content


def analyze_file(filepath, content):
    prompt = f"""
You are a senior software engineer. Analyze the code in the following file and provide: a feature summary, potential issues, and how it may relate to the overall project.

File path: {filepath}

Code:
{content[:6000]}  
    """
    response = client.chat.completions.create(
        model=model_type,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content


def remove_ansi_escape(text):
    """Remove ANSI escape sequences."""
    ansi_escape_pattern = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape_pattern.sub("", text)


def save_analysis_results(
    owner, repo, report_parts, report_content, output_file="analysis_results.json"
):
    """
    Combine analysis results into JSON and save by appending/updating.

    Args:
        owner: Repository owner
        repo: Repository name
        report_parts: List of analysis parts
        output_file: Output JSON path
    """
    # cleaned_parts = [remove_ansi_escape(part) for part in report_parts]
    # report_parts=cleaned_parts
    if "Repo metadata" in report_parts:
        # Build analysis result dict
        analysis_result = {
            "repository": f"{owner}/{repo}",
            "timestamp": datetime.now().isoformat(),  # Analysis timestamp
            "results": {
                # Parse report_parts into key/value pairs
                "repo_metadata": report_parts[0].replace("Repo metadata: ", ""),
                "average_response_time": report_parts[1].replace(
                    "Average response time: ", ""
                ),
                "branch_analysis": report_parts[2].replace("Branch analysis: ", ""),
                "issue_dataframe": report_parts[3].replace("Issue DataFrame: ", ""),
                "vulnerability_scan": report_parts[4].replace(
                    "Vulnerability scan: ", ""
                ),
                "injection_scan": report_parts[5].replace("Injection scan: ", ""),
                "naive_injection_scan": report_parts[6].replace(
                    "Naive injection scan: ", ""
                ),
                "cyclomatic_complexity": report_parts[7].replace(
                    "Cyclomatic complexity:\n", ""
                ),
                "maintainability_index": report_parts[8].replace(
                    "Maintainability index: ", ""
                ),
                "duplicate_content": report_parts[9].replace(
                    "Duplicate content:\n", ""
                ),
                "flake8_lint_warnings": report_parts[10].replace(
                    "Flake8 lint warnings: ", ""
                ),
                "unused_imports_score": report_parts[11].replace(
                    "Unused imports score: ", ""
                ),
                "comment_density": report_parts[12].replace("Comment density: ", ""),
                "potential_privacy_leaks": report_parts[13].replace(
                    "Potential privacy leaks: ", ""
                ),
                "project_structure": report_parts[14].replace(
                    "Project structure: ", ""
                ),
                "high_coupling_components": report_parts[15].replace(
                    "High Coupling Components: ", ""
                ),
                "readme_content": report_parts[16].replace("Readme content: ", ""),
                "file_count": report_parts[17].replace("File count: ", ""),
                "total_tokens": report_parts[18].replace("Total tokens: ", ""),
                "avg_tokens": report_parts[19].replace("Avg tokens: ", ""),
            },
            # Optional overall conclusion
            "summary": report_content,  # Can be filled by analyze_from_report
        }
    else:
        analysis_result = {
            "repository": f"{owner}/{repo}",
            "timestamp": datetime.now().isoformat(),  # Analysis timestamp
            "results": {
                # Parse report_parts into key/value pairs
                "vulnerability_scan": report_parts[0].replace(
                    "Vulnerability scan: ", ""
                ),
                "injection_scan": report_parts[1].replace("Injection scan: ", ""),
                "naive_injection_scan": report_parts[2].replace(
                    "Naive injection scan: ", ""
                ),
                "cyclomatic_complexity": report_parts[3].replace(
                    "Cyclomatic complexity:\n", ""
                ),
                "maintainability_index": report_parts[4].replace(
                    "Maintainability index: ", ""
                ),
                "duplicate_content": report_parts[5].replace(
                    "Duplicate content:\n", ""
                ),
                "flake8_lint_warnings": report_parts[6].replace(
                    "Flake8 lint warnings: ", ""
                ),
                "unused_imports_score": report_parts[7].replace(
                    "Unused imports score: ", ""
                ),
                "comment_density": report_parts[8].replace("Comment density: ", ""),
                "potential_privacy_leaks": report_parts[9].replace(
                    "Potential privacy leaks: ", ""
                ),
                "project_structure": report_parts[10].replace(
                    "Project structure: ", ""
                ),
                "high_coupling_components": report_parts[11].replace(
                    "High Coupling Components: ", ""
                ),
                "readme_content": report_parts[12].replace("Readme content: ", ""),
                "file_count": report_parts[13].replace("File count: ", ""),
                "total_tokens": report_parts[14].replace("Total tokens: ", ""),
                "avg_tokens": report_parts[15].replace("Avg tokens: ", ""),
            },
            # Optional overall conclusion
            "summary": report_content,  # Can be filled by analyze_from_report
        }

    # Load existing data
    existing_data = []
    if os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            try:
                existing_data = json.load(f)
                if not isinstance(existing_data, list):
                    existing_data = [existing_data]  # Ensure list
            except json.JSONDecodeError:
                existing_data = []  # Corrupt file; rebuild

    # Check whether a record for this repo already exists
    repo_identifier = analysis_result["repository"]
    found_index = -1
    for i, item in enumerate(existing_data):
        if item.get("repository") == repo_identifier:
            found_index = i
            break  # First match

    # Replace if exists, otherwise append
    if found_index != -1:
        existing_data[found_index] = analysis_result  # Replace
        print(f"Updated analysis result for {repo_identifier}")
    else:
        existing_data.append(analysis_result)  # Append
        print(f"Appended analysis result for {repo_identifier}")

    # Save to file
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, ensure_ascii=False, indent=2)

    # Print final JSON
    print("\nFinal JSON result:")
    print(json.dumps(existing_data, ensure_ascii=False, indent=2))

    return existing_data


def extract_scores_from_analysis_json(repo_data):
    results = repo_data["results"]
    scores = {}

    # maintainability_index
    try:
        scores["maintainability_index"] = float(results["maintainability_index"])
    except:
        scores["maintainability_index"] = None

    # cyclomatic complexity: average complexity
    cc_text = results.get("cyclomatic_complexity", "")
    m = re.search(r"Average complexity: ([0-9.]+)", cc_text)
    scores["avg_cyclomatic_complexity"] = float(m.group(1)) if m else None

    # flake8 warnings
    try:
        scores["flake8_warnings"] = int(results["flake8_lint_warnings"])
    except:
        scores["flake8_warnings"] = None

    # unused imports score
    m = re.search(r"rated at ([0-9.]+)/10", results.get("unused_imports_score", ""))
    scores["unused_imports_score"] = float(m.group(1)) if m else None

    # comment density
    try:
        scores["comment_density"] = float(results["comment_density"])
    except:
        scores["comment_density"] = None

    try:
        scores["stars"] = int(results["stars"])
    except:
        scores["stars"] = None

    try:
        scores["files_num"] = int(results["files_num"])
    except:
        scores["files_num"] = None

    try:
        scores["total_tokens"] = int(results["total_tokens"])
    except:
        scores["total_tokens"] = None

    try:
        scores["avg_tokens"] = float(results["avg_tokens"])
    except:
        scores["avg_tokens"] = None

    # duplicate_content: extract clones, duplicated lines, duplicated tokens
    dup_text = results.get("duplicate_content", "")
    m_clones = re.search(r"Found (\d+) clones", dup_text)
    scores["clones_found"] = int(m_clones.group(1)) if m_clones else None

    m_lines = re.search(r"Duplicated lines.*?(\d+(\.\d+)?)%", dup_text)
    scores["duplicated_lines_pct"] = float(m_lines.group(1)) if m_lines else None

    m_tokens = re.search(r"Duplicated tokens.*?(\d+(\.\d+)?)%", dup_text)
    scores["duplicated_tokens_pct"] = float(m_tokens.group(1)) if m_tokens else None

    # naive injection count
    inj_text = results.get("naive_injection_scan", "[]")
    matches = re.findall(r"'file':", inj_text)
    scores["naive_injection_count"] = len(matches)

    return scores
