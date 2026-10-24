import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core.filelock import FileLock
from app.core.redact import redact
from app.services import settings as settings_mod
from app.services.settings import load_settings, save_settings


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


# ── redaction ────────────────────────────────────────────────────────────────

def test_redact_url_embedded_secrets():
    assert "AAH" not in redact("GET https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getMe")
    out = redact("POST https://discord.com/api/webhooks/1207050866398462063/abc-DEF_123?wait=true -> 404")
    assert "abc-DEF_123" not in out and "1207050866398462063" in out
    assert "sekret" not in redact("x?client_secret=sekret&a=1")


def test_redact_values_from_settings(set_settings):
    set_settings(openai_api_key="sk-live-very-secret-key")
    assert redact("401 for key sk-live-very-secret-key") == "401 for key •••"


# ── settings file safety ─────────────────────────────────────────────────────

def test_save_refuses_to_overwrite_corrupt_settings(settings_file):
    settings_file.write_text("{not json", encoding="utf-8")
    save_settings({"tg_chat_id_ru": "1"})
    assert settings_file.read_text(encoding="utf-8") == "{not json"


def test_concurrent_saves_keep_every_key(settings_file):
    keys = [k for k in settings_mod.DEFAULTS if isinstance(settings_mod.DEFAULTS[k], str)][:16]
    errors = []

    def save(k, rnd):
        try:
            save_settings({k: f"v{rnd}-{k}"})
        except Exception as exc:  # a lock/replace failure must not happen at all
            errors.append(repr(exc))

    for rnd in range(10):  # the Windows lock-file race needed a few rounds to show up
        settings_file.write_text("{}", encoding="utf-8")
        threads = [threading.Thread(target=save, args=(k, rnd)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cfg = load_settings()
        assert [k for k in keys if cfg[k] != f"v{rnd}-{k}"] == []
    assert errors == []


def test_filelock_breaks_stale_lock_and_times_out():
    lock = FileLock("unit-test", timeout=0.3, stale=60)
    os.makedirs(os.path.dirname(lock.path), exist_ok=True)
    with open(lock.path, "w") as f:
        f.write("999999")
    with pytest.raises(TimeoutError):
        lock.acquire()
    old = time.time() - 3600
    os.utime(lock.path, (old, old))
    with lock:
        assert os.path.exists(lock.path)
    assert not os.path.exists(lock.path)


# ── web hardening ────────────────────────────────────────────────────────────

def test_cross_site_post_is_blocked(client):
    r = client.post("/api/settings/save", data={"tg_chat_id_ru": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/settings/save", data={"tg_chat_id_ru": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client):
    r = client.post("/api/health/run?only=openai", headers={"Origin": "http://127.0.0.1:8000",
                                                           "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert r.json()["results"][0]["status"] == "off"


def test_foreign_host_header_is_rejected(client):
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_oauth_error_param_is_escaped(client):
    r = client.get("/api/accounts/callback/youtube", params={"error": "<script>alert(1)</script>"})
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_oauth_callbacks_require_matching_state(client, set_settings):
    set_settings(oauth_state_x="expected-state")
    r = client.get("/api/accounts/callback/x", params={"code": "c", "state": "forged"})
    assert r.status_code == 400
    assert load_settings()["oauth_state_x"] == "expected-state"  # not consumed by a forged call


def test_status_page_and_last_report(client):
    assert client.get("/status").status_code == 200
    r = client.get("/api/health/last")
    assert r.status_code == 200 and "history" in r.json()


def test_health_run_with_nothing_configured_posts_nothing(client):
    r = client.post("/api/health/run")
    statuses = {x["id"]: x["status"] for x in r.json()["results"]}
    assert all(v == "off" for k, v in statuses.items() if k != "local")
