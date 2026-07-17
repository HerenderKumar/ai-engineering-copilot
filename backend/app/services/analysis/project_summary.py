"""
Project summary — LLM-written when a key is available, heuristic otherwise.

Fixes two latent bugs in the original:
  1. It imported `ask_gemini`, which didn't exist anywhere → ImportError.
  2. Its prompt contained only the repository *path* — the LLM can't read
     your disk, so it could only hallucinate. We now inline the README and a
     top-level file listing (actual evidence).
"""

import logging
import os

logger = logging.getLogger(__name__)

_README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md")
_MAX_README_CHARS = 6000


def _read_readme(repo_path: str) -> str:
    for name in _README_NAMES:
        candidate = os.path.join(repo_path, name)
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()[:_MAX_README_CHARS]
            except Exception:
                pass
    return ""


def _top_level_listing(repo_path: str) -> str:
    try:
        entries = sorted(os.listdir(repo_path))[:40]
        return ", ".join(entries)
    except Exception:
        return ""


def get_project_summary(repo_path: str) -> str:
    readme = _read_readme(repo_path)
    listing = _top_level_listing(repo_path)

    try:
        from app.services.llm.router import ask_llm
        prompt = (
            "You are an expert software architect. Based on the evidence below, "
            "explain in simple, beginner-friendly language: (1) what this project "
            "does, (2) who it is for, (3) the main technologies used. "
            "Be concise (one short paragraph per point). Only use the evidence.\n\n"
            f"TOP-LEVEL CONTENTS: {listing}\n\n"
            f"README (may be truncated):\n{readme or '(no README found)'}"
        )
        return ask_llm(prompt)
    except Exception as e:
        logger.info(f"LLM summary unavailable, using heuristic fallback: {e}")
        if readme:
            first_para = next(
                (p.strip() for p in readme.split("\n\n")
                 if p.strip() and not p.strip().startswith(("#", "<", "["))),
                "")
            if first_para:
                return first_para[:600]
        return (f"This repository contains: {listing}. "
                "No README was found and no LLM key is configured, so this is a "
                "structural summary only.") if listing else "Repository is empty or unreadable."
