import os


IGNORE_DIRS = {"tests", "docs", "docs_src", ".git", ".github"}


def find_entry_points(repo_path: str) -> list:
    """
    Find likely real entry points, ignoring tests and examples.
    """

    entry_points = []

    for root, dirs, files in os.walk(repo_path):
        # Skip noisy folders
        if any(part in IGNORE_DIRS for part in root.split(os.sep)):
            continue

        for file in files:
            if file in ("main.py", "app.py", "index.js", "server.js"):
                entry_points.append(os.path.join(root, file))

    # Limit to a few most relevant ones
    return entry_points[:5] if entry_points else ["Entry point not clearly defined."]
