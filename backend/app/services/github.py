import os
import subprocess


def clone_repo(repo_url, base_path, repo_id):
    path = os.path.join(base_path, repo_id)
    os.makedirs(base_path, exist_ok=True)

    subprocess.run(["git", "clone", repo_url, path], check=True)

    return path