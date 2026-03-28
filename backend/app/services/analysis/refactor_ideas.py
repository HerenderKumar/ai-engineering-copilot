def suggest_refactors(chunks: list[dict]) -> list[str]:
    """
    Suggests safe refactoring ideas.
    """

    return [
        "Split large functions into smaller ones",
        "Extract repeated logic into helper functions",
        "Rename unclear variable names",
        "Add type hints",
        "Add logging for better debugging"
    ]
