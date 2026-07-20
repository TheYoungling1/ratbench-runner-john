"""
Configuration for ablation experiments.
"""

# Whether to use the ablation version of SetupAgent (no image selection, fixed defaults)
ENABLE_SETUP_ABLATION = False

# Whether to remove non-essential tools (keep only read/edit/run-test/stop)
ENABLE_TOOL_ABLATION = True

# Essential tool commands (used when ENABLE_TOOL_ABLATION is True)
ESSENTIAL_TOOLS = {
    # "read-file",
    # "edit-file",
    # "run-test",
    "stop",
    # "construct-test", # Maybe needed if tests aren't ready
}

# Default base images for each language ((used when ENABLE_SETUP_ABLATION is True)
DEFAULT_BASE_IMAGES = {
    "rust": "rust:1.75",
    "python": "python:3.10-slim",
    "javascript": "node:18-slim",
    "node": "node:18-slim",
    "java": "eclipse-temurin:17",
    "go": "golang:1.19",
}
