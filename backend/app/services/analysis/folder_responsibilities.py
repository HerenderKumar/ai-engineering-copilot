import os


IGNORE_FOLDERS = {".git", ".github", "__pycache__"}


def explain_folder_responsibilities(repo_path: str) -> dict:
    """
    Explains what each important folder is responsible for.
    """

    responsibilities = {}

    for name in os.listdir(repo_path):
        full_path = os.path.join(repo_path, name)

        if not os.path.isdir(full_path):
            continue

        if name in IGNORE_FOLDERS:
            continue

        if name.lower() in {"tests", "test"}:
            responsibilities[name] = "Contains automated tests for the project."

        elif "doc" in name.lower():
            responsibilities[name] = "Contains documentation and examples."

        elif name.lower() in {"app", "src", "fastapi"}:
            responsibilities[name] = "Contains the core application logic."

        else:
            responsibilities[name] = "Contains related code or resources."

    return responsibilities
