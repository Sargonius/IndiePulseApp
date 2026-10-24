"""Translation and letters with the user's own key: OpenAI or Claude, then DeepL or the free translators."""
from types import SimpleNamespace

import pytest

from app.services import anthropic_client, llm, machine_translate


class FakeAnthropic:
    """anthropic.Anthropic stand-in: records the request, answers like the Messages API."""
    sent: list = []
    answer = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text="Hello "), SimpleNamespace(type="text", text="world")])

    def __init__(self, **kw):
        self.messages = SimpleNamespace(create=self._create("messages"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create("beta")))

    def _create(self, kind):
        def create(**request):
            FakeAnthropic.sent.append((kind, request))
            return FakeAnthropic.answer
        return create


@pytest.fixture
def claude(monkeypatch):
    import anthropic
    FakeAnthropic.sent = []
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    return FakeAnthropic


def test_which_ai_answers(set_settings):
    assert llm.provider() is None
    set_settings(anthropic_api_key="sk-ant-x")
    assert llm.provider() == "anthropic"
    set_settings(openai_api_key="sk-x")
    assert llm.provider() == "openai"  # auto: OpenAI first, as before Claude was there
    set_settings(llm_provider="anthropic")
    assert llm.provider() == "anthropic"
    set_settings(anthropic_api_key="")
    assert llm.provider() is None  # picked, but no key: not silently the other one


def test_claude_gets_the_same_prompt(claude, set_settings):
    set_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x")
    text, label = llm.chat([{"role": "system", "content": "Translate to English."},
                            {"role": "user", "content": "Привет мир"}], temperature=0.2, max_tokens=50)
    assert (text, label) == ("Hello world", "claude:claude-opus-5-5")  # text blocks only, thinking skipped
    kind, request = claude.sent[-1]
    assert kind == "beta" and request["betas"] == ["server-side-fallback-2026-07-01"] and request["fallbacks"] == "default"
    assert request["system"] == "Translate to English." and request["messages"] == [{"role": "user", "content": "Привет мир"}]
    assert "temperature" not in request and request["max_tokens"] == 16000  # room for its thinking
    set_settings(anthropic_model="claude-haiku-5-5")  # no server-side fallback there
    llm.chat([{"role": "user", "content": "hi"}])
    kind, request = claude.sent[-1]
    assert kind == "messages" and "fallbacks" not in request and request["model"] == "claude-haiku-5-5"


def test_claude_declining_is_an_error_not_an_answer(claude, set_settings, monkeypatch):
    set_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x")
    monkeypatch.setattr(claude, "answer", SimpleNamespace(stop_reason="refusal", model="claude-opus-5-5", content=[]))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(anthropic_client.ClaudeError, match="no API key"):
        anthropic_client.chat([{"role": "user", "content": "hi"}], api_key=" ")


def test_answers_wrapped_around_json():
    assert llm.json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.json_object('Here you go: {"a": {"b": 2}} Enjoy!') == {"a": {"b": 2}}
    assert llm.json_object("no json") is None and llm.json_object("[1, 2]") is None


def test_deepl_first_then_the_free_translators(set_settings, monkeypatch):
    import deep_translator
    calls = []

    class Google:
        def __init__(self, source, target):
            self.target = target

        def translate(self, text):
            calls.append(("google", text))
            return f"[{self.target}] {text}"

    def deepl_post(url, data, headers, timeout):
        calls.append(("deepl", url, data, headers["Authorization"]))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"translations": [{"text": "Hello world"}]})
    monkeypatch.setattr(deep_translator, "GoogleTranslator", Google)
    monkeypatch.setattr(machine_translate.httpx, "post", deepl_post)
    assert machine_translate.translate("Hola", "en") == ("[en] Hola", "google")  # no DeepL key
    set_settings(deepl_api_key="abc:fx")
    assert machine_translate.translate("Привет мир", "en", "ru") == ("Hello world", "deepl")
    _, url, data, auth = calls[-1]
    assert url == "https://api-free.deepl.com/v2/translate" and auth == "DeepL-Auth-Key abc:fx"
    assert data == {"text": "Привет мир", "target_lang": "EN-US", "source_lang": "RU"}

    def broken(*a, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(machine_translate.httpx, "post", broken)
    assert machine_translate.translate("Hola", "en") == ("[en] Hola", "google")


def test_a_translation_still_in_russian_is_not_one():
    assert machine_translate.looks_translated("Привет", "Hello", "en")
    assert not machine_translate.looks_translated("Привет", "Привет", "en")
    assert not machine_translate.looks_translated("Привет мир", "Hello мир", "en")
    assert machine_translate.looks_translated("Hello", "Привет", "ru")
    assert not machine_translate.looks_translated("Hello", "", "ru")


def test_the_translate_button_uses_claude(client, claude, set_settings):
    set_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x")
    html = client.post("/api/translate", data={"text_ru": "Привет мир"}).text
    assert "Hello world" in html and 'data-translator="claude:claude-opus-5-5"' in html
    test = client.get("/api/translate/test_claude", params={"anthropic_model": "claude-sonnet-5-5"}).json()
    assert test["ok"] and claude.sent[-1][1]["model"] == "claude-sonnet-5-5"


def test_the_settings_form_keeps_the_new_keys(client):
    from app.services.settings import load_settings
    client.post("/api/settings/save", data={"llm_provider": "anthropic", "anthropic_api_key": "sk-ant-new",
                                            "anthropic_model": "claude-sonnet-5-5", "deepl_api_key": "d:fx"})
    cfg = load_settings()
    assert (cfg["llm_provider"], cfg["anthropic_api_key"], cfg["anthropic_model"], cfg["deepl_api_key"]) == \
        ("anthropic", "sk-ant-new", "claude-sonnet-5-5", "d:fx")
    page = client.get("/accounts").text
    assert "Test Claude" in page and "•••-new" in page and "sk-ant-new" not in page
    status = client.get("/api/settings/status").json()
    assert "Claude claude-sonnet-5-5 + DeepL" in str(status)
