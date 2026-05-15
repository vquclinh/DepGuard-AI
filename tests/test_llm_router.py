import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from tools.llm_router import LLMRouter, FallbackExhaustedError, _RESPONSE_CACHE, LLMResponse
from anthropic import RateLimitError, AuthenticationError

@pytest.fixture(autouse=True)
def clear_cache():
    _RESPONSE_CACHE.clear()

@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("QWEN_BASE_URL", "http://test-qwen")

@pytest.fixture
def router_setup(mock_env):
    with patch("httpx.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        router = LLMRouter()
        return router

@pytest.mark.asyncio
async def test_claude_success_path(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(return_value="Claude response")
    router._call_gemini = AsyncMock()
    router._call_qwen = AsyncMock()
    
    resp = await router.complete("sys", "user")
    
    assert resp.content == "Claude response"
    assert resp.provider == "claude"
    assert resp.fallback_used is False
    router._call_claude.assert_called_once()
    router._call_gemini.assert_not_called()

@pytest.mark.asyncio
async def test_claude_rate_limit_fallback_to_gemini(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(side_effect=RateLimitError("Rate limited", response=MagicMock(), body=None))
    router._call_gemini = AsyncMock(return_value="Gemini response")
    router._call_qwen = AsyncMock()
    
    resp = await router.complete("sys", "user")
    
    assert resp.content == "Gemini response"
    assert resp.provider == "gemini"
    assert resp.fallback_used is True
    router._call_claude.assert_called_once()
    router._call_gemini.assert_called_once()

@pytest.mark.asyncio
async def test_claude_gemini_fail_fallback_to_qwen(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(side_effect=Exception("Claude down"))
    router._call_gemini = AsyncMock(side_effect=Exception("Gemini down"))
    router._call_qwen = AsyncMock(return_value="Qwen response")
    
    resp = await router.complete("sys", "user")
    
    assert resp.content == "Qwen response"
    assert resp.provider == "qwen"
    assert resp.fallback_used is True
    router._call_claude.assert_called_once()
    router._call_gemini.assert_called_once()
    router._call_qwen.assert_called_once()

@pytest.mark.asyncio
async def test_all_fail_exhausted(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(side_effect=Exception("Claude down"))
    router._call_gemini = AsyncMock(side_effect=Exception("Gemini down"))
    router._call_qwen = AsyncMock(side_effect=Exception("Qwen down"))
    
    with pytest.raises(FallbackExhaustedError) as exc:
        await router.complete("sys", "user")
    
    assert "All configured LLM providers failed" in str(exc.value)
    assert "Claude down" in str(exc.value)

@pytest.mark.asyncio
async def test_cache_hit(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(return_value="First call")
    router._call_qwen = AsyncMock()
    
    resp1 = await router.complete("sys", "same user prompt")
    assert resp1.content == "First call"
    
    router._call_claude.reset_mock()
    
    resp2 = await router.complete("sys", "same user prompt")
    assert resp2.content == "First call"
    router._call_claude.assert_not_called()

@pytest.mark.asyncio
async def test_task_type_patch_simple_qwen_first(router_setup):
    router = router_setup
    router._call_claude = AsyncMock()
    router._call_gemini = AsyncMock()
    router._call_qwen = AsyncMock(return_value="Qwen easy patch")
    
    resp = await router.complete("sys", "user", task_type="patch_simple")
    
    assert resp.provider == "qwen"
    assert resp.fallback_used is False
    router._call_qwen.assert_called_once()
    router._call_claude.assert_not_called()
    router._call_gemini.assert_not_called()

@pytest.mark.asyncio
async def test_task_type_patch_complex_claude_first(router_setup):
    router = router_setup
    router.provider_order = ["claude", "gemini", "qwen"]
    router._call_claude = AsyncMock(return_value="Claude complex patch")
    router._call_gemini = AsyncMock()
    router._call_qwen = AsyncMock()
    
    resp = await router.complete("sys", "user", task_type="patch_complex")
    
    assert resp.provider == "claude"
    assert resp.fallback_used is False
    router._call_claude.assert_called_once()
    router._call_qwen.assert_not_called()

@pytest.mark.asyncio
async def test_qwen_is_first_when_configured_by_default(router_setup):
    router = router_setup
    router._call_qwen = AsyncMock(return_value="Qwen default")
    router._call_claude = AsyncMock()
    router._call_gemini = AsyncMock()

    resp = await router.complete("sys", "user")

    assert resp.provider == "qwen"
    assert resp.fallback_used is False
    router._call_qwen.assert_called_once()
    router._call_claude.assert_not_called()
    router._call_gemini.assert_not_called()


def test_provider_status_order_matches_routing(router_setup):
    providers = router_setup.get_providers_status()

    assert [provider["name"] for provider in providers] == ["qwen", "claude", "gemini"]
    assert providers[0]["priority"] == 1

@pytest.mark.asyncio
async def test_qwen_skipped_gracefully_when_no_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    
    router = LLMRouter()
    router._call_claude = AsyncMock(side_effect=Exception("Claude fail"))
    router._call_gemini = AsyncMock(side_effect=Exception("Gemini fail"))
    
    with pytest.raises(FallbackExhaustedError) as exc:
        await router.complete("sys", "user")
        
    assert "status is not_configured" in str(exc.value)


@pytest.mark.asyncio
async def test_qwen_timeout_is_skipped_for_followup_calls(router_setup):
    router = router_setup
    router.provider_order = ["qwen"]
    router._call_qwen = AsyncMock(side_effect=[Exception("Request timed out."), Exception("should not happen")])

    with pytest.raises(FallbackExhaustedError):
        await router.complete("sys", "user 1")

    router.skip_qwen = True
    with pytest.raises(FallbackExhaustedError) as exc:
        await router.complete("sys", "user 2")

    assert "Qwen skipped after a previous timeout" in str(exc.value)
