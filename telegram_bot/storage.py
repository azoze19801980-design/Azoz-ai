"""Small, local persistence for automatic-signal subscribers."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_PATH = Path(__file__).with_name("subscribers.json")
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