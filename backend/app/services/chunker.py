import ast

# to limit the chunks to embeddings
MAX_CHUNK_SIZE = 1500  # characters


def split_large_text(text: str) -> list[str]:
    """
    Splits large text into smaller, safe chunks.
    This protects memory and embedding limits.
    """
    chunks = []

    for start in range(0, len(text), MAX_CHUNK_SIZE):
        end = start + MAX_CHUNK_SIZE
        chunks.append(text[start:end])

    return chunks


def chunk_repository_files(files: list[dict]) -> list[dict]:
    """
    Convert raw repository files into small, searchable chunks.
    Each chunk keeps a reference to its source file.
    """
    all_chunks = []

    for file in files:
        file_path = file["path"]
        content = file["content"]

        # Python files get special handling (AST-aware)
        if file_path.endswith(".py"):
            try:
                tree = ast.parse(content)
                lines = content.splitlines()

                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                        start_line = node.lineno - 1
                        end_line = getattr(node, "end_lineno", start_line + 1)

                        raw_block = "\n".join(lines[start_line:end_line])

                        for piece in split_large_text(raw_block):
                            all_chunks.append({
                                "text": piece,
                                "source": file_path
                            })

            except SyntaxError:
                # Fallback: treat entire file as plain text
                for piece in split_large_text(content):
                    all_chunks.append({
                        "text": piece,
                        "source": file_path
                    })

        else:
            # Non-Python files are treated as plain text
            for piece in split_large_text(content):
                all_chunks.append({
                    "text": piece,
                    "source": file_path
                })

    return all_chunks
