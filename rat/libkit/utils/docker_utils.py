"""
Utility functions for Docker operations and image/container name handling.
"""


def sanitize_docker_image_name(name: str, prefix: str = "", suffix: str = "") -> str:
    """
    Sanitize a string to create a valid Docker image name.

    Docker image naming rules (from distribution/reference specification):
    - Repository names must be lowercase
    - Path components can contain: a-z, 0-9, separators (., _, -)
    - Maximum total length: 255 characters

    This function handles common transformations:
    - Converts to lowercase
    - Replaces forward slashes (/) with underscores (_)
    - Replaces hyphens (-) with underscores (_) for consistency
    - Supports optional prefix and suffix

    Args:
        name: The base name to sanitize (e.g., repository full_name like "owner/repo")
        prefix: Optional prefix to prepend (e.g., "test_build_", "repo_")
        suffix: Optional suffix to append (e.g., "_1", "_iteration_2")

    Returns:
        Valid Docker image name string

    Examples:
        >>> sanitize_docker_image_name("chenfei-wu/TaskMatrix")
        'chenfei_wu_taskmatrix'

        >>> sanitize_docker_image_name("Microsoft/TypeScript", prefix="test_build_", suffix="_1")
        'test_build_microsoft_typescript_1'

        >>> sanitize_docker_image_name("owner/REPO", prefix="repo_")
        'repo_owner_repo'
    """
    # Replace slashes and hyphens with underscores, convert to lowercase
    sanitized = name.replace("/", "_").replace("-", "_").lower()

    # Combine with prefix and suffix
    result = f"{prefix}{sanitized}{suffix}"

    # Truncate if exceeds Docker's 255 character limit
    if len(result) > 255:
        result = result[:255]

    return result
