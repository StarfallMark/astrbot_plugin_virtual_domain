# 虚拟域 AstrBot 插件

这是可直接安装到 AstrBot 的完整连接器目录。将整个 `plugin/` 目录复制到：

```text
astrbot/data/plugins/astrbot_plugin_virtual_domain/
```

保留目录根部的 `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` 和 `virtual_domain_plugin/` 内部包。在 AstrBot 插件配置中填写服务端地址和 AstrBot 专用连接凭证；配置 `admin_qq` 后，完成 QQ 绑定的虚拟域管理员才能使用 QQ 管理命令。

用户在桌面客户端登录后打开“账号设置”，生成一次性绑定码，再从要绑定的 QQ 账号私聊 Bot 发送 `/vd bind <绑定码>`。绑定码有效 5 分钟且只能兑换一次。绑定是可选的；账号可在客户端输入当前密码解除绑定。管理员 QQ 管理命令只接受已配置管理员 QQ 发出的私聊，并且服务端还会检查该 QQ 是否绑定到管理员账号。

当前指令：

```text
/vd status
/vd worlds
/vd invite [有效小时数]
/vd invite revoke <邀请码>
/vd summary
/vd interactions
/vd quota
/vd audit [条数]
/vd bind <绑定码>
/vd nodes <世界ID> [before_sequence]
/vd rollback <世界ID> <节点ID> [confirm]
/vd pause [世界ID|current]
/vd resume [世界ID|current]
/vd switch <世界ID> [confirm]
/vd delete <世界ID>
/vd backup
/vd backups
```

`/vd nodes <世界ID> [before_sequence]` 由已绑定管理员在 QQ 私聊中查看节点摘要，每页最多 10 项；有更早记录时会返回下一页命令。`/vd rollback <世界ID> <节点ID>` 先显示目标及其后的剧情、节点、相关记忆和待回应交互将被丢弃；管理员再次发送相同命令并追加 `confirm` 才执行。服务端验证插件连接凭证、配置管理员 QQ 和绑定的管理员账号，并使用现有回滚事务记录审计；已产生的 Token 用量保留。

`/vd worlds` 只返回世界摘要，不包含深层设定。`/vd invite [有效小时数]` 仅供已绑定管理员 QQ 私聊使用，在当前世界生成一次性邀请码，有效期可设为 1 到 720 小时；邀请码只提供进入资格，管理员仍需另行提供服务端地址和连接方式。`/vd invite revoke <邀请码>` 撤销当前世界尚未使用的邀请码；已使用的邀请码无法撤销。`/vd summary` 仅供已绑定管理员 QQ 私聊查询，显示当前世界运行状态、在线玩家数、待回应/失败交互数量和额度总用量，不返回世界叙事或深层设定。`/vd interactions` 在管理员私聊中查看当前世界最多 5 条待回应、结算中或失败的交互，显示截断后的行动、回应要求和已进行的结算尝试次数，不提供直接代替玩家回应或重试失败结算的操作。`/vd quota` 显示当前世界的 Bot、世界、记忆和总 Token 使用量及上限，只允许绑定管理员从 QQ 私聊查询；同时提示 AstrBot/第三方插件自行发起的模型调用可能无法完整归入账本，因此 Bot 额度不构成这类调用的完整硬上限。`/vd audit [条数]` 仅供已绑定管理员 QQ 私聊查询，最多返回 20 条操作的时间、类型、结果和目标，不返回审计详情或敏感字段。`pause`/`resume`/`switch`/`backup`/`backups`/`delete` 通过 AstrBot 专用连接凭证和绑定管理员身份执行；手动备份文件仍由管理员在桌面客户端下载。QQ 切换世界时会先显示旧世界在线玩家及断开提示；管理员必须再次发送 `confirm` 明确确认，服务器才会切换、给旧世界写入公开通知并断开在线会话。`/vd delete <世界ID>` 会先展示待删除世界名称，确认时必须原样提供完整名称；服务端再次校验管理员绑定和名称，并写入审计日志。行动输入和游戏内聊天在虚拟域桌面客户端中完成，回复不会发送到普通 QQ 群聊。2026-10-08 隔离游玩确认游戏聊天走已绑定的 AstrBot 日常会话链路；该历史流程证明了绑定会话连续性。MemoryCompanion 匿名隔离、世界范围、跨重启检索和并发持久化专项现已通过，详见 `test/runtime/memory-anonymous-isolation-world-scope-retry1-20261012/memory-isolation-audit.json`。

插件将 Bot 的生成请求放入 AstrBot 正常事件队列，继续使用当前生效的人格、工具与插件钩子。AstrBot 4.27.5 隔离副本已确认可以加载 v0.1.0 插件。QQ 管理命令的离线回归已覆盖，包括节点分页、管理员绑定、私聊范围和二次确认回滚；真实 QQ 事件执行留待实际使用反馈。

连接器通过认证 WebSocket 长连接接收服务端 Bot 请求，并使用 AstrBot 正常事件队列生成回复；断线会自动重连，旧服务端 HTTP 长轮询仍兼容。连接器可配置服务端 CA 和可选的双向 TLS 客户端证书；服务端证书主机名始终校验，不提供跳过验证选项。WSS/mTLS 本地集成测试通过。证书签发、更新和分发目前由部署者负责。

