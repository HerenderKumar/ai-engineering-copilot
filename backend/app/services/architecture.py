from collections import Counter


def infer_architecture(chunks: list[dict]) -> dict:
    file_paths = [c["source"] for c in chunks]

    folder_counter = Counter()
    for path in file_paths:
        parts = path.replace("\\", "/").split("/")
        if len(parts) > 1:
            folder_counter[parts[-2]] += 1

    most_important_folders = [
        folder for folder, _ in folder_counter.most_common(5)
    ]

    entry_points = [
        p for p in file_paths
        if p.endswith("main.py") or p.endswith("app.py") or p.endswith("index.js")
    ]

    return {
        "important_folders": most_important_folders,
        "entry_points": entry_points[:5],
        "recommended_reading_order": entry_points[:3]
    }
