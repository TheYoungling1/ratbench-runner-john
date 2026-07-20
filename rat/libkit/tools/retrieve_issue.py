#!/usr/bin/env python3
"""
retrieve_issue tool implementation.
Purpose: retrieve relevant issues and possible solutions from an issue pool.
Input: error message encountered in a program.
Output: relevant issue content and possible solutions.
"""

import argparse
import json
import sys
import re
import os
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Optional, Tuple, Any

# Add libkit to path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

try:
    from llm import LLMChat
except ImportError:

    class FallbackLLMChat:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return ("LLM is not available", None)

    LLMChat = FallbackLLMChat


class IssueRetriever:
    """Issue retriever."""

    def __init__(self, repo_path="/repo", use_llm=False, llm_name="deepseek-chat"):
        self.repo_path = Path(repo_path)
        self.issue_dir = self.repo_path / "issue"
        self.issues = []
        self.pull_requests = []
        self.use_llm = use_llm
        self.llm_name = llm_name

        if self.use_llm:
            self.llm = LLMChat(llm_name)
            print(f"🤖 LLM mode enabled: {llm_name}")

    def load_issues(self):
        """Load issue and pull request data."""
        print(f"📂 Loading issues from {self.issue_dir}...")
        # Load issues.json
        issues_file = self.issue_dir / "issues.json"
        if issues_file.exists():
            try:
                with open(issues_file, "r", encoding="utf-8") as f:
                    self.issues = json.load(f)
                print(f"✅ Loaded {len(self.issues)} issues")
            except Exception as e:
                print(f"⚠️ Failed to load issues.json: {e}")

        # Load pull_requests.json
        pr_file = self.issue_dir / "pull_requests.json"
        if pr_file.exists():
            try:
                with open(pr_file, "r", encoding="utf-8") as f:
                    self.pull_requests = json.load(f)
                print(f"✅ Loaded {len(self.pull_requests)} pull requests")
            except Exception as e:
                print(f"⚠️ Failed to load pull_requests.json: {e}")

        if not self.issues and not self.pull_requests:
            print("❌ No issue or pull request data found")
            return False

        # Count issues with comments
        issues_with_comments = sum(
            1 for issue in self.issues if issue.get("comments", [])
        )
        if issues_with_comments > 0:
            print(f"📝 {issues_with_comments} issues include comment data")

        return True

    def extract_keywords(self, error_message):
        """Extract keywords from an error message."""
        # Common error patterns
        keywords = set()

        # 1. Error types
        error_types = re.findall(
            r"(Error|Exception|Failed|Warning|Invalid|Cannot|Unable|Missing|Not found|Denied)",
            error_message,
            re.IGNORECASE,
        )
        keywords.update([et.lower() for et in error_types])

        # 2. Module names
        module_patterns = [
            r'ModuleNotFoundError:\s*No module named [\'"]([^\'"]+)[\'"]',
            r'ImportError:\s*.*?[\'"]([^\'"]+)[\'"]',
            r"import\s+(\w+)",
            r"from\s+(\w+)",
        ]
        for pattern in module_patterns:
            matches = re.findall(pattern, error_message)
            keywords.update(matches)

        # 3. File names/paths
        file_matches = re.findall(r"[\w/]+\.py", error_message)
        keywords.update([f.split("/")[-1].replace(".py", "") for f in file_matches])

        # 4. Common tech keywords
        tech_keywords = re.findall(
            r"\b(python|pip|conda|docker|pytest|streamlit|requirements|dependency|version|install|setup|config)\b",
            error_message.lower(),
        )
        keywords.update(tech_keywords)

        # 5. Version numbers
        version_patterns = re.findall(r"\d+\.\d+\.\d+", error_message)
        if version_patterns:
            keywords.add("version")

        # Drop too-short keywords
        keywords = {kw for kw in keywords if len(kw) >= 3}

        return list(keywords)

    def score_issue(self, issue, keywords, error_message):
        """Score an issue by relevance to the error."""
        score = 0
        title = (issue.get("title") or "").lower()
        body = (issue.get("body") or "").lower()
        combined = f"{title} {body}"

        # 1. Title match (higher weight)
        for keyword in keywords:
            if keyword.lower() in title:
                score += 10

        # 2. Body match
        for keyword in keywords:
            count = combined.count(keyword.lower())
            score += count * 3

        # 3. Full error snippet match
        error_snippets = [
            s.strip() for s in error_message.split("\n") if len(s.strip()) > 10
        ][:3]

        for snippet in error_snippets:
            if snippet.lower() in combined:
                score += 20

        # 4. Exact error type match
        error_type_pattern = r"([\w]+Error|[\w]+Exception)"
        error_types_in_msg = set(re.findall(error_type_pattern, error_message))
        error_types_in_issue = set(re.findall(error_type_pattern, combined))

        common_errors = error_types_in_msg & error_types_in_issue
        score += len(common_errors) * 15

        # 5. Prefer closed issues (may be resolved)
        if issue.get("state") == "closed":
            score += 5

        # 6. Issues with comments may have more info
        comments_count = issue.get("comments_count", 0)
        if comments_count > 0:
            score += min(comments_count, 10)  # Add up to 10

        return score

    def _llm_enhance_query(self, error_message: str) -> Optional[Dict[str, Any]]:
        """Use an LLM to understand the error and generate an enhanced search query."""
        if not self.use_llm:
            return None

        try:
            print("🤖 LLM analyzing error message...")

            prompt = f"""Analyze the following error message and extract key signals for searching relevant GitHub Issues.

Error message:
```
{error_message}
```

Return JSON only (no Markdown fences):
{{
  "error_type": "Error type (e.g. ModuleNotFoundError, ImportError, RuntimeError)",
  "error_category": "Category (dependency/configuration/compatibility/runtime/etc.)",
  "key_components": ["Key components (package/module/file names, etc.)"],
  "version_related": true/false,
  "search_keywords": ["Most relevant keywords (3-5)"],
  "search_query": "A GitHub Issues search query",
  "summary": "One-sentence summary"
}}

Notes:
1. Return raw JSON only
2. Keep search_keywords specific (avoid generic words)
3. Focus on root cause rather than symptoms
"""

            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are an error diagnosis expert. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            # Strip potential Markdown fences
            response = str(response or "").strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            result = json.loads(response)
            print(f"  Error type: {result.get('error_type', 'Unknown')}")
            print(f"  Search query: {result.get('search_query', '')}")

            return result

        except Exception as e:
            print(f"⚠️ LLM query enhancement failed: {e}")
            return None

    def _llm_score_issues(
        self,
        issues: List[Dict],
        error_message: str,
        llm_analysis: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Use an LLM to batch-score issue relevance."""
        if not self.use_llm or not issues:
            return {}

        try:
            print(f"🤖 LLM evaluating {len(issues)} candidate issues...")

            # Build candidate issue list (length-limited)
            issues_info = []
            for i, issue in enumerate(issues[:20], 1):  # Up to 20
                title = issue.get("title", "")[:100]
                body = (issue.get("body") or "")[:300]
                comments = issue.get("comments", [])[:3]  # Up to 3 comments

                comments_preview = []
                for comment in comments:
                    comment_body = (comment.get("body") or "")[:200]
                    comments_preview.append(comment_body)

                issues_info.append(
                    {
                        "id": i,
                        "issue_id": issue.get("id"),
                        "title": title,
                        "body": body,
                        "state": issue.get("state"),
                        "comments_preview": comments_preview,
                    }
                )

            # Build error summary
            error_summary = {
                "message": error_message[:500],
                "analysis": llm_analysis or {},
            }

            prompt = f"""Evaluate the relevance and usefulness of the following GitHub issues for the given error.

Error summary:
{json.dumps(error_summary, ensure_ascii=False, indent=2)}

Candidate issues:
{json.dumps(issues_info, ensure_ascii=False, indent=2)}

Score each issue (0-100) for relevance. Consider:
1. **Relevance** (most important): does the issue match the current error?
2. **Usefulness**: does it contain a fix/workaround or valuable discussion?
3. **Accuracy**: do error type/components/versions match?
4. **State**: closed issues may include a resolution

Scoring:
- 90-100: highly relevant; likely resolves the issue
- 70-89: relevant; worth reading
- 50-69: partially relevant
- 30-49: low relevance
- 0-29: irrelevant

Return JSON only (no Markdown fences):
{{
  "scores": [
    {{"id": 1, "score": 85, "reason": "Matches; has solution", "useful": true}},
    {{"id": 2, "score": 45, "reason": "Partially related", "useful": false}}
  ],
  "best_candidates": [1, 3, 5],
  "overall_assessment": "Overall assessment and suggestion"
}}

Notes:
1. Return raw JSON only
2. Keep reason within ~20 words
3. useful indicates whether it's worth reading
"""

            response, _ = self.llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a GitHub Issue analysis expert. Return JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            # Strip potential Markdown fences
            response = str(response or "").strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            result = json.loads(response)

            # Convert to mapping by issue id
            llm_scores = {}
            for item in result.get("scores", []):
                idx = item.get("id", 0) - 1
                if 0 <= idx < len(issues):
                    issue_id = issues[idx].get("id")
                    llm_scores[issue_id] = {
                        "score": item.get("score", 0),
                        "reason": item.get("reason", ""),
                        "useful": item.get("useful", False),
                    }

            print("  LLM evaluation complete")
            return {
                "scores": llm_scores,
                "best_candidates": result.get("best_candidates", []),
                "assessment": result.get("overall_assessment", ""),
            }

        except Exception as e:
            print(f"⚠️ LLM scoring failed: {e}")
            return {"scores": {}, "best_candidates": [], "assessment": ""}

    def search_issues(self, error_message, max_results=10):
        """Search for related issues (optionally LLM-enhanced)."""
        print(f"\n🔍 Searching error: {error_message[:100]}...")
        print()

        # 1. LLM-enhanced query (optional)
        llm_analysis = None
        if self.use_llm:
            llm_analysis = self._llm_enhance_query(error_message)

        # 2. Extract keywords
        keywords = self.extract_keywords(error_message)

        # Merge keywords from LLM analysis
        if llm_analysis and llm_analysis.get("search_keywords"):
            llm_keywords = llm_analysis["search_keywords"]
            keywords = list(set(keywords + llm_keywords))

        print(f"📌 Extracted keywords: {', '.join(keywords[:10])}")
        print()

        if not keywords:
            print("⚠️ No valid keywords extracted; falling back to raw error text")
            keywords = error_message.split()[:10]

        # 3. Merge issues and pull requests
        all_items = []
        for issue in self.issues:
            issue["source"] = "issue"
            all_items.append(issue)
        for pr in self.pull_requests:
            pr["source"] = "pull_request"
            all_items.append(pr)

        # 4. Score each item (rule-based)
        scored_items = []
        for item in all_items:
            score = self.score_issue(item, keywords, error_message)
            if score > 0:
                item["relevance_score"] = score
                item["rule_score"] = score  # Store rule score
                scored_items.append(item)

        # 5. LLM re-scoring (optional)
        if self.use_llm and scored_items:
            # Run LLM evaluation on high-score candidates
            top_candidates = sorted(
                scored_items, key=lambda x: x["relevance_score"], reverse=True
            )[:30]

            llm_result = self._llm_score_issues(
                top_candidates, error_message, llm_analysis
            )
            llm_scores_dict = llm_result.get("scores", {})

            # Combine rule score and LLM score
            for item in scored_items:
                issue_id = item.get("id")
                rule_score = item.get("rule_score", 0)

                if issue_id in llm_scores_dict:
                    score_info = llm_scores_dict[issue_id]
                    llm_score = score_info.get("score", 0)

                    # Combined score: rules 40% + LLM 60%
                    combined_score = rule_score * 0.4 + llm_score * 0.6
                    item["relevance_score"] = combined_score
                    item["llm_score"] = llm_score
                    item["llm_reason"] = score_info.get("reason", "")
                    item["llm_useful"] = score_info.get("useful", False)

            # Filter out results marked as not useful by LLM
            scored_items = [
                item for item in scored_items if item.get("llm_useful", True)
            ]

        # 6. Sort by score
        scored_items.sort(key=lambda x: x["relevance_score"], reverse=True)

        # 7. Return top N
        results = scored_items[:max_results]

        return results, keywords

    def format_results(self, results, keywords):
        """Format search results."""
        if not results:
            return "❌ No related issues or pull requests found"

        output = []
        output.append("=" * 70)
        output.append(f"📋 Found {len(results)} related Issue/PR items")
        output.append("=" * 70)
        output.append("")

        for i, item in enumerate(results, 1):
            source_emoji = "🐛" if item["source"] == "issue" else "🔀"
            state_emoji = "✅" if item.get("state") == "closed" else "🔴"

            output.append(
                f"{source_emoji} {i}. [{item['source'].upper()}] {item.get('title', 'No Title')}"
            )
            output.append(
                f"   State: {state_emoji} {item.get('state', 'unknown').upper()}"
            )

            # Relevance score (include LLM details if present)
            if "llm_score" in item:
                output.append(
                    f"   Relevance: {item['relevance_score']:.1f} (rules:{item.get('rule_score', 0):.0f} + LLM:{item['llm_score']:.0f})"
                )
                if item.get("llm_reason"):
                    output.append(f"   LLM: {item['llm_reason']}")
            else:
                output.append(f"   Relevance: {item['relevance_score']:.1f}")

            output.append(f"   Comments: {item.get('comments_count', 0)}")
            output.append(f"   Link: {item.get('link', 'N/A')}")

            # Body preview
            body = item.get("body")
            if body:
                # Clean body
                body_clean = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
                body_clean = body_clean.strip()

                if body_clean:
                    # Only show first 200 chars
                    body_preview = body_clean[:200].replace("\n", " ")
                    if len(body_clean) > 200:
                        body_preview += "..."
                    output.append(f"   Summary: {body_preview}")

            # Key comments (if any)
            comments = item.get("comments", [])
            if comments:
                output.append("   💬 Key comments:")
                for j, comment in enumerate(comments[:2], 1):  # Up to 2
                    comment_body = (comment.get("body") or "")[:150]
                    comment_author = comment.get("user", {}).get("login", "unknown")
                    comment_clean = re.sub(
                        r"<!--.*?-->", "", comment_body, flags=re.DOTALL
                    ).strip()
                    if comment_clean:
                        preview = comment_clean.replace("\n", " ")
                        output.append(f"      {j}. @{comment_author}: {preview}...")

            output.append("")

        output.append("=" * 70)
        output.append("💡 Suggestions:")
        output.append("-" * 70)

        # Suggestions based on results
        closed_issues = [r for r in results if r.get("state") == "closed"]
        if closed_issues:
            output.append(
                f"   • Found {len(closed_issues)} closed related items; likely has solutions"
            )
            output.append(f"   • Check first: {closed_issues[0].get('link', 'N/A')}")

        open_issues = [r for r in results if r.get("state") == "open"]
        if open_issues:
            output.append(
                f"   • Found {len(open_issues)} open related items; could be a known issue"
            )

        high_score_items = [r for r in results if r["relevance_score"] >= 30]
        if high_score_items:
            output.append(
                f"   • {len(high_score_items)} high-relevance results (>= 30); prioritize these"
            )

        output.append("   • Open the link to read the full discussion")
        output.append(
            "   • Look for fixes, code changes, and config changes mentioned in the thread"
        )

        output.append("=" * 70)

        return "\n".join(output)

    def generate_solution_hints(self, results, error_message):
        """Generate solution hints from issue content."""
        hints = []

        # Common solution patterns
        solution_patterns = {
            "install": r"(?:pip|conda)\s+install\s+[\w\-\.]+",
            "upgrade": r"(?:pip|conda)\s+install\s+--upgrade\s+[\w\-\.]+",
            "config": r"(?:config|configuration|setting)",
            "version": r"version\s+[\d\.]+",
            "file_change": r"(?:modify|change|update|edit)\s+[\w/]+\.py",
        }

        for result in results[:5]:  # Only analyze top 5
            body = result.get("body", "")
            if not body:
                continue

            for pattern_name, pattern in solution_patterns.items():
                matches = re.findall(pattern, body, re.IGNORECASE)
                if matches:
                    hints.append(
                        {
                            "type": pattern_name,
                            "content": matches[0],
                            "source": result.get("link", "N/A"),
                        }
                    )

        return hints


def retrieve_issue_main(
    error_message,
    repo_path="/repo",
    max_results=10,
    use_llm=False,
    llm_name="deepseek-chat",
):
    """
    Main entry point: retrieve relevant issues and solutions.

    Args:
        error_message: Error message.
        repo_path: Repository path.
        max_results: Max number of results.
        use_llm: Whether to use LLM enhancement.
        llm_name: LLM model name.

    Returns:
        0: success
        1: failure
    """
    print("=" * 70)
    print("🔍 Retrieve Issue - Retrieve related issues and solutions")
    if use_llm:
        print("🤖 LLM enhanced mode")
    print("=" * 70)
    print()

    retriever = IssueRetriever(repo_path, use_llm=use_llm, llm_name=llm_name)

    # 1. Load issues
    if not retriever.load_issues():
        print("❌ Failed to load issue data")
        return 1

    # 2. Search
    results, keywords = retriever.search_issues(error_message, max_results)

    # 3. Format and print
    output = retriever.format_results(results, keywords)
    print()
    print(output)

    # 4. Generate solution hints
    if results:
        hints = retriever.generate_solution_hints(results, error_message)
        if hints:
            print()
            print("💡 Possible solutions extracted from issues:")
            print("-" * 70)
            for i, hint in enumerate(hints[:5], 1):
                print(f"   {i}. [{hint['type']}] {hint['content']}")
                print(f"      Source: {hint['source']}")
            print()

    # 5. Save results to file
    result_file = Path("/tmp/retrieve_issue_result.json")
    try:
        save_data = {
            "error_message": error_message,
            "keywords": keywords,
            "results_count": len(results),
            "results": results[:max_results],
        }
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        print(f"💾 Results saved to: {result_file}")
    except Exception as e:
        print(f"⚠️ Failed to save results: {e}")

    print()
    if results:
        print(f"✅ Done; found {len(results)} related result(s)")
        return 0
    else:
        print("⚠️ No related issues found")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Retrieve relevant issues from issue pool based on error message",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic mode (fast, rule-based)
  python retrieve_issue.py -e "ModuleNotFoundError: No module named 'torch'" --repo /path/to/repo

  # LLM mode (semantic, deeper understanding)
  python retrieve_issue.py -e "CUDA out of memory" --repo /path/to/repo --llm

  # Specify LLM model
  python retrieve_issue.py -e "ImportError" --repo /path/to/repo --llm --llm-name gpt-4

LLM mode benefits:
  - Better semantic understanding of the error
  - Smarter relevance/usefulness scoring
  - Extracts potential solutions from key comments
  - More precise search results
        """,
    )
    parser.add_argument(
        "-e",
        "--error",
        type=str,
        required=True,
        help="Error message or problem description",
    )
    parser.add_argument(
        "--repo", type=str, default="/repo", help="Repository path (default: /repo)"
    )
    parser.add_argument(
        "--max", type=int, default=10, help="Maximum number of results (default: 10)"
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use LLM for intelligent issue search and analysis",
    )
    parser.add_argument(
        "--llm-name",
        type=str,
        default="deepseek-chat",
        help="LLM model name (default: deepseek-chat)",
    )

    args = parser.parse_args()

    sys.exit(
        retrieve_issue_main(args.error, args.repo, args.max, args.llm, args.llm_name)
    )
