#!/usr/bin/env python3
"""
detect_environment.py - Automatically detect container environment configuration

Detects:
1. GPU information
2. Python package mirror configuration
3. APT mirror configuration
4. System information (OS/arch/etc.)
5. Network connectivity
6. Installed key tools
"""

import subprocess
import json
import sys
import os
import re


def run_command(cmd, timeout=5):
    """
    Run a command and return its result.

    Args:
        cmd: Command string or list.
        timeout: Timeout in seconds.

    Returns:
        (stdout, stderr, returncode)
    """
    try:
        if isinstance(cmd, str):
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
        else:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timeout", -1
    except Exception as e:
        return "", str(e), -1


def extract_version(tool_name, version_text):
    """
    Extract a semantic version from a version string.

    Args:
        tool_name: Tool name (gcc, g++, clang, python, etc.)
        version_text: Raw version output.

    Returns:
        Extracted version. If not found, returns the first line of the raw output.
    """
    if not version_text:
        return ""

    # Common version patterns: X.Y.Z, X.Y, X.Y.Z.W
    version_pattern = r"\b(\d+\.\d+(?:\.\d+)*(?:[-_+][a-zA-Z0-9]+)?)\b"

    # Tool-specific heuristics
    if tool_name == "gcc" or tool_name == "g++":
        # gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0
        # Apple clang version 17.0.0 (clang-1700.4.4.1)
        # Prefer matching against the first line
        first_line = version_text.split("\n")[0] if version_text else ""
        matches = re.findall(version_pattern, first_line)
        if matches:
            # Return the first match (usually the primary version)
            return matches[0]
        # If not found, fall back to scanning the entire output
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    elif tool_name == "clang":
        # clang version 14.0.0-1ubuntu1.1
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    elif tool_name == "python" or tool_name == "python3":
        # Python 3.8.10
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    elif tool_name == "node" or tool_name == "npm":
        # v18.19.0
        # Remove 'v' prefix
        version_text = version_text.replace("v", "")
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    elif tool_name == "docker":
        # Docker version 24.0.7, build afdd53b
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    elif tool_name == "git":
        # git version 2.34.1
        matches = re.findall(version_pattern, version_text)
        if matches:
            return matches[0]

    # Generic fallback: first version-like token
    matches = re.findall(version_pattern, version_text)
    if matches:
        return matches[0]

    # Fallback: return the first line
    lines = version_text.strip().split("\n")
    if lines:
        return lines[0].strip()

    return version_text.strip()


def detect_gpu():
    """
    Detect GPU info.

    Returns:
        dict: GPU info
    """
    gpu_info = {
        "available": False,
        "cuda_available": False,
        "gpu_count": 0,
        "gpus": [],
        "cuda_version": None,
        "cudnn_version": None,
        "nvidia_driver": None,
    }

    # Check nvidia-smi
    stdout, stderr, returncode = run_command(
        "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
    )
    if returncode == 0 and stdout.strip():
        gpu_info["available"] = True
        lines = stdout.strip().split("\n")
        gpu_info["gpu_count"] = len(lines)

        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                gpu_info["gpus"].append(
                    {
                        "name": parts[0],
                        "memory": parts[1] if len(parts) > 1 else "Unknown",
                    }
                )

        if len(lines) > 0 and len(lines[0].split(",")) >= 3:
            gpu_info["nvidia_driver"] = lines[0].split(",")[2].strip()

    # Check CUDA
    stdout, stderr, returncode = run_command("nvcc --version")
    if returncode == 0:
        gpu_info["cuda_available"] = True
        match = re.search(r"release (\d+\.\d+)", stdout)
        if match:
            gpu_info["cuda_version"] = match.group(1)

    # Check cuDNN (via Python) - disabled because importing torch may fail
    pass

    return gpu_info


def detect_python_mirrors():
    """
    Detect Python package mirror configuration.

    Returns:
        dict: Mirror info
    """
    mirror_info = {
        "pip_mirrors": [],
        "has_tsinghua": False,
        "has_aliyun": False,
        "has_tencent": False,
        "default_mirror": None,
    }

    # Check pip config files
    pip_conf_locations = [
        "/etc/pip.conf",
        os.path.expanduser("~/.pip/pip.conf"),
        os.path.expanduser("~/.config/pip/pip.conf"),
    ]

    for conf_path in pip_conf_locations:
        if os.path.exists(conf_path):
            try:
                with open(conf_path, "r") as f:
                    content = f.read()

                    # Detect common mirrors
                    if "tsinghua" in content or "tuna" in content:
                        mirror_info["has_tsinghua"] = True
                        mirror_info["pip_mirrors"].append("Tsinghua")
                    if "aliyun" in content:
                        mirror_info["has_aliyun"] = True
                        mirror_info["pip_mirrors"].append("Aliyun")
                    if "tencent" in content:
                        mirror_info["has_tencent"] = True
                        mirror_info["pip_mirrors"].append("Tencent")

                    # Extract index-url
                    match = re.search(r"index-url\s*=\s*(\S+)", content)
                    if match:
                        mirror_info["default_mirror"] = match.group(1)
            except Exception as e:
                pass

    # If no config file found, check `pip config`
    if not mirror_info["pip_mirrors"]:
        stdout, stderr, returncode = run_command("pip config get global.index-url")
        if returncode == 0 and stdout.strip():
            url = stdout.strip()
            mirror_info["default_mirror"] = url

            if "tsinghua" in url or "tuna" in url:
                mirror_info["has_tsinghua"] = True
                mirror_info["pip_mirrors"].append("Tsinghua")
            elif "aliyun" in url:
                mirror_info["has_aliyun"] = True
                mirror_info["pip_mirrors"].append("Aliyun")
            elif "tencent" in url:
                mirror_info["has_tencent"] = True
                mirror_info["pip_mirrors"].append("Tencent")
            elif "pypi.org" in url:
                mirror_info["pip_mirrors"].append("Official")

    return mirror_info


def detect_apt_mirrors():
    """
    Detect APT mirror configuration.

    Returns:
        dict: APT mirror info
    """
    apt_info = {
        "sources": [],
        "has_tsinghua": False,
        "has_aliyun": False,
        "has_163": False,
        "has_ustc": False,
    }

    sources_file = "/etc/apt/sources.list"

    if os.path.exists(sources_file):
        try:
            with open(sources_file, "r") as f:
                content = f.read()

                # Detect common mirrors
                if "tsinghua" in content or "tuna" in content:
                    apt_info["has_tsinghua"] = True
                    apt_info["sources"].append("Tsinghua")
                if "aliyun" in content:
                    apt_info["has_aliyun"] = True
                    apt_info["sources"].append("Aliyun")
                if "163.com" in content:
                    apt_info["has_163"] = True
                    apt_info["sources"].append("163")
                if "ustc" in content:
                    apt_info["has_ustc"] = True
                    apt_info["sources"].append("USTC")

                # Extract primary sources
                for line in content.split("\n"):
                    if line.strip().startswith("deb ") and not line.strip().startswith(
                        "#"
                    ):
                        match = re.search(r"deb\s+(\S+)", line)
                        if match and match.group(1) not in apt_info["sources"]:
                            apt_info["sources"].append(match.group(1))
        except Exception as e:
            pass

    return apt_info


def detect_system_info():
    """
    Detect basic system information.

    Returns:
        dict: System info
    """
    sys_info = {
        "os": "Unknown",
        "os_version": "Unknown",
        "architecture": "Unknown",
        "kernel": "Unknown",
        "hostname": "Unknown",
        "python_version": sys.version.split()[0],
        "is_container": False,
    }

    # OS info
    stdout, stderr, returncode = run_command("cat /etc/os-release")
    if returncode == 0:
        for line in stdout.split("\n"):
            if line.startswith("NAME="):
                sys_info["os"] = line.split("=")[1].strip('"')
            elif line.startswith("VERSION="):
                sys_info["os_version"] = line.split("=")[1].strip('"')

    # Architecture
    stdout, stderr, returncode = run_command("uname -m")
    if returncode == 0:
        sys_info["architecture"] = stdout.strip()

    # Kernel
    stdout, stderr, returncode = run_command("uname -r")
    if returncode == 0:
        sys_info["kernel"] = stdout.strip()

    # Hostname
    stdout, stderr, returncode = run_command("hostname")
    if returncode == 0:
        sys_info["hostname"] = stdout.strip()

    # Detect whether running inside a container
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        sys_info["is_container"] = True

    return sys_info


def detect_network():
    """
    Detect network connectivity.

    Returns:
        dict: Network info
    """
    network_info = {
        "internet_available": False,
        "can_reach_pypi": False,
        "can_reach_github": False,
        "dns_working": True,
    }

    # Check internet connectivity with curl HEAD request
    # --connect-timeout controls connect timeout; --max-time controls total timeout
    stdout, stderr, returncode = run_command(
        "curl -I --connect-timeout 2 --max-time 3 -s -o /dev/null -w '%{http_code}' https://www.baidu.com",
        timeout=4,
    )
    if returncode == 0 and stdout.strip() and stdout.strip().startswith("2"):
        network_info["internet_available"] = True

    # Check PyPI (simple API)
    stdout, stderr, returncode = run_command(
        "curl -I --connect-timeout 2 --max-time 3 -s -o /dev/null -w '%{http_code}' https://pypi.org/simple/",
        timeout=4,
    )
    if returncode == 0 and stdout.strip() and stdout.strip().startswith("2"):
        network_info["can_reach_pypi"] = True

    # Check GitHub
    stdout, stderr, returncode = run_command(
        "curl -I --connect-timeout 2 --max-time 3 -s -o /dev/null -w '%{http_code}' https://github.com",
        timeout=4,
    )
    if returncode == 0 and stdout.strip() and stdout.strip().startswith("2"):
        network_info["can_reach_github"] = True

    return network_info


def detect_installed_tools():
    """
    Detect installed key tools.

    Returns:
        dict: Tool info
    """
    tools = {
        "git": False,
        "curl": False,
        "wget": False,
        "gcc": False,
        "g++": False,
        "clang": False,
        "make": False,
        "docker": False,
        "pip": False,
        "conda": False,
        "poetry": False,
        "npm": False,
        "versions": {},
    }

    tool_commands = {
        "git": "git --version",
        "curl": "curl --version",
        "wget": "wget --version",
        "gcc": "gcc --version",
        "g++": "g++ --version",
        "clang": "clang --version",
        "make": "make --version",
        "docker": "docker --version",
        "pip": "pip --version",
        "conda": "conda --version",
        "poetry": "poetry --version",
        "npm": "npm --version",
    }

    for tool, cmd in tool_commands.items():
        stdout, stderr, returncode = run_command(cmd)
        if returncode == 0:
            tools[tool] = True
            # Extract version
            version_text = stdout.strip() if stdout else ""
            extracted_version = extract_version(tool, version_text)
            tools["versions"][tool] = extracted_version

    return tools


def format_output(env_info, output_format="text"):
    """
    Format environment info for output.

    Args:
        env_info: Environment info dict.
        output_format: Output format ('text' or 'json').

    Returns:
        str: Formatted output
    """
    if output_format == "json":
        return json.dumps(env_info, indent=2, ensure_ascii=False)

    # Text format - compact
    output = []
    output.append("=" * 70)
    output.append("🔍 Environment detection")
    output.append("=" * 70)

    # GPU
    gpu = env_info["gpu"]
    if gpu["available"]:
        gpu_info = f"GPU: ✅ {gpu['gpu_count']}"
        if gpu["gpus"]:
            gpu_info += f" ({gpu['gpus'][0]['name']})"
        if gpu["cuda_version"]:
            gpu_info += f" | CUDA {gpu['cuda_version']}"
        output.append(gpu_info)
    else:
        output.append("GPU: ❌ Unavailable")

    # Python mirrors
    pip_mirrors = env_info["python_mirrors"]
    if pip_mirrors["pip_mirrors"]:
        output.append(f"Python mirror: ✅ {', '.join(pip_mirrors['pip_mirrors'])}")
    else:
        output.append("Python mirror: ⚠️  Not configured")

    # APT mirrors
    apt = env_info["apt_mirrors"]
    if apt["sources"]:
        output.append(f"APT mirror: ✅ {', '.join(apt['sources'][:2])}")
    else:
        output.append("APT mirror: ⚠️  Default")

    # System - single line
    sys_info = env_info["system"]
    output.append(
        f"System: {sys_info['os']} | {sys_info['architecture']} | Python {sys_info['python_version']} | {'container' if sys_info['is_container'] else 'host'}"
    )

    # Network - single line
    network = env_info["network"]
    net_status = []
    if network["internet_available"]:
        net_status.append("Internet✅")
    if network["can_reach_pypi"]:
        net_status.append("PyPI✅")
    if network["can_reach_github"]:
        net_status.append("GitHub✅")
    if not net_status:
        net_status.append("Network unavailable❌")
    output.append(f"Network: {' | '.join(net_status)}")

    # Installed tools - single line, with versions
    tools = env_info["tools"]
    versions = tools.get("versions", {})
    installed_with_versions = []

    for tool, is_installed in tools.items():
        if tool == "versions":
            continue
        if is_installed:
            version = versions.get(tool, "")
            if version:
                # Use concise version (may already be normalized by extract_version)
                installed_with_versions.append(f"{tool}({version})")
            else:
                installed_with_versions.append(tool)

    if installed_with_versions:
        # Show up to 6 tools to avoid overly long lines
        max_display = 6
        displayed = installed_with_versions[:max_display]
        remaining = len(installed_with_versions) - max_display
        tool_line = f"Tools: {', '.join(displayed)}"
        if remaining > 0:
            tool_line += f" +{remaining} more"
        output.append(tool_line)
    else:
        output.append("Tools: ⚠️ No common tools detected")

    output.append("=" * 70)

    return "\n".join(output)


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Detect container environment info")
    parser.add_argument(
        "--format",
        type=str,
        choices=["text", "json"],
        default="text",
        help="Output format (text or json)",
    )
    parser.add_argument("--save", type=str, help="Save results to file")
    args = parser.parse_args()

    # Collect environment info
    print("Detecting environment...", file=sys.stderr)

    env_info = {
        "gpu": detect_gpu(),
        "python_mirrors": detect_python_mirrors(),
        "apt_mirrors": detect_apt_mirrors(),
        "system": detect_system_info(),
        "network": detect_network(),
        "tools": detect_installed_tools(),
    }

    # Format output
    output = format_output(env_info, args.format)
    print(output)

    # Save to file
    if args.save:
        try:
            with open(args.save, "w", encoding="utf-8") as f:
                if args.format == "json":
                    json.dump(env_info, f, indent=2, ensure_ascii=False)
                else:
                    f.write(output)
            print(f"\n✅ Results saved to: {args.save}", file=sys.stderr)
        except Exception as e:
            print(f"\n❌ Save failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
