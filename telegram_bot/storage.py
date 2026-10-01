"""Small, local persistence for bot subscribers and news-alert delivery state."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_PATH = Path(__file__).with_name("subscribers.json")
_NEWS_SUBSCRIBERS_PATH = Path(__file__).with_name("news_subscribers.json")
_NEWS_STATE_PATH = Path(__file__).with_name("news_state.json")
_LOCK = threading.Lock()


def load_subscribers() -> set[int]:
    with _LOCK:
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            return {int(chat_id) for chat_id in data if str(chat_id).lstrip("-").isdigit()}
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError, TypeError):
            return set()


def save_subscribers(subscribers: set[int]) -> None:
    with _LOCK:
        temporary = _PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(sorted(subscribers), ensure_ascii=True),
            encoding="utf-8",
        )
        os.replace(temporary, _PATH)


def load_news_subscribers() -> set[int]:
    with _LOCK:
        try:
            data = json.loads(_NEWS_SUBSCRIBERS_PATH.read_text(encoding="utf-8"))
            return {int(chat_id) for chat_id in data if str(chat_id).lstrip("-").isdigit()}
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError, TypeError):
            return set()


def save_news_subscribers(subscribers: set[int]) -> None:
    with _LOCK:
        temporary = _NEWS_SUBSCRIBERS_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(sorted(subscribers), ensure_ascii=True),
            encoding="utf-8",
        )
        os.replace(temporary, _NEWS_SUBSCRIBERS_PATH)


def load_news_state() -> dict[str, object]:
    with _LOCK:
        try:
            data = json.loads(_NEWS_STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError, TypeError):
            data = {}

    delivered = data.get("delivered", []) if isinstance(data, dict) else []
    outlooks = data.get("outlooks", {}) if isinstance(data, dict) else {}
    return {
        "delivered": {str(key) for key in delivered if isinstance(key, str)}
        if isinstance(delivered, list)
        else set(),
        "outlooks": {
            str(key): value
            for key, value in outlooks.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        if isinstance(outlooks, dict)
        else {},
    }


def save_news_state(state: dict[str, object]) -> None:
    delivered = state.get("delivered", set())
    outlooks = state.get("outlooks", {})
    serialized = {
        "delivered": sorted(delivered)[-5000:] if isinstance(delivered, set) else [],
        "outlooks": dict(list(outlooks.items())[-500:]) if isinstance(outlooks, dict) else {},
    }
    with _LOCK:
        temporary = _NEWS_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(serialized, ensure_ascii=True),
            encoding="utf-8",
        )
        os.replace(temporary, _NEWS_STATE_PATH)