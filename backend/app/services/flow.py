def infer_execution_flow(chunks: list[dict]) -> dict:
    """
    Tries to infer how the application starts and flows.
    This is heuristic-based, not perfect — but safe.
    """
    entry_points = []
    calls = set()

    for chunk in chunks:
        path = chunk["source"]
        text = chunk["text"]

        # Common entry patterns
        if (
            "if __name__ == '__main__'" in text
            or "uvicorn.run" in text
            or "app = FastAPI" in text
        ):
            entry_points.append(path)

        # Very basic call detection
        for line in text.splitlines():
            if "(" in line and ")" in line and "import" not in line:
                calls.add(line.strip())

    return {
        "entry_points": list(set(entry_points))[:5],
        "example_execution_steps": list(calls)[:10]
    }
