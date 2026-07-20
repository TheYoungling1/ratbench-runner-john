#!/usr/bin/env python3
"""
Memory mechanism implementation for environment setup.

Provides short-term, long-term, and working-memory management.
Supports configuration history, error-solution retrieval, and reuse of similar project experience.
"""

import json
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
from collections import defaultdict
import hashlib


class EnvConfigMemory:
    """Environment configuration memory system."""

    def __init__(self, memory_dir: str = ".memory", max_short_term: int = 20):
        """
        Initialize the memory system.

        Args:
            memory_dir: Memory storage directory.
            max_short_term: Max number of short-term entries.
        """
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # Short-term memory: steps in the current session
        self.short_term_memory: List[Dict] = []
        self.max_short_term = max_short_term

        # Working memory: current context
        self.working_memory: Dict[str, Any] = {
            "current_project": None,
            "current_language": None,
            "current_dependencies": [],
            "current_errors": [],
            "current_solutions": [],
            "config_decisions": [],
        }

        # Long-term storage paths
        self.long_term_file = self.memory_dir / "long_term_memory.json"
        self.error_solutions_file = self.memory_dir / "error_solutions.json"
        self.image_knowledge_file = self.memory_dir / "image_knowledge.json"

        # Load persisted memory
        self.long_term_memory = self._load_long_term_memory()
        self.error_solutions = self._load_error_solutions()
        self.image_knowledge = self._load_image_knowledge()

    def _load_long_term_memory(self) -> List[Dict]:
        """Load long-term memory."""
        if self.long_term_file.exists():
            try:
                with open(self.long_term_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Failed to load long-term memory: {e}")
                return []
        return []

    def _load_error_solutions(self) -> Dict[str, List[Dict]]:
        """Load error solution library."""
        if self.error_solutions_file.exists():
            try:
                with open(self.error_solutions_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Failed to load error solutions: {e}")
                return defaultdict(list)
        return defaultdict(list)

    def _load_image_knowledge(self) -> Dict[str, List[Dict]]:
        """Load image knowledge base."""
        if self.image_knowledge_file.exists():
            try:
                with open(self.image_knowledge_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Failed to load image knowledge: {e}")
                return defaultdict(list)
        return defaultdict(list)

    def _save_long_term_memory(self):
        """Save long-term memory."""
        try:
            with open(self.long_term_file, "w", encoding="utf-8") as f:
                json.dump(self.long_term_memory, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Failed to save long-term memory: {e}")

    def _save_error_solutions(self):
        """Save error solutions."""
        try:
            with open(self.error_solutions_file, "w", encoding="utf-8") as f:
                json.dump(dict(self.error_solutions), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Failed to save error solutions: {e}")

    def _save_image_knowledge(self):
        """Save image knowledge base."""
        try:
            with open(self.image_knowledge_file, "w", encoding="utf-8") as f:
                json.dump(dict(self.image_knowledge), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Failed to save image knowledge: {e}")

    def add_to_short_term(self, memory_type: str, content: Dict):
        """
        Add an entry to short-term memory.

        Args:
            memory_type: Memory type (config_step, decision, error, solution).
            content: Memory content.
        """
        memory_entry = {
            "type": memory_type,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        self.short_term_memory.append(memory_entry)

        # Limit short-term size
        if len(self.short_term_memory) > self.max_short_term:
            # Keep the most recent
            self.short_term_memory = self.short_term_memory[-self.max_short_term :]

    def update_working_memory(self, key: str, value: Any):
        """
        Update working memory.

        Args:
            key: Key.
            value: Value.
        """
        if key in self.working_memory:
            self.working_memory[key] = value
        else:
            print(f"⚠️ Working memory key not found: {key}")

    def add_config_decision(self, decision: Dict):
        """
        Record a configuration decision.

        Args:
            decision: Decision dict including decision_type, content, reasoning.
        """
        decision_entry = {**decision, "timestamp": datetime.now().isoformat()}
        self.working_memory["config_decisions"].append(decision_entry)
        self.add_to_short_term("decision", decision_entry)

    def add_error(self, error_type: str, error_msg: str, context: Dict = None):
        """
        Record an encountered error.

        Args:
            error_type: Error type (dependency_conflict, build_error, runtime_error, etc.).
            error_msg: Error message.
            context: Error context.
        """
        error_entry = {
            "error_type": error_type,
            "error_msg": error_msg,
            "context": context or {},
            "timestamp": datetime.now().isoformat(),
        }
        self.working_memory["current_errors"].append(error_entry)
        self.add_to_short_term("error", error_entry)

    def add_solution(self, error_type: str, solution: str, success: bool = True):
        """
        Record a solution.

        Args:
            error_type: Associated error type.
            solution: Solution description.
            success: Whether it resolved the issue.
        """
        solution_entry = {
            "error_type": error_type,
            "solution": solution,
            "success": success,
            "timestamp": datetime.now().isoformat(),
        }
        self.working_memory["current_solutions"].append(solution_entry)
        self.add_to_short_term("solution", solution_entry)

        # If successful, persist to long-term memory
        if success:
            if error_type not in self.error_solutions:
                self.error_solutions[error_type] = []
            self.error_solutions[error_type].append(solution_entry)
            self._save_error_solutions()

    def record_image_selection(
        self,
        language: str,
        version: str,
        image: str,
        success: bool = True,
        notes: str = "",
    ):
        """
        Record an image selection outcome.

        Args:
            language: Programming language.
            version: Version.
            image: Image name.
            success: Whether it worked.
            notes: Notes.
        """
        image_entry = {
            "language": language,
            "version": version,
            "image": image,
            "success": success,
            "notes": notes,
            "timestamp": datetime.now().isoformat(),
        }

        key = f"{language}:{version}"
        if key not in self.image_knowledge:
            self.image_knowledge[key] = []
        self.image_knowledge[key].append(image_entry)
        self._save_image_knowledge()

    def retrieve_similar_configs(
        self, project_features: Dict, top_k: int = 3
    ) -> List[Dict]:
        """
        Retrieve similar configuration records.

        Args:
            project_features: Project features {language, dependencies, framework, etc.}
            top_k: Return top-k most similar.

        Returns:
            List of similar configs.
        """
        similar_configs = []

        for memory in self.long_term_memory:
            similarity = self._calculate_similarity(
                project_features, memory.get("features", {})
            )
            if similarity > 0.3:  # Similarity threshold
                similar_configs.append({"memory": memory, "similarity": similarity})

        # Sort by similarity
        similar_configs.sort(key=lambda x: x["similarity"], reverse=True)
        return similar_configs[:top_k]

    def _calculate_similarity(self, features1: Dict, features2: Dict) -> float:
        """
        Compute similarity between two project feature dicts.

        Args:
            features1: Feature dict 1.
            features2: Feature dict 2.

        Returns:
            Similarity score (0-1).
        """
        if not features1 or not features2:
            return 0.0

        score = 0.0
        weights = {
            "language": 0.4,
            "framework": 0.3,
            "dependencies": 0.2,
            "version": 0.1,
        }

        # Language match
        if features1.get("language") == features2.get("language"):
            score += weights["language"]

        # Framework match
        if features1.get("framework") == features2.get("framework"):
            score += weights["framework"]

        # Dependency overlap
        deps1 = set(features1.get("dependencies", []))
        deps2 = set(features2.get("dependencies", []))
        if deps1 and deps2:
            overlap = len(deps1 & deps2) / len(deps1 | deps2)
            score += weights["dependencies"] * overlap

        # Version match
        if features1.get("version") == features2.get("version"):
            score += weights["version"]

        return score

    def retrieve_error_solutions(
        self, error_type: str, error_msg: str = ""
    ) -> List[Dict]:
        """
        Retrieve solutions for an error.

        Args:
            error_type: Error type.
            error_msg: Error message (optional, for fuzzy matching).

        Returns:
            List of solutions.
        """
        solutions = []

        # Exact match
        if error_type in self.error_solutions:
            solutions.extend(self.error_solutions[error_type])

        # Fuzzy match
        if error_msg:
            for err_type, err_solutions in self.error_solutions.items():
                if (
                    error_msg.lower() in err_type.lower()
                    or err_type.lower() in error_msg.lower()
                ):
                    solutions.extend(err_solutions)

        # Deduplicate and sort by time
        unique_solutions = []
        seen = set()
        for sol in solutions:
            sol_hash = hashlib.md5(sol["solution"].encode()).hexdigest()
            if sol_hash not in seen:
                seen.add(sol_hash)
                unique_solutions.append(sol)

        unique_solutions.sort(key=lambda x: x["timestamp"], reverse=True)
        return unique_solutions

    def retrieve_image_recommendations(
        self, language: str, version: str = None
    ) -> List[Dict]:
        """
        Retrieve image recommendations.

        Args:
            language: Programming language.
            version: Version (optional).

        Returns:
            List of recommended images.
        """
        recommendations = []

        # Exact match
        if version:
            key = f"{language}:{version}"
            if key in self.image_knowledge:
                recommendations.extend(self.image_knowledge[key])

        # Include all versions for this language
        for key, images in self.image_knowledge.items():
            if key.startswith(f"{language}:"):
                recommendations.extend(images)

        # Keep only successful ones
        successful = [rec for rec in recommendations if rec.get("success", False)]

        # Sort by time
        successful.sort(key=lambda x: x["timestamp"], reverse=True)
        return successful

    def save_session_to_long_term(self, session_summary: Dict):
        """
        Save the current session to long-term memory.

        Args:
            session_summary: Session summary including features, decisions, success/failure, etc.
        """
        session_entry = {
            "summary": session_summary,
            "working_memory": self.working_memory.copy(),
            "short_term_memory": self.short_term_memory.copy(),
            "timestamp": datetime.now().isoformat(),
        }

        self.long_term_memory.append(session_entry)
        self._save_long_term_memory()

    def get_context_summary(self) -> str:
        """
        Get a summary of the current context (for prompting).

        Returns:
            Context summary string.
        """
        summary_parts = []

        # Current project info
        if self.working_memory["current_project"]:
            summary_parts.append(
                f"Current project: {self.working_memory['current_project']}"
            )

        if self.working_memory["current_language"]:
            summary_parts.append(f"Language: {self.working_memory['current_language']}")

        # Decisions
        if self.working_memory["config_decisions"]:
            summary_parts.append(
                f"\nDecisions made ({len(self.working_memory['config_decisions'])}):"
            )
            for decision in self.working_memory["config_decisions"][
                -3:
            ]:  # Most recent 3
                summary_parts.append(
                    f"  - {decision.get('decision_type')}: {decision.get('content')}"
                )

        # Errors
        if self.working_memory["current_errors"]:
            summary_parts.append(
                f"\nErrors encountered ({len(self.working_memory['current_errors'])}):"
            )
            for error in self.working_memory["current_errors"][-3:]:  # Most recent 3
                summary_parts.append(
                    f"  - {error.get('error_type')}: {error.get('error_msg')[:100]}"
                )

        # Solutions tried
        if self.working_memory["current_solutions"]:
            summary_parts.append(
                f"\nSolutions tried ({len(self.working_memory['current_solutions'])}):"
            )
            for solution in self.working_memory["current_solutions"][
                -3:
            ]:  # Most recent 3
                status = "✓" if solution.get("success") else "✗"
                summary_parts.append(f"  {status} {solution.get('solution')[:100]}")

        return "\n".join(summary_parts)

    def get_relevant_memory_for_prompt(self, query: str, max_items: int = 5) -> str:
        """
        Get memory relevant to a query (for prompt augmentation).

        Args:
            query: Query string.
            max_items: Max number of items.

        Returns:
            Formatted related-memory string.
        """
        relevant_memory = []

        # Search short-term memory
        for memory in reversed(self.short_term_memory):
            content_str = json.dumps(memory.get("content", {}), ensure_ascii=False)
            if any(keyword in content_str.lower() for keyword in query.lower().split()):
                relevant_memory.append(memory)
                if len(relevant_memory) >= max_items:
                    break

        if not relevant_memory:
            return ""

        # Format output
        output_parts = ["Relevant memory:"]
        for mem in relevant_memory:
            mem_type = mem.get("type", "unknown")
            content = mem.get("content", {})
            output_parts.append(
                f"  [{mem_type}] {json.dumps(content, ensure_ascii=False)[:150]}"
            )

        return "\n".join(output_parts)

    def clear_session(self):
        """Clear current-session memory (keep long-term memory)."""
        self.short_term_memory.clear()
        self.working_memory = {
            "current_project": None,
            "current_language": None,
            "current_dependencies": [],
            "current_errors": [],
            "current_solutions": [],
            "config_decisions": [],
        }

    def get_statistics(self) -> Dict:
        """Get memory statistics."""
        return {
            "short_term_count": len(self.short_term_memory),
            "long_term_count": len(self.long_term_memory),
            "error_types_count": len(self.error_solutions),
            "image_knowledge_count": sum(len(v) for v in self.image_knowledge.values()),
            "current_errors": len(self.working_memory["current_errors"]),
            "current_solutions": len(self.working_memory["current_solutions"]),
            "config_decisions": len(self.working_memory["config_decisions"]),
        }

    def __repr__(self):
        stats = self.get_statistics()
        return f"EnvConfigMemory(short_term={stats['short_term_count']}, long_term={stats['long_term_count']}, errors={stats['error_types_count']}, images={stats['image_knowledge_count']})"


# Convenience helper
def create_memory(memory_dir: str = ".memory") -> EnvConfigMemory:
    """Create an EnvConfigMemory instance."""
    return EnvConfigMemory(memory_dir)


if __name__ == "__main__":
    # Test code
    print("=" * 70)
    print("Testing EnvConfigMemory")
    print("=" * 70)

    # Create instance
    memory = create_memory(".test_memory")

    # Short-term memory
    memory.add_to_short_term(
        "config_step",
        {"step": "analyze_dependencies", "result": "found requirements.txt"},
    )

    # Decisions
    memory.update_working_memory("current_project", "test-project")
    memory.update_working_memory("current_language", "python")
    memory.add_config_decision(
        {
            "decision_type": "base_image",
            "content": "python:3.9",
            "reasoning": "Project uses Python 3.9",
        }
    )

    # Errors
    memory.add_error(
        "dependency_conflict",
        "numpy==1.20.0 conflicts with pandas==1.3.0",
        {"package": "numpy", "version": "1.20.0"},
    )

    # Solutions
    memory.add_solution("dependency_conflict", "Upgrade numpy to 1.21.0", success=True)

    # Image knowledge
    memory.record_image_selection(
        "python",
        "3.9",
        "python:3.9",
        success=True,
        notes="Works for most Python 3.9 projects",
    )

    # Context summary
    print("\n" + "=" * 70)
    print("Current context summary:")
    print("=" * 70)
    print(memory.get_context_summary())

    # Statistics
    print("\n" + "=" * 70)
    print("Memory statistics:")
    print("=" * 70)
    print(json.dumps(memory.get_statistics(), indent=2, ensure_ascii=False))

    # Retrieve error solutions
    print("\n" + "=" * 70)
    print("Retrieve error solutions:")
    print("=" * 70)
    solutions = memory.retrieve_error_solutions("dependency_conflict")
    for sol in solutions:
        print(f"  - {sol['solution']} (success: {sol['success']})")

    # Retrieve image recommendations
    print("\n" + "=" * 70)
    print("Retrieve image recommendations:")
    print("=" * 70)
    images = memory.retrieve_image_recommendations("python", "3.9")
    for img in images:
        print(f"  - {img['image']} (success: {img['success']}) - {img['notes']}")

    print("\n" + "=" * 70)
    print("Done")
    print("=" * 70)
