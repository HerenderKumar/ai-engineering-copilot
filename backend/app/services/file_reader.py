import os

IGNORED_DIRS = {".git", "__pycache__", "node_modules", "dist", "build"}
IGNORED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".exe"}


def _should_skip(path: str) -> bool:
    for part in path.split(os.sep):
        if part in IGNORED_DIRS:
            return True
    return any(path.endswith(ext) for ext in IGNORED_EXTENSIONS)


def read_repository_files(repo_path: str) -> list[dict]:
    files = []

    for root, _, filenames in os.walk(repo_path):
        for name in filenames:
            full_path = os.path.join(root, name)

            if _should_skip(full_path):
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    files.append({
                        "path": full_path,
                        "content": f.read()
                    })
            except Exception:
                continue

    return files
