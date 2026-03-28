import os


IGNORE_DIRS = {"tests", "docs", "docs_src", ".git", ".github"}


def suggest_reading_order(repo_path: str) -> list:
    """
    Suggests a small, beginner-friendly reading order.
    """

    reading_order = []

    # 1. README always first
    readme_path = os.path.join(repo_path, "README.md")
    if os.path.exists(readme_path):
        reading_order.append("README.md")

    # 2. Look for core app files
    for root, dirs, files in os.walk(repo_path):
        if any(part in IGNORE_DIRS for part in root.split(os.sep)):
            continue

        for file in files:
            if file in ("main.py", "app.py"):
                relative_path = os.path.relpath(
                    os.path.join(root, file), repo_path
                )
                reading_order.append(relative_path)

        if len(reading_order) >= 5:
            break

    if not reading_order:
        return ["Start with the README and explore core source files."]

    return reading_order[:5]
