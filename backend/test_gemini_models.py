from google import genai
import os

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

models = client.models.list()

for m in models:
    print("MODEL:", m.name)
    print("ACTIONS:", getattr(m, "supported_actions", "N/A"))
    print("-" * 40)
