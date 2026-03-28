from app.services.llm.gemini import ask_gemini


def get_project_summary(repo_path: str) -> str:
    """
    Uses Gemini to generate a smart project summary.
    """

    prompt = f"""
    You are an expert software architect.

    Analyze the following repository and explain in simple beginner-friendly language:

    1. What the project does
    2. Who it is for
    3. Main technologies used
    4. Why it is important

    Repository path:
    {repo_path}
    """

    return ask_gemini(prompt)
