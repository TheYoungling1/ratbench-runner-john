#!/usr/bin/env python3
"""
search_web tool implementation.
Purpose: search the web for error information and solutions.
Input: error message or problem description.
Output: relevant solutions from Stack Overflow, GitHub, docs, etc.
"""

import argparse
import json
import sys
import re
import requests
import os
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import quote_plus
import time

# Add path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

try:
    from llm import LLMChat
except ImportError:
    # Fallback LLMChat
    class FallbackLLMChat:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, *args, **kwargs):
            return ("LLM is not available (missing dependencies)", None)

    LLMChat = FallbackLLMChat


class WebSearcher:
    """Web searcher."""

    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.headers = {"User-Agent": self.user_agent}
        self.timeout = 10

    def extract_error_keywords(self, error_message: str) -> List[str]:
        """Extract keywords from an error message."""
        keywords = []

        # 1. Error types
        error_types = re.findall(
            r"([\w]+Error|[\w]+Exception|[\w]+Warning)", error_message
        )
        keywords.extend(error_types)

        # 2. Module names
        module_patterns = [
            r'ModuleNotFoundError:\s*No module named [\'"]([^\'"]+)[\'"]',
            r'ImportError:\s*.*?[\'"]([^\'"]+)[\'"]',
            r"from\s+([\w\.]+)\s+import",
            r"import\s+([\w\.]+)",
        ]
        for pattern in module_patterns:
            matches = re.findall(pattern, error_message)
            keywords.extend(matches)

        # 3. File path hints
        file_matches = re.findall(r'File\s+"([^"]+)"', error_message)
        for file_path in file_matches:
            # Extract file name (no path)
            filename = file_path.split("/")[-1].replace(".py", "")
            if filename and filename not in ["<stdin>", "<string>"]:
                keywords.append(filename)

        # 4. Version numbers
        version_matches = re.findall(r"\d+\.\d+\.\d+", error_message)
        keywords.extend(version_matches)

        # 5. Specific error verbs
        error_desc_patterns = [
            r"cannot\s+(\w+)",
            r"unable\s+to\s+(\w+)",
            r"failed\s+to\s+(\w+)",
            r"no\s+such\s+(\w+)",
        ]
        for pattern in error_desc_patterns:
            matches = re.findall(pattern, error_message.lower())
            keywords.extend(matches)

        # Deduplicate
        unique_keywords = []
        seen = set()
        for kw in keywords:
            kw_clean = kw.strip().lower()
            if kw_clean and len(kw_clean) > 2 and kw_clean not in seen:
                seen.add(kw_clean)
                unique_keywords.append(kw)

        return unique_keywords[:10]  # Up to 10 keywords

    def get_stackoverflow_answers(self, question_id: int) -> List[Dict]:
        """Fetch answers for a Stack Overflow question."""
        answers = []
        try:
            api_url = (
                f"https://api.stackexchange.com/2.3/questions/{question_id}/answers"
            )
            params = {
                "order": "desc",
                "sort": "votes",
                "site": "stackoverflow",
                "filter": "withbody",
                "pagesize": 3,  # Top 3 answers
            }

            response = requests.get(api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            for item in data.get("items", []):
                # Clean HTML, keep code blocks
                body = item.get("body", "")
                # Extract code blocks
                code_blocks = re.findall(r"<code>(.*?)</code>", body, re.DOTALL)
                # Strip other HTML
                body_clean = re.sub(r"<[^>]+>", "\n", body)

                answers.append(
                    {
                        "score": item.get("score", 0),
                        "is_accepted": item.get("is_accepted", False),
                        "body": body_clean[:1000],
                        "code_blocks": [code[:500] for code in code_blocks[:3]],
                    }
                )

            time.sleep(0.1)

        except Exception as e:
            print(f"⚠️ Failed to fetch answers: {e}")

        return answers

    def search_stackoverflow(self, query: str, limit: int = 5) -> List[Dict]:
        """Search Stack Overflow."""
        print("🔍 Searching Stack Overflow...")

        results = []
        try:
            # Stack Exchange API
            api_url = "https://api.stackexchange.com/2.3/search/advanced"
            params = {
                "q": query,
                "order": "desc",
                "sort": "relevance",
                "site": "stackoverflow",
                "pagesize": limit,
                "filter": "withbody",
            }

            response = requests.get(api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            for item in data.get("items", []):
                # Strip HTML tags
                body = re.sub(r"<[^>]+>", "", item.get("body", ""))
                body_preview = body[:300].replace("\n", " ")

                question_id = item.get("question_id")

                result = {
                    "source": "Stack Overflow",
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "score": item.get("score", 0),
                    "is_answered": item.get("is_answered", False),
                    "answer_count": item.get("answer_count", 0),
                    "view_count": item.get("view_count", 0),
                    "body_preview": body_preview,
                    "tags": item.get("tags", []),
                    "question_id": question_id,
                }

                # If answered, fetch answers
                if item.get("is_answered") and question_id:
                    answers = self.get_stackoverflow_answers(question_id)
                    if answers:
                        result["answers"] = answers
                        # Accepted answer
                        accepted = [a for a in answers if a.get("is_accepted")]
                        if accepted:
                            result["accepted_answer"] = accepted[0]
                        else:
                            # Otherwise, use top-scored
                            result["top_answer"] = answers[0]

                results.append(result)

            # API rate limiting
            time.sleep(0.1)

        except requests.exceptions.RequestException as e:
            print(f"⚠️ Stack Overflow search failed: {e}")
        except Exception as e:
            print(f"⚠️ Failed to parse Stack Overflow results: {e}")

        return results

    def get_github_issue_comments(
        self, comments_url: str, limit: int = 3
    ) -> List[Dict]:
        """Fetch comments for a GitHub issue."""
        comments = []
        try:
            response = requests.get(
                comments_url, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()

            for item in data[:limit]:  # Only a few comments
                body = item.get("body", "")
                # Extract code blocks
                code_blocks = re.findall(r"```[\w]*\n(.*?)\n```", body, re.DOTALL)

                comments.append(
                    {
                        "author": item.get("user", {}).get("login", "unknown"),
                        "created_at": item.get("created_at", ""),
                        "body": body[:800],
                        "code_blocks": [code[:500] for code in code_blocks[:2]],
                    }
                )

            time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ Failed to fetch comments: {e}")

        return comments

    def search_github_issues(self, query: str, limit: int = 5) -> List[Dict]:
        """Search GitHub issues."""
        print("🔍 Searching GitHub Issues...")

        results = []
        try:
            # GitHub Search API
            api_url = "https://api.github.com/search/issues"
            params = {
                "q": query,
                "sort": "relevance",
                "order": "desc",
                "per_page": limit,
            }

            response = requests.get(
                api_url, params=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()

            for item in data.get("items", []):
                body = item.get("body") or ""
                body_preview = body[:300].replace("\n", " ")
                comments_url = item.get("comments_url", "")

                result = {
                    "source": "GitHub",
                    "title": item.get("title", ""),
                    "link": item.get("html_url", ""),
                    "state": item.get("state", "unknown"),
                    "comments_count": item.get("comments", 0),
                    "body_preview": body_preview,
                    "labels": [label["name"] for label in item.get("labels", [])],
                }

                # If closed and has comments, fetch comments (may contain a fix)
                if (
                    item.get("state") == "closed"
                    and item.get("comments", 0) > 0
                    and comments_url
                ):
                    comments = self.get_github_issue_comments(comments_url, limit=3)
                    if comments:
                        result["comments"] = comments
                        # The last comment often includes the solution
                        result["solution_comment"] = comments[-1] if comments else None

                results.append(result)

            # API rate limiting
            time.sleep(1)

        except requests.exceptions.RequestException as e:
            print(f"⚠️ GitHub search failed: {e}")
        except Exception as e:
            print(f"⚠️ Failed to parse GitHub results: {e}")

        return results

    def search_python_docs(self, query: str) -> List[Dict]:
        """Search Python documentation."""
        print("🔍 Searching Python docs...")

        results = []
        try:
            # Use DuckDuckGo to search Python docs
            search_query = f"site:docs.python.org {query}"
            results = self._generic_web_search(search_query, limit=3)

            for result in results:
                result["source"] = "Python Docs"

        except Exception as e:
            print(f"⚠️ Python doc search failed: {e}")

        return results

    def _generic_web_search(self, query: str, limit: int = 5) -> List[Dict]:
        """Generic web search (DuckDuckGo HTML endpoint)."""
        results = []

        try:
            # DuckDuckGo HTML search (no API key)
            # Note: simplified implementation; production should use an official search API
            search_url = f"https://html.duckduckgo.com/html/"
            params = {"q": query}

            response = requests.post(
                search_url, data=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()

            # Naive HTML parsing (a real implementation should use BeautifulSoup)
            link_pattern = (
                r'<a[^>]+href="([^"]+)"[^>]*class="result__a"[^>]*>([^<]+)</a>'
            )
            matches = re.findall(link_pattern, response.text)

            for i, (link, title) in enumerate(matches[:limit]):
                # Strip DuckDuckGo redirect
                if link.startswith("//duckduckgo.com/l/?"):
                    # Extract actual URL
                    actual_url_match = re.search(r"uddg=([^&]+)", link)
                    if actual_url_match:
                        from urllib.parse import unquote

                        link = unquote(actual_url_match.group(1))

                results.append(
                    {
                        "source": "Web Search",
                        "title": title.strip(),
                        "link": link,
                        "snippet": "",
                    }
                )

        except Exception as e:
            print(f"⚠️ Web search failed: {e}")

        return results

    def search_package_docs(self, package_name: str) -> List[Dict]:
        """Search package documentation."""
        print(f"🔍 Searching docs for package: {package_name}...")

        results = []

        # PyPI link
        pypi_url = f"https://pypi.org/project/{package_name}/"
        results.append(
            {
                "source": "PyPI",
                "title": f"{package_name} on PyPI",
                "link": pypi_url,
                "description": "Official package page on PyPI",
            }
        )

        # ReadTheDocs
        rtd_url = f"https://{package_name}.readthedocs.io/"
        results.append(
            {
                "source": "ReadTheDocs",
                "title": f"{package_name} documentation",
                "link": rtd_url,
                "description": "Documentation on ReadTheDocs (if available)",
            }
        )

        # GitHub
        github_url = f"https://github.com/search?q={package_name}&type=repositories"
        results.append(
            {
                "source": "GitHub",
                "title": f"Search {package_name} on GitHub",
                "link": github_url,
                "description": "Find the source repository",
            }
        )

        return results

    def score_result(self, result: Dict, keywords: List[str]) -> float:
        """Score a search result."""
        score = 0.0

        title = result.get("title", "").lower()
        body = result.get("body_preview", "").lower()
        combined = f"{title} {body}"

        # 1. Keyword match
        for keyword in keywords:
            if keyword.lower() in combined:
                score += 10

        # 2. Stack Overflow scoring
        if result.get("source") == "Stack Overflow":
            if result.get("is_answered"):
                score += 20
            score += min(result.get("score", 0), 30)
            score += min(result.get("answer_count", 0) * 5, 20)

        # 3. GitHub scoring
        if result.get("source") == "GitHub":
            if result.get("state") == "closed":
                score += 15  # Closed issues may be resolved
            score += min(result.get("comments", 0) * 2, 20)

        return score


def search_web_main(
    error_message: str,
    search_stackoverflow: bool = True,
    search_github: bool = True,
    search_docs: bool = True,
    max_results: int = 10,
):
    """
    Main entry point: search the web for solutions.

    Args:
        error_message: Error message or problem description.
        search_stackoverflow: Whether to search Stack Overflow.
        search_github: Whether to search GitHub issues.
        search_docs: Whether to search docs.
        max_results: Max results per source.

    Returns:
        0: success
        1: failure
    """
    print("=" * 70)
    print("🌐 Search Web - Find solutions")
    print("=" * 70)
    print()

    searcher = WebSearcher()

    # 1. Extract keywords
    print(f"📝 Error: {error_message[:100]}...")
    print()
    keywords = searcher.extract_error_keywords(error_message)
    print(f"📌 Keywords: {', '.join(keywords[:10])}")
    print()

    # Build search query
    search_query = " ".join(keywords[:5])  # Use first 5 keywords
    if not search_query:
        search_query = error_message[:100]  # Fall back to raw error prefix

    all_results = []

    # 2. Search Stack Overflow
    if search_stackoverflow:
        try:
            so_results = searcher.search_stackoverflow(search_query, limit=max_results)
            all_results.extend(so_results)
            print(f"✅ Stack Overflow: {len(so_results)} results")
        except Exception as e:
            print(f"❌ Stack Overflow search failed: {e}")

    # 3. Search GitHub issues
    if search_github:
        try:
            gh_results = searcher.search_github_issues(search_query, limit=max_results)
            all_results.extend(gh_results)
            print(f"✅ GitHub: {len(gh_results)} results")
        except Exception as e:
            print(f"❌ GitHub search failed: {e}")

    # 4. Search docs
    if search_docs:
        try:
            # If the error suggests a specific package, search its docs
            for keyword in keywords[:3]:
                if (
                    keyword
                    and not keyword.endswith("Error")
                    and not keyword.endswith("Exception")
                ):
                    doc_results = searcher.search_package_docs(keyword)
                    all_results.extend(doc_results)
                    break
        except Exception as e:
            print(f"❌ Doc search failed: {e}")

    print()

    # 5. Score and sort results
    for result in all_results:
        result["relevance_score"] = searcher.score_result(result, keywords)

    all_results.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # 6. Print results
    print("=" * 70)
    print(f"📊 Results (total {len(all_results)})")
    print("=" * 70)
    print()

    if not all_results:
        print("❌ No relevant results found")
        return 1

    for i, result in enumerate(all_results[:15], 1):  # Show top 15
        source_emoji = {
            "Stack Overflow": "📚",
            "GitHub": "🐙",
            "Python Docs": "🐍",
            "PyPI": "📦",
            "ReadTheDocs": "📖",
            "Web Search": "🌐",
        }.get(result.get("source", ""), "🔍")

        print(
            f"{source_emoji} {i}. [{result.get('source', 'Unknown')}] {result.get('title', 'No Title')}"
        )
        print(f"   Link: {result.get('link', 'N/A')}")
        print(f"   Relevance: {result.get('relevance_score', 0):.1f}")

        # Extra info per source
        if result.get("source") == "Stack Overflow":
            answered = "✅ answered" if result.get("is_answered") else "❌ unanswered"
            print(
                f"   Status: {answered} | Score: {result.get('score', 0)} | Answers: {result.get('answer_count', 0)}"
            )
            if result.get("tags"):
                print(f"   Tags: {', '.join(result['tags'][:5])}")

            # Accepted or top answer
            if result.get("accepted_answer"):
                answer = result["accepted_answer"]
                print(f"   ✅ Accepted answer (score: {answer.get('score', 0)}):")
                print(f"      {answer.get('body', '')[:400]}...")
                if answer.get("code_blocks"):
                    print("      💻 Code:")
                    for code in answer["code_blocks"][:1]:  # Only the first code block
                        print(f"         {code[:200]}...")
            elif result.get("top_answer"):
                answer = result["top_answer"]
                print(f"   ⭐ Top answer (score: {answer.get('score', 0)}):")
                print(f"      {answer.get('body', '')[:400]}...")
                if answer.get("code_blocks"):
                    print("      💻 Code:")
                    for code in answer["code_blocks"][:1]:
                        print(f"         {code[:200]}...")

        elif result.get("source") == "GitHub":
            state = "✅ closed" if result.get("state") == "closed" else "🔴 open"
            print(f"   State: {state} | Comments: {result.get('comments_count', 0)}")
            if result.get("labels"):
                print(f"   Labels: {', '.join(result['labels'][:5])}")

            # Solution comment
            if result.get("solution_comment"):
                comment = result["solution_comment"]
                print(f"   💡 Possible fix (by {comment.get('author', 'unknown')}):")
                print(f"      {comment.get('body', '')[:400]}...")
                if comment.get("code_blocks"):
                    print("      💻 Code:")
                    for code in comment["code_blocks"][:1]:
                        print(f"         {code[:200]}...")

        # Body preview (when no answers)
        elif result.get("body_preview"):
            preview = result["body_preview"][:200]
            if len(result.get("body_preview", "")) > 200:
                preview += "..."
            print(f"   Summary: {preview}")

        print()

    print("=" * 70)
    print("💡 Tips:")
    print("-" * 70)
    print("   1. Prioritize high-relevance answered Stack Overflow posts")
    print("   2. Check closed GitHub issues for resolutions")
    print("   3. Read official docs for correct usage")
    print("=" * 70)

    # 8. LLM summary (if we have good solutions)
    top_results_with_solutions = [
        r
        for r in all_results[:5]
        if r.get("accepted_answer") or r.get("top_answer") or r.get("solution_comment")
    ]

    if top_results_with_solutions:
        print("\n" + "=" * 70)
        print("🤖 AI summary")
        print("=" * 70)
        print()

        try:
            # Build LLM input
            solutions_text = []
            for idx, r in enumerate(top_results_with_solutions[:3], 1):  # Up to 3
                solutions_text.append(f"Source {idx}: {r.get('title', 'Unknown')}")

                if r.get("accepted_answer"):
                    ans = r["accepted_answer"]
                    solutions_text.append(f"Answer (score {ans.get('score', 0)}):")
                    solutions_text.append(ans.get("body", "")[:600])
                    if ans.get("code_blocks"):
                        solutions_text.append("Code:")
                        solutions_text.append(ans["code_blocks"][0][:400])

                elif r.get("top_answer"):
                    ans = r["top_answer"]
                    solutions_text.append(f"Answer (score {ans.get('score', 0)}):")
                    solutions_text.append(ans.get("body", "")[:600])
                    if ans.get("code_blocks"):
                        solutions_text.append("Code:")
                        solutions_text.append(ans["code_blocks"][0][:400])

                elif r.get("solution_comment"):
                    comm = r["solution_comment"]
                    solutions_text.append(
                        f"Solution comment (by {comm.get('author', 'unknown')}):"
                    )
                    solutions_text.append(comm.get("body", "")[:600])
                    if comm.get("code_blocks"):
                        solutions_text.append("Code:")
                        solutions_text.append(comm["code_blocks"][0][:400])

                solutions_text.append("")

            combined_solutions = "\n".join(solutions_text)

            # Call LLM to summarize
            try:
                llm = LLMChat("glm-4-flash")  # Preferred if available
            except:
                llm = LLMChat("deepseek-chat")  # Fallback

            prompt = f"""Analyze the following solutions for the error "{error_message[:100]}" and extract the most effective steps.

Collected solutions:
{combined_solutions}

Provide:
1. Root cause (1-2 sentences)
2. Recommended steps (numbered, executable)
3. Key commands/code snippets (if any)

Keep the output concise and actionable."""

            response, _ = llm.chat(
                [
                    {
                        "role": "system",
                        "content": "You are a technical problem-solving assistant. Be concise and actionable.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            if response and str(response).strip():
                print("🎯 AI summary:")
                print(response)
                print()
            else:
                print("⚠️  AI summary unavailable")
                print()

        except Exception as e:
            # Best-effort only
            print(f"⚠️  AI summary unavailable ({type(e).__name__})")
            print()

    print("=" * 70)

    # 7. Save results
    result_file = Path("/tmp/search_web_result.json")
    try:
        save_data = {
            "error_message": error_message,
            "keywords": keywords,
            "search_query": search_query,
            "results_count": len(all_results),
            "results": all_results[:20],
        }
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Results saved to: {result_file}")
    except Exception as e:
        print(f"⚠️ Failed to save results: {e}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Search web for error solutions from Stack Overflow, GitHub, and documentation"
    )
    parser.add_argument(
        "-e",
        "--error",
        type=str,
        required=True,
        help="Error message or problem description",
    )
    parser.add_argument(
        "--no-stackoverflow", action="store_true", help="Do not search Stack Overflow"
    )
    parser.add_argument("--no-github", action="store_true", help="Do not search GitHub")
    parser.add_argument(
        "--no-docs", action="store_true", help="Do not search documentation"
    )
    parser.add_argument(
        "--max", type=int, default=5, help="Maximum results per source (default: 5)"
    )

    args = parser.parse_args()

    sys.exit(
        search_web_main(
            args.error,
            not args.no_stackoverflow,
            not args.no_github,
            not args.no_docs,
            args.max,
        )
    )
