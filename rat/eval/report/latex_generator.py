#!/usr/bin/env python3
"""LaTeX table report generator."""

import os
import subprocess
import shutil
from typing import Dict, Any, Optional


class LaTeXReportGenerator:
    """LaTeX report generator."""

    def __init__(self, data: Dict[str, Any]):
        """
        Initialize the generator.

        Args:
            data: Dict containing evaluation data.
                {
                    'eval_id': str,
                    'eval_name': str,
                    'started_at': str,
                    'ended_at': str,
                    'summary': {...},
                    'repos': [...]
                }
        """
        self.data = data
        self.summary = data.get("summary", {})
        self.repos = data.get("repos", [])

    @staticmethod
    def escape_latex(text: str) -> str:
        """Escape LaTeX special characters."""
        if not isinstance(text, str):
            text = str(text)

        replacements = {
            "\\": r"\textbackslash{}",
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "{": r"\{",
            "}": r"\}",
            "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}",
        }

        for char, replacement in replacements.items():
            text = text.replace(char, replacement)

        return text

    @staticmethod
    def format_percentage(value: float, decimals: int = 1) -> str:
        """Format a percentage."""
        return f"{value * 100:.{decimals}f}\\%"

    @staticmethod
    def format_time(seconds: float) -> str:
        """Format a duration."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f}m"
        else:
            hours = seconds / 3600
            return f"{hours:.1f}h"

    def generate_summary_table(self) -> str:
        """Generate the summary table."""
        total_repos = len(self.repos)
        success_count = self.summary.get("success_count", 0)
        success_rate = self.summary.get("success_rate", 0.0)

        # Count timeouts and errors
        timeout_count = sum(1 for r in self.repos if r.get("status") == "timeout")
        error_count = sum(1 for r in self.repos if r.get("status") == "error")

        table = (
            r"""\begin{table}[htbp]
\centering
\caption{Evaluation Summary Statistics}
\label{tab:summary}
\begin{tabular}{lr}
\toprule
\textbf{Metric} & \textbf{Value} \\
\midrule
Total Repositories & """
            + str(total_repos)
            + r""" \\
Successful & """
            + f"{success_count} ({self.format_percentage(success_rate)})"
            + r""" \\
Timeout & """
            + f"{timeout_count} ({self.format_percentage(timeout_count / total_repos if total_repos > 0 else 0)})"
            + r""" \\
Error & """
            + f"{error_count} ({self.format_percentage(error_count / total_repos if total_repos > 0 else 0)})"
            + r""" \\
\midrule
Avg Test Pass Rate & """
            + self.format_percentage(self.summary.get("avg_pass_rate", 0.0))
            + r""" \\
Avg Total Tests & """
            + f"{self.summary.get('avg_total_tests', 0):.1f}"
            + r""" \\
Avg Tests Passed & """
            + f"{self.summary.get('avg_passed', 0):.1f}"
            + r""" \\
Avg Tests Failed & """
            + f"{self.summary.get('avg_failed', 0):.1f}"
            + r""" \\
\midrule
Avg Execution Time & """
            + self.format_time(self.summary.get("avg_latency", 0.0))
            + r""" \\
\bottomrule
\end{tabular}
\end{table}
"""
        )
        return table

    def generate_detail_table(self) -> str:
        """Generate the detailed results table (longtable)."""
        table_header = r"""\begin{longtable}{p{4.5cm}crrrrr}
\caption{Detailed Evaluation Results} \label{tab:details} \\
\toprule
\textbf{Repository} & \textbf{Status} & \textbf{Total} & \textbf{Passed} & \textbf{Failed} & \textbf{Pass Rate} & \textbf{Pass Rate*} \\
\midrule
\endfirsthead

\multicolumn{7}{c}{{\bfseries \tablename\ \thetable{} -- continued from previous page}} \\
\toprule
\textbf{Repository} & \textbf{Status} & \textbf{Total} & \textbf{Passed} & \textbf{Failed} & \textbf{Pass Rate} & \textbf{Pass Rate*} \\
\midrule
\endhead

\midrule
\multicolumn{7}{r}{{Continued on next page}} \\
\endfoot

\bottomrule
\multicolumn{7}{l}{* Excluding ModuleNotFoundError and ImportError} \\
\endlastfoot

"""

        # Add rows
        rows = []
        for repo in self.repos:
            repo_name = self.escape_latex(repo.get("full_name", "unknown"))
            status = repo.get("status", "unknown")

            # Status display
            status_map = {"success": "Success", "timeout": "Timeout", "error": "Error"}
            status_str = status_map.get(status, status.capitalize())

            if status == "success" and repo.get("pytest_executed", False):
                total = repo.get("pytest_total_tests", 0)
                passed = repo.get("pytest_passed", 0)
                failed = repo.get("pytest_failed", 0)
                pass_rate = self.format_percentage(repo.get("pytest_pass_rate", 0.0))
                pass_rate_excl = self.format_percentage(
                    repo.get("pass_rate_exclude_code_issues", 0.0)
                )

                row = f"{repo_name} & {status_str} & {total} & {passed} & {failed} & {pass_rate} & {pass_rate_excl} \\\\"
            else:
                row = f"{repo_name} & {status_str} & -- & -- & -- & -- & -- \\\\"

            rows.append(row)

        table_body = "\n".join(rows)
        table_footer = r"""
\end{longtable}
"""

        return table_header + table_body + table_footer

    def generate_error_breakdown_table(self) -> str:
        """Generate the error type breakdown table."""
        # Aggregate errors
        error_counts = {}
        for repo in self.repos:
            if repo.get("error_breakdown"):
                for error_type, count in repo["error_breakdown"].items():
                    error_counts[error_type] = error_counts.get(error_type, 0) + count

        if not error_counts:
            return ""  # No errors

        total_errors = sum(error_counts.values())

        table = r"""\begin{table}[htbp]
\centering
\caption{Error Type Breakdown}
\label{tab:errors}
\begin{tabular}{lrr}
\toprule
\textbf{Error Type} & \textbf{Count} & \textbf{Percentage} \\
\midrule
"""

        # Sort by count
        sorted_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)

        for error_type, count in sorted_errors:
            error_name = self.escape_latex(error_type)
            percentage = self.format_percentage(
                count / total_errors if total_errors > 0 else 0
            )
            table += f"{error_name} & {count} & {percentage} \\\\\n"

        table += r"""\bottomrule
\end{tabular}
\end{table}
"""
        return table

    def generate_full_report(
        self, output_path: str, auto_compile: bool = True
    ) -> Optional[str]:
        """
        Generate a full LaTeX report.

        Args:
            output_path: Output .tex path.
            auto_compile: Whether to auto-compile a PDF.

        Returns:
            PDF path if compilation succeeds, otherwise None.
        """
        # LaTeX document template
        latex_doc = (
            r"""\documentclass[11pt]{article}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{geometry}
\usepackage{array}

\geometry{margin=1in}

\title{RAT Evaluation Report}
\author{Generated from W\&B Weave}
\date{"""
            + self.data.get("started_at", "N/A")
            + r"""}

\begin{document}
\maketitle

\section{Summary Statistics}

"""
            + self.generate_summary_table()
            + r"""

\section{Detailed Results}

"""
            + self.generate_detail_table()
            + r"""

\section{Error Analysis}

"""
            + self.generate_error_breakdown_table()
            + r"""

\end{document}
"""
        )

        # Write .tex file
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(latex_doc)

        # Optionally compile PDF
        if auto_compile:
            return self.compile_to_pdf(output_path)

        return None

    def compile_to_pdf(self, tex_path: str) -> Optional[str]:
        """
        Compile PDF using pdflatex.

        Args:
            tex_path: Path to the .tex file.

        Returns:
            PDF path, or None on failure.
        """
        # Check pdflatex availability
        if not shutil.which("pdflatex"):
            print("⚠️  pdflatex not installed; skipping PDF compilation")
            return None

        tex_dir = os.path.dirname(os.path.abspath(tex_path))
        tex_file = os.path.basename(tex_path)

        # Compile twice to fix cross-references
        for i in range(2):
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", tex_file],
                cwd=tex_dir,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                print(f"⚠️  pdflatex failed (run {i + 1}):")
                # Print only the last 500 chars
                error_msg = (
                    result.stdout[-500:] if len(result.stdout) > 500 else result.stdout
                )
                print(error_msg)
                return None

        # Check output
        pdf_path = tex_path.replace(".tex", ".pdf")
        if os.path.exists(pdf_path):
            # Clean auxiliary files
            for ext in [".aux", ".log", ".out"]:
                aux_file = tex_path.replace(".tex", ext)
                if os.path.exists(aux_file):
                    try:
                        os.remove(aux_file)
                    except:
                        pass

            return pdf_path

        return None
