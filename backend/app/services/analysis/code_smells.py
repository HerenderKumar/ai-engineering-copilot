def detect_code_smells(chunks: list[dict]) -> list[str]:
    """
    Detects simple code smells using heuristics.
    """

    smells = []

    for c in chunks:
        lines = c["text"].splitlines()

        if len(lines) > 80:
            smells.append(
                f"Large block detected in {c['source']} (consider splitting)"
            )

        if "try:" in c["text"] and "except" not in c["text"]:
            smells.append(
                f"Possible missing exception handling in {c['source']}"
            )

    return smells[:10]
