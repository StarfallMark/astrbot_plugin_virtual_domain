虚拟域桌面客户端
================

本客户端连接自托管的虚拟域服务端。世界模型、Bot 模型和记忆模型由服务端或已连接的 AstrBot 提供；客户端不会直接调用模型供应商。

启动
----

Windows：解压后双击 VirtualDomain.exe。

Linux：解压后运行 VirtualDomain/VirtualDomain。Linux 包含 Qt 运行库，但仍依赖系统图形会话和基础 C 库。不同发行版之间的兼容性尚未完成实机验收。

Debian/Ubuntu 图形环境缺少 Qt/X11 依赖时，可安装：

sudo apt-get install libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libegl1 libgl1 fonts-noto-cjk

libxcb-shape0 缺失也可能触发 Qt 关于 xcb 插件无法加载的通用提示。2026-10-31 的 Linux x64/ARM64 托管运行器检查已在补齐此依赖后通过。

macOS：解压后打开 VirtualDomain.app。当前自动构建包未经 Apple Developer ID 签名和公证，macOS 可能显示开发者验证提示。

连接与登录
----------

首次连接时，填写管理员提供的服务端地址和网络连接条件。通过公网或域名连接时，优先使用 HTTPS/TLS；Tailscale、WireGuard 等私有组网也可以作为连接路径。客户端会对远程 HTTP 明文连接显示风险确认。

登录现有账号后：

- 管理员输入服务端配对码，连接创作模式。
- 加入者使用管理员提供的邀请码注册或加入世界；邀请码只授予进入资格，本身不提供网络连接。
- 管理员连接创作模式后，可在“加入模式”选择世界并切换当前世界，或永久删除选中的世界。删除会清除该世界的设定、角色、事件、互动、记忆索引和 Token 额度记录；操作前需要再次输入世界名称确认。建议先通过“备份管理”创建服务端备份。

模型供应商设置和 API 密钥保存在服务端，不要把模型密钥粘贴到客户端。

本安装包只包含桌面客户端；虚拟域服务端需由管理员另行部署和启动。
