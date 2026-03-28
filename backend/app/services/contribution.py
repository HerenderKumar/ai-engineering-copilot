def infer_contribution_areas(chunks: list[dict]) -> dict:
    """
    Heuristically identify good areas for first-time contributors.
    This avoids core logic and prefers isolated, readable files.
    """

    files = set(chunk["source"] for chunk in chunks)

    safe_files = []
    risky_files = []

    for path in files:
        normalized = path.replace("\\", "/").lower()

        # Core / risky areas
        if any(
            key in normalized
            for key in ["auth", "security", "database", "core", "engine"]
        ):
            risky_files.append(path)
            continue

        # Configs, utils, docs, handlers are usually safe
        if any(
            key in normalized
            for key in ["utils", "helpers", "config", "constants", "schemas"]
        ):
            safe_files.append(path)
            continue

        # API / routes are often good contribution points
        if "api" in normalized or "routes" in normalized:
            safe_files.append(path)
            continue

    return {
        "safe_first_contribution_files": sorted(safe_files)[:10],
        "files_to_approach_later": sorted(risky_files)[:10],
        "contribution_tips": [
            "Start with small changes (docs, validations, edge cases)",
            "Avoid touching core logic in first PR",
            "Add tests if available",
            "Read existing patterns before adding new ones"
        ]
    }
