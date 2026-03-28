from collections import defaultdict


def explain_folders(chunks: list[dict]) -> dict:
    """
    Explains what each folder is likely responsible for.
    """

    folders = defaultdict(int)

    for c in chunks:
        path = c["source"].replace("\\", "/")
        parts = path.split("/")
        if len(parts) > 1:
            folders[parts[-2]] += 1

    explanations = {}

    for folder in folders:
        if folder in ["api", "routes"]:
            explanations[folder] = "Handles HTTP requests and routing"
        elif folder in ["services"]:
            explanations[folder] = "Contains business logic"
        elif folder in ["models", "schemas"]:
            explanations[folder] = "Defines data structures"
        elif folder in ["utils", "helpers"]:
            explanations[folder] = "Shared helper functions"
        else:
            explanations[folder] = "Supporting code for the application"

    return explanations
