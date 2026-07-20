#!/usr/bin/env python3
"""
retrieve_image tool (refactored)
Purpose: infer the most suitable Docker base image from repository config files via an LLM
Input: repository path
Output: recommended Docker image info (see ALLOWED_VERSIONS.json)
"""

import json
import requests
from pathlib import Path
from typing import Dict, List

from libkit.llm import LLMChat


class ImageRetriever:
    """
    Docker image inferencer.

    Simplified flow:
    1. Collect repo config files (requirements.txt, package.json, ...)
    2. Collect README content
    3. Load ALLOWED_VERSIONS.json as constraints
    4. Use an LLM to infer the best Docker base image
    """

    # Supported config file patterns
    CONFIG_FILES = {
        # Python
        "requirements.txt": "python",
        "setup.py": "python",
        "setup.cfg": "python",
        "pyproject.toml": "python",
        "Pipfile": "python",
        "Pipfile.lock": "python",
        "poetry.lock": "python",
        "environment.yml": "python",
        "environment.yaml": "python",
        "conda.yml": "python",
        "conda.yaml": "python",
        # JavaScript/Node
        "package.json": "javascript",
        "package-lock.json": "javascript",
        "yarn.lock": "javascript",
        "pnpm-lock.yaml": "javascript",
        ".nvmrc": "javascript",
        # Java
        "pom.xml": "java",
        "build.gradle": "java",
        "build.gradle.kts": "java",
        "settings.gradle": "java",
        "settings.gradle.kts": "java",
        "gradle.properties": "java",
        # Rust
        "Cargo.toml": "rust",
        "Cargo.lock": "rust",
        # Go
        "go.mod": "go",
        "go.sum": "go",
        # Docker
        "Dockerfile": "docker",
        "docker-compose.yml": "docker",
        "docker-compose.yaml": "docker",
        ".dockerignore": "docker",
        # Build tools
        "Makefile": "build",
        "CMakeLists.txt": "build",
    }

    # README filename patterns
    README_PATTERNS = [
        "README.md",
        "README.MD",
        "readme.md",
        "Readme.md",
        "README.rst",
        "README.txt",
        "README",
    ]

    # Directories to skip
    SKIP_DIRS = {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "bower_components",
        "venv",
        ".venv",
        "env",
        ".env",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        ".idea",
        ".vscode",
    }

    def __init__(self, repo_path: str, llm_name: str = "deepseek-chat"):
        """
        Initialize ImageRetriever.

        Args:
            repo_path: Repository path.
            llm_name: LLM model name.
        """
        self.repo_path = Path(repo_path)
        self.llm_name = llm_name
        self.llm = LLMChat(llm_name)
        self.docker_hub_api = "https://hub.docker.com/v2"
        self.docker_hub_search = f"{self.docker_hub_api}/search/repositories"
        self.use_llm = True

        # Hard-coded ALLOWED_VERSIONS.json path
        self.allowed_versions_path = (
            Path(__file__).parent.parent
            / "dockerfile_templates"
            / "ALLOWED_VERSIONS.json"
        )

    def retrieve_image(self) -> Dict:
        """
        Main entry point: infer the most suitable Docker image.

        Returns:
            {
                "language": "python",
                "version": "3.10",
                "variant": "slim",
                "base_image": "python",
                "full_image": "python:3.10-slim",
                "reason": "Reason for recommendation",
                "confidence": "high|medium|low",
                "frameworks": [...],  # optional
                "dependencies": [...]  # optional
            }
        """
        print("🔍 ImageRetriever: starting repository analysis...")

        # 1. Collect config files
        config_files = self._collect_config_files()
        print(f"  📄 Found {len(config_files)} config files")

        # 2. Collect README files
        readme_content = self._collect_readme_files()
        if readme_content:
            print(f"  📖 Collected README content ({len(readme_content)} chars)")

        # 3. Load allowed versions
        allowed_versions = self._load_allowed_versions()

        # 4. Use LLM to infer best image
        result = self._llm_infer_image(config_files, readme_content, allowed_versions)

        # 5. Optional Docker Hub search for better images
        try:
            if not self._should_search_docker_hub(result, config_files):
                print("  ⏭️  Dependencies look light; skipping Docker Hub search")
                print(f"  ✅ Recommended image: {result.get('full_image', 'unknown')}")
                print(f"  📝 Reason: {result.get('reason', 'N/A')[:100]}...")
                return result

            print("  🔍 Searching Docker Hub for a better image...")
            search_keywords = []

            # Generate keywords with LLM
            search_keywords = self._llm_generate_search_keywords(result)

            # Fall back to a default strategy
            if not search_keywords and result.get("language") != "unknown":
                lang = result["language"]
                search_keywords = [lang]
                if result.get("frameworks"):
                    search_keywords.extend(result["frameworks"][:1])

            if search_keywords:
                candidates = []
                print(f"    Keywords: {search_keywords}")

                for kw in search_keywords:
                    images = self.search_docker_hub(kw, limit=2)
                    candidates.extend(images)

                # Deduplicate
                unique_candidates = {}
                for img in candidates:
                    key = f"{img.get('namespace', 'library')}/{img.get('name', '')}"
                    if key not in unique_candidates:
                        unique_candidates[key] = img

                candidate_list = list(unique_candidates.values())

                if candidate_list:
                    # initial scoring
                    # for img in candidate_list:
                    #     img["relevance_score"] = self.score_image(img, result)

                    # candidate_list.sort(key=lambda x: x["relevance_score"], reverse=True)
                    top_candidates = candidate_list
                    print("🔝 Candidate images:", top_candidates)
                    # LLM scoring
                    llm_eval = self.llm_score_images(top_candidates, result)

                    # Log recommendation
                    if llm_eval.get("recommendation"):
                        print(f"    💡 LLM suggestion: {llm_eval['recommendation']}")

                    # Check for high score candidates
                    best_candidate = None
                    max_score = llm_eval.get("max_score", 0)

                    if (
                        max_score >= 80
                        and llm_eval.get("best_strategy") == "use_candidate"
                    ):
                        # Find the max score candidate
                        for key, score_info in llm_eval.get("scores", {}).items():
                            if score_info.get("score") == max_score and score_info.get(
                                "suitable"
                            ):
                                ns, name = (
                                    key.split("/", 1)
                                    if "/" in key
                                    else ("library", key)
                                )
                                for img in top_candidates:
                                    if (
                                        img.get("name") == name
                                        and img.get("namespace", "library") == ns
                                    ):
                                        best_candidate = img
                                        break
                                if best_candidate:
                                    break

                    if best_candidate:
                        print(
                            f"    🚀 Found a better image: {best_candidate.get('name')} (score: {max_score})"
                        )

                        ns = best_candidate.get("namespace", "library")
                        repo = best_candidate.get("name", "")
                        full_repo = f"{ns}/{repo}" if ns != "library" else repo

                        tags = self.get_image_tags(ns, repo)
                        tag_name = "latest"
                        if tags:
                            tag_name = tags[0].get("name", "latest")

                        result["base_image"] = full_repo
                        result["full_image"] = f"{full_repo}:{tag_name}"
                        result["reason"] += (
                            f" Docker Hub search found a more relevant image ({full_repo}); LLM score {max_score}."
                        )
                        result["confidence"] = "high"

                    # Add suggestions
                    result["suggestions"] = top_candidates
        except Exception as e:
            print(f"  ⚠️ Error during Docker Hub search: {e}")

        print(f"  ✅ Recommended image: {result.get('full_image', 'unknown')}")
        print(f"  📝 Reason: {result.get('reason', 'N/A')[:100]}...")

        return result

    def _collect_config_files(self) -> Dict[str, str]:
        """
        Collect configuration files from the repository.

        Returns:
            {
                "requirements.txt": "file contents...",
                "package.json": "file contents...",
                ...
            }
        """
        collected = {}
        max_file_size = 5000  # Read up to 5,000 chars per file
        max_files = 15  # Collect up to 15 config files

        # Prefer config files in repo root
        for filename, lang_hint in self.CONFIG_FILES.items():
            if len(collected) >= max_files:
                break

            file_path = self.repo_path / filename
            if file_path.exists() and file_path.is_file():
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(max_file_size)
                        collected[filename] = content
                        print(f"    ✓ {filename} ({len(content)} chars)")
                except Exception as e:
                    print(f"    ✗ Failed to read {filename}: {e}")

        # If too few files in root, search recursively (limited depth)
        if len(collected) < 3:
            self._collect_config_files_recursive(collected, max_files, max_file_size)

        return collected

    def _should_search_docker_hub(
        self, analysis: Dict, config_files: Dict[str, str]
    ) -> bool:
        """
        Decide whether Docker Hub search is needed.
        Only search when dependencies include heavy libraries or high-risk build signals.
        """
        heavy_deps = {
            "torch",
            "tensorflow",
            "jax",
            "pytorch",
            "opencv",
            "mmcv",
            "xgboost",
            "lightgbm",
            "catboost",
            "paddlepaddle",
            "mxnet",
            "cuda",
            "cudnn",
            "faiss",
            "triton",
            "fasttext",
        }

        deps = {d.lower() for d in analysis.get("dependencies", [])}
        frameworks = {f.lower() for f in analysis.get("frameworks", [])}
        if deps & heavy_deps or frameworks & heavy_deps:
            return True

        lang = (analysis.get("language") or "").lower()
        if lang == "python":
            blob = " ".join(config_files.values()).lower()
            if any(k in blob for k in ["cuda", "cudnn", "torch", "tensorflow", "jax"]):
                return True

        return False

    def _collect_config_files_recursive(
        self,
        collected: Dict[str, str],
        max_files: int,
        max_file_size: int,
        max_depth: int = 2,
    ):
        """
        Recursively collect config files (limited depth).
        """
        for config_name in self.CONFIG_FILES.keys():
            if len(collected) >= max_files:
                break

            # Skip if already collected from root
            if config_name in collected:
                continue

            try:
                # Search subdirectories (depth-limited)
                for file_path in self.repo_path.rglob(config_name):
                    # Check depth
                    relative = file_path.relative_to(self.repo_path)
                    if len(relative.parts) > max_depth + 1:
                        continue

                    # Skip ignored directories
                    if any(skip_dir in file_path.parts for skip_dir in self.SKIP_DIRS):
                        continue

                    # Read file
                    if file_path.is_file():
                        try:
                            with open(
                                file_path, "r", encoding="utf-8", errors="ignore"
                            ) as f:
                                content = f.read(max_file_size)
                                # Use relative path as key to avoid collisions
                                key = str(relative)
                                collected[key] = content
                                print(f"    ✓ {key} ({len(content)} chars)")

                                if len(collected) >= max_files:
                                    return
                        except Exception:
                            continue
            except Exception:
                continue

    def _collect_readme_files(self) -> str:
        """
        Collect README content.

        Returns:
            Concatenated README content (prefers repo root).
        """
        readme_contents = []
        max_total_size = 15000  # Total cap: 15,000 chars
        total_size = 0

        # Prefer README in repo root
        for pattern in self.README_PATTERNS:
            readme_path = self.repo_path / pattern
            if readme_path.exists() and readme_path.is_file():
                try:
                    with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(max_total_size)
                        readme_contents.append(f"### {pattern}\n{content}")
                        total_size += len(content)
                        print(f"    ✓ {pattern} ({len(content)} chars)")
                        break  # Only read the first README in repo root
                except Exception as e:
                    print(f"    ✗ Failed to read {pattern}: {e}")

        # If there is remaining budget, read README in subdirectories (optional)
        if total_size < max_total_size:
            remaining = max_total_size - total_size
            for pattern in ["README.md", "README.MD"]:
                if total_size >= max_total_size:
                    break

                try:
                    for readme_path in self.repo_path.rglob(pattern):
                        # Skip repo root (already handled)
                        if readme_path.parent == self.repo_path:
                            continue

                        # Skip ignored directories
                        if any(
                            skip_dir in readme_path.parts for skip_dir in self.SKIP_DIRS
                        ):
                            continue

                        # Read file
                        if readme_path.is_file():
                            try:
                                with open(
                                    readme_path, "r", encoding="utf-8", errors="ignore"
                                ) as f:
                                    content = f.read(remaining)
                                    relative = readme_path.relative_to(self.repo_path)
                                    readme_contents.append(f"### {relative}\n{content}")
                                    total_size += len(content)
                                    remaining = max_total_size - total_size

                                    if total_size >= max_total_size:
                                        break
                            except Exception:
                                continue
                except Exception:
                    continue

        return "\n\n".join(readme_contents) if readme_contents else ""

    def _load_allowed_versions(self) -> Dict:
        """
        Load allowed version constraints.

        Returns:
            Parsed contents of ALLOWED_VERSIONS.json.
        """
        try:
            with open(self.allowed_versions_path, "r", encoding="utf-8") as f:
                allowed_versions = json.load(f)
                print(
                    f"  ✓ Loaded version constraints: {list(allowed_versions.keys())}"
                )
                return allowed_versions
        except Exception as e:
            print(f"  ✗ Failed to load version constraints: {e}")
            # Return empty config so the LLM can infer freely
            return {}

    def _llm_infer_image(
        self, config_files: Dict[str, str], readme_content: str, allowed_versions: Dict
    ) -> Dict:
        """
        Use an LLM to infer the most suitable Docker image.

        Args:
            config_files: Mapping of filename -> file contents.
            readme_content: README content.
            allowed_versions: Allowed version constraints.

        Returns:
            Inference result.
        """
        response = ""
        try:
            print("  🤖 Calling LLM for image inference...")

            # Build config blob
            config_str = ""
            for filename, content in config_files.items():
                config_str += f"\n### {filename}\n```\n{content}\n```\n"

            # Build prompt
            prompt = f"""You are a Docker image selection expert. Infer the best Docker base image from the information below.

## Allowed versions (ALLOWED_VERSIONS.json)
```json
{json.dumps(allowed_versions, ensure_ascii=False, indent=2)}
```

## Repository config files
{config_str if config_str else "(No config files found)"}

## README
{readme_content if readme_content else "(No README found)"}

## Requirements
1. **Identify the primary language** (python, rust, javascript, node, java, ...)
2. **Analyze version requirements**: extract language/framework versions from configs and README
3. **Select the best image**:
   - Prefer selecting language/version/variant from ALLOWED_VERSIONS.json
   - If the project requires a specific version, pick the closest allowed version
   - If version is unclear, use default_version
   - For variant (e.g. slim, alpine), use default_variant by default
4. **Extract dependency info** (optional): key frameworks and dependencies

## Output format (raw JSON only; no Markdown)
{{
  "language": "python|rust|javascript|node|java (must be a key in ALLOWED_VERSIONS; use unknown if unsure)",
  "version": "A version from the versions list (e.g. 3.10, 1.75, 18)",
  "variant": "A variant from the variants list (e.g. slim, alpine, or empty string)",
  "base_image": "From base_image (e.g. python, rust, node)",
  "full_image": "Full image name+tag (e.g. python:3.10-slim, rust:1.75, node:18)",
  "reason": "Why this image was chosen (2-3 sentences)",
  "confidence": "high|medium|low (confidence level)",
  "frameworks": ["Detected frameworks (optional)"],
  "dependencies": ["Key dependencies (max 10)"]
}}

## Important rules
1. **Version constraint**: version must be selected from ALLOWED_VERSIONS. Do not invent versions.
2. **Variant constraint**: variant must be selected from variants (including empty string "").
3. **Image format**:
   - If variant is empty: full_image = "{{base_image}}:{{version}}" (e.g. "rust:1.75")
   - If variant is not empty: full_image = "{{base_image}}:{{version}}-{{variant}}" (e.g. "python:3.10-slim")
4. **Unknown language**: if language is not in ALLOWED_VERSIONS, set language to "unknown" and explain.
5. **Return raw JSON only**: no ```json``` wrappers, no extra text.

## Example output
{{
  "language": "python",
  "version": "3.10",
  "variant": "slim",
  "base_image": "python",
  "full_image": "python:3.10-slim",
  "reason": "The requirements.txt and pyproject.toml indicate Python 3.10+. The 3.10-slim variant keeps the image smaller while meeting dependency needs.",
  "confidence": "high",
  "frameworks": ["django", "celery"],
  "dependencies": ["django", "celery", "redis", "psycopg2-binary", "gunicorn"]
}}
"""

            # Call LLM
            raw_response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a Docker image selection expert. Return raw JSON only (no Markdown, no extra text).",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            # Strip potential Markdown fences (defensive)
            response = str(raw_response or "").strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            # Parse JSON
            result = json.loads(response)

            # Validate required fields
            required_fields = [
                "language",
                "version",
                "base_image",
                "full_image",
                "reason",
            ]
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"LLM response is missing required field: {field}")

            # Fill optional defaults
            result.setdefault("variant", "")
            result.setdefault("confidence", "medium")
            result.setdefault("frameworks", [])
            result.setdefault("dependencies", [])

            print(
                f"    ✓ LLM inference complete; confidence: {result.get('confidence', 'unknown')}"
            )
            return result

        except json.JSONDecodeError as e:
            print(f"  ✗ Failed to parse LLM JSON: {e}")
            print(f"  Raw response: {response[:200]}...")
            return self._get_fallback_result(config_files, allowed_versions)

        except Exception as e:
            print(f"  ✗ LLM inference failed: {e}")
            return self._get_fallback_result(config_files, allowed_versions)

    def _get_fallback_result(
        self, config_files: Dict[str, str], allowed_versions: Dict
    ) -> Dict:
        """
        Fallback: when LLM inference fails, use simple heuristics.

        Args:
            config_files: Config file mapping.
            allowed_versions: Allowed version constraints.

        Returns:
            Fallback inference result.
        """
        print("  🔄 Using fallback strategy...")

        # Simple language detection heuristics
        language = "python"  # default

        # Infer language from config files
        if any(
            f.endswith((".toml", ".lock")) and "Cargo" in f for f in config_files.keys()
        ):
            language = "rust"
        elif any("package.json" in f for f in config_files.keys()):
            language = "node"
        elif any(f.endswith((".gradle", ".xml")) for f in config_files.keys()):
            language = "java"
        elif any("go.mod" in f for f in config_files.keys()):
            language = "go"

        # Get defaults
        if language in allowed_versions:
            config = allowed_versions[language]
            version = config.get("default_version", "latest")
            variant = config.get("default_variant", "")
            base_image = config.get("base_image", language)

            # Build full image name
            if variant:
                full_image = f"{base_image}:{version}-{variant}"
            else:
                full_image = f"{base_image}:{version}"

            return {
                "language": language,
                "version": version,
                "variant": variant,
                "base_image": base_image,
                "full_image": full_image,
                "reason": f"LLM inference failed; using default {language} config as fallback",
                "confidence": "low",
                "frameworks": [],
                "dependencies": [],
            }
        else:
            # Completely unknown case
            return {
                "language": "unknown",
                "version": "latest",
                "variant": "",
                "base_image": "python",
                "full_image": "python:3.10-slim",
                "reason": "Could not identify project language; using python:3.10-slim as a default fallback",
                "confidence": "low",
                "frameworks": [],
                "dependencies": [],
            }

    def search_docker_hub(self, query: str, limit: int = 25) -> List[Dict]:
        """Search Docker Hub repositories."""
        try:
            params = {
                "query": query,
                "page_size": limit,
            }
            response = requests.get(self.docker_hub_search, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            raw_results = data.get("results", [])

            standardized_results = []
            for item in raw_results:
                standardized = {}

                repo_name = item.get("repo_name", "")
                if "/" in repo_name:
                    namespace, name = repo_name.split("/", 1)
                    standardized["namespace"] = namespace
                    standardized["name"] = name
                else:
                    standardized["namespace"] = "library"
                    standardized["name"] = repo_name

                standardized["repo_name"] = repo_name
                standardized["description"] = item.get("short_description") or ""
                standardized["star_count"] = item.get("star_count", 0)
                standardized["pull_count"] = item.get("pull_count", 0)
                standardized["is_official"] = item.get("is_official", False)

                standardized_results.append(standardized)

            return standardized_results
        except requests.exceptions.RequestException as e:
            print(f"  ⚠️ Docker Hub API request failed: {e}")
            return []

    def get_image_tags(
        self, namespace: str, repository: str, limit: int = 10
    ) -> List[Dict]:
        """Fetch tag list for an image."""
        try:
            url = f"{self.docker_hub_api}/repositories/{namespace}/{repository}/tags"
            params = {"page_size": limit}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            print(f"  ⚠️ Failed to fetch tags for {namespace}/{repository}: {e}")
            return []

    def llm_score_images(self, images: List[Dict], analysis: Dict) -> Dict:
        """Use an LLM to batch-score candidate image relevance."""
        if not images:
            return {}

        try:
            print("  🤖 LLM scoring candidate image relevance...")
            images_info = []
            for i, img in enumerate(images[:20], 1):
                namespace = img.get("namespace", "library")
                name = img.get("name", "")
                full_name = f"{namespace}/{name}" if namespace != "library" else name
                images_info.append(
                    {
                        "id": i,
                        "name": full_name,
                        "description": (img.get("description") or "")[:200],
                        "is_official": img.get("is_official", False),
                    }
                )

            project_info = {
                "language": analysis.get("language", "unknown"),
                "version": analysis.get("version"),
                "base_image": analysis.get("base_image"),
                "full_image": analysis.get("full_image"),
                "frameworks": analysis.get("frameworks", [])[:5],
                "dependencies": analysis.get("dependencies", [])[:10],
            }

            prompt = f"""Evaluate the relevance/fitness of the following Docker images for this project.

Project info:
{json.dumps(project_info, ensure_ascii=False, indent=2)}

Candidate images:
{json.dumps(images_info, ensure_ascii=False, indent=2)}

Return JSON:
{{
  "scores": [{{"id": 1, "score": 85, "reason": "...", "suitable": true}}],
  "recommendation": "Suggestion...",
  "best_strategy": "use_candidate" or "use_base_image"
}}

Notes: return raw JSON only (no Markdown, no extra text).
"best_strategy" indicates whether to use the best candidate image (use_candidate) or keep the original base image (use_base_image).
If you choose use_base_image, consider whether installing required components (pip/apt-get/etc.) would exceed ~5 minutes and likely time out (e.g. pip install torch often times out, while pulling a torch image does not).
"""
            # print(prompt)
            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a Docker image evaluation expert. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            response = str(response or "").strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            result = json.loads(response)
            llm_scores = {}
            max_score = 0
            for item in result.get("scores", []):
                idx = item.get("id", 0) - 1
                if 0 <= idx < len(images):
                    namespace = images[idx].get("namespace", "library")
                    name = images[idx].get("name", "")
                    key = f"{namespace}/{name}"
                    llm_scores[key] = item
                    max_score = max(max_score, item.get("score", 0))

            return {
                "scores": llm_scores,
                "best_strategy": result.get("best_strategy", "use_candidate"),
                "max_score": max_score,
                "recommendation": result.get("recommendation", ""),
            }

        except Exception as e:
            print(f"  ✗ LLM scoring failed: {e}")
            return {"scores": {}, "best_strategy": "use_candidate"}

    def _llm_generate_search_keywords(self, analysis: Dict) -> List[str]:
        """Use an LLM to generate Docker Hub search keywords."""
        try:
            print("  🤖 Generating search keywords with LLM...")
            project_info = {
                "language": analysis.get("language", "unknown"),
                "frameworks": analysis.get("frameworks", []),
                "dependencies": analysis.get("dependencies", [])[:10],
            }
            prompt = f"""Generate 4 Docker Hub search keywords (image names) based on the project info.

Project info:
{json.dumps(project_info, ensure_ascii=False, indent=2)}

Notes: keep image names as short as possible; include only the image name (one word), no author/org prefix. Avoid formats like "organization-project" or "organization/project".
Return JSON: {{"keywords": ["kw1", "kw2", "kw3", "kw4"]}}
"""
            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "Docker search expert. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            response = str(response or "").strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            return json.loads(response).get("keywords", [])
        except Exception as e:
            print(f"  ✗ LLM keyword generation failed: {e}")
            return []


def main():
    """CLI entry point (for testing)."""
    import argparse

    parser = argparse.ArgumentParser(description="Docker image inference tool")
    parser.add_argument("repo_path", help="Repository path")
    parser.add_argument("--llm", default="deepseek-chat", help="LLM model name")

    args = parser.parse_args()

    retriever = ImageRetriever(repo_path=args.repo_path, llm_name=args.llm)
    result = retriever.retrieve_image()

    print("\n" + "=" * 60)
    print("Inference result:")
    print("=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def test_docker_search(query: str):
    """Test Docker Hub search."""
    print(f"🔍 Test search: {query}")
    # Use current directory as a dummy repo path
    retriever = ImageRetriever(repo_path=".", llm_name="deepseek-chat")
    results = retriever.search_docker_hub(query, limit=5)

    print(f"📦 Found {len(results)} images:")
    for i, img in enumerate(results, 1):
        name = f"{img.get('namespace', 'library')}/{img.get('name', '')}"
        desc = img.get("description", "") or ""
        stars = img.get("star_count", 0)
        print(f"  {i}. {name:<30} (⭐ {stars})")
        if desc:
            print(f"     {desc[:60]}...")


if __name__ == "__main__":
    import sys

    if "--search" in sys.argv:
        try:
            idx = sys.argv.index("--search")
            if idx + 1 < len(sys.argv):
                query = sys.argv[idx + 1]
                test_docker_search(query)
            else:
                print("Please provide a search keyword")
        except Exception as e:
            print(f"Error: {e}")

    else:
        main()
