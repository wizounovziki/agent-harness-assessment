import json

import httpx
import pytest
from openai import AsyncOpenAI

from scripts.build_mock_dataset import final
from src.errors import ModelProviderError
from src.llm_client import OpenAILLMProvider
from src.schemas import ChatMessage, LLMAction


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
    finally:
        await provider.aclose()


def test_real_mode_requires_credentials_and_model():
    with pytest.raises(ValueError, match="Real mode requires"):
        OpenAILLMProvider("", "")
