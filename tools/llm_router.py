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

from anthropic.types import TextBlock

from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

# Parse Response From LLMs
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
        self.qwen_base_url = self._normalize_base_url(os.getenv("QWEN_BASE_URL"))
        self.qwen_model = os.getenv("QWEN_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
        self.claude_model = "claude-sonnet-4-20250514"
        self.gemini_model = "gemini-2.0-flash"
        self.provider_order = self._parse_provider_order(os.getenv("LLM_PROVIDER_ORDER"))
        self.qwen_timeout = httpx.Timeout(
            connect=self._env_float("QWEN_CONNECT_TIMEOUT", 5.0),
            read=self._env_float("QWEN_READ_TIMEOUT", 60.0),
            write=self._env_float("QWEN_WRITE_TIMEOUT", 10.0),
            pool=self._env_float("QWEN_POOL_TIMEOUT", 5.0),
        )

        self.skip_claude = False
        self.skip_gemini = self._env_bool("DISABLE_GEMINI") or self._env_bool("LLM_DISABLE_GEMINI")
        self.skip_qwen = False
        self.qwen_status = "not_configured"
        self.qwen_headers = {"ngrok-skip-browser-warning": "true"}

        # Create Client For Each Models
        if self.gemini_api_key and not self.skip_gemini:
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
                api_key="not-needed",
                default_headers=self.qwen_headers,
                max_retries=0,
                timeout=self.qwen_timeout,
            )
        else:
            self.qwen_client = None

        # Determine Qwen status on init if possible 
        if self.qwen_base_url:
            self.qwen_status = "available"
            try:
                # Fast timeout sync check
                resp = httpx.get(
                    f"{self.qwen_base_url}/v1/models",
                    headers=self.qwen_headers,
                    timeout=3.0,
                    follow_redirects=True,
                )
                if resp.status_code < 500:
                    self.qwen_status = "available"
                else:
                    self.qwen_status = "offline"
            except Exception:
                self.qwen_status = "offline"

    def _normalize_base_url(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        base_url = value.strip().rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        return base_url or None

    def _env_bool(self, name: str) -> bool:
        return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}

    def _env_float(self, name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except ValueError:
            return default

    def _parse_provider_order(self, value: Optional[str]) -> list[str] | None:
        if not value:
            return None
        allowed = {"claude", "gemini", "qwen"}
        order = []
        for item in value.split(","):
            provider = item.strip().lower()
            if provider in allowed and provider not in order:
                order.append(provider)
        return order or None

    def _get_cache_key(self, system_prompt: str, user_prompt: str) -> str:
        combined = f"{system_prompt}|||{user_prompt}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    # Call Claude, if there is auth error, skip_claude will be True
    async def _call_claude(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self.skip_claude or not self.anthropic_client:
            raise Exception("Claude skipped or not configured")
        
        try:
            response = await self.anthropic_client.messages.create(
                model=self.claude_model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": user_prompt
                    }
                ]
            )
            texts: list[str] = []

            for block in response.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)

            return "\n".join(texts)
        except AuthenticationError as e:
            self.skip_claude = True
            raise e
        except (APIStatusError, RateLimitError) as e:
            raise e

    # Call Gemini
    async def _call_gemini(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self.skip_gemini:
            raise Exception("Gemini disabled")
        if not self.gemini_api_key or not self.gemini_client:
            raise Exception("Gemini API key not configured")

        import asyncio
        try:
            client = self.gemini_client
            assert client is not None

            def _sync_call():
                config = genai_types.GenerateContentConfig.model_validate({
                    "system_instruction": system_prompt,
                    "max_output_tokens": max_tokens,
                    "temperature": 0.1,
                })
                response = client.models.generate_content(
                    model=self.gemini_model,
                    contents=user_prompt,
                    config=config
                )
                return response.text

            text = await asyncio.to_thread(_sync_call)
            if text:
                return text
            raise Exception("Gemini returned empty response")
        except Exception as e:
            message = str(e).lower()
            if "quota" in message or "rate limit" in message or "429" in message:
                self.skip_gemini = True
            raise e

    # Call Qwen
    async def _call_qwen(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if self.skip_qwen:
            raise Exception("Qwen skipped after a previous timeout")
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
            return response.choices[0].message.content or ""
        except Exception as e:
            if "timed out" in str(e).lower() or e.__class__.__name__ == "APITimeoutError":
                self.skip_qwen = True
            raise e

    # Choose the order of models
    def _get_routing_order(self, task_type: str) -> List[str]:
        if self.provider_order:
            return self.provider_order

        if self.qwen_base_url:
            return ["qwen", "claude", "gemini"]

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
                    if self.skip_qwen:
                        raise Exception("Qwen skipped after a previous timeout")
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
        priority = {
            provider: index + 1
            for index, provider in enumerate(self._get_routing_order("general"))
        }
        providers = []
        
        # Claude
        claude_status = "available" if self.claude_api_key and not self.skip_claude else "not_configured"
        providers.append({
            "name": "claude",
            "status": claude_status,
            "model": self.claude_model,
            "host": "anthropic",
            "priority": priority.get("claude", 99)
        })
        
        # Gemini
        if self.skip_gemini:
            gemini_status = "disabled"
        else:
            gemini_status = "available" if self.gemini_api_key else "not_configured"
        providers.append({
            "name": "gemini",
            "status": gemini_status,
            "model": self.gemini_model,
            "host": "google",
            "priority": priority.get("gemini", 99)
        })
        
        # Qwen
        qwen_status = "temporarily_unavailable" if self.skip_qwen else self.qwen_status
        providers.append({
            "name": "qwen",
            "status": qwen_status,
            "model": self.qwen_model,
            "host": "ngrok",
            "priority": priority.get("qwen", 99),
            "note": "Hosted via OpenAI-compatible Qwen endpoint"
        })
        
        return sorted(providers, key=lambda provider: provider["priority"])
