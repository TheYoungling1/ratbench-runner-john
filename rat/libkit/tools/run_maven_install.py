#!/usr/bin/env python3
"""run_maven_install.py - Run a Maven build inside the container (skip tests).

Behavior:
1. Prefer ./mvnw clean install -DskipTests
2. Fall back to mvn clean install -DskipTests
3. Determine whether the build succeeded
4. Generate a detailed build report
5. Save results to a JSON file
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


def detect_maven_wrapper(repo_path: str) -> bool:
    """
    Detect whether Maven Wrapper exists.

    Args:
        repo_path: Repository path

    Returns:
        True if mvnw exists; otherwise False
    """
    mvnw_path = os.path.join(repo_path, "mvnw")
    return os.path.exists(mvnw_path) and os.access(mvnw_path, os.X_OK)


def run_maven_build(
    repo_path: str, timeout: int = 900, verbose: bool = True
) -> Tuple[Dict, int]:
    """
    Run a Maven build (skip tests).

    Args:
        repo_path: Repository path
        timeout: Timeout (seconds)
        verbose: Whether to print verbose output

    Returns:
        (result dict, return code)
    """
    # Decide whether to use mvnw or mvn
    use_wrapper = detect_maven_wrapper(repo_path)

    if use_wrapper:
        cmd = ["./mvnw", "clean", "install", "-DskipTests"]
        maven_type = "Maven Wrapper (./mvnw)"
    else:
        cmd = ["mvn", "clean", "install", "-DskipTests"]
        maven_type = "Maven (global)"

    if verbose:
        print(f"🔧 Using: {maven_type}")
        print(f"🔧 Command: {' '.join(cmd)}")
        print(f"📁 Working directory: {repo_path}")
        print(f"⏱️  Timeout: {timeout}s")
        print("=" * 60)

    try:
        # Run Maven build
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=timeout
        )

        output = result.stdout + "\n" + result.stderr

        if verbose:
            print("\n📋 Maven output:")
            print(output)
            print("=" * 60)

        # Parse build results
        parsed_result = parse_maven_output(output, use_wrapper, verbose)
        parsed_result["returncode"] = result.returncode
        parsed_result["raw_output"] = output if verbose else ""
        parsed_result["maven_type"] = maven_type

        return parsed_result, result.returncode

    except subprocess.TimeoutExpired:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "maven_type": maven_type,
            "summary": {
                "status": "TIMEOUT",
                "build_time": f"{timeout}s (timeout)",
            },
            "error_message": f"Maven build timed out ({timeout}s)",
            "raw_output": "",
        }
        return error_result, -1

    except FileNotFoundError:
        # mvn command not found
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "maven_type": maven_type,
            "summary": {
                "status": "ERROR",
                "build_time": "N/A",
            },
            "error_message": f"Cannot find {'mvnw' if use_wrapper else 'mvn'}; ensure Maven is installed",
            "raw_output": "",
        }
        return error_result, -1

    except Exception as e:
        error_result = {
            "command": " ".join(cmd),
            "success": False,
            "returncode": -1,
            "maven_type": maven_type,
            "summary": {
                "status": "ERROR",
                "build_time": "N/A",
            },
            "error_message": f"Execution error: {str(e)}",
            "raw_output": "",
        }
        return error_result, -1


def parse_maven_output(
    output: str, use_wrapper: bool = False, verbose: bool = False
) -> Dict:
    """
    Parse Maven output and extract key information.

    Args:
        output: Maven output text
        use_wrapper: Whether Maven Wrapper was used
        verbose: Whether to print verbose output

    Returns:
        Dict containing build information
    """
    maven_cmd = "./mvnw" if use_wrapper else "mvn"
    result = {
        "command": f"{maven_cmd} clean install -DskipTests",
        "success": False,
        "summary": {
            "status": "UNKNOWN",
            "build_time": "N/A",
        },
        "error_message": "",
    }

    # Determine build success
    if "BUILD SUCCESS" in output:
        result["success"] = True
        result["summary"]["status"] = "SUCCESS"
        if verbose:
            print("✅ Maven build succeeded")
    elif "BUILD FAILURE" in output:
        result["success"] = False
        result["summary"]["status"] = "FAILURE"
        if verbose:
            print("❌ Maven build failed")
    else:
        result["success"] = False
        result["summary"]["status"] = "UNKNOWN"
        if verbose:
            print("⚠️  Unable to determine build status")

    # Extract build time
    # Format: "Total time: 1.234 s" or "Total time: 01:23 min"
    time_match = re.search(r"Total time:\s+(.+?)(?:\n|$)", output, re.IGNORECASE)
    if time_match:
        result["summary"]["build_time"] = time_match.group(1).strip()
        if verbose:
            print(f"⏱️  Build time: {result['summary']['build_time']}")

    # If build failed, try extracting error messages
    if not result["success"]:
        # Common error patterns
        error_patterns = [
            r"\[ERROR\]\s+(.+?)(?:\n|$)",  # Maven ERROR lines
            r"BUILD FAILURE.*?(?=\[INFO\]|\[ERROR\]|$)",  # BUILD FAILURE block
            r"Failed to execute goal.*?(?:\n|$)",  # Failed to execute goal
            r"Compilation failure.*?(?:\n|$)",  # Compilation failure
        ]

        error_messages = []
        for pattern in error_patterns:
            matches = re.findall(pattern, output, re.DOTALL | re.IGNORECASE)
            error_messages.extend(matches[:3])  # Take at most first 3 matches

        if error_messages:
            # Clean and limit length
            cleaned_errors = [
                msg.strip()[:200] for msg in error_messages if msg.strip()
            ]
            result["error_message"] = "\n".join(cleaned_errors[:5])  # Up to 5 errors
            if verbose:
                print(f"🔍 Error message(s):\n{result['error_message']}")

    return result


def format_output(result: Dict, verbose: bool = False) -> str:
    """
    Format output.

    Args:
        result: Result dict
        verbose: Whether to print verbose output

    Returns:
        Formatted string
    """
    output = []
    output.append("\n" + "=" * 60)
    output.append("📊 Maven Build Result")
    output.append("=" * 60)

    # Basic info
    output.append(f"\nCommand: {result.get('command', 'N/A')}")
    if "maven_type" in result:
        output.append(f"Type: {result['maven_type']}")
    output.append(f"Status: {result['summary']['status']}")
    output.append(f"Success: {'✅ Yes' if result['success'] else '❌ No'}")
    output.append(f"Build time: {result['summary']['build_time']}")

    # Error info
    if not result["success"] and result.get("error_message"):
        output.append("\n" + "-" * 60)
        output.append("❌ Error message:")
        output.append("-" * 60)
        output.append(result["error_message"])

    output.append("\n" + "=" * 60)

    return "\n".join(output)


def save_results(result: Dict, output_file: str):
    """
    Save results to a JSON file.

    Args:
        result: Result dict
        output_file: Output file path
    """
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"\n💾 Results saved to: {output_file}")
    except Exception as e:
        print(f"\n⚠️  Failed to save results: {e}")


def main():
    """Main entry - fixed configuration, no args."""
    # Fixed configuration
    repo_path = os.getcwd()
    timeout = 900  # 15 minutes
    verbose = True  # Always print verbose output

    # Fixed output path (inside container)
    output_file = os.path.join(repo_path, "logs/run_maven_install_results.json")

    print("=" * 60)
    print("🏗️  Run Maven Build")
    print("=" * 60)

    # Validate repo path
    if not os.path.exists(repo_path):
        print(f"❌ Error: repo path does not exist: {repo_path}")
        return 1

    # Check whether pom.xml exists
    pom_file = os.path.join(repo_path, "pom.xml")
    if not os.path.exists(pom_file):
        print(f"⚠️  Warning: pom.xml not found: {pom_file}")
        print("This may not be a Maven project, but Maven will still be attempted...")

    # Run build
    print("\n🚀 Starting Maven build (skip tests)...")
    result, returncode = run_maven_build(
        repo_path=repo_path, timeout=timeout, verbose=verbose
    )

    # Print result
    print(format_output(result, verbose))

    # Save results to a fixed location
    save_results(result, output_file)

    # Return code: -1=timeout/error, 0=success, 1=failure
    if result.get("returncode") == -1:
        print("\n❌ Maven build error (timeout or other error)")
        return -1
    elif result["success"]:
        print("\n✅ Maven build succeeded!")
        return 0
    else:
        print("\n❌ Maven build failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
