# 虚拟域服务端

服务端独立运行于 Windows、Linux 或 macOS。需要 Python 3.11 或更新版本；桌面客户端和 AstrBot 插件分别下载、配置。服务端源码包仅含示例模型配置，不含 API 密钥、数据库或运行数据。

## 安装与启动

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[models]"
Copy-Item .\config\model.example.json .\config\model.json
```

编辑 `config/model.json`，填入模型供应商、模型名、API 地址和 API Key，然后双击 `start_server.bat`。也可以运行 `.\start_server.ps1` 并传入启动参数。

Linux 或 macOS：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[models]"
cp config/model.example.json config/model.json
```

编辑 `config/model.json` 后运行：

```sh
sh ./start_server.sh
```

首次启动会在服务端控制台显示管理员初始化码和创作连接配对码。数据库及加密模型配置保存在 `data/`。请将整个服务端目录放在受保护的位置，并自行备份该目录中的运行数据。

默认监听 `127.0.0.1:8787`。远程连接前应设置 TLS/mTLS 或可信的 Tailscale/WireGuard 私有组网，并配置防火墙；不要直接将未加密端口暴露到公网。Linux/macOS 可通过 `VD_BIND_HOST`、`VD_PORT` 等环境变量配置监听；Windows 可向 `start_server.ps1` 传入 `-BindHost`、`-Port` 等参数。

## AstrBot 接入

先在独立服务端配置世界模型和记忆模型，再用 AstrBot 插件连接服务。Windows 示例：

```powershell
.\start_server.ps1 -BotMode astrbot -ConnectorConfig C:\secure\virtual-domain-connector.json
```

Linux/macOS 示例：

```sh
VD_BOT_MODE=astrbot VD_CONNECTOR_CONFIG=/secure/virtual-domain-connector.json sh ./start_server.sh
```

连接配置中的服务地址和凭证应与 AstrBot 插件配置匹配。不要把连接凭证或 `config/model.json` 提交到代码仓库。
