"""Destinations des messages captés (console, fichier JSONL)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from imo.models import ChatMessage


class Sink(Protocol):
    def write(self, message: ChatMessage) -> None: ...


class ConsoleSink:
    def write(self, message: ChatMessage) -> None:
        hour = message.captured_at[11:19]
        print(f"[{hour}] {message.sender} : {message.text}", flush=True)


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: ChatMessage) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")


class MultiSink:
    def __init__(self, *sinks: Sink) -> None:
        self.sinks = sinks

    def write(self, message: ChatMessage) -> None:
        for sink in self.sinks:
            sink.write(message)
