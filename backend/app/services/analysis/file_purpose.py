def explain_file_purpose(file_path: str, chunks: list[dict]) -> str:
    """
    Gives a simple explanation of why a file likely exists.
    """

    path = file_path.lower()

    if "main.py" in path:
        return "This file is likely the main entry point of the application."

    if "api" in path:
        return "This file likely defines API endpoints or request handlers."

    if "service" in path:
        return "This file likely contains core business logic."

    if "model" in path or "schema" in path:
        return "This file defines data structures or database models."

    return "This file supports the application logic."
