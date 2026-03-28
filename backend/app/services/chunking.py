import os
import logging
from typing import List, Dict, Any
from tree_sitter_languages import get_language, get_parser

logger = logging.getLogger(__name__)

# Core filtering sets
IGNORED_DIRS = {
    ".git", ".github", "node_modules", "venv", ".venv", "env", "__pycache__", 
    "dist", "build", ".next", "coverage", "out", "target"
}

IGNORED_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", 
    ".zip", ".tar", ".gz", ".mp4", ".mp3", ".wav", 
    ".lock", ".csv", ".db", ".sqlite", ".parquet", "package-lock.json", "yarn.lock"
}

# Map file extensions to Tree-Sitter languages
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".cpp": "cpp",
    ".go": "go",
    ".rs": "rust"
}

class ASTChunker:
    """
    Advanced Syntax-Aware Chunker.
    Uses Tree-Sitter to split code at logical boundaries (Functions, Classes).
    """
    def __init__(self, max_chunk_size: int = 1500):
        self.max_chunk_size = max_chunk_size

    def is_processable_file(self, file_path: str) -> bool:
        path_parts = set(file_path.split(os.sep))
        if path_parts.intersection(IGNORED_DIRS):
            return False
        _, ext = os.path.splitext(file_path)
        return ext.lower() in LANGUAGE_MAP

    def get_chunks(self, text: str, file_path: str) -> List[Dict[str, Any]]:
        _, ext = os.path.splitext(file_path)
        lang_name = LANGUAGE_MAP.get(ext.lower())
        
        # Fallback to line-based chunking if language isn't supported by Tree-Sitter
        if not lang_name:
            return self._fallback_line_chunking(text, file_path)

        try:
            language = get_language(lang_name)
            parser = get_parser(lang_name)
            tree = parser.parse(bytes(text, "utf8"))
            
            chunks = []
            current_chunk = []
            current_size = 0
            
            # Walk the top-level nodes (classes, functions, etc.)
            root_node = tree.root_node
            for child in root_node.children:
                node_text = text[child.start_byte:child.end_byte]
                node_size = len(node_text)
                
                # If a single function/class is bigger than max_chunk_size, 
                # we have to split it internally (fallback)
                if node_size > self.max_chunk_size:
                    if current_chunk:
                        chunks.append(self._format_chunk("".join(current_chunk), file_path, len(chunks)))
                        current_chunk = []
                        current_size = 0
                    
                    # Split massive node by lines
                    sub_chunks = self._fallback_line_chunking(node_text, file_path, start_index=len(chunks))
                    chunks.extend(sub_chunks)
                    continue

                if current_size + node_size > self.max_chunk_size:
                    chunks.append(self._format_chunk("".join(current_chunk), file_path, len(chunks)))
                    current_chunk = [node_text]
                    current_size = node_size
                else:
                    current_chunk.append(node_text)
                    current_size += node_size

            if current_chunk:
                chunks.append(self._format_chunk("".join(current_chunk), file_path, len(chunks)))

            return chunks

        except Exception as e:
            logger.error(f"AST Chunking failed for {file_path}, falling back: {e}")
            return self._fallback_line_chunking(text, file_path)

    def _format_chunk(self, content: str, file_path: str, index: int) -> Dict[str, Any]:
        return {
            "file_path": file_path,
            "content": f"File: {file_path}\n\n{content}",
            "chunk_index": index
        }

    def _fallback_line_chunking(self, text: str, file_path: str, start_index: int = 0) -> List[Dict[str, Any]]:
        """Simple line-based splitting for non-code files or massive code blocks."""
        lines = text.splitlines(keepends=True)
        chunks = []
        current_lines = []
        current_len = 0
        
        for line in lines:
            if current_len + len(line) > self.max_chunk_size and current_lines:
                chunks.append(self._format_chunk("".join(current_lines), file_path, start_index + len(chunks)))
                current_lines = []
                current_len = 0
            current_lines.append(line)
            current_len += len(line)
            
        if current_lines:
            chunks.append(self._format_chunk("".join(current_lines), file_path, start_index + len(chunks)))
            
        return chunks

def process_directory(repo_path: str) -> List[Dict[str, Any]]:
    chunker = ASTChunker()
    all_chunks = []
    
    for root, _, files in os.walk(repo_path):
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, repo_path)
            
            if not chunker.is_processable_file(rel_path):
                continue
                
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                all_chunks.extend(chunker.get_chunks(content, rel_path))
            except Exception as e:
                logger.error(f"Error reading {rel_path}: {e}")

    return all_chunks