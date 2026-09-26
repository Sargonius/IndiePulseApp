"""openai_client drops parameters a model rejects, so any model name in Settings works."""
import json

import httpx
import pytest

from app.services import openai_client


@pytest.fixture
def fake_openai(monkeypatch, set_settings):
    set_settings(openai_api_key="sk-test", openai_model="some-model")
    monkeypatch.setattr(openai_client, "_unsupported", {})
    calls = []
    rejects = set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        for p in rejects:
            if p in body:
                if p == "reasoning_effort":  # verbatim from gpt-4o-mini (2026-09-26)
                    err = {"message": "Unrecognized request argument supplied: reasoning_effort",
                           "type": "invalid_request_error", "param": None, "code": None}
                else:
                    err = {"message": f"Unsupported parameter: '{p}' is not supported with this model.",
                           "type": "invalid_request_error", "param": p, "code": "unsupported_parameter"}
                return httpx.Response(400, json={"error": err})
        return httpx.Response(200, json={"choices": [{"message": {"content": " Hello world "}}]})

    real_client = httpx.Client
    monkeypatch.setattr(openai_client.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return calls, rejects


def test_old_model_without_reasoning_effort(fake_openai):
    calls, rejects = fake_openai
    rejects.add("reasoning_effort")  # gpt-4o-mini style
    r, model = openai_client.chat_completion([{"role": "user", "content": "x"}], temperature=0.2)
    assert r.status_code == 200 and openai_client.content_of(r) == "Hello world" and model == "some-model"
    assert "reasoning_effort" in calls[0] and "reasoning_effort" not in calls[1]
    assert calls[1]["temperature"] == 0.2
    # remembered: next call goes straight through
    openai_client.chat_completion([{"role": "user", "content": "y"}], temperature=0.2)
    assert len(calls) == 3 and "reasoning_effort" not in calls[2]


def test_reasoning_model_without_temperature(fake_openai):
    calls, rejects = fake_openai
    rejects.add("temperature")
    r, _ = openai_client.chat_completion([{"role": "user", "content": "x"}], temperature=0.2)
    assert r.status_code == 200
    assert calls[-1]["reasoning_effort"] == openai_client.REASONING_EFFORT and "temperature" not in calls[-1]


def test_model_override(fake_openai):
    calls, _ = fake_openai
    r, model = openai_client.chat_completion([{"role": "user", "content": "x"}], model="gpt-6-luna")
    assert r.status_code == 200 and model == calls[0]["model"] == "gpt-6-luna" and len(calls) == 1


def test_other_errors_are_not_retried(monkeypatch, set_settings):
    set_settings(openai_api_key="sk-test")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={"error": {"code": "model_not_found", "message": "no such model"}})

    real_client = httpx.Client
    monkeypatch.setattr(openai_client.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    r, _ = openai_client.chat_completion([{"role": "user", "content": "x"}], temperature=0)
    assert r.status_code == 404 and len(calls) == 1
