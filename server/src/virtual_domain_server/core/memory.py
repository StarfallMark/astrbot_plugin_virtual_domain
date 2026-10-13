"""Persistent world experiences with embedding and rerank retrieval."""

from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .storage import SQLiteStore


def _estimated_payload_tokens(payload: dict) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(char) < 128 for char in text)
    other_chars = len(text) - ascii_chars
    estimate = (ascii_chars + 2) // 3 + other_chars
    return max(1, estimate + max(128, (estimate + 3) // 4))


class WorldMemory:
    def __init__(
        self,
        store: SQLiteStore,
        embedding: dict,
        rerank: dict,
        *,
        usage_support_recorder: Callable[[str, bool], None] | None = None,
    ) -> None:
        self.store = store
        self.embedding = embedding
        self.rerank = rerank
        self.call_limiter = None
        self.usage_support_recorder = usage_support_recorder
        self._config_lock = threading.RLock()
        with store.transaction() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS world_memories(
                    event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    visibility_json TEXT NOT NULL DEFAULT '["*"]'
                )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(world_memories)").fetchall()}
            if "visibility_json" not in columns:
                conn.execute("""ALTER TABLE world_memories ADD COLUMN visibility_json TEXT NOT NULL DEFAULT '["*"]'""")

    def reconfigure(
        self,
        embedding: dict,
        rerank: dict,
        *,
        call_limiter=None,
        usage_support_recorder: Callable[[str, bool], None] | None = None,
    ) -> None:
        with self._config_lock:
            self.embedding = dict(embedding)
            self.rerank = dict(rerank)
            self.call_limiter = call_limiter
            self.usage_support_recorder = usage_support_recorder

    def _post(
        self,
        config: dict,
        url: str,
        payload: dict,
        charge: Callable[[int], None],
        *,
        reserve: Callable[[int], str] | None = None,
        settle: Callable[[str, int], None] | None = None,
        release: Callable[[str], None] | None = None,
        usage_role: str | None = None,
    ) -> dict:
        req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, method="POST")
        reservation_id = reserve(_estimated_payload_tokens(payload)) if reserve else None

        def send() -> dict:
            try:
                with urlopen(req, timeout=60) as response:
                    value = json.loads(response.read())
            except (HTTPError, URLError, TimeoutError) as exc:
                raise RuntimeError(f"记忆模型请求失败：{getattr(exc, 'code', type(exc).__name__)}") from exc
            if not isinstance(value, dict):
                raise RuntimeError("记忆模型返回格式不是 JSON 对象")
            return value

        try:
            if self.call_limiter:
                with self.call_limiter.slot():
                    value = send()
            else:
                value = send()
            usage = value.get("usage") or {}
            tokens = usage.get("total_tokens")
            usage_tokens = None
            if tokens is not None and not isinstance(tokens, bool):
                try:
                    usage_tokens = int(tokens)
                except (TypeError, ValueError):
                    pass
            supported = usage_tokens is not None and usage_tokens >= 0
            if usage_role and self.usage_support_recorder:
                try:
                    self.usage_support_recorder(usage_role, supported)
                except Exception:
                    logging.getLogger("virtual_domain").exception(
                        "Could not persist memory usage capability for role %s",
                        usage_role,
                    )
            if usage_tokens is None or usage_tokens <= 0:
                raise RuntimeError("记忆模型未返回正数的实际 Token 用量，请管理员检查供应商")
            if reservation_id and settle:
                settle(reservation_id, usage_tokens)
                reservation_id = None
            else:
                charge(usage_tokens)
            return value
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"记忆模型请求失败：{getattr(exc, 'code', type(exc).__name__)}") from exc
        finally:
            if reservation_id and release:
                release(reservation_id)

    def _embed(
        self,
        text: str,
        charge: Callable[[int], None],
        config: dict,
        *,
        reserve: Callable[[int], str] | None = None,
        settle: Callable[[str, int], None] | None = None,
        release: Callable[[str], None] | None = None,
    ) -> list[float]:
        # Keep the legacy/test hook contract when no quota callbacks are
        # supplied.  A number of integrations replace ``_post`` with a small
        # positional fake; passing optional keyword arguments whose values are
        # all ``None`` needlessly breaks those callers.
        def post(url: str, payload: dict) -> dict:
            if reserve is None and settle is None and release is None:
                if self.usage_support_recorder:
                    return self._post(
                        config,
                        url,
                        payload,
                        charge,
                        usage_role="embedding",
                    )
                return self._post(config, url, payload, charge)
            call_options = {
                "reserve": reserve,
                "settle": settle,
                "release": release,
            }
            if self.usage_support_recorder:
                call_options["usage_role"] = "embedding"
            return self._post(config, url, payload, charge, **call_options)

        if config.get("protocol", "dashscope") == "openai":
            value = post(
                config["endpoint"].rstrip("/") + "/embeddings",
                {"model": config["model"], "input": text},
            )
            return value["data"][0]["embedding"]
        value = post(
            config["endpoint"].rstrip("/")
            + "/services/embeddings/text-embedding/text-embedding",
            {"model": config["model"], "input": {"texts": [text]}},
        )
        return value["output"]["embeddings"][0]["embedding"]

    def save(
        self,
        event,
        charge: Callable[[int], None],
        *,
        reserve: Callable[[int], str] | None = None,
        settle: Callable[[str, int], None] | None = None,
        release: Callable[[str], None] | None = None,
    ) -> None:
        if self.store.connection.execute("SELECT 1 FROM world_memories WHERE event_id=?", (event.id,)).fetchone():
            return
        with self._config_lock:
            embedding = dict(self.embedding)
        vector = self._embed(event.content, charge, embedding, reserve=reserve, settle=settle, release=release)
        visible_to = list(getattr(event, "visible_to", ["*"]))
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO world_memories(event_id,world_id,content,vector_json,embedding_model,visibility_json) VALUES(?,?,?,?,?,?)", (event.id, event.world_id, event.content, json.dumps(vector), embedding["model"], json.dumps(visible_to, ensure_ascii=False)))

    def search(
        self,
        world_id: str,
        query: str,
        charge: Callable[[int], None],
        *,
        check: Callable[[], None],
        viewer_id: str | None,
        reserve: Callable[[int], str] | None = None,
        settle: Callable[[str, int], None] | None = None,
        release: Callable[[str], None] | None = None,
    ) -> str:
        """Recall for a player/Bot perspective; None is reserved for the omniscient adjudicator."""
        with self._config_lock:
            embedding = dict(self.embedding)
            rerank = dict(self.rerank)
        rows = self.store.connection.execute("SELECT content,vector_json,visibility_json FROM world_memories WHERE world_id=? AND embedding_model=?", (world_id, embedding["model"])).fetchall()
        if viewer_id is not None:
            rows = [row for row in rows if "*" in json.loads(row["visibility_json"] or '["*"]') or viewer_id in json.loads(row["visibility_json"] or '["*"]')]
        if not rows:
            return ""
        check()
        vector = self._embed(query, charge, embedding, reserve=reserve, settle=settle, release=release)
        norm = math.sqrt(sum(value * value for value in vector)) or 1

        def similarity(row):
            other = json.loads(row["vector_json"])
            if len(other) != len(vector):
                return -1
            other_norm = math.sqrt(sum(value * value for value in other)) or 1
            return sum(a * b for a, b in zip(vector, other)) / (norm * other_norm)

        docs = [row["content"] for row in sorted(rows, key=similarity, reverse=True)[:8]]
        check()
        payload = {
            "model": rerank["model"],
            "input": {"query": query, "documents": docs},
            "parameters": {"top_n": min(3, len(docs))},
        }
        if reserve is None and settle is None and release is None:
            if self.usage_support_recorder:
                value = self._post(
                    rerank,
                    rerank["endpoint"],
                    payload,
                    charge,
                    usage_role="rerank",
                )
            else:
                value = self._post(rerank, rerank["endpoint"], payload, charge)
        else:
            call_options = {
                "reserve": reserve,
                "settle": settle,
                "release": release,
            }
            if self.usage_support_recorder:
                call_options["usage_role"] = "rerank"
            value = self._post(rerank, rerank["endpoint"], payload, charge, **call_options)
        ranked = value.get("output", {}).get("results", [])
        check()
        return "\n\n".join(docs[int(result["index"])] for result in ranked[:3])


def load_memory_configs(path: str) -> tuple[dict, dict] | None:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data.get("memory"), dict):
        return data["memory"]["embedding"], data["memory"]["rerank"]
    entries = data.get("provider")
    if not isinstance(entries, list):
        return None
    embedding = next((item for item in entries if item.get("id") == "qwen3.7-text-embedding"), None)
    rerank = next((item for item in entries if item.get("id") == "qwen3-vl-rerank"), None)
    if not embedding or not rerank:
        return None
    return (
        {"model": embedding["embedding_model"], "endpoint": embedding["embedding_api_base"], "api_key": embedding["embedding_api_key"], "protocol": "dashscope"},
        {"model": rerank["rerank_model"], "endpoint": rerank["rerank_api_base"], "api_key": rerank["rerank_api_key"]},
    )
