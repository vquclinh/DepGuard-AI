import httpx
from openai import AsyncOpenAI


class OpenRouterQwenClient:
    """OpenRouter-hosted Qwen chat client used behind DepGuard's qwen provider."""

    DEFAULT_MODEL = "qwen/qwen3-coder-30b-a3b-instruct"
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        timeout: httpx.Timeout | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.client = (
            AsyncOpenAI(
                base_url=self.BASE_URL,
                api_key=api_key,
                default_headers={
                    "HTTP-Referer": "https://github.com/depguard-ai/depguard-ai",
                    "X-Title": "DepGuard AI",
                },
                max_retries=0,
                timeout=timeout,
            )
            if api_key
            else None
        )

    @property
    def status(self) -> str:
        return "available" if self.client else "not_configured"

    async def complete(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if not self.client:
            raise Exception("OpenRouter API key not configured")

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise Exception(
                "OpenRouter response hit the output token limit before completing JSON. "
                "Increase QWEN_MAX_TOKENS or reduce requested replacement size."
            )
        return choice.message.content or ""
