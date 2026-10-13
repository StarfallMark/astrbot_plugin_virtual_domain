# 虚拟域

虚拟域由三个可独立部署的部分组成：AstrBot 连接器插件、独立服务端和 PySide6 桌面客户端。首版目标是 Windows、Linux、macOS 桌面使用；Windows 汇总候选已通过集中审核，Linux/macOS 本机打包脚本和 GitHub Actions 构建矩阵已就绪，四种非 Windows 发布包仍待产出。通用服务端源码 ZIP/tar.gz 已生成。OpenAI/Anthropic 与 QQ 的协议、用量和权限已有离线覆盖；真实上游调用和 QQ 平台事件留待实际使用反馈，不阻塞首版。当前唯一发布门槛是生成并核验 Linux x64、Linux ARM64、macOS Intel 和 macOS Apple Silicon 包，详见 [DESIGN.md](DESIGN.md)。测试运行记录保存在本机工作区的 `test/` 目录，不随本项目仓库发布。

第一版目标仍有效，没有撤回：交付独立服务端、可切换创作/加入模式的原生桌面客户端和可选 AstrBot 插件；完成 Bot 与用户共同参演的世界创建、游玩、记忆、额度、账号、存档和管理闭环。手机端是后续计划。当前进度与剩余验收项以 [DESIGN.md](DESIGN.md) 开头的首版目标和实现状态为准。

```text
plugin/   AstrBot 连接器
server/   世界、账号、模型和记忆服务
client/   创作与加入共用的桌面客户端
```

所有运行配置、API 密钥和数据库应保存在部署者本机，不要提交或分享密钥文件。

## 启动服务端

需要 Python 3.11 或更新版本。安装世界模型调用依赖并准备本机模型配置：

```powershell
cd server
pip install -e ".[models]"
Copy-Item config/model.example.json config/model.json
```

编辑 `config/model.json` 后，Windows 可双击 `start_server.bat`，Linux/macOS 执行 `./start_server.sh`。脚本会检查 Python 版本和模型配置，并将 SQLite 数据库保存在 `server/data/`。首次启动时，服务端控制台显示创建初始管理员所需的一次性初始化码和创作模式配对码。没有模型配置时，只有明确指定 `-AllowNoModel`（或设置 `VD_ALLOW_NO_MODEL=1`）才会启动无推演开发模式。

首次启动成功后，创作模式的“模型供应商与并发设置”页面可配置世界模型、独立 Bot 模型、记忆模型和并发上限。配置保存在数据库目录的 `model-profiles.enc`；Windows 自动生成的本地密钥由当前 Windows 用户 DPAPI 保护，Linux/macOS 使用仅当前账户可读的密钥文件。也可通过服务环境变量 `VD_MODEL_VAULT_KEY` 提供 Fernet 密钥。迁移到其他 Windows 账号或机器时，需在原部署中准备可解密的配置与密钥，或显式设置外部密钥；不要把密钥文件公开。确认加密配置已写入后，可移除仍含初始模型密钥的 `config/model.json`。

服务默认仅监听本机回环地址。远程加入请使用管理员提供的安全网络连接方式，例如 Tailscale。当前服务端可配置 TLS 证书与私钥，客户端及 AstrBot 连接器支持 CA 校验和可选的双向 TLS 客户端证书。服务端附带可签发、登记和撤销客户端凭证的本地工具；证书私钥分发和 CRL 更新后的服务端重启由部署者完成，详见下文。不要在未设置防火墙和传输保护的情况下把端口直接暴露到公网。

桌面客户端使用远程 `http://` 地址前会显示传输风险确认；本机回环地址不提示。只有确认该连接位于 Tailscale/WireGuard 等可信加密网络或接受明文传输风险时才继续。远程访问推荐 HTTPS/mTLS；确认提示本身不会加密 HTTP 流量。

### 管理双向 TLS 客户端证书

需要逐个撤销桌面客户端或 AstrBot 连接凭证时，可使用服务端附带的证书工具。先在服务端安装可选依赖并创建本地 CA；CA 私钥使用口令加密保存，应只放在管理员可访问的受保护目录：

```powershell
cd server
pip install -e ".[security]"
virtual-domain-certificates init --dir .\data\client-ca
virtual-domain-certificates issue --dir .\data\client-ca --out-dir C:\secure-delivery\desktop --name desktop-star --days 365
```

`init` 会交互式要求输入至少 12 位 CA 口令；签发、撤销和续期 CRL 时也会重新询问，不把口令放进命令行。签发会为每份凭证生成唯一 ID、单独的客户端证书和私钥，不导出 CA 私钥。通过受保护的渠道把客户端 `.pem` 证书和 `-key.pem` 私钥交给对应使用者。私钥文件应放在仅该使用者可读取的目录中；Windows 上应检查并收紧目标目录 ACL。

服务端启动时同时指定 TLS 服务证书、客户端 CA 和 CRL：

```powershell
.\start_server.ps1 -BindHost 0.0.0.0 -TlsCertFile C:\certs\server.pem -TlsKeyFile C:\certs\server-key.pem -TlsClientCaFile .\data\client-ca\ca-cert.pem -TlsClientCrlFile .\data\client-ca\client-crl.pem
```

Linux/macOS 使用环境变量 `VD_TLS_CERT_FILE`、`VD_TLS_KEY_FILE`、`VD_TLS_CLIENT_CA_FILE` 和 `VD_TLS_CLIENT_CRL_FILE` 配置相同文件。桌面客户端的 TLS 设置和 AstrBot 插件配置中分别填写受信任的**服务端** CA，以及签发的客户端证书和私钥。服务端 CA 与客户端 CA 是两种不同用途的信任配置，不要把服务端私钥分发给客户端。

用 `virtual-domain-certificates list --dir <CA目录>` 查看凭证状态；`revoke --dir <CA目录> --id <凭证ID>` 撤销单份凭证并更新 CRL；续签时签发新证书、更新客户端配置后撤销旧证书。CRL 默认 30 天到期，使用 `refresh-crl --dir <CA目录>` 续期。**签发、撤销或刷新后必须重启服务端**，让 TLS 层加载新 CRL；重启也会关闭当时已建立的连接。服务端必须同时设置客户端 CA 和 CRL，撤销检查才会启用。

连接 AstrBot 时，服务端以 `-BotMode astrbot -ConnectorConfig <插件连接配置>` 启动；插件和服务端使用同一组服务地址与 AstrBot 专用连接凭证。AstrBot 接入不会回退为独立 Bot 模型。完整 AstrBot 调用会进入正常消息事件队列；游戏回复留在虚拟域客户端，不发送到 QQ 群聊。

标准库 HTTP 服务是当前推荐运行入口，也承载 AstrBot 认证 WebSocket 长连接和流动式调度；连接器会自动重连，旧版 HTTP 长轮询仍兼容。可选 FastAPI REST 适配层用于 API 集成测试与开发，安装方式为 `pip install -e ".[http,models]"`；它不是当前完整运行入口。

## 启动桌面客户端

从源码运行：

```powershell
cd client
pip install -e ".[desktop]"
python -m virtual_domain_client.app
```

Windows 单文件程序位于 `client/dist/VirtualDomain.exe`，双击即可运行，不需要安装 Python 或浏览器。Windows 重新构建需要 `pip install -e ".[packaging]"`，然后在 `client/` 执行 `./build_client.ps1`；默认构建单文件 EXE，`-OneDirectory` 构建目录版。

Linux 和 macOS 需要在对应系统的原生构建机打包，不能从 Windows 交叉生成。安装 Python 3.11 或更新版本及打包依赖后，在 `client/` 运行：

```sh
python3 -m pip install -e ".[packaging]"
sh build_client.sh
```

默认输出在 `client/dist/<系统>-<架构>/`：Linux 为 `VirtualDomain/` 目录版，macOS 为 `VirtualDomain.app`。使用 `sh build_client.sh --onefile` 可构建单文件程序；macOS 默认目录版更适合作为 `.app` 分发。也可用 `--python` 指定 Python 解释器、`--output` 指定输出目录。Linux 打包结果依赖构建系统的基础 C 库版本，建议在计划支持的较旧 Linux 发行版上构建。不同 CPU 架构需要各自构建。Linux/macOS 的构建和运行尚未实测验收。

GitHub Actions 的 `Build native desktop clients` 工作流会为 Windows x64、Linux x64/ARM64、macOS Intel/Apple Silicon 分别打包桌面客户端，并同时生成不含密钥和运行数据的服务端源码 ZIP/tar.gz。工作流位于本项目根目录 `.github/workflows/build-clients.yml`；本项目作为独立仓库时，仓库根就是包含 `client/`、`server/`、`plugin/` 的本目录。推送相关文件后工作流会自动触发，也可在 GitHub Actions 页面手动运行。客户端构建会检查归档内容并附带 SHA-256 校验文件；构建产物短期保存。该工作流只负责构建，不代表已完成目标系统运行验收。服务端部署说明见 [server/README.md](server/README.md)。

客户端在创作与加入模式间切换。创作模式需要管理员登录并使用服务端配对码建立管理连接；加入模式使用管理员给出的服务地址、连接条件和世界邀请。加入世界后可提交行动、在游戏内与 Bot 聊天，并查看世界屏幕、事件、在线状态和 Token 账本。服务端支持静止式和流动式世界，额度按实际返回用量记录。

创作页面可选填“关键状态属性规则（JSON）”，为影响规则判断的世界字段声明类型、数值范围、长度范围或枚举值。例如：

```json
{
  "health": {"type": "integer", "minimum": 0, "maximum": 100},
  "location": {"type": "string", "enum": ["集市", "码头"]},
  "characters": {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "health": {"type": "integer", "minimum": 0}
      },
      "required": ["name", "health"]
    }
  }
}
```

对象规则支持 `properties`、`required` 和布尔型 `additional_properties`；声明 `properties` 时默认拒绝其他字段。数组可用 `items` 递归声明元素结构。配置后，初始状态中的每个顶层属性都必须在规则中声明；世界模型只能修改已声明的顶层字段，且新值及其嵌套内容必须符合规则。未配置规则时保留自由结构状态兼容。`event_count`、`last_event` 和 `memory_entries` 始终由服务端维护，模型不能直接改写。该校验保护已声明的结构化字段，不替代对场景事实、观察者范围或危险语义的判断。

## 安装 AstrBot 插件

将完整 `plugin/` 目录复制到：

```text
astrbot/data/plugins/astrbot_plugin_virtual_domain/
```

在 AstrBot 插件配置中填写服务端地址、AstrBot 专用连接凭证，以及可选的管理员 QQ 号。用户在桌面客户端“账号设置”生成一次性绑定码，再用目标 QQ 私聊发送 `/vd bind <绑定码>`；绑定码 5 分钟有效且只能使用一次。QQ 绑定可选，可在客户端通过当前密码解除。管理员账号须绑定与 `admin_qq` 相同的 QQ 号，才可使用 QQ 管理命令：

```text
/vd status
/vd worlds
/vd invite [有效小时数]
/vd invite revoke <邀请码>
/vd quota
/vd bind <绑定码>
/vd pause [世界ID|current]
/vd resume [世界ID|current]
/vd switch <世界ID> [confirm]
/vd delete <世界ID>
/vd backup
/vd backups
```

`/vd worlds` 只返回安全摘要；`invite`、`quota`、`pause`、`resume`、`switch`、`delete`、`backup` 与 `backups` 只接受绑定管理员的 QQ 私聊。`/vd invite [有效小时数]` 为当前世界生成一次性邀请码，默认 24 小时、最长 720 小时；`/vd invite revoke <邀请码>` 撤销当前世界尚未使用的邀请码，已兑换的邀请码不能撤销。邀请码只提供进入资格，连接地址和组网方式仍需管理员另行提供。额度查看同时校验插件配置的管理员 QQ 与已绑定的虚拟域管理员账号。切换时若旧世界有人在线，第一次请求只返回在线提示，管理员显式发送 `confirm` 后才断开玩家并通知旧世界，并由服务端再次校验账号权限。`/vd delete <世界ID>` 先展示待删除世界名称，第二条命令必须原样包含完整名称；服务端复核绑定管理员身份和名称，并保留审计记录。QQ 可创建和查看手动备份，文件下载、账号和配置管理仍在桌面客户端完成，行动和游戏内聊天也在桌面客户端中完成。隔离实机已验证绑定账号的游戏聊天进入绑定时记录的 AstrBot 日常会话，并在连续两条消息间回忆随机标记；这尚不能单独证明第三方长期记忆插件的持久化与检索效果。测试报告：`test/bound_chat_continuity_20261004.json`。

## 验收进度更新（2026-10-12）

Windows 汇总候选已完成一次集中审核：同一 EXE 通过注册与加入、假模型行动和聊天、备份恢复、Bot 模式双向切换、管理员一次性推演及服务重启后待回应恢复。报告位于 `test/runtime/v1-release-candidate-20261012/final-audit-temp-fixed/windows-final-audit.json`。审核使用本机隔离服务和假模型，没有连接真实模型、AstrBot 或 QQ。

首次审核曾因测试机 `C:` 盘无可用空间导致 PyInstaller 解包失败；将审核临时目录放在 `test/` 所在的 `E:` 后，同一候选通过。该历史问题已解决，不是当前候选的启动缺陷。全插件 Gemini/AstrBot 游玩已有一次独立通过的隔离整轮，除非出现新故障，不重复运行。

首版当前唯一待完成的发行项是生成并检查 Linux x64/ARM64、macOS Intel/Apple Silicon 客户端包；跨平台构建工作流已配置，但尚未触发。OpenAI 兼容和 Anthropic 的真实 Token 用量、真实 QQ 管理命令事件均作为实际使用反馈，不阻塞首版；对应的离线协议、用量解析、管理员身份与私聊权限用例已有覆盖。MemoryCompanion 匿名隔离、跨重启检索和并发持久化专项已通过。完整状态见 [DESIGN.md](DESIGN.md)；测试证据留在本机工作区的 `test/`，不包含在独立插件仓库中。

## 测试

回归测试副本、测试数据库、运行日志、截图与测试构建产物都放在开发工作区根目录的 `test/`，不会提交到本独立项目仓库。以下命令仅适用于完整本机开发工作区；单独克隆本仓库不包含这些隔离测试副本。从 PowerShell 运行服务端测试：

```powershell
cd ..\test\server
$env:PYTHONPATH = "$PWD\src;$PWD\..\plugin"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```


AstrBot 插件隔离命令测试：

```powershell
cd ..\test\plugin
$env:PYTHONPATH = "$PWD"
..\server\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
客户端 API 测试：

```powershell
cd ..\client
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

最近回归记录：服务端完整离线回归运行 214 项、213 项通过、1 项 Windows 不适用的 POSIX 权限测试跳过；AstrBot 插件 43 项通过。管理员节点回滚与 QQ 命令离线回归日志见 `test/server/unittest_qq_node_rollback_20261010.log` 和 `test/plugin/unittest_qq_node_rollback_20261010.log`。2026-10-12 Gemini/AstrBot 隔离整轮通过：两次玩家行动、两次 Bot 行动和游戏聊天成功，共 5 个世界事件；Bot、世界、记忆账本总计 141,986 Token，额度预留归零。此前 404 保留为历史间歇故障，若无新故障不重复整轮。报告与运行数据见 `test/runtime/gemini-resume-20261010-5994daae/` 和 `test/TEST_REPORT.md`。同日 Windows 汇总候选集中审核的五项均通过；早期 `Error` 窗口由测试机 `C:` 盘空间不足导致，现有报告记录通过时将 `TEMP`/`TMP` 放在 `E:` 盘。当前唯一待完成 Linux/macOS 四种桌面包生成与归档核验；OpenAI/Anthropic 真实调用及真实 QQ 事件作为实际使用反馈，MemoryCompanion 匿名隔离/重启/并发专项已通过，完整状态见 [DESIGN.md](DESIGN.md)。




