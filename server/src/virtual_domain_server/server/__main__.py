"""服务端命令行入口。"""

from __future__ import annotations

import argparse
import os
import json
from pathlib import Path

from ..core.model_config import load_model_provider
from ..core.bot_bridge import WebSocketBotBridge
from ..core.simulation import WorldSimulationEngine
from ..core.providers import ModelConfig, provider_from_config
from ..core.memory import load_memory_configs
from ..core.secret_store import EncryptedConfigStore
from .builtin import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="虚拟域自托管服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--db", default="virtual_domain.sqlite3")
    parser.add_argument("--model-config", default="", help="简化模型配置或 AstrBot cmd_config.json")
    parser.add_argument("--model-id", default="", help="AstrBot 配置中要使用的模型 ID")
    parser.add_argument("--max-retries", default=1, type=int)
    parser.add_argument("--bot-mode", choices=("standalone", "astrbot"), default="standalone")
    parser.add_argument("--bot-token", default=os.environ.get("VD_CONNECTOR_TOKEN", ""), help="AstrBot 连接器凭证，也可通过 VD_CONNECTOR_TOKEN 提供")
    parser.add_argument("--connector-config", default="", help="包含 connector_token 的本机配置文件")
    parser.add_argument("--tls-certfile", default="", help="HTTPS 服务端 PEM 证书")
    parser.add_argument("--tls-keyfile", default="", help="HTTPS 服务端 PEM 私钥")
    parser.add_argument("--tls-client-ca", default="", help="启用双向 TLS 并校验客户端证书的 CA PEM 文件")
    parser.add_argument("--tls-client-crl", default="", help="客户端证书吊销列表 PEM 文件；更新后需重启服务端")
    args = parser.parse_args()
    if args.connector_config:
        args.bot_token = json.loads(Path(args.connector_config).read_text(encoding="utf-8-sig"))["connector_token"]
    engine = None
    memory_configs = None
    model_settings_dir = (
        Path(args.db).resolve().parent if args.db != ":memory:" else None
    )
    saved_profiles = None
    if model_settings_dir and (model_settings_dir / "model-profiles.enc").is_file():
        saved_profiles = EncryptedConfigStore(model_settings_dir).load()
    configured_bot_mode = (
        saved_profiles.get("bot_mode", args.bot_mode)
        if isinstance(saved_profiles, dict)
        else args.bot_mode
    )
    if configured_bot_mode not in {"standalone", "astrbot"}:
        parser.error("加密配置中的 Bot 运行模式无效")
    if saved_profiles is not None:
        world_profile = saved_profiles.get("world")
        if isinstance(world_profile, dict):
            provider = provider_from_config(
                ModelConfig(
                    provider=str(world_profile["provider"]),
                    model=str(world_profile["model"]),
                    api_key=str(world_profile.get("api_key", "")),
                    endpoint=str(world_profile.get("endpoint", "")),
                    timeout=float(world_profile.get("timeout", 120)),
                    extra=dict(world_profile.get("extra") or {}),
                )
            )
            engine = WorldSimulationEngine(provider, max_retries=args.max_retries)
            print(f"加密配置中的世界模型已加载：{provider.config.provider}/{provider.config.model}")
        embedding = saved_profiles.get("embedding")
        rerank = saved_profiles.get("rerank")
        if isinstance(embedding, dict) and isinstance(rerank, dict):
            memory_configs = (embedding, rerank)
            print(f"加密配置中的记忆模型已加载：{embedding.get('model')} / {rerank.get('model')}")
        if args.model_id:
            print("已存在加密模型配置；本次忽略 --model-id，模型选择以管理员配置为准。")
    elif args.model_config:
        provider = load_model_provider(args.model_config, model_id=args.model_id or None)
        engine = WorldSimulationEngine(provider, max_retries=args.max_retries)
        print(f"世界模型已加载：{provider.config.provider}/{provider.config.model}")
        memory_configs = load_memory_configs(args.model_config)
        if memory_configs:
            print(f"记忆模型已加载：{memory_configs[0]['model']} / {memory_configs[1]['model']}")
    bridge = WebSocketBotBridge(args.bot_token) if args.bot_token else None
    if configured_bot_mode == "astrbot" and bridge is None:
        parser.error("AstrBot 模式需要连接凭证 --bot-token")
    try:
        serve(
            args.host,
            args.port,
            args.db,
            simulation_engine=engine,
            bot_bridge=bridge,
            bot_mode=configured_bot_mode,
            memory_configs=memory_configs,
            model_settings_dir=str(model_settings_dir) if model_settings_dir else None,
            tls_certfile=args.tls_certfile or None,
            tls_keyfile=args.tls_keyfile or None,
            tls_client_ca_file=args.tls_client_ca or None,
            tls_client_crl_file=args.tls_client_crl or None,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
