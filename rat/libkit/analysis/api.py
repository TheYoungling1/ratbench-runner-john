import requests
from datetime import datetime
import pandas as pd
import os
import requests
import zipfile
import io
import base64
from libkit.config import config

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = config.GITHUB_TOKEN
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}


def get_default_branch(owner, repo):
    """Get the repository default branch."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    return data.get("default_branch", "main")


def list_branches(owner, repo, per_page=100):
    url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    r = requests.get(url, params={"per_page": per_page})
    r.raise_for_status()
    return r.json()


def fetch_repo_tree(owner, repo, branch="master"):
    """Fetch the repository tree (recursive)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Failed to fetch {repo}: {resp.text}")
        return []
    tree = resp.json().get("tree", [])
    return tree


def download_repo(owner, repo, save_dir="./repos"):
    # Fetch metadata to get default branch
    _, info_dict = fetch_repo_metadata(owner, repo)
    branch = info_dict.get("default_branch", "main")  # default: main
    # Build download URL
    url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
    resp = requests.get(url, headers=HEADERS, stream=True)

    if resp.status_code != 200:
        print(f"Failed to download {repo}: {resp.text}")
        return None
    os.makedirs(save_dir, exist_ok=True)
    zip_path = os.path.join(save_dir, f"{repo}.zip")

    # Download zip
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
            if chunk:  # Skip empty chunks
                f.write(chunk)

    extract_dir = os.path.join(save_dir, f"{owner}__{repo}")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(extract_dir)

    # Remove temp zip
    if os.path.exists(zip_path):
        os.remove(zip_path)
        print(f"Deleted temporary zip file: {zip_path}")

    print(f"✅ Repo {owner}/{repo} downloaded to {extract_dir}")
    return extract_dir


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


def fetch_github_issues(
    owner, repo, item_type="issues", state="all", n=100, token=None
):
    """
    Fetch GitHub repository issues or pull requests.

    Args:
        owner (str): Repository owner
        repo (str): Repository name
        item_type (str): "issues" or "pulls"
        state (str): "open" / "closed" / "all"
        n (int): Number to fetch (default: 100)
        token (str): GitHub token to avoid rate limiting (optional)

    Returns:
        pd.DataFrame: Includes id, number, title, body, user, state, url, etc.
    """
    assert item_type in ["issues", "pulls"], "item_type must be issues or pulls"

    url = f"https://api.github.com/repos/{owner}/{repo}/{item_type}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    all_items = []
    per_page = 50
    page = 1
    while len(all_items) < n:
        params = {"state": state, "per_page": per_page, "page": page}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        for item in items:
            all_items.append(
                {
                    "id": item["id"],
                    "number": item["number"],
                    "title": item.get("title"),
                    "body": item.get("body"),
                    "user": item["user"]["login"] if "user" in item else None,
                    "state": item.get("state"),
                    "url": item.get("html_url"),
                }
            )
        page += 1
    df = pd.DataFrame(all_items[:n])
    return df


def fetch_github_files(owner, repo, path="", branch=None):
    """Recursively download GitHub repository files."""
    if branch is None:  # Auto-detect default branch
        branch = get_default_branch(owner, repo)

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    response = requests.get(url)
    response.raise_for_status()
    items = response.json()

    files = {}
    for item in items:
        if item["type"] == "file":
            if any(
                item["name"].endswith(ext)
                for ext in [".py", ".js", ".ts", ".java", ".cpp"]
            ):
                file_content = requests.get(item["download_url"]).text
                files[item["path"]] = file_content
                print(file_content)
        elif item["type"] == "dir":
            files.update(fetch_github_files(owner, repo, item["path"], branch))
    return files


def fetch_file_content(owner, repo, path, branch="master"):
    """Fetch file content."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Failed to fetch file {path}: {resp.text}")
        return None
    content = resp.json().get("content", "")
    return base64.b64decode(content).decode("utf-8", errors="ignore")


def get_issue_response_time(owner, repo, limit=5):
    """Compute average response time for the first few issues."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
    issues = requests.get(url, params={"state": "all", "per_page": limit}).json()
    times = []
    # print("Issues:", issues)
    for issue in issues:
        if "pull_request" in issue:
            continue
        comments_url = issue["comments_url"]
        comments = requests.get(comments_url).json()
        if comments:
            created = datetime.fromisoformat(issue["created_at"][:-1])
            first_reply = datetime.fromisoformat(comments[0]["created_at"][:-1])
            times.append((first_reply - created).total_seconds() / 3600)
    return sum(times) / len(times) if times else None


def branch_analysis(owner, repo):
    default = get_default_branch(owner, repo)
    branches = list_branches(owner, repo)
    out = []
    for b in branches:
        name = b["name"]
        sha = b["commit"]["sha"]
        # Latest commit details
        c = requests.get(f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}")
        c.raise_for_status()
        commit = c.json()
        pushed = commit["commit"]["committer"]["date"]

        # Compare ahead/behind against default branch
        cmp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/compare/{default}...{name}"
        )
        if cmp.status_code == 200:
            diff = cmp.json()
            ahead, behind = diff.get("ahead_by", None), diff.get("behind_by", None)
        else:
            ahead = behind = None

        out.append(
            {
                "branch": name,
                "default": name == default,
                "last_commit_iso": pushed,
                "last_commit_age_days": (
                    datetime.utcnow() - datetime.fromisoformat(pushed.replace("Z", ""))
                ).days,
                "ahead_by": ahead,
                "behind_by": behind,
            }
        )
    return out
