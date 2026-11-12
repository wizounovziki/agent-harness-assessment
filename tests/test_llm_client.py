import json

import httpx
import pytest
from openai import AsyncOpenAI

from scripts.build_mock_dataset import final
from src.config import Settings
from src.errors import ModelProviderError
from src.llm_client import DeepSeekLLMProvider, OpenAILLMProvider
from src.schemas import ChatMessage, LLMAction
from src.service import RunManager


async def test_real_sdk_serialization_and_json_response_contract():
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(final("Verified status report.")),
                        },
                    }
                ],
            },
        )

    client = AsyncOpenAI(
        api_key="test-only-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    provider = OpenAILLMProvider("test-only-key", "test-model", client=client)
    try:
        response = await provider.generate([ChatMessage(role="user", content="Return JSON.")], 1)
        assert LLMAction.model_validate_json(response).action_type == "FINAL_ANSWER"
        assert captured[0]["response_format"] == {"type": "json_object"}
        assert captured[0]["model"] == "test-model"
    finally:
        await provider.aclose()


async def test_sdk_automatic_retries_are_disabled():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500, json={"error": {"message": "Injected API failure"}})

    client = AsyncOpenAI(
        api_key="test-only-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    provider = OpenAILLMProvider("test-only-key", "test-model", client=client)
    try:
        with pytest.raises(ModelProviderError, match="HTTP 500"):
            await provider.generate([ChatMessage(role="user", content="Return JSON.")], 1)
        assert len(requests) == 1
        assert provider.metrics[0]["http_status"] == 500
        assert "Injected API failure" not in json.dumps(list(provider.metrics))
    finally:
        await provider.aclose()


def test_real_mode_requires_credentials_and_model():
    with pytest.raises(ValueError, match="Real mode requires"):
        OpenAILLMProvider("", "")


async def test_deepseek_profile_uses_documented_endpoint_and_safe_metrics():
    captured = []

    def handle(request):
        captured.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "id": "deepseek-test-request",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(final("Verified."))},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    client = AsyncOpenAI(
        api_key="fixture-credential",
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    provider = DeepSeekLLMProvider("fixture-credential", "deepseek-flash", client=client)
    try:
        await provider.generate([ChatMessage(role="user", content="Private objective: JSON.")], 1)
        url, body = captured[0]
        assert url == "https://api.deepseek.com/chat/completions"
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == 0 and body["max_tokens"] == 2048
        assert body["response_format"] == {"type": "json_object"}
        assert provider.metrics[0]["usage"]["total_tokens"] == 120
        assert provider.metrics[0]["response_model"] == "deepseek-flash"
        metadata = json.dumps(list(provider.metrics))
        assert "fixture-credential" not in metadata and "Private objective" not in metadata
        assert provider.metrics.maxlen == 256
    finally:
        await provider.aclose()


def test_provider_credentials_are_selected_by_profile(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "other-provider-fixture")
    settings = Settings.from_env()
    assert settings.llm_api_key == "deepseek-fixture"
    assert "deepseek-fixture" not in repr(settings)
    monkeypatch.setenv("LLM_API_KEY", "explicit-fixture")
    assert Settings.from_env().llm_api_key == "explicit-fixture"


def test_invalid_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "unsupported")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings.from_env()


async def test_run_manager_selects_deepseek_without_network(tmp_path):
    manager = RunManager(
        Settings(
            llm_mode="real",
            llm_provider="deepseek",
            llm_model="deepseek-flash",
            llm_api_key="fixture-credential",
            database_path=tmp_path / "state.sqlite3",
            trace_dir=tmp_path / "traces",
        )
    )
    try:
        assert isinstance(manager.harness_for("healthy").llm, DeepSeekLLMProvider)
    finally:
        await manager.close()
