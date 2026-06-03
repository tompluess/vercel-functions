"""Unit tests for TelegramNotifier — verify URL/token construction, payload
shape, and the best-effort (never-raises) contract on transport errors."""

import io
import json
from urllib import error as urlerror

import pytest

import api.telegram_notifier as tg_mod
from api.telegram_notifier import TelegramNotifier
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def calls(monkeypatch):
    state: dict = {"calls": []}

    def fake_urlopen(req, timeout=None):
        state["calls"].append({
            "url": req.full_url,
            "method": req.get_method(),
            "payload": json.loads(req.data) if req.data else None,
            "headers": dict(req.header_items()),
        })
        return FakeUrlopenResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(tg_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def notifier():
    return TelegramNotifier(bot_token="123:ABC", chat_id="-1002342319319")


def test_notify_posts_to_send_message_with_token_in_path(notifier, calls):
    ok = notifier.notify("boom")
    assert ok is True
    call = calls["calls"][0]
    # Bot token rides in the URL path; chat + text in the JSON body.
    assert call["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call["method"] == "POST"
    assert call["payload"] == {"chat_id": "-1002342319319", "text": "boom"}


def test_notify_swallows_http_error_and_returns_false(notifier, monkeypatch):
    """A Telegram 4xx/5xx must not raise — the caller is already inside an
    error path (or just finished a successful sync) and must not be disrupted."""
    def boom(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 400, "Bad Request", {},
                                 fp=io.BytesIO(b'{"description":"chat not found"}'))

    monkeypatch.setattr(tg_mod.urlrequest, "urlopen", boom)
    assert notifier.notify("hi") is False


def test_notify_swallows_url_error_and_returns_false(notifier, monkeypatch):
    def boom(req, timeout=None):
        raise urlerror.URLError("telegram unreachable")

    monkeypatch.setattr(tg_mod.urlrequest, "urlopen", boom)
    assert notifier.notify("hi") is False
