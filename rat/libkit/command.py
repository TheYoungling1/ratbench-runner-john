import os
import requests
import subprocess
import json
import time
from libkit.config import config
from libkit.llm import LLMChat

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = config.GITHUB_TOKEN
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}


def print_timing_summary(task_timings):
    """Print a timing summary for all stages."""
    if not task_timings:
        return

    def display_width(s):
        """Compute display width (treat wide characters as width 2)."""
        return sum(2 if ord(c) > 127 else 1 for c in s)

    print("\n" + "=" * 50)
    print("  Timing Summary")
    print("=" * 50)

    max_width = 40
    for task_name, elapsed in task_timings:
        padding = max_width - display_width(task_name)
        print(f"  {task_name}{' ' * padding} {elapsed:>6.2f}s")

    print("-" * 50)
    total = sum(t[1] for t in task_timings)
    padding = max_width - display_width("Total")
    print(f"  Total{' ' * padding} {total:>6.2f}s")
    print("=" * 50)


def timer():
    time.sleep(3600 * 2)
    print("Timeout for 2 hour!")
    os._exit(1)


def get_default_namespace(language):
    if language == "python":
        return "python:3.10"
    elif language == "java":
        return "eclipse-temurin:17-jdk"
    else:
        return "python:3.10"


def detect_language_from_dockerfile(dockerfile_path):
    """
    Detect language from a Dockerfile.

    Args:
        dockerfile_path: Dockerfile path

    Returns:
        str: Detected language (python/java/javascript/go, etc.)
    """
    language = "python"  # Default
    try:
        with open(dockerfile_path, "r") as f:
            dockerfile_content = f.read().lower()
            # Simple language detection
            if (
                "openjdk" in dockerfile_content
                or "maven" in dockerfile_content
                or "gradle" in dockerfile_content
            ):
                language = "java"
            elif "node" in dockerfile_content or "npm" in dockerfile_content:
                language = "javascript"
            elif "golang" in dockerfile_content or "go:" in dockerfile_content:
                language = "go"
            elif "python" in dockerfile_content or "pip" in dockerfile_content:
                language = "python"
    except Exception as e:
        print(f"⚠️  Failed to read Dockerfile: {e}")
    return language


def find_dockerfiles(repo_path):
    """
    Find all Dockerfiles in a repository.

    Args:
        repo_path: Repository path

    Returns:
        list: List of Dockerfile paths
    """
    import glob

    dockerfiles = []

    # Root Dockerfile
    root_dockerfile = os.path.join(repo_path, "Dockerfile")
    if os.path.exists(root_dockerfile) and not os.path.isdir(root_dockerfile):
        dockerfiles.append(root_dockerfile)

    # Root-level *.Dockerfile
    for dockerfile in glob.glob(os.path.join(repo_path, "*.Dockerfile")):
        if os.path.isfile(dockerfile):
            dockerfiles.append(dockerfile)

    # Dockerfile in subdirectories (search up to 2 levels)
    for dockerfile in glob.glob(os.path.join(repo_path, "*/Dockerfile")):
        if os.path.isfile(dockerfile):
            dockerfiles.append(dockerfile)

    for dockerfile in glob.glob(os.path.join(repo_path, "*/*/Dockerfile")):
        if os.path.isfile(dockerfile):
            dockerfiles.append(dockerfile)

    return dockerfiles


def setup_environment_config(root_path, full_name, llm_name, use_dockerfile=False):
    """
    Environment setup: decide between the repo Dockerfile and SetupAgent analysis.

    Args:
        root_path: Project root path
        full_name: Repository full name (owner/repo)
        llm_name: LLM model name
        use_dockerfile: Whether to use the repo-provided Dockerfile

    Returns:
        dict: Config including language and namespace
    """
    try:
        from libkit.ablation_config import ENABLE_SETUP_ABLATION
    except ImportError:
        ENABLE_SETUP_ABLATION = False

    if ENABLE_SETUP_ABLATION:
        print("**************************************************")
        print("* ABLATION MODE ENABLED: Using SetupAgentAblation *")
        print("**************************************************")
        from libkit.setupagent_ablation import SetupAgent
    else:
        from libkit.setupagent import SetupAgent

    # Determine whether to use the repo-provided Dockerfile
    repo_path = f"{root_path}/input/repo/{full_name}"
    dockerfiles = find_dockerfiles(repo_path)

    if use_dockerfile and dockerfiles:
        # Use the repo Dockerfile and skip SetupAgent analysis
        print("=" * 80)
        print("🐳 Detected --use-dockerfile and the repo contains Dockerfile")
        print(f"📄 Found {len(dockerfiles)} Dockerfile(s):")
        for df in dockerfiles:
            rel_path = os.path.relpath(df, repo_path)
            print(f"   - {rel_path}")

        # Prefer the root Dockerfile; otherwise use the first one found
        primary_dockerfile = dockerfiles[0]
        for df in dockerfiles:
            if (
                os.path.basename(df) == "Dockerfile"
                and os.path.dirname(df) == repo_path
            ):
                primary_dockerfile = df
                break

        print(f"📌 Will use: {os.path.relpath(primary_dockerfile, repo_path)}")
        print("⏭️  Skipping SetupAgent image recommendation; using repo Dockerfile")
        print("=" * 80)

        # Detect language from Dockerfile
        language = detect_language_from_dockerfile(primary_dockerfile)
        namespace = "repo_dockerfile"  # Placeholder; replaced during build_image

        print(f"🔍 Detected language from Dockerfile: {language}")

        return {
            "language": language,
            "namespace": namespace,
            "use_repo_dockerfile": True,
            "primary_dockerfile": primary_dockerfile,
        }
    else:
        # Use SetupAgent analysis
        if use_dockerfile and not dockerfiles:
            print("⚠️  --use-dockerfile specified but no Dockerfile found in repo")
            print("📌 Will use SetupAgent to analyze and generate a Dockerfile")

        setup_agent = SetupAgent(
            full_name, root_path, llm_name, use_memory=False, use_llm_analysis=True
        )
        try:
            setup_result = setup_agent.run()
            language = setup_result.get("language", "unknown")
            namespace = setup_result.get(
                "recommended_base_image", get_default_namespace(language)
            )
            dockerfile_content = setup_result.get("dockerfile_content", None)

            return {
                "language": language,
                "namespace": namespace,
                "use_repo_dockerfile": False,
                "dockerfile_content": dockerfile_content,
            }
        finally:
            # Ensure Docker client resources are cleaned up
            setup_agent.cleanup()


def stop_and_remove_container():
    pass
    # running_containers = (
    #     subprocess.check_output("docker ps -q", shell=True).decode("utf-8").strip()
    # )
    # if running_containers:
    #     print("📋 Running command:", "docker stop $(docker ps -q)")
    #     subprocess.run("docker stop $(docker ps -q)", shell=True)
    # all_containers = (
    #     subprocess.check_output("docker ps -a -q", shell=True).decode("utf-8").strip()
    # )
    # if all_containers:
    #     print("📋 Running command:", "docker rm $(docker ps -a -q)")
    #     subprocess.run("docker rm $(docker ps -a -q)", shell=True)


def fetch_repo_metadata(owner, repo):
    """Fetch repository metadata."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    # data = requests.get(url, headers=HEADERS).json()
    data = requests.get(url, headers=HEADERS).json()
    return data, {
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "watchers": data.get("subscribers_count"),
        "open_issues": data.get("open_issues_count"),
        "last_updated": data.get("updated_at"),
        "default_branch": data.get("default_branch", "master"),
    }


def init_output_and_repo(root_path, full_name, renew=True):
    """
    Initialize files.
    """
    # if os.path.exists(f'{root_path}/output/{full_name}/patch'):
    #     rm_cmd = f"rm -rf {root_path}/output/{full_name}/patch"
    #     subprocess.run(rm_cmd, shell=True, check=True)
    if not os.path.exists(
        f"{root_path}/output/{full_name.split('/')[0]}/{full_name.split('/')[1]}"
    ):
        subprocess.run(
            f"mkdir -p {root_path}/output/{full_name.split('/')[0]}/{full_name.split('/')[1]}",
            shell=True,
        )
    if os.path.exists(f"{root_path}/input/repo/{full_name}"):
        if renew:
            init_cmd = f"rm -rf {root_path}/input/repo/{full_name} && mkdir -p {root_path}/input/repo/{full_name}"
        else:
            init_cmd = f"mkdir -p {root_path}/input/repo/{full_name}"
    else:
        init_cmd = f"mkdir -p {root_path}/input/repo/{full_name}"
    print("📋 Running command:", init_cmd)
    subprocess.run(init_cmd, check=True, shell=True)


def save_trajectory(root_path, full_name, trajectory):
    with open(
        f"{root_path}/output/{full_name.split('/')[0]}/{full_name.split('/')[1]}/trajectory.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(trajectory, f, ensure_ascii=False, indent=2)


def download_repo(
    root_path, full_name, has_issue=True, use_repo_dockerfile=False, use_pipreqs=False
):
    """
    Download a repository with git clone; if it already exists, reset to a clean state.

    Args:
        root_path: Project root path
        full_name: Repository full name (owner/repo)
        has_issue: Whether to download issues
        use_repo_dockerfile: Whether to use the repo-provided Dockerfile (False means delete repo Dockerfile and use a generated one)
        use_pipreqs: Whether to analyze dependencies via pipreqs

    Behavior:
        - First download: git clone --depth=1
        - If already exists: git reset --hard HEAD + git clean -fdx (no pull)
    """
    if len(full_name.split("/")) != 2:
        raise Exception("full_name Wrong!!!")
    owner = full_name.split("/")[0]
    repo = full_name.split("/")[1]
    repo_dir = f"{root_path}/input/repo/{owner}/{repo}"

    # Fetch repository metadata (for stats)
    # _, info_dict = fetch_repo_metadata(owner, repo)
    # Git operations: clone or reset
    if os.path.exists(f"{repo_dir}/.git"):
        # Repo already exists: reset to a clean state
        print(f"🔄 Resetting existing repo: {full_name}")
        subprocess.run("git reset --hard HEAD", cwd=repo_dir, shell=True, check=True)
        # Exclude issue and .pipreqs directories to preserve cache
        subprocess.run(
            "git clean -fdx -e issue -e .pipreqs", cwd=repo_dir, shell=True, check=True
        )
        print(f"✅ Successfully reset repo {owner}/{repo}")
    else:
        # First clone
        print(f"📥 Cloning repo: {full_name}")
        os.makedirs(f"{root_path}/input/repo/{owner}", exist_ok=True)
        clone_cmd = f"git clone --depth=1 https://github.com/{full_name}.git {repo_dir}"
        print(f"📋 Running command: {clone_cmd}")
        subprocess.run(clone_cmd, shell=True, check=True)
        print(f"✅ Successfully cloned repo {owner}/{repo}")

    # Download issues
    if has_issue:
        download_issue(root_path, full_name)

    # Handle Dockerfile
    if not use_repo_dockerfile:
        dockerfile_path = f"{repo_dir}/Dockerfile"
        if os.path.exists(dockerfile_path) and not os.path.isdir(dockerfile_path):
            print(f"📋 Running command (delete Dockerfile): rm -rf {dockerfile_path}")
            os.remove(dockerfile_path)
    else:
        if os.path.exists(f"{repo_dir}/Dockerfile"):
            print("✅ Keeping repo Dockerfile")
        else:
            print(
                "⚠️ No Dockerfile found at repo root; using system default configuration"
            )

    # Analyze dependencies via pipreqs
    if use_pipreqs:
        pipreqs_dir = f"{repo_dir}/.pipreqs"
        os.makedirs(pipreqs_dir, exist_ok=True)

        # Cache check: skip if result file already exists
        pipreqs_result_file = f"{pipreqs_dir}/requirements_pipreqs.txt"
        if os.path.exists(pipreqs_result_file):
            print(f"⏭️  Skipping already-analyzed dependencies: {full_name}")
        else:
            pipreqs_cmd = "pipreqs --savepath=.pipreqs/requirements_pipreqs.txt --force --ignore tests"
            print(f"📋 Running command: {pipreqs_cmd}")

            pipreqs_result = subprocess.run(
                pipreqs_cmd,
                cwd=repo_dir,
                check=False,
                shell=True,
                capture_output=True,
            )

            with open(f"{pipreqs_dir}/pipreqs_output.txt", "w") as w1:
                w1.write(pipreqs_result.stdout.decode("utf-8"))
            with open(f"{pipreqs_dir}/pipreqs_error.txt", "w") as w2:
                w2.write(pipreqs_result.stderr.decode("utf-8"))

            print(f"✅ Successfully analyzed dependencies for {owner}/{repo}")


def fetch_issue_comments(owner, repo, issue_number, max_comments=10):
    """
    Fetch comments for a single issue.

    Args:
        owner: Repository owner
        repo: Repository name
        issue_number: Issue number
        max_comments: Max number of comments to fetch (default 10)

    Returns:
        List of comments
    """
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
        params = {"per_page": max_comments}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)

        if resp.status_code != 200:
            print(
                f"  ⚠️ Failed to fetch comments for issue #{issue_number}: {resp.status_code}"
            )
            return []

        comments_data = resp.json()

        # Clean comment data and keep only essential fields
        cleaned_comments = []
        for comment in comments_data[:max_comments]:  # Limit count
            cleaned_comments.append(
                {
                    "id": comment.get("id"),
                    "user": {
                        "login": comment.get("user", {}).get("login"),
                        "avatar_url": comment.get("user", {}).get("avatar_url"),
                    },
                    "body": comment.get("body"),
                    "created_at": comment.get("created_at"),
                    "updated_at": comment.get("updated_at"),
                }
            )

        return cleaned_comments

    except Exception as e:
        print(f"  ⚠️ Error fetching comments for issue #{issue_number}: {e}")
        return []


def clean_issue(issues, owner, repo, fetch_comments=True, max_comments=10):
    """
    Clean and enrich issue data.

    Args:
        issues: Raw issue list
        owner: Repository owner
        repo: Repository name
        fetch_comments: Whether to fetch comments (default True)
        max_comments: Max number of comments per issue

    Returns:
        (pull_list, issue_list): PR list and Issue list
    """
    pull_list = []
    issue_list = []

    for idx, issue in enumerate(issues, 1):
        comments_data = []
        comments_count = issue.get("comments", 0)

        # Fetch comments only for issues that have comments (performance optimization)
        if fetch_comments and comments_count > 0:
            print(
                f"  📝 Fetching comments for issue #{issue.get('number')}... ({idx}/{len(issues)})"
            )
            comments_data = fetch_issue_comments(
                owner, repo, issue.get("number"), max_comments
            )

        clean_issue_data = {
            "id": issue.get("number"),  # Issue number
            "title": issue.get("title"),  # Title
            "state": issue.get("state"),  # State (open/closed)
            "body": issue.get("body"),  # Body
            "comments_count": comments_count,  # Comment count
            "comments": comments_data,  # Comment content (added)
            "link": issue.get("html_url"),  # Issue link
            "created_at": issue.get("created_at"),  # Created time
            "updated_at": issue.get("updated_at"),  # Updated time
            "labels": [
                label.get("name") for label in issue.get("labels", [])
            ],  # Labels
        }

        if "pull_request" in issue:
            pull_list.append(clean_issue_data)
        else:
            issue_list.append(clean_issue_data)

    return pull_list, issue_list


def download_issue(
    root_path,
    full_name,
    fetch_comments=True,
    max_issues=50,
    max_comments=10,
    force_update=False,
):
    """
    Download repository issues and pull requests (optionally with comments).

    Args:
        root_path: Project root path
        full_name: Repository full name (owner/repo)
        fetch_comments: Whether to fetch comments (default True)
        max_issues: Max number of issues to fetch (limit API calls)
        max_comments: Max comments per issue (default 10)
        force_update: Force re-download (default False; skip if exists)
    """
    if len(full_name.split("/")) != 2:
        raise Exception("Full_name Wrong!!!")
    owner, repo = full_name.split("/")
    issue_save_dir = f"{root_path}/input/repo/{owner}/{repo}/issue"
    os.makedirs(issue_save_dir, exist_ok=True)

    # Cache check: skip if files exist and not force updating
    issues_path = os.path.join(issue_save_dir, "issues.json")
    pulls_path = os.path.join(issue_save_dir, "pull_requests.json")

    if not force_update and os.path.exists(issues_path) and os.path.exists(pulls_path):
        print(f"⏭️  Skipping already-downloaded issues: {full_name}")
        return

    print(f"📥 Downloading issues for {full_name}...")
    if fetch_comments:
        print(f"  📝 Will fetch comments for each issue (up to {max_comments})")

    all_issues = []
    page = 1
    per_page = min(20, max_issues)  # GitHub API supports up to 100

    while len(all_issues) < max_issues:
        url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        params = {
            "state": "all",  # all, open, closed
            "page": page,
            "per_page": per_page,
            "sort": "updated",  # Sort by update time to fetch newest first
            "direction": "desc",
        }

        resp = requests.get(url, headers=HEADERS, params=params)
        if resp.status_code != 200:
            print(f"⚠️ Failed to fetch issues: {resp.status_code} - {resp.text}")
            break
        issues = resp.json()
        if not issues:
            break
        all_issues.extend(issues)
        # If fewer than per_page were returned, we've reached the last page
        if len(issues) < per_page:
            break

        page += 1

        # Stop after hitting the limit
        if len(all_issues) >= max_issues:
            all_issues = all_issues[:max_issues]
            break

    print(f"  📋 Fetched {len(all_issues)} issues/PRs")

    # Clean and enrich issue data (including comments)
    pull_list, issue_list = clean_issue(
        all_issues, owner, repo, fetch_comments, max_comments
    )

    # Save to files
    issues_path = os.path.join(issue_save_dir, "issues.json")
    with open(issues_path, "w", encoding="utf-8") as f:
        json.dump(issue_list, f, ensure_ascii=False, indent=2)

    pulls_path = os.path.join(issue_save_dir, "pull_requests.json")
    with open(pulls_path, "w", encoding="utf-8") as f:
        json.dump(pull_list, f, ensure_ascii=False, indent=2)

    # Count comments
    total_comments = sum(len(issue.get("comments", [])) for issue in issue_list)
    total_pr_comments = sum(len(pr.get("comments", [])) for pr in pull_list)

    print(
        f"✅ Downloaded {len(issue_list)} issues successfully (total {total_comments} comments)"
    )
    print(
        f"✅ Downloaded {len(pull_list)} pull requests successfully (total {total_pr_comments} comments)"
    )
    print(f"💾 Saved to: {issues_path}")
