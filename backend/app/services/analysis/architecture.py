import os


def explain_architecture(repo_path: str) -> str:
    """
    Explains high-level project structure.
    """

    if not os.path.exists(repo_path):
        return "Repository path is invalid."

    folders = [
        name for name in os.listdir(repo_path)
        if os.path.isdir(os.path.join(repo_path, name))
    ]

    if not folders:
        return "This project has a flat structure with very few folders."

    return (
        "This project is organized into the following main folders: "
        + ", ".join(folders)
        + ". Each folder likely represents a different responsibility."
    )
