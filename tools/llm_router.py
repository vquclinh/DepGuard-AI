import os
import time
import json
import logging
import hashlib
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from dotenv import load_dotenv

import httpx

import anthropic
from anthropic import AsyncAnthropic, APIStatusError, RateLimitError, AuthenticationError

import google.genai as genai
from google.genai import types as genai_types

from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    latency_ms: int
    fallback_used: bool

class FallbackExhaustedError(Exception):
    """Raised when all configured LLM providers fail."""
    pass

# Global module-level cache
_RESPONSE_CACHE: Dict[str, Tuple[LLMResponse, float]] = {}
CACHE_TTL_SECONDS = 3600  # 1 hour

class LLMRouter:
    def __init__(self):
        self.claude_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.qwen_base_url = os.getenv("QWEN_BASE_URL")
        self.qwen_model = os.getenv("QWEN_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
        self.claude_model = "claude-sonnet-4-20250514"
        self.gemini_model = "gemini-2.0-flash"

        self.skip_claude = False
        self.qwen_status = "not_configured"

        if self.gemini_api_key:
            self.gemini_client = genai.Client(api_key=self.gemini_api_key)
        else:
            self.gemini_client = None

        if self.claude_api_key:
            self.anthropic_client = AsyncAnthropic(api_key=self.claude_api_key)
        else:
            self.anthropic_client = None
            self.skip_claude = True

        if self.qwen_base_url:
            self.qwen_client = AsyncOpenAI(
                base_url=f"{self.qwen_base_url}/v1",
                api_key="not-needed"
            )
        else:
            self.qwen_client = None

        # Determine Qwen status on init if possible, 
        # though synchronous ping might block, we will do a fast sync request or just assume available if set.
        # Actually, the spec says "On LLMRouter __init__, ping GET {QWEN_BASE_URL}/v1/models"
        if self.qwen_base_url:
            try:
                # Fast timeout sync check
                resp = httpx.get(f"{self.qwen_base_url}/v1/models", timeout=2.0)
                if resp.status_code == 200:
                    self.qwen_status = "available"
                else:
                    self.qwen_status = "offline"
            except Exception:
                self.qwen_status = "offline"

    def _get_cache_key(self, system_prompt: str, user_prompt: str) -> str:
        combined = f"{system_prompt}|||{user_prompt}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    async def _call_claude(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self.skip_claude or not self.anthropic_client:
            raise Exception("Claude skipped or not configured")
        
        try:
            response = await self.anthropic_client.messages.create(
                model=self.claude_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            return response.content[0].text
        except AuthenticationError as e:
            self.skip_claude = True
            raise e
        except (APIStatusError, RateLimitError) as e:
            raise e

    async def _call_gemini(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if not self.gemini_api_key or not self.gemini_client:
            raise Exception("Gemini API key not configured")

        import asyncio
        try:
            def _sync_call():
                response = self.gemini_client.models.generate_content(
                    model=self.gemini_model,
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        max_output_tokens=max_tokens,
                        temperature=0.1
                    )
                )
                return response.text

            text = await asyncio.to_thread(_sync_call)
            if text:
                return text
            raise Exception("Gemini returned empty response")
        except Exception as e:
            raise e

    async def _call_qwen(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self.qwen_status in ["offline", "not_configured"] or not self.qwen_client:
            raise Exception(f"Qwen skipped: status is {self.qwen_status}")

        try:
            response = await self.qwen_client.chat.completions.create(
                model=self.qwen_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=max_tokens,
                temperature=0.1
            )
            return response.choices[0].message.content
        except Exception as e:
            raise e

    def _get_routing_order(self, task_type: str) -> List[str]:
        if task_type == "patch_simple":
            return ["qwen", "gemini", "claude"]
        else: # "changelog", "patch_complex", "general"
            return ["claude", "gemini", "qwen"]

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1000,
        task_type: str = "general"
    ) -> LLMResponse:
        
        # Check cache
        cache_key = self._get_cache_key(system_prompt, user_prompt)
        current_time = time.time()
        if cache_key in _RESPONSE_CACHE:
            cached_resp, timestamp = _RESPONSE_CACHE[cache_key]
            if current_time - timestamp < CACHE_TTL_SECONDS:
                logger.info("LLMRouter: Cache hit")
                return cached_resp

        routing_order = self._get_routing_order(task_type)
        errors = []
        fallback_used = False

        start_time = time.time()

        for idx, provider in enumerate(routing_order):
            try:
                if idx > 0:
                    fallback_used = True
                    
                content = ""
                model_used = ""
                
                if provider == "claude":
                    content = await self._call_claude(system_prompt, user_prompt, max_tokens)
                    model_used = self.claude_model
                elif provider == "gemini":
                    content = await self._call_gemini(system_prompt, user_prompt, max_tokens)
                    model_used = self.gemini_model
                elif provider == "qwen":
                    content = await self._call_qwen(system_prompt, user_prompt, max_tokens)
                    model_used = self.qwen_model
                    
                latency_ms = int((time.time() - start_time) * 1000)
                
                response = LLMResponse(
                    content=content,
                    provider=provider,
                    model=model_used,
                    latency_ms=latency_ms,
                    fallback_used=fallback_used
                )
                
                # Cache success
                _RESPONSE_CACHE[cache_key] = (response, time.time())
                return response
                
            except Exception as e:
                logger.warning(f"LLMRouter: Provider '{provider}' failed: {e}")
                errors.append(f"{provider}: {str(e)}")

        raise FallbackExhaustedError(f"All configured LLM providers failed. Details: {', '.join(errors)}")

    def get_providers_status(self) -> List[dict]:
        providers = []
        
        # Claude
        claude_status = "available" if self.claude_api_key and not self.skip_claude else "not_configured"
        providers.append({
            "name": "claude",
            "status": claude_status,
            "model": self.claude_model,
            "host": "anthropic",
            "priority": 1
        })
        
        # Gemini
        gemini_status = "available" if self.gemini_api_key else "not_configured"
        providers.append({
            "name": "gemini",
            "status": gemini_status,
            "model": self.gemini_model,
            "host": "google",
            "priority": 2
        })
        
        # Qwen
        providers.append({
            "name": "qwen",
            "status": self.qwen_status,
            "model": self.qwen_model,
            "host": "kaggle",
            "priority": 3,
            "note": "Hosted on Kaggle GPU via ngrok"
        })
        
        return providers
