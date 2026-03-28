import logging
from typing import AsyncGenerator
from google import genai
from app.core.config import settings

logger = logging.getLogger(__name__)

class GeminiClient:
    """
    Production client for Gemini 2.5 Flash.
    Uses the modern `google-genai` SDK for async and streaming generation.
    """
    def __init__(self):
        # Extract the key string (Pydantic SecretStr requires .get_secret_value())
        api_key_val = settings.GEMINI_API_KEY.get_secret_value() if hasattr(settings.GEMINI_API_KEY, 'get_secret_value') else settings.GEMINI_API_KEY
        
        if not api_key_val:
            logger.error("GEMINI_API_KEY is missing from environment/config.")
            raise ValueError("Gemini API Key is strictly required.")
        
        # Initialize the modern GenAI client
        self.client = genai.Client(api_key=api_key_val)
        self.model_name = settings.GEMINI_MODEL

    async def generate_response(self, prompt: str) -> str:
        """Standard asynchronous generation."""
        try:
            logger.info(f"Dispatching prompt to {self.model_name}...")
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            
            if not response.text:
                logger.warning("Received an empty response from Gemini.")
                return "The reasoning engine returned an empty response. Please try refining your query."
                
            return response.text
            
        except Exception as e:
            logger.error(f"Communication with Gemini API failed: {e}", exc_info=True)
            raise RuntimeError(f"LLM Generation failed: {str(e)}")

    async def generate_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Streams the LLM response token-by-token."""
        try:
            logger.info(f"Initiating stream from {self.model_name}...")
            response_stream = await self.client.aio.models.generate_content_stream(
                model=self.model_name,
                contents=prompt
            )
            
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
                    
            logger.debug("Successfully completed LLM stream.")
            
        except Exception as e:
            logger.error(f"Streaming from Gemini API failed: {e}", exc_info=True)
            yield f"\n\n[System Error: LLM Generation failed - {str(e)}]"

# Singleton instance
gemini_client = GeminiClient()