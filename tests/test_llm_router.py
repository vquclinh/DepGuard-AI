import pytest
from unittest.mock import AsyncMock, MagicMock
from tools.llm_router import LLMRouter, FallbackExhaustedError, _RESPONSE_CACHE, LLMResponse
from anthropic import RateLimitError, AuthenticationError

@pytest.fixture(autouse=True)
def clear_cache():
    _RESPONSE_CACHE.clear()

@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    monkeypatch.setenv("QWEN_MODEL", "qwen/qwen3-coder-30b-a3b-instruct")
    monkeypatch.setenv("ENABLE_CLAUDE", "true")
    monkeypatch.setenv("ENABLE_GEMINI", "true")
    monkeypatch.setenv("ENABLE_QWEN", "true")
    monkeypatch.delenv("DISABLE_GEMINI", raising=False)
    monkeypatch.delenv("LLM_DISABLE_GEMINI", raising=False)

@pytest.fixture
def router_setup(mock_env):
    return LLMRouter()

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
    router.cache_enabled = True
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
    router.provider_order = ["qwen", "gemini", "claude"]
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
async def test_quality_order_is_default_even_when_qwen_is_configured(router_setup):
    router = router_setup
    router._call_claude = AsyncMock(return_value="Claude default")
    router._call_qwen = AsyncMock(return_value="Qwen default")
    router._call_gemini = AsyncMock()

    resp = await router.complete("sys", "user")

    assert resp.provider == "claude"
    assert resp.fallback_used is False
    router._call_claude.assert_called_once()
    router._call_qwen.assert_not_called()
    router._call_gemini.assert_not_called()


def test_provider_status_order_matches_routing(router_setup):
    providers = router_setup.get_providers_status()

    assert [provider["name"] for provider in providers] == ["claude", "gemini", "qwen"]
    assert providers[0]["priority"] == 1


def test_env_switches_can_make_qwen_the_only_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    monkeypatch.setenv("ENABLE_CLAUDE", "false")
    monkeypatch.setenv("ENABLE_GEMINI", "false")
    monkeypatch.setenv("ENABLE_QWEN", "true")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "claude,gemini,qwen")

    router = LLMRouter()

    assert router._get_routing_order("patch_complex") == ["qwen"]
    providers = {provider["name"]: provider for provider in router.get_providers_status()}
    assert providers["claude"]["status"] == "disabled"
    assert providers["gemini"]["status"] == "disabled"
    assert providers["qwen"]["status"] == "available"

@pytest.mark.asyncio
async def test_qwen_skipped_gracefully_when_no_openrouter_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    
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
