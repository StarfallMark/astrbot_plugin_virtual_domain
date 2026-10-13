"""虚拟域桌面客户端首版界面。

PySide6 未安装时，命令行入口会给出安装提示；核心 API 客户端只依赖标准
库，因此也可被测试脚本复用。UI 已把创作模式与加入模式分开，后续页面会
在这两个入口内扩展，而不会再引入浏览器依赖。
"""

from __future__ import annotations

import json
import ipaddress
import ssl
import sys
import uuid
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from socket import timeout as SocketTimeout


class ApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8787") -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.management_token: str | None = None
        self.registered_world_id: str | None = None
        self.tls_ca_file = ""
        self.tls_client_cert_file = ""
        self.tls_client_key_file = ""

    def requires_insecure_remote_http_confirmation(
        self,
        base_url: str | None = None,
    ) -> bool:
        try:
            parsed = urlsplit(self.base_url if base_url is None else base_url)
            hostname = parsed.hostname
        except ValueError:
            return False
        if parsed.scheme.lower() != "http" or not hostname:
            return False
        if hostname.lower() == "localhost":
            return False
        try:
            return not ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return True

    def _tls_context(self):
        cert = self.tls_client_cert_file.strip()
        key = self.tls_client_key_file.strip()
        ca = self.tls_ca_file.strip()
        if bool(cert) != bool(key):
            raise RuntimeError("双向 TLS 客户端证书与私钥必须同时填写")
        if not self.base_url.lower().startswith("https://"):
            if ca or cert or key:
                raise RuntimeError("配置了 TLS 证书时，服务端地址必须以 https:// 开头")
            return None
        context = ssl.create_default_context(cafile=ca or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if cert:
            context.load_cert_chain(certfile=cert, keyfile=key)
        return context

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.management_token:
            headers["X-Management-Credential"] = self.management_token
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            context = self._tls_context()
            timeout = 600 if path.endswith(("/actions", "/chat", "/join", "/response", "/generate", "/configuration/apply-once")) or "/admin/backups" in path else 30
            opener_options = {"context": context} if context is not None else {}
            with urlopen(request, timeout=timeout, **opener_options) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"message": str(exc)}
            finally:
                exc.close()
            detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
            raise RuntimeError(detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)) from exc
        except (URLError, SocketTimeout, OSError) as exc:
            raise RuntimeError(f"无法连接服务端：{getattr(exc, 'reason', str(exc))}") from exc

    def login(self, username: str, password: str) -> dict[str, Any]:
        result = self.request("POST", "/api/v1/auth/login", {"username": username, "password": password})
        self.token = result["token"]
        self.management_token = None
        self.registered_world_id = None
        return result["account"]

    def register(
        self,
        username: str,
        password: str,
        *,
        initialization_code: str | None = None,
        invitation_code: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"username": username, "password": password}
        if initialization_code:
            body["initialization_code"] = initialization_code
        if invitation_code:
            body["invitation_code"] = invitation_code
        result = self.request("POST", "/api/v1/auth/register", body)
        self.token = result["token"]
        self.management_token = None
        self.registered_world_id = result.get("world_id")
        return result["account"]

    def connect_management(self, pairing_code: str) -> None:
        result = self.request("POST", "/api/v1/auth/management", {"pairing_code": pairing_code})
        self.management_token = result["management_token"]

    def disconnect_management(self) -> None:
        if not self.management_token:
            return
        self.request("DELETE", "/api/v1/auth/management")
        self.management_token = None

    def update_account(self, *, username: str | None, current_password: str, new_password: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"current_password": current_password}
        if username is not None:
            body["username"] = username
        if new_password is not None:
            body["new_password"] = new_password
        return self.request("PATCH", "/api/v1/auth/me", body)

    def qq_binding_status(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/auth/me/qq-binding")

    def preview_backup_import(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/admin/backups/import/preview", {"snapshot": snapshot})

    def import_backup(self, snapshot: dict[str, Any], account_mappings: dict[str, str | None]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/admin/backups/import",
            {"snapshot": snapshot, "account_mappings": account_mappings},
        )

    def create_qq_binding_code(self) -> dict[str, Any]:
        return self.request("POST", "/api/v1/auth/me/qq-binding-code", {})

    def unbind_qq(self, current_password: str) -> dict[str, Any]:
        return self.request("DELETE", "/api/v1/auth/me/qq-binding", {"current_password": current_password})

    def download(self, path: str) -> bytes:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.management_token:
            headers["X-Management-Credential"] = self.management_token
        request = Request(f"{self.base_url}{path}", headers=headers, method="GET")
        try:
            context = self._tls_context()
            opener_options = {"context": context} if context is not None else {}
            with urlopen(request, timeout=600, **opener_options) as response:
                return response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except Exception:
                detail = {"message": str(exc)}
            finally:
                exc.close()
            raise RuntimeError(detail.get("message", str(detail))) from exc
        except (URLError, SocketTimeout, OSError) as exc:
            raise RuntimeError(f"无法连接服务端：{getattr(exc, 'reason', str(exc))}") from exc


def create_application():
    try:
        from PySide6.QtCore import Qt, QThread, QTimer, Signal, QObject, QEvent
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import (
            QApplication,
            QComboBox,
            QCheckBox,
            QSpinBox,
            QDoubleSpinBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QFileDialog,
            QGroupBox,
            QInputDialog,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QSplitter,
            QScrollArea,
            QStackedWidget,
            QTabWidget,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print(f"虚拟域客户端需要安装 PySide6：pip install 'virtual-domain-client[desktop]' ({exc})", file=sys.stderr)
        raise SystemExit(2)

    class RequestWorker(QThread):
        completed = Signal(object)
        failed = Signal(str)

        def __init__(self, task, parent):
            super().__init__(parent)
            self.task = task

        def run(self):
            try:
                self.completed.emit(self.task())
            except Exception as exc:
                self.failed.emit(str(exc))

    class ActivityMonitor(QObject):
        def __init__(self, window):
            super().__init__(window)
            self.window = window

        def eventFilter(self, obj, event):
            if event.type() in {QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel} and self.window.stack.currentWidget() == self.window.game_page and QApplication.activeModalWidget() is None:
                self.window.last_activity = time.monotonic()
            return False

    class Window(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.api = ApiClient()
            self.account: dict[str, Any] | None = None
            self.registered_world_id: str | None = None
            self.generated_initial_state: dict[str, Any] = {}
            self.generated_opening_narrative = ""
            self.world_generation_request_id: str | None = None
            self.editing_world_id: str | None = None
            self.editing_world_is_current = False
            self.pending_configuration_available = False
            self.apply_configuration_busy = False
            self.world_generation_busy = False
            self.world_ids: list[str] = []
            self.world_names: dict[str, str] = {}
            self.current_world_ids: set[str] = set()
            self.world_statuses: dict[str, str] = {}
            self.delete_world_busy = False
            self.current_world_id: str | None = None
            self.current_state_version = 0
            self.game_state_ready = False
            self.gameplay_allowed = False
            self.active_interaction_id: str | None = None
            self.workers: set[RequestWorker] = set()
            self.play_busy = False
            self.refresh_busy = False
            self.pending_submissions: dict[str, tuple[str, str]] = {}
            self._http_transport_approved_url: str | None = None
            self.last_activity = time.monotonic()
            self.last_sent_activity = 0.0
            self.presence_busy = False
            self.closing_requested = False
            self.joining_busy = False
            self.setWindowTitle("虚拟域 · 共创世界")
            self.resize(1240, 820)
            self.setMinimumSize(960, 680)
            root = QWidget()
            root.setObjectName("appRoot")
            self.setCentralWidget(root)
            layout = QVBoxLayout(root)
            layout.setContentsMargins(20, 18, 20, 16)
            layout.setSpacing(14)
            brand_row = QHBoxLayout()
            brand_block = QVBoxLayout()
            brand = QLabel("虚拟域")
            brand.setObjectName("brand")
            tagline = QLabel("和 Bot 一起走进一个会回应你的世界")
            tagline.setObjectName("tagline")
            brand_block.addWidget(brand)
            brand_block.addWidget(tagline)
            brand_row.addLayout(brand_block)
            brand_row.addStretch()
            self.connection_toggle = QPushButton("收起连接设置")
            self.connection_toggle.setCheckable(True)
            brand_row.addWidget(self.connection_toggle)
            self.connection_status = QLabel("未连接")
            self.connection_status.setObjectName("connectionStatus")
            brand_row.addWidget(self.connection_status, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(brand_row)

            connection_panel = QWidget()
            self.connection_panel = connection_panel
            connection_panel.setObjectName("connectionPanel")
            connection_panel_layout = QVBoxLayout(connection_panel)
            connection_panel_layout.setSpacing(8)
            service_row = QHBoxLayout()
            service_row.setSpacing(10)
            service_row.addWidget(QLabel("服务端"))
            self.url = QLineEdit("http://127.0.0.1:8787")
            self.url.setObjectName("serverAddressInput")
            self.url.setAccessibleName("服务端地址")
            self.url.setMinimumWidth(300)
            service_row.addWidget(self.url, 1)
            tls_settings = QPushButton("TLS 证书设置")
            tls_settings.clicked.connect(self.show_tls_settings)
            service_row.addWidget(tls_settings)
            connection_panel_layout.addLayout(service_row)

            account_row = QHBoxLayout()
            account_row.setSpacing(10)
            account_row.addWidget(QLabel("账号"))
            self.username = QLineEdit()
            self.username.setObjectName("usernameInput")
            self.username.setAccessibleName("账号")
            self.username.setMinimumWidth(135)
            account_row.addWidget(self.username)
            account_row.addWidget(QLabel("密码"))
            self.password = QLineEdit()
            self.password.setObjectName("passwordInput")
            self.password.setAccessibleName("密码")
            self.password.setMinimumWidth(150)
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            account_row.addWidget(self.password)
            login = QPushButton("登录")
            login.setObjectName("loginButton")
            login.setAccessibleName("登录")
            self.login_button = login
            login.clicked.connect(self.login)
            account_row.addWidget(login)
            register = QPushButton("注册")
            self.register_button = register
            register.clicked.connect(self.register)
            account_row.addWidget(register)
            self.account_button = QPushButton("账号设置")
            self.account_button.setEnabled(False)
            self.account_button.clicked.connect(self.show_account_settings)
            account_row.addWidget(self.account_button)
            self.management_button = QPushButton("连接创作模式")
            self.management_button.setEnabled(False)
            self.management_button.clicked.connect(self.toggle_management_connection)
            account_row.addWidget(self.management_button)
            account_row.addStretch(1)
            connection_panel_layout.addLayout(account_row)
            layout.addWidget(connection_panel)
            self.connection_toggle.setChecked(True)
            self.connection_toggle.toggled.connect(connection_panel.setVisible)
            self.connection_toggle.toggled.connect(
                lambda expanded: self.connection_toggle.setText("收起连接设置" if expanded else "连接设置")
            )

            self.mode = QComboBox()
            self.mode.setObjectName("modeSwitch")
            self.mode.setAccessibleName("客户端模式")
            self.mode.addItems(["加入模式", "创作模式（需管理员连接码）"])
            self.mode.setEnabled(False)
            layout.addWidget(self.mode)

            self.stack = QStackedWidget()
            self.stack.setObjectName("pageStack")
            self.join_page = QWidget()
            self.join_page.setObjectName("joinPage")
            join_layout = QVBoxLayout(self.join_page)
            join_layout.setContentsMargins(16, 12, 16, 16)
            join_layout.setSpacing(10)
            self.worlds = QListWidget()
            self.worlds.setObjectName("worldList")
            self.worlds.setAccessibleName("世界列表")
            join_layout.addWidget(QLabel("可加入的世界"))
            join_layout.addWidget(self.worlds, 1)
            refresh = QPushButton("刷新世界")
            refresh.clicked.connect(self.refresh_worlds)
            join_layout.addWidget(refresh)
            self.invite_world_button = QPushButton("为选中世界生成加入邀请")
            self.invite_world_button.setEnabled(False)
            self.invite_world_button.clicked.connect(self.create_invitation)
            join_layout.addWidget(self.invite_world_button)
            self.revoke_invitation_button = QPushButton("撤销选中世界的邀请码")
            self.revoke_invitation_button.setEnabled(False)
            self.revoke_invitation_button.clicked.connect(self.revoke_invitation)
            join_layout.addWidget(self.revoke_invitation_button)
            self.switch_world_button = QPushButton("切换当前世界")
            self.switch_world_button.setEnabled(False)
            self.switch_world_button.clicked.connect(self.switch_current_world)
            join_layout.addWidget(self.switch_world_button)
            self.pause_world_button = QPushButton("暂停选中世界")
            self.pause_world_button.setEnabled(False)
            self.pause_world_button.clicked.connect(self.toggle_selected_world_pause)
            join_layout.addWidget(self.pause_world_button)
            self.delete_world_button = QPushButton("删除选中世界")
            self.delete_world_button.setToolTip("永久删除此世界及其事件、角色、记忆和额度记录；删除前需要再次输入世界名称确认。")
            self.delete_world_button.setEnabled(False)
            self.delete_world_button.clicked.connect(self.delete_selected_world)
            join_layout.addWidget(self.delete_world_button)
            join_form = QFormLayout()
            self.display_name = QLineEdit()
            self.display_name.setPlaceholderText("可选：进入世界时使用的角色名")
            join_form.addRow("显示名", self.display_name)
            self.invite_code = QLineEdit()
            self.invite_code.setPlaceholderText("可选：粘贴管理员邀请码即可加入，无需先选择世界")
            join_form.addRow("邀请码", self.invite_code)
            join_layout.addLayout(join_form)
            join_world = QPushButton("加入选中世界")
            join_world.setObjectName("primaryButton")
            self.join_button = join_world
            join_world.clicked.connect(self.join_selected_world)
            join_layout.addWidget(join_world)

            self.author_page = QWidget()
            self.author_page.setObjectName("authorPage")
            author_layout = QFormLayout(self.author_page)
            author_layout.setContentsMargins(18, 16, 18, 18)
            author_layout.setVerticalSpacing(10)
            author_layout.setHorizontalSpacing(14)
            self.worldview_prompt = QTextEdit()
            self.worldview_prompt.setPlaceholderText("描述想要的世界类型、基调、时代、玩法倾向等。世界模型只生成设定草案，不会替 Bot 玩家进行行动。")
            self.worldview_prompt.setFixedHeight(82)
            author_layout.addRow("世界生成提示词", self.worldview_prompt)
            generation_row = QHBoxLayout()
            self.generate_worldview_button = QPushButton("用世界模型生成草案")
            self.generate_worldview_button.clicked.connect(self.generate_worldview)
            generation_row.addWidget(self.generate_worldview_button)
            self.worldview_generation_status = QLabel("生成结果会填入下方可编辑字段；实际用量按世界模型账本记录。")
            self.worldview_generation_status.setWordWrap(True)
            generation_row.addWidget(self.worldview_generation_status, 1)
            author_layout.addRow(generation_row)
            self.world_name = QLineEdit()
            author_layout.addRow("世界名称", self.world_name)
            self.world_mode = QComboBox()
            self.world_mode.addItems(["静止式", "流动式"])
            author_layout.addRow("推进模式", self.world_mode)
            self.time_scale = QDoubleSpinBox()
            self.time_scale.setRange(0.1, 10)
            self.time_scale.setSingleStep(0.1)
            self.time_scale.setValue(1)
            self.time_scale.setSuffix("x")
            author_layout.addRow("时间倍率", self.time_scale)
            self.routine_threshold = QSpinBox()
            self.routine_threshold.setRange(1, 525600)
            self.routine_threshold.setValue(30)
            self.routine_threshold.setSuffix(" 分钟")
            author_layout.addRow("日常代管门槛", self.routine_threshold)
            self.interaction_timeout = QSpinBox()
            self.interaction_timeout.setRange(1, 60)
            self.interaction_timeout.setValue(5)
            self.interaction_timeout.setSuffix(" 分钟")
            author_layout.addRow("玩家回应倒计时", self.interaction_timeout)
            self.bot_turn = QCheckBox("用户行动后，Bot 行动一次")
            self.bot_turn.setObjectName("settingToggle")
            self.bot_turn.setChecked(True)
            author_layout.addRow(self.bot_turn)
            self.offline_bot = QCheckBox("允许 Bot 离线独自游玩")
            author_layout.addRow(self.offline_bot)
            self.offline_interval = QSpinBox()
            self.offline_interval.setRange(1, 525600)
            self.offline_interval.setValue(30)
            self.offline_interval.setSuffix(" 分钟")
            author_layout.addRow("离线机会间隔", self.offline_interval)
            self.offline_questions = QSpinBox()
            self.offline_questions.setRange(0, 10000)
            self.offline_questions.setValue(12)
            author_layout.addRow("每日最多询问", self.offline_questions)
            self.offline_actions = QSpinBox()
            self.offline_actions.setRange(0, 10000)
            self.offline_actions.setValue(6)
            author_layout.addRow("每日最多行动", self.offline_actions)
            self.world_mode.currentIndexChanged.connect(self.update_mode_fields)
            self.allow_death = QCheckBox("允许在线角色死亡")
            self.allow_permanent = QCheckBox("允许在线角色永久负面效果")
            self.allow_long_term = QCheckBox("允许在线角色长期失去行动能力")
            author_layout.addRow(self.allow_death)
            author_layout.addRow(self.allow_permanent)
            author_layout.addRow(self.allow_long_term)
            self.shallow_worldview = QTextEdit()
            self.shallow_worldview.setPlaceholderText("世界常识、公开地理、社会规则……")
            self.shallow_worldview.setFixedHeight(70)
            author_layout.addRow("浅层设定", self.shallow_worldview)
            self.deep_worldview = QTextEdit()
            self.deep_worldview.setPlaceholderText("只有世界模型可见的深层设定……")
            self.deep_worldview.setFixedHeight(70)
            author_layout.addRow("深层设定", self.deep_worldview)
            self.state_schema = QTextEdit()
            self.state_schema.setPlaceholderText(
                '{\n  "health": {"type": "integer", "minimum": 0, "maximum": 100},\n'
                '  "characters": {"type": "array", "items": {\n'
                '    "type": "object", "properties": {\n'
                '      "name": {"type": "string"},\n'
                '      "health": {"type": "integer", "minimum": 0}\n'
                '    }, "required": ["name", "health"]\n'
                '  }}\n}'
            )
            self.state_schema.setFixedHeight(150)
            author_layout.addRow("关键状态属性规则（JSON，可选）", self.state_schema)
            self.bot_persona = QTextEdit()
            self.bot_persona.setPlaceholderText("Bot 在世界中的人设（可留空）")
            self.bot_persona.setFixedHeight(60)
            author_layout.addRow("Bot 人设", self.bot_persona)
            self.player_persona = QTextEdit()
            self.player_persona.setPlaceholderText("用户本人的性格、身份与行为倾向")
            self.player_persona.setFixedHeight(60)
            author_layout.addRow("玩家本人设", self.player_persona)
            self.role_mode = QComboBox()
            self.role_mode.addItem("以自己身份参演", "self")
            self.role_mode.addItem("各自绑定一个角色", "character")
            author_layout.addRow("玩家参演方式", self.role_mode)
            self.player_character_name = QLineEdit()
            self.player_character_name.setPlaceholderText("用户固定操控的角色名")
            author_layout.addRow("用户绑定角色", self.player_character_name)
            self.player_character_persona = QTextEdit()
            self.player_character_persona.setPlaceholderText("该角色的身份、性格、能力与已知信息")
            self.player_character_persona.setFixedHeight(55)
            author_layout.addRow("用户角色设定", self.player_character_persona)
            self.bot_character_name = QLineEdit()
            self.bot_character_name.setPlaceholderText("Bot 固定操控的角色名")
            author_layout.addRow("Bot 绑定角色", self.bot_character_name)
            self.bot_character_persona = QTextEdit()
            self.bot_character_persona.setPlaceholderText("Bot 所操控角色的设定；与 Bot 自身人设分开填写")
            self.bot_character_persona.setFixedHeight(55)
            author_layout.addRow("Bot 角色设定", self.bot_character_persona)
            self.config_status = QLabel("新世界使用“创建世界”；参演方式和绑定角色在开局时固定。选中现有世界后，可加载并保存待生效配置。")
            self.config_status.setWordWrap(True)
            author_layout.addRow(self.config_status)
            config_buttons = QHBoxLayout()
            create = QPushButton("创建世界")
            create.setObjectName("primaryButton")
            create.clicked.connect(self.create_world)
            config_buttons.addWidget(create)
            load_config = QPushButton("加载选中世界配置")
            load_config.clicked.connect(self.load_selected_configuration)
            config_buttons.addWidget(load_config)
            save_config = QPushButton("保存到下一次推演")
            save_config.clicked.connect(self.save_selected_configuration)
            config_buttons.addWidget(save_config)
            self.apply_configuration_button = QPushButton("立即应用并推演一次")
            self.apply_configuration_button.setObjectName("applyWorldConfigurationOnce")
            self.apply_configuration_button.setAccessibleName("立即应用并推演一次")
            self.apply_configuration_button.setToolTip("对当前运行中的世界执行一次无玩家行动的世界推演，并消耗世界模型额度。")
            self.apply_configuration_button.setEnabled(False)
            self.apply_configuration_button.clicked.connect(self.apply_pending_configuration_once)
            config_buttons.addWidget(self.apply_configuration_button)
            author_layout.addRow(config_buttons)
            admin_tools = QHBoxLayout()
            self.admin_interactions_button = QPushButton("查看待回应和失败交互")
            self.admin_interactions_button.setEnabled(False)
            self.admin_interactions_button.clicked.connect(self.show_admin_interactions)
            admin_tools.addWidget(self.admin_interactions_button)
            self.world_nodes_button = QPushButton("查看节点并回滚")
            self.world_nodes_button.setEnabled(False)
            self.world_nodes_button.clicked.connect(self.show_world_nodes)
            admin_tools.addWidget(self.world_nodes_button)
            self.model_settings_button = QPushButton("模型供应商与并发设置")
            self.model_settings_button.setEnabled(False)
            self.model_settings_button.clicked.connect(self.show_model_settings)
            admin_tools.addWidget(self.model_settings_button)
            self.manage_accounts_button = QPushButton("账号管理与重置密码")
            self.manage_accounts_button.setEnabled(False)
            self.manage_accounts_button.clicked.connect(self.reset_player_password)
            admin_tools.addWidget(self.manage_accounts_button)
            self.audit_button = QPushButton("查看审计日志")
            self.audit_button.setEnabled(False)
            self.audit_button.clicked.connect(self.show_audit_logs)
            admin_tools.addWidget(self.audit_button)
            admin_tools.addStretch()
            author_layout.addRow(admin_tools)

            self.game_page = QWidget()
            self.game_page.setObjectName("gamePage")
            game_layout = QVBoxLayout(self.game_page)
            game_layout.setContentsMargins(14, 12, 14, 14)
            game_layout.setSpacing(10)
            game_header = QHBoxLayout()
            self.game_title = QLabel("尚未进入世界")
            self.game_title.setObjectName("gameTitle")
            self.game_title.setWordWrap(True)
            game_header.addWidget(self.game_title, 1)
            self.quota_button = QPushButton("额度设置")
            self.quota_button.setEnabled(False)
            self.quota_button.clicked.connect(self.edit_quota)
            game_header.addWidget(self.quota_button)
            self.backup_button = QPushButton("备份管理")
            self.backup_button.setEnabled(False)
            self.backup_button.clicked.connect(self.show_backups)
            game_header.addWidget(self.backup_button)
            refresh_game = QPushButton("刷新世界")
            refresh_game.clicked.connect(self.refresh_game)
            game_header.addWidget(refresh_game)
            back_to_worlds = QPushButton("返回世界列表")
            self.back_button = back_to_worlds
            back_to_worlds.clicked.connect(self.leave_game)
            game_header.addWidget(back_to_worlds)
            game_layout.addLayout(game_header)
            self.quota_status = QLabel("Token：尚未读取")
            self.quota_status.setWordWrap(True)
            game_layout.addWidget(self.quota_status)
            self.quota_scope_notice = QLabel("")
            self.quota_scope_notice.setObjectName("quotaScopeNotice")
            self.quota_scope_notice.setWordWrap(True)
            self.quota_scope_notice.setVisible(False)
            game_layout.addWidget(self.quota_scope_notice)
            presence_row = QHBoxLayout()
            self.presence_status = QLabel("在线")
            presence_row.addWidget(self.presence_status, 1)
            self.here_button = QPushButton("我在")
            self.here_button.clicked.connect(self.stay_online)
            presence_row.addWidget(self.here_button)
            game_layout.addLayout(presence_row)

            panes = QSplitter(Qt.Orientation.Horizontal)
            world_panel = QWidget()
            world_panel.setObjectName("worldPanel")
            world_layout = QVBoxLayout(world_panel)
            world_layout.setContentsMargins(14, 12, 14, 14)
            world_layout.setSpacing(9)
            world_screen_heading = QLabel("世界屏幕")
            world_screen_heading.setObjectName("sectionTitle")
            world_layout.addWidget(world_screen_heading)
            self.world_screen = QTextEdit()
            self.world_screen.setReadOnly(True)
            world_layout.addWidget(self.world_screen, 1)
            self.interaction_list = QListWidget()
            self.interaction_list.setMaximumHeight(112)
            self.interaction_list.setAccessibleName("待回应交互")
            world_layout.addWidget(QLabel("待回应交互 / 倒计时"))
            world_layout.addWidget(self.interaction_list)
            response_row = QHBoxLayout()
            self.interaction_response = QLineEdit()
            self.interaction_response.setObjectName("interactionResponseInput")
            self.interaction_response.setAccessibleName("交互回应")
            self.interaction_response.setPlaceholderText("收到回应请求时，在这里写下你的回应")
            response_row.addWidget(self.interaction_response, 1)
            self.respond_button = QPushButton("提交回应")
            self.respond_button.setObjectName("submitInteractionResponse")
            self.respond_button.setAccessibleName("提交交互回应")
            self.respond_button.setEnabled(False)
            self.respond_button.clicked.connect(self.send_interaction_response)
            response_row.addWidget(self.respond_button)
            world_layout.addLayout(response_row)
            action_row = QHBoxLayout()
            self.action_input = QLineEdit()
            self.action_input.setPlaceholderText("输入角色行动，例如：走到集市观察周围")
            action_row.addWidget(self.action_input, 1)
            send_action = QPushButton("提交行动")
            send_action.setObjectName("primaryButton")
            self.send_action_button = send_action
            send_action.clicked.connect(self.send_action)
            action_row.addWidget(send_action)
            world_layout.addLayout(action_row)
            panes.addWidget(world_panel)

            chat_panel = QWidget()
            chat_panel.setObjectName("chatPanel")
            chat_layout = QVBoxLayout(chat_panel)
            chat_layout.setContentsMargins(14, 12, 14, 14)
            chat_layout.setSpacing(9)
            chat_heading = QLabel("游戏内聊天")
            chat_heading.setObjectName("sectionTitle")
            chat_layout.addWidget(chat_heading)
            self.game_chat = QTextEdit()
            self.game_chat.setReadOnly(True)
            chat_layout.addWidget(self.game_chat, 1)
            chat_row = QHBoxLayout()
            self.chat_input = QLineEdit()
            self.chat_input.setPlaceholderText("与 Bot 或其他玩家聊天")
            chat_row.addWidget(self.chat_input, 1)
            send_chat = QPushButton("发送")
            send_chat.setObjectName("primaryButton")
            self.send_chat_button = send_chat
            send_chat.clicked.connect(self.send_chat)
            chat_row.addWidget(send_chat)
            chat_layout.addLayout(chat_row)
            panes.addWidget(chat_panel)
            panes.setSizes([700, 400])
            game_layout.addWidget(panes, 1)

            self.stack.addWidget(self.join_page)
            author_scroll = QScrollArea()
            author_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            author_scroll.setWidgetResizable(True)
            author_scroll.setWidget(self.author_page)
            self.stack.addWidget(author_scroll)
            self.stack.addWidget(self.game_page)
            self.stack.currentChanged.connect(self._update_shell_visibility)
            self.mode.currentIndexChanged.connect(self.change_mode)
            content_surface = QWidget()
            content_surface.setObjectName("contentSurface")
            content_layout = QVBoxLayout(content_surface)
            content_layout.setContentsMargins(8, 8, 8, 8)
            content_layout.addWidget(self.stack, 1)
            layout.addWidget(content_surface, 1)
            self.status = QLabel("尚未登录")
            self.status.setObjectName("footerStatus")
            self.status.setAlignment(Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(self.status)
            self.action_input.returnPressed.connect(self.send_action)
            self.chat_input.returnPressed.connect(self.send_chat)
            self.worlds.itemDoubleClicked.connect(lambda _: self.join_selected_world())
            self.worlds.currentRowChanged.connect(self._selected_world_changed)
            self.timer = QTimer(self)
            self.timer.setInterval(5000)
            self.timer.timeout.connect(self.poll_game)
            self.timer.start()
            self.activity_monitor = ActivityMonitor(self)
            QApplication.instance().installEventFilter(self.activity_monitor)
            self.update_mode_fields()

        def run_task(self, task, on_complete, *, on_error=None) -> None:
            worker = RequestWorker(task, self)
            self.workers.add(worker)
            worker.completed.connect(on_complete)

            def failed(message):
                detail = str(message).strip() or "请求失败，服务端未返回错误详情。请检查上游状态后重试。"
                if on_error:
                    on_error(detail)
                else:
                    QMessageBox.warning(self, "请求失败", detail)

            worker.failed.connect(failed)
            worker.finished.connect(lambda: self.workers.discard(worker))
            worker.finished.connect(lambda: QTimer.singleShot(0, self.close) if self.closing_requested and not self.workers else None)
            worker.finished.connect(worker.deleteLater)
            worker.start()

        def _update_shell_visibility(self, *_args) -> None:
            self.mode.setVisible(self.stack.currentWidget() != self.game_page)

        def poll_game(self) -> None:
            if self.stack.currentWidget() == self.game_page:
                self.send_presence()
                if not self.play_busy:
                    self.refresh_game()

        def _update_game_controls(self) -> None:
            can_submit = self.game_state_ready and self.gameplay_allowed and not self.play_busy
            can_respond = (
                self.game_state_ready
                and self.gameplay_allowed
                and bool(self.active_interaction_id)
                and not self.play_busy
            )
            self.action_input.setEnabled(can_submit)
            self.send_action_button.setEnabled(can_submit)
            self.chat_input.setEnabled(can_submit)
            self.send_chat_button.setEnabled(can_submit)
            self.interaction_response.setEnabled(can_respond)
            self.respond_button.setEnabled(can_respond)

        def update_mode_fields(self):
            flowing = self.world_mode.currentIndex() == 1
            for field in (self.time_scale, self.routine_threshold, self.offline_bot, self.offline_interval, self.offline_questions, self.offline_actions):
                field.setEnabled(flowing)

        def change_mode(self, index):
            if index == 1 and (not self.account or not self.account.get("is_admin") or not self.api.management_token):
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(0)
                self.mode.blockSignals(False)
                QMessageBox.information(self, "需要创作连接", "请先登录管理员账号，再输入服务端控制台显示的管理员连接码。")
                return
            if self.stack.currentWidget() == self.game_page:
                self.leave_game()
            self.stack.setCurrentIndex(index)

        def toggle_management_connection(self):
            if not self.account or not self.account.get("is_admin") or not self.api.token:
                QMessageBox.warning(self, "权限不足", "请先登录管理员账号。")
                return
            if self.api.management_token:
                try:
                    self.api.disconnect_management()
                except RuntimeError as exc:
                    QMessageBox.warning(self, "断开失败", str(exc))
                    return
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(0)
                self.mode.blockSignals(False)
                self.mode.setEnabled(False)
                self.management_button.setText("连接创作模式")
                self._set_admin_controls(False)
                self.status.setText(f"已登录：{self.account['username']}（创作连接已断开）")
                self.refresh_worlds()
                return
            pairing_code, accepted = QInputDialog.getText(
                self,
                "连接创作模式",
                "请输入服务端启动控制台显示的管理员连接码：",
                QLineEdit.EchoMode.Password,
            )
            if not accepted or not pairing_code.strip():
                return
            try:
                self.api.connect_management(pairing_code.strip())
            except RuntimeError as exc:
                QMessageBox.warning(self, "连接失败", str(exc))
                return
            self.management_button.setText("断开创作连接")
            self.mode.setEnabled(True)
            self._set_admin_controls(True)
            self.refresh_worlds()
            self.status.setText(f"已登录：{self.account['username']}（创作连接已启用）")
            self.mode.setCurrentIndex(1)

        def _set_admin_controls(self, enabled: bool) -> None:
            enabled = bool(enabled and self.account and self.account.get("is_admin") and self.api.management_token)
            self.invite_world_button.setEnabled(enabled)
            self.revoke_invitation_button.setEnabled(enabled)
            self.switch_world_button.setEnabled(enabled)
            self.quota_button.setEnabled(enabled)
            self.backup_button.setEnabled(enabled)
            self.model_settings_button.setEnabled(enabled)
            self.manage_accounts_button.setEnabled(enabled)
            self.audit_button.setEnabled(enabled)
            self.admin_interactions_button.setEnabled(enabled)
            self.world_nodes_button.setEnabled(enabled)
            self._update_pause_world_button()
            self._update_delete_world_button()
            self._update_apply_configuration_button()

        def _selected_world_changed(self, *_args) -> None:
            selected = self._selected_world_id()
            if selected != self.editing_world_id:
                self.pending_configuration_available = False
                self.editing_world_is_current = False
            self._update_delete_world_button()
            self._update_pause_world_button()
            self._update_apply_configuration_button()

        def _update_pause_world_button(self) -> None:
            button = getattr(self, "pause_world_button", None)
            if button is None:
                return
            state = self.world_statuses.get(self._selected_world_id() or "")
            enabled = bool(
                self.account
                and self.account.get("is_admin")
                and self.api.management_token
                and state in {"active", "paused"}
                and not self.play_busy
            )
            button.setEnabled(enabled)
            button.setText("恢复选中世界" if state == "paused" else "暂停选中世界")

        def toggle_selected_world_pause(self) -> None:
            world_id = self._selected_world_id()
            state = self.world_statuses.get(world_id or "")
            if not world_id or state not in {"active", "paused"}:
                return
            paused = state == "active"
            action = "暂停" if paused else "恢复"
            answer = QMessageBox.question(
                self,
                f"{action}世界",
                f"确定{action}当前选中的世界？"
                + ("暂停期间不会推进剧情或触发超时接管。" if paused else "世界将从暂停状态继续运行。"),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            endpoint = "pause" if paused else "resume"
            try:
                self.api.request("POST", f"/api/v1/worlds/{world_id}/{endpoint}", {})
                self.status.setText(f"世界已{action}")
                self.refresh_worlds()
            except RuntimeError as exc:
                QMessageBox.warning(self, f"{action}失败", str(exc))

        def _update_delete_world_button(self) -> None:
            button = getattr(self, "delete_world_button", None)
            if button is None:
                return
            button.setEnabled(
                bool(
                    self.account
                    and self.account.get("is_admin")
                    and self.api.management_token
                    and self._selected_world_id()
                    and not self.delete_world_busy
                    and not self.play_busy
                )
            )

        def _update_apply_configuration_button(self) -> None:
            button = getattr(self, "apply_configuration_button", None)
            if button is None:
                return
            button.setEnabled(
                bool(
                    self.account
                    and self.account.get("is_admin")
                    and self.api.management_token
                    and self.editing_world_id
                    and self.editing_world_id == self._selected_world_id()
                    and self.editing_world_is_current
                    and self.pending_configuration_available
                    and not self.apply_configuration_busy
                    and not self.play_busy
                )
            )

        def show_model_settings(self) -> None:
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "权限不足", "请先连接创作模式。")
                return
            try:
                settings = self.api.request("GET", "/api/v1/admin/model-profiles")
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取模型配置失败", str(exc))
                return

            dialog = QDialog(self)
            dialog.setWindowTitle("模型供应商与并发设置")
            dialog.resize(720, 650)
            layout = QVBoxLayout(dialog)
            tabs = QTabWidget()
            layout.addWidget(tabs, 1)
            role_values = settings.get("roles", {})
            usage_support = dict(settings.get("usage_support", {}))
            provider_options = [
                ("OpenAI / OpenAI 兼容", "openai-compatible"),
                ("Google Gemini", "gemini"),
                ("Anthropic", "anthropic"),
            ]
            status = QLabel("密钥只发送到服务端并加密保存；已有密钥不会回传。留空表示保留原密钥。")
            status.setWordWrap(True)
            layout.addWidget(status)

            mode_row = QHBoxLayout()
            mode_row.addWidget(QLabel("Bot 运行模式"))
            bot_mode = QComboBox()
            bot_mode.setObjectName("botModeSwitch")
            bot_mode.setAccessibleName("Bot 运行模式")
            bot_mode.addItem("独立模式", "standalone")
            bot_mode.addItem("AstrBot 接入模式", "astrbot")
            current_bot_mode = str(settings.get("bot_mode", "standalone"))
            current_mode_index = bot_mode.findData(current_bot_mode)
            bot_mode.setCurrentIndex(current_mode_index if current_mode_index >= 0 else 0)
            mode_availability = dict(settings.get("bot_mode_availability") or {})
            switch_mode = QPushButton("确认切换")
            switch_mode.setObjectName("confirmBotModeSwitch")
            switch_mode.setAccessibleName("确认切换 Bot 运行模式")
            mode_status = QLabel(
                "切换前暂停当前世界；切换后世界保持暂停，需由管理员手动恢复。"
            )
            mode_status.setWordWrap(True)
            mode_row.addWidget(bot_mode)
            mode_row.addWidget(switch_mode)
            mode_row.addWidget(mode_status, 1)
            layout.addLayout(mode_row)

            def switch_bot_mode():
                target_mode = str(bot_mode.currentData())
                if target_mode == str(settings.get("bot_mode", "standalone")):
                    return
                target_name = "AstrBot 接入模式" if target_mode == "astrbot" else "独立模式"
                if not mode_availability.get(target_mode, False):
                    reason = (
                        "请先配置服务端 AstrBot 连接凭证。"
                        if target_mode == "astrbot"
                        else "请先保存独立 Bot 模型配置。"
                    )
                    QMessageBox.warning(dialog, "模式不可用", reason)
                    return
                answer = QMessageBox.question(
                    dialog,
                    "切换 Bot 运行模式",
                    f"将切换到{target_name}。当前运行中的世界必须先暂停；切换成功后世界会继续保持暂停，"
                    "需要管理员手动恢复。世界存档和 Token 账本会保留。\n\n确认切换？",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                try:
                    result = self.api.request(
                        "POST",
                        "/api/v1/admin/bot-mode",
                        {"bot_mode": target_mode, "confirmed": True},
                    )
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "模式切换失败", str(exc))
                    return
                settings["bot_mode"] = str(result.get("bot_mode", target_mode))
                mode_availability.update(result.get("bot_mode_availability") or {})
                new_index = bot_mode.findData(settings["bot_mode"])
                if new_index >= 0:
                    bot_mode.setCurrentIndex(new_index)
                mode_status.setText(
                    f"已切换到{target_name}；世界保持暂停。模式配置版本 {result.get('revision', '?')}。"
                )
                self.refresh_worlds()

            switch_mode.clicked.connect(switch_bot_mode)

            concurrency_row = QHBoxLayout()
            concurrency_row.addWidget(QLabel("全服务模型并发上限"))
            concurrency = QSpinBox()
            concurrency.setRange(1, 32)
            concurrency.setValue(int(settings.get("max_concurrent_calls", 2)))
            concurrency_row.addWidget(concurrency)
            concurrency_row.addStretch()
            layout.addLayout(concurrency_row)

            def current_profile(role: str) -> dict[str, Any]:
                profile = role_values.get(role)
                return dict(profile) if isinstance(profile, dict) else {}

            def usage_support_text(role: str) -> str:
                state = usage_support.get(role, "unknown")
                if state == "missing":
                    return "统计缺口：模型响应未返回可用的实际 Token 用量"
                if state == "supported":
                    return "已确认：模型响应返回实际 Token 用量"
                return "尚未实测：首次模型响应后检测用量支持"

            def make_text(value: str, placeholder: str = ""):
                field = QLineEdit()
                field.setText(value)
                field.setPlaceholderText(placeholder)
                return field

            def make_model_tab(role: str, title: str, *, disabled: bool = False) -> None:
                page = QWidget()
                form = QFormLayout(page)
                profile = current_profile(role)
                provider = QComboBox()
                for label, value in provider_options:
                    provider.addItem(label, value)
                current_provider = str(profile.get("provider", "openai-compatible"))
                provider_index = provider.findData(current_provider)
                if provider_index >= 0:
                    provider.setCurrentIndex(provider_index)
                model = make_text(str(profile.get("model", "")), "填写供应商提供的模型 ID")
                endpoint = make_text(str(profile.get("endpoint", "")), "可留空使用该供应商的默认地址")
                api_key = make_text(
                    "",
                    "已配置密钥；留空保留。输入内容会替换现有密钥。",
                )
                api_key.setEchoMode(QLineEdit.EchoMode.Password)
                timeout = QSpinBox()
                timeout.setRange(1, 600)
                timeout.setSuffix(" 秒")
                timeout.setValue(int(profile.get("timeout", 120)))
                output_tokens = QSpinBox()
                output_tokens.setRange(1, 65536)
                output_tokens.setSuffix(" Token")
                output_tokens.setValue(
                    int(profile.get("max_output_tokens", 8192 if role == "world" else 1024))
                )
                worldview_output_tokens = None
                if role == "world":
                    worldview_output_tokens = QSpinBox()
                    worldview_output_tokens.setRange(1, 65536)
                    worldview_output_tokens.setSuffix(" Token")
                    worldview_output_tokens.setValue(
                        int(profile.get("worldview_max_output_tokens", 3072))
                    )
                clear_key = QCheckBox("清除已保存的 API Key")
                form.addRow("供应商", provider)
                form.addRow("模型 ID", model)
                form.addRow("API 地址", endpoint)
                form.addRow("API Key", api_key)
                form.addRow("请求超时", timeout)
                form.addRow("单次输出 Token 上限", output_tokens)
                if worldview_output_tokens is not None:
                    form.addRow("世界观生成 Token 上限", worldview_output_tokens)
                form.addRow(clear_key)
                saved_key = QLabel(
                    "已保存密钥" if profile.get("api_key_configured") else "尚未设置密钥"
                )
                form.addRow("密钥状态", saved_key)
                usage_status = QLabel(usage_support_text(role))
                usage_status.setWordWrap(True)
                form.addRow("Token 统计", usage_status)
                if role == "bot" and settings.get("bot_mode") == "astrbot":
                    note = QLabel(
                        "AstrBot 接入模式实际使用 AstrBot 当前生效的 Bot 模型；此处配置仅供切回独立模式时使用。"
                    )
                    note.setWordWrap(True)
                    form.addRow(note)
                save = QPushButton(f"保存{title}配置")
                form.addRow(save)

                def save_model_profile():
                    payload = {
                        "role": role,
                        "config": {
                            "provider": str(provider.currentData()),
                            "model": model.text().strip(),
                            "endpoint": endpoint.text().strip(),
                            "api_key": api_key.text(),
                            "timeout": timeout.value(),
                            "max_output_tokens": output_tokens.value(),
                            **(
                                {
                                    "worldview_max_output_tokens": worldview_output_tokens.value()
                                }
                                if worldview_output_tokens is not None
                                else {}
                            ),
                        },
                        "max_concurrent_calls": concurrency.value(),
                        "clear_api_key": clear_key.isChecked(),
                    }
                    try:
                        result = self.api.request("PATCH", "/api/v1/admin/model-profiles", payload)
                    except RuntimeError as exc:
                        QMessageBox.warning(dialog, "保存失败", str(exc))
                        return
                    role_values.update(result.get("roles", {}))
                    usage_support.update(result.get("usage_support", {}))
                    mode_availability.update(result.get("bot_mode_availability") or {})
                    usage_status.setText(usage_support_text(role))
                    api_key.clear()
                    saved_key.setText(
                        "已保存密钥"
                        if current_profile(role).get("api_key_configured")
                        else "尚未设置密钥"
                    )
                    status.setText(f"{title}配置已保存，版本 {result.get('revision', '?')}。")

                save.clicked.connect(save_model_profile)
                tabs.addTab(page, title)

            make_model_tab("world", "世界模型")
            make_model_tab("bot", "Bot 模型")

            memory_page = QWidget()
            memory_layout = QVBoxLayout(memory_page)
            memory_enabled = QCheckBox("启用独立记忆嵌入与重排序")
            memory_enabled.setChecked(
                isinstance(role_values.get("embedding"), dict)
                and isinstance(role_values.get("rerank"), dict)
            )
            memory_layout.addWidget(memory_enabled)
            memory_form = QFormLayout()
            embedding = current_profile("embedding")
            rerank = current_profile("rerank")
            embedding_model = make_text(str(embedding.get("model", "")), "例如 text-embedding 模型 ID")
            embedding_endpoint = make_text(str(embedding.get("endpoint", "")), "Embedding API 地址")
            embedding_key = make_text("", "已配置密钥；留空保留")
            embedding_key.setEchoMode(QLineEdit.EchoMode.Password)
            embedding_protocol = QComboBox()
            embedding_protocol.addItem("OpenAI 兼容", "openai")
            embedding_protocol.addItem("DashScope 兼容", "dashscope")
            protocol_index = embedding_protocol.findData(str(embedding.get("protocol", "dashscope")))
            if protocol_index >= 0:
                embedding_protocol.setCurrentIndex(protocol_index)
            memory_form.addRow("嵌入模型 ID", embedding_model)
            memory_form.addRow("嵌入 API 地址", embedding_endpoint)
            memory_form.addRow("嵌入 API Key", embedding_key)
            memory_form.addRow("嵌入协议", embedding_protocol)
            embedding_usage_status = QLabel(usage_support_text("embedding"))
            embedding_usage_status.setWordWrap(True)
            memory_form.addRow("嵌入 Token 统计", embedding_usage_status)
            rerank_model = make_text(str(rerank.get("model", "")), "例如 rerank 模型 ID")
            rerank_endpoint = make_text(str(rerank.get("endpoint", "")), "Rerank API 地址")
            rerank_key = make_text("", "已配置密钥；留空保留")
            rerank_key.setEchoMode(QLineEdit.EchoMode.Password)
            memory_form.addRow("重排序模型 ID", rerank_model)
            memory_form.addRow("重排序 API 地址", rerank_endpoint)
            memory_form.addRow("重排序 API Key", rerank_key)
            rerank_usage_status = QLabel(usage_support_text("rerank"))
            rerank_usage_status.setWordWrap(True)
            memory_form.addRow("重排序 Token 统计", rerank_usage_status)
            memory_layout.addLayout(memory_form)
            memory_note = QLabel(
                "记忆模型使用独立 Token 账本。当前嵌入支持 OpenAI 或 DashScope 兼容接口，"
                "重排序支持 DashScope 兼容接口。"
            )
            memory_note.setWordWrap(True)
            memory_layout.addWidget(memory_note)
            save_memory = QPushButton("保存记忆模型配置")
            memory_layout.addWidget(save_memory)

            def save_memory_profiles():
                config = None
                if memory_enabled.isChecked():
                    config = {
                        "embedding": {
                            "model": embedding_model.text().strip(),
                            "endpoint": embedding_endpoint.text().strip(),
                            "api_key": embedding_key.text(),
                            "protocol": str(embedding_protocol.currentData()),
                        },
                        "rerank": {
                            "model": rerank_model.text().strip(),
                            "endpoint": rerank_endpoint.text().strip(),
                            "api_key": rerank_key.text(),
                        },
                    }
                try:
                    result = self.api.request(
                        "PATCH",
                        "/api/v1/admin/model-profiles",
                        {
                            "role": "memory",
                            "config": config,
                            "max_concurrent_calls": concurrency.value(),
                        },
                    )
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "保存失败", str(exc))
                    return
                role_values.update(result.get("roles", {}))
                usage_support.update(result.get("usage_support", {}))
                embedding_usage_status.setText(usage_support_text("embedding"))
                rerank_usage_status.setText(usage_support_text("rerank"))
                embedding_key.clear()
                rerank_key.clear()
                status.setText(f"记忆模型配置已保存，版本 {result.get('revision', '?')}。")

            save_memory.clicked.connect(save_memory_profiles)
            tabs.addTab(memory_page, "记忆模型")

            dialog.exec()

        def show_audit_logs(self) -> None:
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "权限不足", "请先连接创作模式。")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("管理员审计日志")
            dialog.resize(900, 520)
            layout = QVBoxLayout(dialog)
            split = QSplitter(Qt.Orientation.Horizontal)
            entries = QListWidget()
            details = QTextEdit()
            details.setReadOnly(True)
            split.addWidget(entries)
            split.addWidget(details)
            split.setStretchFactor(0, 2)
            split.setStretchFactor(1, 3)
            layout.addWidget(split, 1)
            message = QLabel("")
            layout.addWidget(message)
            buttons = QHBoxLayout()
            refresh = QPushButton("刷新")
            close = QPushButton("关闭")
            buttons.addWidget(refresh)
            buttons.addStretch()
            buttons.addWidget(close)
            layout.addLayout(buttons)

            def load_logs() -> None:
                try:
                    result = self.api.request("GET", "/api/v1/admin/audit?limit=100")
                    entries.clear()
                    for entry in result.get("entries", []):
                        item = QListWidgetItem(f"{entry['created_at']} · {entry['action']} · {entry['target']} · {entry['result']}")
                        item.setData(Qt.ItemDataRole.UserRole, entry)
                        entries.addItem(item)
                    message.setText(f"显示最近 {entries.count()} 条日志")
                    if entries.count():
                        entries.setCurrentRow(0)
                except RuntimeError as exc:
                    message.setText(str(exc))
                    details.clear()

            def show_entry(item, _previous=None) -> None:
                entry = item.data(Qt.ItemDataRole.UserRole) if item else None
                details.setPlainText(json.dumps(entry, ensure_ascii=False, indent=2) if entry else "")

            entries.currentItemChanged.connect(show_entry)
            refresh.clicked.connect(load_logs)
            close.clicked.connect(dialog.accept)
            load_logs()
            dialog.exec()

        def reset_player_password(self) -> None:
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "权限不足", "请先连接创作模式。")
                return
            try:
                accounts = self.api.request("GET", "/api/v1/admin/accounts").get("accounts", [])
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取失败", str(exc))
                return
            candidates = [item for item in accounts if item.get("id") != self.account.get("id")]
            if not candidates:
                QMessageBox.information(self, "没有可重置的账号", "当前没有其他账号。管理员可在账号设置中修改自己的密码。")
                return
            labels = [f"{item['username']}（{'管理员' if item.get('is_admin') else '玩家'}）" for item in candidates]
            selected, accepted = QInputDialog.getItem(self, "重置账号密码", "选择要重置密码的账号：", labels, 0, False)
            if not accepted:
                return
            target = candidates[labels.index(selected)]
            new_password, accepted = QInputDialog.getText(
                self, "重置账号密码", f"为 {target['username']} 设置至少 8 位的新密码：", QLineEdit.EchoMode.Password
            )
            if not accepted:
                return
            if len(new_password) < 8:
                QMessageBox.warning(self, "密码无效", "密码至少需要 8 位。")
                return
            confirm, accepted = QInputDialog.getText(self, "确认新密码", "再次输入新密码：", QLineEdit.EchoMode.Password)
            if not accepted:
                return
            if confirm != new_password:
                QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致，没有执行重置。")
                return
            try:
                self.api.request("POST", f"/api/v1/admin/accounts/{target['id']}/reset-password", {"new_password": new_password})
            except RuntimeError as exc:
                QMessageBox.warning(self, "重置失败", str(exc))
                return
            QMessageBox.information(self, "密码已重置", f"{target['username']} 的所有登录会话已撤销，请将新密码安全地交给该用户。")

        def stay_online(self):
            self.last_activity = time.monotonic()
            self.send_presence()

        def send_presence(self):
            if not self.current_world_id or not self.api.token or self.presence_busy:
                return
            self.presence_busy = True
            world_id = self.current_world_id
            activity = self.last_activity
            active = activity > self.last_sent_activity

            def received(data):
                self.presence_busy = False
                if self.current_world_id != world_id:
                    return
                if active:
                    self.last_sent_activity = activity
                idle = data.get("idle_seconds", 0)
                self.presence_status.setText("离线" if not data.get("online") else "即将离线，请确认仍在场" if idle >= 240 else "在线")

            def failed(message):
                self.presence_busy = False
                self.presence_status.setText("连接中断")

            self.run_task(lambda: self.api.request("POST", f"/api/v1/worlds/{world_id}/presence", {"active": active}), received, on_error=failed)

        def leave_game(self):
            if self.play_busy:
                return
            world_id = self.current_world_id
            self.current_world_id = None
            self.game_state_ready = False
            self.gameplay_allowed = False
            self.active_interaction_id = None
            self._update_game_controls()
            self.stack.setCurrentWidget(self.join_page)
            if world_id and self.api.token:
                self.run_task(lambda: self.api.request("POST", f"/api/v1/worlds/{world_id}/leave", {}), lambda _: None, on_error=lambda _: None)

        def set_play_busy(self, busy: bool) -> None:
            self.play_busy = busy
            self._update_game_controls()
            self.backup_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.invite_world_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.revoke_invitation_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.manage_accounts_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.audit_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.admin_interactions_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self._update_delete_world_button()
            self.mode.setEnabled(not busy and bool(self.account and self.account.get("is_admin") and self.api.management_token))
            self.management_button.setEnabled(not busy and bool(self.account and self.account.get("is_admin")))
            self.account_button.setEnabled(not busy and self.account is not None)
            self._update_apply_configuration_button()
            for widget in (self.url, self.username, self.password, self.login_button, self.register_button, self.back_button):
                widget.setEnabled(not busy)

        def show_tls_settings(self) -> None:
            dialog = QDialog(self)
            dialog.setWindowTitle("TLS 连接证书")
            dialog.resize(680, 220)
            layout = QFormLayout(dialog)
            ca = QLineEdit(self.api.tls_ca_file)
            cert = QLineEdit(self.api.tls_client_cert_file)
            key = QLineEdit(self.api.tls_client_key_file)

            def add_file_row(label: str, field: QLineEdit, caption: str, file_filter: str) -> None:
                row = QHBoxLayout()
                row.addWidget(field, 1)
                browse = QPushButton("选择…")
                def choose() -> None:
                    path, _ = QFileDialog.getOpenFileName(dialog, caption, field.text(), file_filter)
                    if path:
                        field.setText(path)
                browse.clicked.connect(choose)
                row.addWidget(browse)
                layout.addRow(label, row)

            add_file_row("受信任的 CA（留空使用系统信任）", ca, "选择 CA 证书", "证书 (*.pem *.crt *.cer);;所有文件 (*)")
            add_file_row("客户端证书（双向 TLS）", cert, "选择客户端证书", "证书 (*.pem *.crt);;所有文件 (*)")
            add_file_row("客户端私钥", key, "选择客户端私钥", "私钥 (*.pem *.key);;所有文件 (*)")
            layout.addRow(QLabel("HTTPS 始终校验证书和主机名；不会提供跳过验证的选项。"))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            layout.addRow(buttons)
            def save() -> None:
                if not self._setup():
                    return
                self.api.tls_ca_file = ca.text().strip()
                self.api.tls_client_cert_file = cert.text().strip()
                self.api.tls_client_key_file = key.text().strip()
                try:
                    self.api._tls_context()
                except (OSError, ssl.SSLError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, "TLS 设置无效", str(exc))
                    return
                dialog.accept()
            buttons.accepted.connect(save)
            buttons.rejected.connect(dialog.reject)
            dialog.exec()

        def _setup(self) -> bool:
            base_url = self.url.text().strip().rstrip("/")
            if not self.api.requires_insecure_remote_http_confirmation(base_url):
                self.api.base_url = base_url
                return True
            if self._http_transport_approved_url == base_url:
                self.api.base_url = base_url
                return True
            result = QMessageBox.warning(
                self,
                "远程 HTTP 未加密",
                "当前服务地址使用未加密的 HTTP。账号密码、管理凭证、模型 API Key、"
                "世界内容和聊天内容可能以明文经过网络。\n\n"
                "只有在确认连接经过 Tailscale、WireGuard 等可信加密组网，或你明确接受"
                "该网络风险时才继续；否则请取消并配置 HTTPS/mTLS。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return False
            self._http_transport_approved_url = base_url
            self.api.base_url = base_url
            return True

        def login(self) -> None:
            if not self._setup():
                return
            try:
                account = self.api.login(self.username.text(), self.password.text())
                self._set_account(account)
            except RuntimeError as exc:
                QMessageBox.warning(self, "登录失败", str(exc))

        def register(self) -> None:
            if not self._setup():
                return
            if not self.username.text().strip() or not self.password.text():
                QMessageBox.warning(self, "注册失败", "请先填写用户名和至少 8 位密码")
                return
            kind, accepted = QInputDialog.getItem(
                self,
                "注册账号",
                "选择注册凭证类型：管理员使用服务端本机控制台的一次性初始化码；玩家使用管理员提供的邀请。",
                ["初始化管理员账号", "使用世界邀请加入"],
                0,
                False,
            )
            if not accepted:
                return
            credential, accepted = QInputDialog.getText(
                self,
                "注册账号",
                "请输入服务端控制台显示的初始化码：" if kind == "初始化管理员账号" else "请输入管理员提供的邀请码：",
                QLineEdit.EchoMode.Password,
            )
            if not accepted or not credential.strip():
                return
            try:
                if kind == "初始化管理员账号":
                    account = self.api.register(self.username.text(), self.password.text(), initialization_code=credential.strip())
                else:
                    account = self.api.register(self.username.text(), self.password.text(), invitation_code=credential.strip())
                self._set_account(account)
                joined_world = self.registered_world_id
                if joined_world:
                    self.open_world(joined_world)
            except RuntimeError as exc:
                QMessageBox.warning(self, "注册失败", str(exc))

        def _set_account(self, account: dict[str, Any]) -> None:
            self.account = account
            self.registered_world_id = self.api.registered_world_id
            self.username.setText(account["username"])
            self.status.setText(f"已登录：{account['username']}（管理员：{'是' if account['is_admin'] else '否'}）")
            self.connection_toggle.setChecked(False)
            self.account_button.setEnabled(True)
            self.password.clear()
            self.management_button.setText("断开创作连接" if self.api.management_token else "连接创作模式")
            self.management_button.setEnabled(bool(account.get("is_admin")))
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(0)
            self.mode.blockSignals(False)
            self.mode.setEnabled(bool(account.get("is_admin") and self.api.management_token))
            self._set_admin_controls(bool(self.api.management_token))
            self.refresh_worlds()

        def show_account_settings(self) -> None:
            if not self.account:
                QMessageBox.warning(self, "尚未登录", "请先登录账号")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("账号设置")
            form = QFormLayout(dialog)
            username = QLineEdit(self.account["username"])
            form.addRow("新用户名", username)
            current_password = QLineEdit()
            current_password.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("当前密码", current_password)
            new_password = QLineEdit()
            new_password.setEchoMode(QLineEdit.EchoMode.Password)
            new_password.setPlaceholderText("留空表示不修改密码")
            form.addRow("新密码", new_password)
            confirm_password = QLineEdit()
            confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
            confirm_password.setPlaceholderText("修改密码时填写")
            form.addRow("确认新密码", confirm_password)
            try:
                qq_status = self.api.qq_binding_status()
            except RuntimeError as exc:
                qq_status = {"bound": False, "qq_id": None}
                QMessageBox.warning(dialog, "读取 QQ 绑定状态失败", str(exc))
            qq_label = QLabel(
                f"已绑定 QQ：{qq_status['qq_id']}" if qq_status.get("bound") else "未绑定 QQ（可选）"
            )
            form.addRow("QQ 身份", qq_label)
            make_qq_code = QPushButton("生成 QQ 私聊绑定码")
            make_qq_code.setToolTip("绑定码 5 分钟有效，只能使用一次")
            form.addRow("绑定操作", make_qq_code)
            unbind_qq_button = QPushButton("解除 QQ 绑定")
            unbind_qq_button.setEnabled(bool(qq_status.get("bound")))
            form.addRow("", unbind_qq_button)

            def create_qq_code() -> None:
                try:
                    result = self.api.create_qq_binding_code()
                    QMessageBox.information(
                        dialog,
                        "QQ 绑定码",
                        "请从要绑定的 QQ 账号私聊 Bot，发送：\n"
                        f"/vd bind {result['code']}\n\n"
                        "绑定码 5 分钟后失效，只能使用一次。",
                    )
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "生成绑定码失败", str(exc))

            def remove_qq_binding() -> None:
                current = current_password.text()
                if not current:
                    QMessageBox.warning(dialog, "解除绑定失败", "请先填写当前密码")
                    return
                if QMessageBox.question(dialog, "解除 QQ 绑定", "解除后，QQ 管理命令将不可用，确定继续？") != QMessageBox.StandardButton.Yes:
                    return
                try:
                    self.api.unbind_qq(current)
                    qq_label.setText("未绑定 QQ（可选）")
                    unbind_qq_button.setEnabled(False)
                    QMessageBox.information(dialog, "解除成功", "QQ 身份已解除绑定")
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "解除绑定失败", str(exc))

            make_qq_code.clicked.connect(create_qq_code)
            unbind_qq_button.clicked.connect(remove_qq_binding)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            form.addRow(buttons)

            def save() -> None:
                next_username = username.text().strip()
                current = current_password.text()
                next_password = new_password.text()
                if not next_username:
                    QMessageBox.warning(dialog, "保存失败", "用户名不能为空")
                    return
                if not current:
                    QMessageBox.warning(dialog, "保存失败", "请输入当前密码")
                    return
                if next_password and next_password != confirm_password.text():
                    QMessageBox.warning(dialog, "保存失败", "两次新密码不一致")
                    return
                try:
                    updated = self.api.update_account(
                        username=next_username,
                        current_password=current,
                        new_password=next_password or None,
                    )
                    self._set_account(updated)
                    self.password.clear()
                    dialog.accept()
                    QMessageBox.information(self, "保存成功", "账号信息已更新")
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "保存失败", str(exc))

            buttons.accepted.connect(save)
            buttons.rejected.connect(dialog.reject)
            dialog.exec()

        def show_backups(self) -> None:
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "权限不足", "请登录管理员账号并连接创作模式后再管理备份")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("世界备份管理")
            dialog.resize(820, 560)
            layout = QVBoxLayout(dialog)
            config = QFormLayout()
            automatic = QCheckBox("启用自动备份（默认每天一次）")
            interval = QSpinBox()
            interval.setRange(1, 8760)
            interval.setSuffix(" 小时")
            retention = QLineEdit()
            retention.setPlaceholderText("留空时不能启用自动备份")
            config.addRow(automatic)
            config.addRow("备份周期", interval)
            config.addRow("自动备份保留数量", retention)
            layout.addLayout(config)
            backup_list = QListWidget()
            layout.addWidget(QLabel("仅管理员可见的备份文件"))
            layout.addWidget(backup_list, 1)
            status = QLabel("")
            status.setWordWrap(True)
            layout.addWidget(status)
            row = QHBoxLayout()
            row_actions = QHBoxLayout()
            save_settings = QPushButton("保存设置")
            manual_backup = QPushButton("立即备份")
            download = QPushButton("下载选中项")
            delete = QPushButton("删除选中项")
            restore = QPushButton("恢复选中备份")
            import_restore = QPushButton("导入 JSON 并恢复")
            refresh = QPushButton("刷新")
            close = QPushButton("关闭")
            for button in (save_settings, manual_backup, download, refresh):
                row.addWidget(button)
            for button in (restore, import_restore, delete, close):
                row_actions.addWidget(button)
            layout.addLayout(row)
            layout.addLayout(row_actions)

            def reload():
                try:
                    values = self.api.request("GET", "/api/v1/admin/backups/settings")
                    automatic.setChecked(values["automatic_enabled"])
                    interval.setValue(int(values["interval_hours"]))
                    retention.setText("" if values.get("retention_count") is None else str(values["retention_count"]))
                    backup_list.clear()
                    for item in self.api.request("GET", "/api/v1/admin/backups")["backups"]:
                        label = f"{item['created_at']} | {'自动' if item['kind'] == 'automatic' else '手动'} | {item['file_name']} | {item['size_bytes']:,} 字节"
                        entry = QListWidgetItem(label)
                        entry.setData(Qt.ItemDataRole.UserRole, item["id"])
                        backup_list.addItem(entry)
                    status.setText("配置与备份列表已更新")
                except RuntimeError as exc:
                    status.setText(str(exc))

            def save():
                raw_count = retention.text().strip()
                try:
                    count = int(raw_count) if raw_count else None
                    self.api.request("POST", "/api/v1/admin/backups/settings", {"automatic_enabled": automatic.isChecked(), "interval_hours": interval.value(), "retention_count": count})
                    status.setText("自动备份设置已保存")
                    reload()
                except (ValueError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, "保存失败", str(exc))

            def create_manual():
                try:
                    value = self.api.request("POST", "/api/v1/admin/backups", {})
                    status.setText(f"备份创建完成：{value['file_name']}")
                    reload()
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "备份失败", str(exc))

            def download_selected():
                item = backup_list.currentItem()
                if item is None:
                    return
                backup_id = item.data(Qt.ItemDataRole.UserRole)
                default_name = item.text().split(" | ")[2]
                target, _ = QFileDialog.getSaveFileName(dialog, "保存备份", default_name, "虚拟域备份 (*.json)")
                if not target:
                    return
                try:
                    data = self.api.download(f"/api/v1/admin/backups/{backup_id}/download")
                    from pathlib import Path
                    Path(target).write_bytes(data)
                    status.setText(f"已下载备份：{target}")
                except (OSError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, "下载失败", str(exc))

            def restore_selected():
                item = backup_list.currentItem()
                if item is None:
                    return
                warning = (
                    "恢复会用所选快照覆盖其中同 ID 世界的当前状态，撤销快照之后的剧情变化。"
                    "快照没有包含的新世界会保留；账号、凭证与已累计 Token 用量不会回退。"
                    "正在游玩的玩家会被设为离线。此操作无法自动撤销，建议先创建当前备份。\n\n"
                    f"确定恢复：{item.text()}"
                )
                if QMessageBox.question(dialog, "恢复世界备份", warning) != QMessageBox.StandardButton.Yes:
                    return
                try:
                    backup_id = item.data(Qt.ItemDataRole.UserRole)
                    result = self.api.request("POST", f"/api/v1/admin/backups/{backup_id}/restore", {})
                    self.current_world_id = None
                    self.stack.setCurrentWidget(self.join_page)
                    self.refresh_worlds()
                    reload()
                    status.setText(f"已恢复 {result['world_count']} 个世界；当前游玩界面已退出")
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "恢复失败", str(exc))

            def import_and_restore():
                source, _ = QFileDialog.getOpenFileName(dialog, "选择虚拟域备份", "", "虚拟域备份 (*.json)")
                if not source:
                    return
                if QMessageBox.question(
                    dialog,
                    "导入并恢复世界",
                    "这会创建暂停的恢复世界，不会覆盖同 ID 世界或切换当前世界。请逐个确认备份账号映射；未映射角色不能加入。确认后还需重新配置模型连接。建议先创建当前备份。\n\n继续预览账号映射？",
                ) != QMessageBox.StandardButton.Yes:
                    return
                try:
                    from pathlib import Path
                    snapshot = json.loads(Path(source).read_text(encoding="utf-8"))
                    preview = self.api.preview_backup_import(snapshot)
                    conflicts = preview.get("world_id_conflicts", [])
                    if conflicts:
                        conflict_summary = "\n".join(
                            f"{item['world_name']} · {item['source_world_id']}"
                            for item in conflicts[:8]
                        )
                        status.setText("存在 ID 冲突的世界将作为新暂停副本导入：\n" + conflict_summary)
                    source_accounts = preview.get("source_accounts", [])
                    target_accounts = preview.get("target_accounts", [])
                    choices = ["暂不映射"] + [
                        f"{item['username']} · {item['id']}"
                        for item in target_accounts
                        if item.get("id") and item.get("username")
                    ]
                    choice_targets = {
                        f"{item['username']} · {item['id']}": item["id"]
                        for item in target_accounts
                        if item.get("id") and item.get("username")
                    }
                    mappings: dict[str, str | None] = {}
                    target_worlds: dict[str, set[str]] = {}
                    for item in source_accounts:
                        roles = item.get("roles", [])
                        mapping_required = bool(item.get("mapping_required"))
                        source_worlds = {str(role.get("world_id", "")) for role in roles}
                        unavailable = {
                            target_id
                            for target_id, mapped_worlds in target_worlds.items()
                            if source_worlds.intersection(mapped_worlds)
                        }
                        available_choices = ([] if mapping_required else ["暂不映射"]) + [
                            label for label, target_id in choice_targets.items() if target_id not in unavailable
                        ]
                        if not available_choices:
                            raise RuntimeError(
                                f"账号 {item.get('source_username') or item['source_account_id']} "
                                "关联未完成的回应，但当前没有可用目标账号；请先创建账号后重新导入。"
                            )
                        role_text = "、".join(
                            f"{role.get('world_name', '')} / {role.get('display_name', '')}"
                            for role in roles[:4]
                        ) or "历史记录中的账号"
                        prompt = (
                            f"来源账号：{item.get('source_username') or '未知账号'} "
                            f"({item['source_account_id']})\n关联角色：{role_text}\n\n"
                            + ("该账号关联未完成的回应，必须映射。\n" if mapping_required else "")
                            + "仅映射到确认属于该玩家的本服务账号；不应按同名角色自动认领。"
                        )
                        choice, accepted = QInputDialog.getItem(
                            dialog,
                            "确认备份账号映射",
                            prompt,
                            available_choices,
                            0,
                            False,
                        )
                        if not accepted:
                            status.setText("已取消导入；没有创建恢复世界。")
                            return
                        target_id = choice_targets.get(choice) if choice != "暂不映射" else None
                        mappings[item["source_account_id"]] = target_id
                        if target_id:
                            target_worlds.setdefault(target_id, set()).update(source_worlds)
                    result = self.api.import_backup(snapshot, mappings)
                    self.current_world_id = None
                    self.stack.setCurrentWidget(self.join_page)
                    unmapped = len(result.get("unmapped_account_ids", []))
                    world_map = result.get("world_id_map", {})
                    restored_worlds = []
                    for source_world in preview.get("world_id_conflicts", []):
                        restored_id = world_map.get(source_world["source_world_id"])
                        if restored_id:
                            restored_worlds.append(f"{source_world['world_name']}：{restored_id}")
                    self.refresh_worlds()
                    reload()
                    status.setText(
                        f"已导入 {result['world_count']} 个暂停的恢复世界；未映射账号 {unmapped} 个。"
                        + ("冲突世界已另存为：" + "；".join(restored_worlds[:4]) + "。" if restored_worlds else "")
                        + "当前世界未切换，请完成连接配置并由管理员启用。"
                    )
                except (OSError, json.JSONDecodeError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, "导入恢复失败", str(exc))

            def delete_selected():
                item = backup_list.currentItem()
                if item is None:
                    return
                if QMessageBox.question(dialog, "删除备份", f"确定删除这个备份吗？\n{item.text()}") != QMessageBox.StandardButton.Yes:
                    return
                try:
                    backup_id = item.data(Qt.ItemDataRole.UserRole)
                    self.api.request("DELETE", f"/api/v1/admin/backups/{backup_id}")
                    status.setText("备份已删除")
                    reload()
                except RuntimeError as exc:
                    QMessageBox.warning(dialog, "删除失败", str(exc))

            save_settings.clicked.connect(save)
            manual_backup.clicked.connect(create_manual)
            download.clicked.connect(download_selected)
            delete.clicked.connect(delete_selected)
            restore.clicked.connect(restore_selected)
            import_restore.clicked.connect(import_and_restore)
            refresh.clicked.connect(reload)
            close.clicked.connect(dialog.accept)
            reload()
            dialog.exec()

        def edit_quota(self):
            if not self.current_world_id or not self.account or not self.account.get("is_admin") or self.play_busy:
                return
            try:
                current = self.api.request("GET", f"/api/v1/worlds/{self.current_world_id}/quota")
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取失败", str(exc))
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Token 额度")
            layout = QFormLayout(dialog)
            fields = {}
            for key, title in (("total_limit", "总额度"), ("world_limit", "世界模型额度"), ("bot_limit", "Bot 额度"), ("memory_limit", "记忆额度")):
                field = QLineEdit("" if current.get(key) is None else str(current[key]))
                field.setPlaceholderText("未配置")
                fields[key] = field
                layout.addRow(title, field)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            layout.addRow(buttons)

            def save():
                try:
                    values = {key: int(field.text()) if field.text().strip() else None for key, field in fields.items()}
                    if any(value is not None and value < 0 for value in values.values()):
                        raise ValueError("额度必须是非负整数")
                    self.api.request("POST", f"/api/v1/worlds/{self.current_world_id}/quota", values)
                    dialog.accept()
                    self.refresh_game()
                except (ValueError, RuntimeError) as exc:
                    QMessageBox.warning(dialog, "保存失败", str(exc))

            buttons.accepted.connect(save)
            buttons.rejected.connect(dialog.reject)
            dialog.exec()

        def refresh_worlds(self) -> None:
            if not self.api.token:
                return
            try:
                self.worlds.clear()
                self.world_ids.clear()
                self.world_names.clear()
                self.current_world_ids.clear()
                self.world_statuses.clear()
                for world in self.api.request("GET", "/api/v1/worlds")["worlds"]:
                    self.world_ids.append(world["id"])
                    self.world_names[world["id"]] = world["name"]
                    self.world_statuses[world["id"]] = str(world.get("status", ""))
                    if world.get("current"):
                        self.current_world_ids.add(world["id"])
                    current = "当前世界 | " if world.get("current") else ""
                    label = f"{current}{world['name']} | {world['mode']} | {world['status']}"
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.AccessibleTextRole, label)
                    self.worlds.addItem(item)
                if self.world_ids:
                    selected_index = next(
                        (
                            index
                            for index, world_id in enumerate(self.world_ids)
                            if world_id in self.current_world_ids
                        ),
                        0,
                    )
                    self.worlds.setCurrentRow(selected_index)
                self._selected_world_changed()
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取失败", str(exc))

        def delete_selected_world(self) -> None:
            if self.delete_world_busy or self.play_busy:
                return
            world_id = self._selected_world_id()
            if not world_id:
                QMessageBox.warning(self, "未选择世界", "请先选择要删除的世界。")
                return
            world_name = self.world_names.get(world_id, world_id)
            current = "这是当前运行中的世界；删除会立即断开其中的玩家。" if world_id in self.current_world_ids else ""
            answer = QMessageBox.warning(
                self,
                "永久删除世界",
                f"即将永久删除世界「{world_name}」及其设定、角色、事件、记忆、互动和额度记录。\n"
                f"{current}如有服务端备份，可之后由管理员恢复。\n\n此操作无法撤销，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            typed_name, accepted = QInputDialog.getText(
                self,
                "确认删除世界",
                f"请输入世界名称以确认删除：\n{world_name}",
            )
            if not accepted or typed_name.strip() != world_name:
                self.status.setText("世界名称不匹配，已取消删除。")
                return

            self.delete_world_busy = True
            self._update_delete_world_button()

            def deleted(result: dict[str, Any]) -> None:
                self.delete_world_busy = False
                if self.current_world_id == world_id:
                    self.current_world_id = None
                    self.active_interaction_id = None
                    self.stack.setCurrentWidget(self.join_page)
                disconnected = result.get("online_players_disconnected", [])
                self.refresh_worlds()
                self.status.setText(f"世界「{world_name}」已删除。")
                if disconnected:
                    self.status.setText(
                        f"世界「{world_name}」已删除，并断开 {len(disconnected)} 名在线玩家。"
                    )
                QMessageBox.information(self, "世界已删除", self.status.text())
                self._update_delete_world_button()

            def failed(message: str) -> None:
                self.delete_world_busy = False
                self._update_delete_world_button()
                QMessageBox.warning(self, "删除失败", message)

            self.run_task(
                lambda: self.api.request("DELETE", f"/api/v1/worlds/{world_id}"),
                deleted,
                on_error=failed,
            )

        def switch_current_world(self):
            index = self.worlds.currentRow()
            if index < 0:
                return
            world_id = self.world_ids[index]
            try:
                try:
                    self.api.request("POST", f"/api/v1/worlds/{world_id}/current", {"confirm_online_disconnect": False})
                except RuntimeError as exc:
                    detail = str(exc)
                    if not detail.startswith("切换确认："):
                        raise
                    if QMessageBox.question(self, "当前世界仍有玩家在线", detail + "\n\n继续切换？") != QMessageBox.StandardButton.Yes:
                        self.status.setText("已取消世界切换")
                        return
                    self.api.request("POST", f"/api/v1/worlds/{world_id}/current", {"confirm_online_disconnect": True})
                self.current_world_id = None
                self.refresh_worlds()
                self.status.setText("当前世界已切换；旧世界在线玩家会收到切换提示并断开")
            except RuntimeError as exc:
                QMessageBox.warning(self, "切换失败", str(exc))

        def join_selected_world(self) -> None:
            if self.joining_busy:
                return
            if not self.api.token:
                QMessageBox.warning(self, "尚未登录", "请先登录账号")
                return
            if self.invite_code.text().strip():
                try:
                    self.joining_busy = True
                    self.join_button.setEnabled(False)
                    result = self.api.request(
                        "POST",
                        "/api/v1/worlds/join-by-invite",
                        {"invite_code": self.invite_code.text().strip(), "display_name": self.display_name.text().strip() or None},
                    )
                    self.invite_code.clear()
                    self.refresh_worlds()
                    self.open_world(result["world_id"])
                except RuntimeError as exc:
                    QMessageBox.warning(self, "加入失败", str(exc))
                finally:
                    self.joining_busy = False
                    self.join_button.setEnabled(True)
                return
            index = self.worlds.currentRow()
            if index < 0 or index >= len(self.world_ids):
                QMessageBox.warning(self, "未选择世界", "请先选择已授权的世界，或填写管理员提供的邀请码")
                return
            body: dict[str, Any] = {}
            if self.display_name.text().strip():
                body["display_name"] = self.display_name.text().strip()
            if self.invite_code.text().strip():
                body["invite_code"] = self.invite_code.text().strip()
            try:
                world_id = self.world_ids[index]
                self.joining_busy = True
                self.join_button.setEnabled(False)
                self.status.setText("正在进入世界，流动式可能需要结算离开期间...")
                def joined(_):
                    self.joining_busy = False
                    self.join_button.setEnabled(True)
                    self.open_world(world_id)

                def failed(message):
                    self.joining_busy = False
                    self.join_button.setEnabled(True)
                    self.status.setText(message)
                    QMessageBox.warning(self, "加入失败", message)

                self.run_task(lambda: self.api.request("POST", f"/api/v1/worlds/{world_id}/join", body), joined, on_error=failed)
            except RuntimeError as exc:
                QMessageBox.warning(self, "加入失败", str(exc))

        def create_invitation(self) -> None:
            if not self.account or not self.account.get("is_admin"):
                QMessageBox.warning(self, "无权限", "只有管理员可以创建加入邀请")
                return
            index = self.worlds.currentRow()
            if index < 0 or index >= len(self.world_ids):
                QMessageBox.warning(self, "未选择世界", "请先选择要邀请玩家加入的世界")
                return
            world_id = self.world_ids[index]
            try:
                result = self.api.request("POST", f"/api/v1/worlds/{world_id}/invitations", {"hours": 24})
                QMessageBox.information(
                    self,
                    "加入邀请已创建",
                    f"请将此一次性邀请码和服务端连接方式分别提供给玩家：\n\n{result['code']}\n\n过期时间：{result['expires_at']}",
                )
            except RuntimeError as exc:
                QMessageBox.warning(self, "创建邀请失败", str(exc))

        def revoke_invitation(self) -> None:
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "无权限", "请先连接管理员创作模式")
                return
            index = self.worlds.currentRow()
            if index < 0 or index >= len(self.world_ids):
                QMessageBox.warning(self, "未选择世界", "请先选择要管理邀请码的世界")
                return
            world_id = self.world_ids[index]
            if QMessageBox.question(
                self,
                "撤销加入邀请",
                "撤销后，尚未使用的此邀请码将立即失效。继续输入要撤销的邀请码？",
            ) != QMessageBox.StandardButton.Yes:
                return
            code, accepted = QInputDialog.getText(
                self,
                "撤销加入邀请",
                "输入要撤销的一次性邀请码：",
                QLineEdit.EchoMode.Password,
            )
            if not accepted or not code.strip():
                return
            try:
                result = self.api.request(
                    "DELETE",
                    f"/api/v1/worlds/{world_id}/invitations",
                    {"code": code.strip()},
                )
                QMessageBox.information(
                    self,
                    "邀请码已撤销",
                    f"世界 {result.get('world_id', world_id)} 中未使用的邀请码已撤销。",
                )
            except RuntimeError as exc:
                QMessageBox.warning(self, "撤销邀请失败", str(exc))

        def open_world(self, world_id: str) -> None:
            self.current_world_id = world_id
            self.game_state_ready = False
            self.gameplay_allowed = False
            self.active_interaction_id = None
            self._update_game_controls()
            self.last_activity = time.monotonic()
            self.send_presence()
            self.stack.setCurrentWidget(self.game_page)
            self.status.setText("正在读取世界状态，完成后即可行动……")
            self.refresh_game()

        def refresh_game(self) -> None:
            if not self.api.token or not self.current_world_id or self.refresh_busy or self.play_busy:
                return
            world_id = self.current_world_id
            self.refresh_busy = True

            def read():
                world = self.api.request("GET", f"/api/v1/worlds/{world_id}")
                events = []
                after = 0
                while True:
                    page = self.api.request("GET", f"/api/v1/worlds/{world_id}/events?after={after}")["events"]
                    events.extend(page)
                    if len(page) < 100:
                        break
                    after = int(page[-1]["sequence"])
                quota = self.api.request("GET", f"/api/v1/worlds/{world_id}/quota")
                health = self.api.request("GET", "/api/v1/health")
                runtime = self.api.request("GET", f"/api/v1/worlds/{world_id}/runtime")
                interactions = self.api.request("GET", f"/api/v1/worlds/{world_id}/interactions")["interactions"]
                return world, events, quota, health, runtime, interactions

            def show(data):
                self.refresh_busy = False
                if self.current_world_id != world_id:
                    return
                world, events, quota, health, runtime, interactions = data
                if not world.get("current", True):
                    self.current_world_id = None
                    self.game_state_ready = False
                    self.gameplay_allowed = False
                    self.active_interaction_id = None
                    self._update_game_controls()
                    self.stack.setCurrentWidget(self.join_page)
                    self.status.setText("世界已切换，请重新选择并加入当前世界")
                    self.refresh_worlds()
                    return
                self.current_state_version = int(world.get("state_version", 0))
                mode = "静止式" if world["mode"] == "static" else "流动式"
                self.game_title.setText(f"{world['name']} | {mode} | 版本 {self.current_state_version}")
                idle = runtime.get("idle_seconds", 0)
                self.presence_status.setText("离线" if not runtime.get("online") else "即将离线，请确认仍在场" if idle >= 240 else "在线")
                if runtime.get("bot_request_running"):
                    self.presence_status.setText(self.presence_status.text() + " | Bot 正在独自游玩")
                world_lines = [f"[{e.get('sequence', '?')}] {e.get('content', '')}" for e in events if e.get("kind") != "chat"]
                chat_lines = [e.get("content", "") for e in events if e.get("kind") == "chat"]
                self.world_screen.setPlainText("\n\n".join(world_lines) or str(world.get("settings", {}).get("shallow_worldview", "")))
                self.game_chat.setPlainText("\n\n".join(chat_lines))
                self.interaction_list.clear()
                self.active_interaction_id = None
                has_incoming = False
                own_id = str((self.account or {}).get("id", ""))
                for group in interactions:
                    for item in group.get("pending_interactions", []):
                        remaining = max(0, int(item.get("seconds_remaining", 0)))
                        if item.get("status") != "pending":
                            state_label = "世界结算重试中" if item.get("status") == "ready" else "回应已提交" if item.get("status") == "responded" else "已由世界模拟器接管" if item.get("status") == "timed_out" else str(item.get("status", "处理中"))
                            label = f"{state_label} · {item.get('target_name', '玩家')} · {item.get('prompt', '')}"
                            row = QListWidgetItem(label)
                            row.setData(Qt.ItemDataRole.AccessibleTextRole, label)
                            self.interaction_list.addItem(row)
                        elif item.get("target_id") == own_id:
                            label = f"需要你回应 · {remaining // 60}:{remaining % 60:02d} · {item.get('prompt', '')}"
                            row = QListWidgetItem(label)
                            row.setData(Qt.ItemDataRole.AccessibleTextRole, label)
                            self.interaction_list.addItem(row)
                            if self.active_interaction_id is None:
                                self.active_interaction_id = item.get("id")
                                has_incoming = True
                        else:
                            label = f"等待 {item.get('target_name', '玩家')} 回应 · {remaining // 60}:{remaining % 60:02d} · {item.get('prompt', '')}"
                            row = QListWidgetItem(label)
                            row.setData(Qt.ItemDataRole.AccessibleTextRole, label)
                            self.interaction_list.addItem(row)
                    if group.get("status") == "resolving":
                        label = "世界结算重试中，回应已保存。"
                        row = QListWidgetItem(label)
                        row.setData(Qt.ItemDataRole.AccessibleTextRole, label)
                        self.interaction_list.addItem(row)
                self.respond_button.setEnabled(has_incoming and not self.play_busy)
                self.interaction_response.setEnabled(has_incoming and not self.play_busy)
                for field in (self.world_screen, self.game_chat):
                    field.verticalScrollBar().setValue(field.verticalScrollBar().maximum())
                total_limit = quota.get("total_limit")
                total_ceiling = "未设置" if total_limit is None else f"{int(total_limit):,}"
                total_remaining = quota.get("remaining_total")
                remaining_label = "未设置" if total_remaining is None else f"{int(total_remaining):,}"
                reserved = int(quota.get("reserved_total", 0))

                def quota_ledger(name: str, used_key: str, limit_key: str, reserved_key: str) -> str:
                    limit = quota.get(limit_key)
                    ceiling = "未设置" if limit is None else f"{int(limit):,}"
                    reserved_ledger = int(quota.get(reserved_key, 0))
                    return f"{name} {int(quota.get(used_key, 0)):,}/{ceiling}（预留约 {reserved_ledger:,}）"

                usage_role_names = {
                    "bot": "Bot",
                    "world": "世界模型",
                    "embedding": "记忆嵌入模型",
                    "rerank": "记忆重排模型",
                }
                missing_usage = [
                    usage_role_names[role]
                    for role, state in (runtime.get("usage_support") or {}).items()
                    if role in usage_role_names and state == "missing"
                ]
                usage_warning = (
                    f" | 统计缺口：{'、'.join(missing_usage)}未返回实际 Token 用量"
                    if missing_usage
                    else ""
                )
                self.quota_status.setText(
                    f"实际已用 {int(quota['total_used']):,}/{total_ceiling} Token"
                    f" | 剩余 {remaining_label}"
                    f" | 在途预留约 {reserved:,}"
                    f" | {quota_ledger('世界', 'world_used', 'world_limit', 'reserved_world')}"
                    f" | {quota_ledger('Bot', 'bot_used', 'bot_limit', 'reserved_bot')}"
                    f" | {quota_ledger('记忆', 'memory_used', 'memory_limit', 'reserved_memory')}"
                    + (" | 额度已耗尽" if quota["exhausted"] else "")
                    + usage_warning
                )
                show_astrbot_scope = bool(
                    self.account
                    and self.account.get("is_admin")
                    and health.get("bot_mode") == "astrbot"
                )
                self.quota_scope_notice.setText(
                    "统计范围提示：AstrBot 或第三方插件自行发起的模型调用，"
                    "以及未返回给连接器的额外用量，可能不在此账本中；"
                    "Bot 额度不构成这类调用的完整硬上限。"
                )
                self.quota_scope_notice.setVisible(show_astrbot_scope)
                connected = health.get("bot_connected")
                self.connection_status.setText("AstrBot 已连接" if connected else "独立模式" if health.get("bot_mode") == "standalone" else "AstrBot 未连接")
                self.gameplay_allowed = not quota["exhausted"] and world["status"] == "active"
                self.game_state_ready = True
                self._update_game_controls()
                if self.status.text().startswith(("正在读取世界状态", "世界状态读取失败")):
                    self.status.setText("已进入世界")

            def failed(message):
                self.refresh_busy = False
                if message == "世界不存在" and self.current_world_id == world_id:
                    self.current_world_id = None
                    self.game_state_ready = False
                    self.gameplay_allowed = False
                    self.active_interaction_id = None
                    self._update_game_controls()
                    self.stack.setCurrentWidget(self.join_page)
                    self.status.setText("该世界已被管理员删除，已返回世界列表。")
                    self.refresh_worlds()
                    QMessageBox.information(self, "世界已删除", self.status.text())
                    return
                self.connection_status.setText("连接中断")
                if not self.game_state_ready:
                    self.status.setText(f"世界状态读取失败，当前不能提交操作：{message}。可点击“刷新世界”重试。")
                else:
                    self.status.setText(message)

            self.run_task(read, show, on_error=failed)

        def _append_event(self, event: dict[str, Any]) -> None:
            line = f"[{event.get('sequence', '?')}] {event.get('content', '')}"
            if event.get("kind") == "chat":
                self.game_chat.append(line)
            else:
                self.world_screen.append(line)

        def send_action(self) -> None:
            if self.play_busy:
                return
            if not self.game_state_ready:
                self.status.setText("正在读取世界状态，请稍后再提交行动。")
                return
            if not self.gameplay_allowed:
                self.status.setText("当前世界或额度状态不允许继续行动。")
                return
            if not self.api.token or not self.current_world_id:
                QMessageBox.warning(self, "尚未进入世界", "请先加入并进入一个世界")
                return
            content = self.action_input.text().strip()
            if not content:
                return
            self.set_play_busy(True)
            self.status.setText("世界正在推演...")
            previous = self.pending_submissions.get("action")
            request_id = previous[1] if previous and previous[0] == content else str(uuid.uuid4())
            self.pending_submissions["action"] = (content, request_id)
            world_id = self.current_world_id
            version = self.current_state_version

            def send():
                return self.api.request(
                    "POST",
                    f"/api/v1/worlds/{world_id}/actions",
                    {
                        "content": content,
                        "expected_version": version,
                        "request_id": request_id,
                    },
                )
            self.run_task(send, lambda result: self.submission_finished("action", result), on_error=self.submission_failed)

        def send_interaction_response(self) -> None:
            if (
                self.play_busy
                or not self.game_state_ready
                or not self.gameplay_allowed
                or not self.current_world_id
                or not self.active_interaction_id
            ):
                return
            content = self.interaction_response.text().strip()
            if not content:
                return
            self.set_play_busy(True)
            self.status.setText("正在提交回应并结算交互...")
            world_id = self.current_world_id
            interaction_id = self.active_interaction_id
            def send():
                return self.api.request("POST", f"/api/v1/worlds/{world_id}/interactions/{interaction_id}/response", {"content": content})
            def finished(result):
                self.interaction_response.clear()
                self.set_play_busy(False)
                self.status.setText("回应已记录" if result.get("status") == "resolved" else "回应已记录，等待其他玩家")
                self.refresh_game()
            self.run_task(send, finished, on_error=self.submission_failed)

        def send_chat(self) -> None:
            if self.play_busy:
                return
            if not self.game_state_ready:
                self.status.setText("正在读取世界状态，请稍后再发送聊天。")
                return
            if not self.gameplay_allowed:
                self.status.setText("当前世界或额度状态不允许继续聊天。")
                return
            if not self.api.token or not self.current_world_id:
                QMessageBox.warning(self, "尚未进入世界", "请先加入并进入一个世界")
                return
            content = self.chat_input.text().strip()
            if not content:
                return
            self.set_play_busy(True)
            self.status.setText("等待 Bot 回复...")
            previous = self.pending_submissions.get("chat")
            request_id = previous[1] if previous and previous[0] == content else str(uuid.uuid4())
            self.pending_submissions["chat"] = (content, request_id)
            world_id = self.current_world_id

            def send():
                return self.api.request(
                    "POST",
                    f"/api/v1/worlds/{world_id}/chat",
                    {"content": content, "request_id": request_id},
                )
            self.run_task(send, lambda result: self.submission_finished("chat", result), on_error=self.submission_failed)

        def submission_finished(self, kind, result=None):
            self.pending_submissions.pop(kind, None)
            (self.action_input if kind == "action" else self.chat_input).clear()
            self.set_play_busy(False)
            if isinstance(result, dict) and result.get("status") == "waiting_for_response":
                self.status.setText(result.get("message", "行动已暂存，等待回应；倒计时已经开始。"))
            elif isinstance(result, dict) and result.get("status") == "resubmit_required":
                self.status.setText(result.get("message", "行动已失效，请重新提交。"))
            else:
                self.status.setText("已完成")
            self.refresh_game()

        def submission_failed(self, message):
            self.set_play_busy(False)
            self.status.setText(message)
            self.refresh_game()
            QMessageBox.warning(self, "请求失败", message)

        def closeEvent(self, event):
            self.closing_requested = True
            self.timer.stop()
            if self.workers:
                event.ignore()
                self.status.setText("请求仍在处理，完成后可关闭窗口")
                return
            if self.current_world_id:
                world_id = self.current_world_id
                self.current_world_id = None
                self.run_task(lambda: self.api.request("POST", f"/api/v1/worlds/{world_id}/leave", {}), lambda _: self.close(), on_error=lambda _: self.close())
                event.ignore()
                return
            super().closeEvent(event)

        def _selected_world_id(self) -> str | None:
            index = self.worlds.currentRow()
            if 0 <= index < len(self.world_ids):
                return self.world_ids[index]
            return next(
                (world_id for world_id in self.world_ids if world_id in self.current_world_ids),
                None,
            )

        def _collect_world_settings(self) -> dict[str, Any]:
            raw_schema = self.state_schema.toPlainText().strip()
            if raw_schema:
                state_schema = json.loads(raw_schema)
                if not isinstance(state_schema, dict):
                    raise ValueError("关键状态属性规则必须是 JSON 对象")
            else:
                state_schema = {}
            return {
                "shallow_worldview": self.shallow_worldview.toPlainText().strip(),
                "deep_worldview": self.deep_worldview.toPlainText().strip(),
                "state_schema": state_schema,
                "bot_persona": self.bot_persona.toPlainText().strip(),
                "player_persona": self.player_persona.toPlainText().strip(),
                "role_mode": str(self.role_mode.currentData()),
                "player_character_name": self.player_character_name.text().strip(),
                "player_character_persona": self.player_character_persona.toPlainText().strip(),
                "bot_character_name": self.bot_character_name.text().strip(),
                "bot_character_persona": self.bot_character_persona.toPlainText().strip(),
                "bot_turn_enabled": self.bot_turn.isChecked(),
                "time_scale": self.time_scale.value(),
                "routine_threshold_seconds": self.routine_threshold.value() * 60,
                "interaction_timeout_seconds": self.interaction_timeout.value() * 60,
                "offline_bot_enabled": self.offline_bot.isChecked(),
                "offline_interval_seconds": self.offline_interval.value() * 60,
                "offline_daily_questions": self.offline_questions.value(),
                "offline_daily_actions": self.offline_actions.value(),
                "allow_death": self.allow_death.isChecked(),
                "allow_permanent_negative": self.allow_permanent.isChecked(),
                "allow_long_term": self.allow_long_term.isChecked(),
            }

        def _load_world_settings(self, settings: dict[str, Any]) -> None:
            self.time_scale.setValue(float(settings.get("time_scale", 1)))
            self.routine_threshold.setValue(max(1, int(settings.get("routine_threshold_seconds", 1800) // 60)))
            self.interaction_timeout.setValue(max(1, int(settings.get("interaction_timeout_seconds", 300) // 60)))
            self.bot_turn.setChecked(bool(settings.get("bot_turn_enabled", True)))
            self.offline_bot.setChecked(bool(settings.get("offline_bot_enabled", False)))
            self.offline_interval.setValue(max(1, int(settings.get("offline_interval_seconds", 1800) // 60)))
            self.offline_questions.setValue(int(settings.get("offline_daily_questions", 12)))
            self.offline_actions.setValue(int(settings.get("offline_daily_actions", 6)))
            self.allow_death.setChecked(bool(settings.get("allow_death", False)))
            self.allow_permanent.setChecked(bool(settings.get("allow_permanent_negative", False)))
            self.allow_long_term.setChecked(bool(settings.get("allow_long_term", False)))
            self.shallow_worldview.setPlainText(str(settings.get("shallow_worldview", settings.get("worldview", ""))))
            self.deep_worldview.setPlainText(str(settings.get("deep_worldview", "")))
            self.state_schema.setPlainText(
                json.dumps(settings.get("state_schema") or {}, ensure_ascii=False, indent=2)
                if settings.get("state_schema")
                else ""
            )
            self.bot_persona.setPlainText(str(settings.get("bot_persona", "")))
            self.player_persona.setPlainText(str(settings.get("player_persona", "")))
            role_index = self.role_mode.findData(str(settings.get("role_mode", "self")))
            self.role_mode.setCurrentIndex(role_index if role_index >= 0 else 0)
            self.player_character_name.setText(str(settings.get("player_character_name", "")))
            self.player_character_persona.setPlainText(str(settings.get("player_character_persona", "")))
            self.bot_character_name.setText(str(settings.get("bot_character_name", "")))
            self.bot_character_persona.setPlainText(str(settings.get("bot_character_persona", "")))

        def load_selected_configuration(self) -> None:
            world_id = self._selected_world_id()
            if not world_id:
                QMessageBox.warning(self, "未选择世界", "请先在加入模式的世界列表中选中一个世界")
                return
            try:
                world = self.api.request("GET", f"/api/v1/worlds/{world_id}")
                configuration = self.api.request("GET", f"/api/v1/worlds/{world_id}/configuration")
                selected = configuration.get("pending") or configuration.get("active") or {"settings": world.get("settings", {}), "mode": world.get("mode", "static")}
                self.editing_world_id = world_id
                self.editing_world_is_current = bool(world.get("current") and world.get("status") == "active")
                self.pending_configuration_available = configuration.get("pending") is not None
                self.world_generation_request_id = None
                self.generated_initial_state = {}
                self.generated_opening_narrative = ""
                self.world_name.setText(world["name"])
                self.world_mode.setCurrentIndex(1 if selected.get("mode") == "flowing" else 0)
                self._load_world_settings(selected.get("settings") or {})
                pending = configuration.get("pending")
                if pending:
                    self.config_status.setText(f"正在编辑待生效配置 v{pending['version']}；保存会替换当前待生效版本。")
                else:
                    active = configuration.get("active") or {}
                    self.config_status.setText(f"当前生效配置 v{active.get('version', 1)}。保存后将在下一次世界推演时生效。")
                self._update_apply_configuration_button()
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取配置失败", str(exc))

        def save_selected_configuration(self) -> None:
            world_id = self._selected_world_id()
            if not world_id:
                QMessageBox.warning(self, "未选择世界", "请先在加入模式的世界列表中选中一个世界")
                return
            try:
                result = self.api.request(
                    "PATCH",
                    f"/api/v1/worlds/{world_id}/configuration",
                    {"mode": "flowing" if self.world_mode.currentIndex() == 1 else "static", "settings": self._collect_world_settings()},
                )
                if self.editing_world_id == world_id:
                    self.pending_configuration_available = True
                self.config_status.setText(f"配置 v{result['version']} 已保存，将在下一次世界模型推演开始时生效。")
                self._update_apply_configuration_button()
                QMessageBox.information(self, "配置已保存", self.config_status.text())
            except (RuntimeError, ValueError) as exc:
                QMessageBox.warning(self, "保存配置失败", str(exc))

        def apply_pending_configuration_once(self) -> None:
            world_id = self._selected_world_id()
            if not world_id or world_id != self.editing_world_id:
                QMessageBox.warning(self, "请先加载配置", "请先选中并加载目标世界的配置，再执行一次性推演。")
                return
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "需要创作连接", "请先连接管理员创作模式。")
                return
            if not self.editing_world_is_current:
                QMessageBox.warning(self, "世界未运行", "只有当前运行中的世界可以执行一次性推演。")
                return
            if not self.pending_configuration_available:
                QMessageBox.information(self, "没有待生效配置", "请先保存一份待生效配置。")
                return
            answer = QMessageBox.question(
                self,
                "立即应用并推演一次",
                "将使用待生效配置调用一次世界模型。成功后配置生效并产生一个世界事件，"
                "实际世界模型 Token 会计入额度；模型不得替玩家行动或改变玩家状态。\n\n继续？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            request_id = f"admin-once:{uuid.uuid4()}"
            self.apply_configuration_busy = True
            self.config_status.setText("正在使用待生效配置进行一次世界推演……")
            self._update_apply_configuration_button()

            def completed(event: dict[str, Any]) -> None:
                self.apply_configuration_busy = False
                self.pending_configuration_available = False
                self.config_status.setText(
                    f"配置已应用并完成一次世界推演（事件 {event.get('id', '未知')}）。"
                )
                self._update_apply_configuration_button()
                self.refresh_worlds()
                if self.current_world_id == world_id:
                    self.refresh_game()
                QMessageBox.information(self, "推演完成", self.config_status.text())

            def failed(message: str) -> None:
                self.apply_configuration_busy = False
                self.config_status.setText(
                    "一次性推演请求未确认。请重新加载目标世界配置核实结果；"
                    f"若服务端明确报告推演失败，待生效配置会保留。详情：{message}"
                )
                self._update_apply_configuration_button()
                QMessageBox.warning(self, "一次性推演失败", self.config_status.text())

            self.run_task(
                lambda: self.api.request(
                    "POST",
                    f"/api/v1/worlds/{world_id}/configuration/apply-once",
                    {"request_id": request_id},
                ),
                completed,
                on_error=failed,
            )

        def show_world_nodes(self) -> None:
            world_id = self._selected_world_id()
            if not world_id:
                QMessageBox.warning(self, "未选择世界", "请先选择要查看或回滚的世界。")
                return
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "需要创作连接", "请先连接管理员创作模式。")
                return
            try:
                nodes = self.api.request("GET", f"/api/v1/worlds/{world_id}/nodes").get("nodes", [])
            except RuntimeError as exc:
                QMessageBox.warning(self, "读取世界节点失败", str(exc))
                return
            if not nodes:
                QMessageBox.information(self, "世界节点", "这个世界还没有可回滚的节点。")
                return
            node_labels = []
            node_by_label = {}
            for node in nodes:
                label = (
                    f"事件 {node.get('sequence')} · 状态版本 {node.get('state_version')} · "
                    f"{node.get('status')} · 配置 {node.get('configuration_version')} · "
                    f"{node.get('created_at')}\n{node.get('summary', '')}"
                )
                node_labels.append(label)
                node_by_label[label] = node
            selected_label, accepted = QInputDialog.getItem(
                self,
                "世界存档节点",
                f"选择要恢复的节点。世界「{self.world_names.get(world_id, world_id)}」",
                node_labels,
                0,
                False,
            )
            if not accepted or selected_label not in node_by_label:
                return
            node = node_by_label[selected_label]
            node_id = str(node["node_id"])
            confirmation = QMessageBox.warning(
                self,
                "确认回滚世界",
                f"将「{self.world_names.get(world_id, world_id)}」恢复到事件 {node.get('sequence')}。\n\n"
                f"{node.get('summary', '')}\n\n"
                "该节点之后的世界事件、记忆和待回应交互会被丢弃；已产生的 Token 用量保留。"
                "这不会退还额度。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirmation != QMessageBox.StandardButton.Yes:
                return
            typed, accepted = QInputDialog.getText(
                self,
                "再次确认回滚",
                f"请输入节点编号前 8 位以确认：\n{node_id[:8]}",
            )
            if not accepted or typed.strip() != node_id[:8]:
                self.status.setText("节点编号不匹配，已取消回滚。")
                return
            self.world_nodes_button.setEnabled(False)
            try:
                result = self.api.request(
                    "POST",
                    f"/api/v1/worlds/{world_id}/rollback",
                    {"node_id": node_id},
                )
            except RuntimeError as exc:
                QMessageBox.warning(self, "回滚失败", str(exc))
                return
            finally:
                self.world_nodes_button.setEnabled(
                    bool(self.account and self.account.get("is_admin") and self.api.management_token)
                )
            self.refresh_worlds()
            self.status.setText(
                f"世界已恢复到节点 {node_id[:8]}；丢弃 "
                f"{int(result.get('discarded_event_count', 0))} 条后续事件，状态版本已递增。"
            )
            if self.current_world_id == world_id:
                self.refresh_game()
            QMessageBox.information(self, "回滚完成", self.status.text())

        def show_admin_interactions(self) -> None:
            world_id = self._selected_world_id()
            if not world_id:
                QMessageBox.warning(self, "未选择世界", "请先选择一个世界。")
                return
            if not self.account or not self.account.get("is_admin") or not self.api.management_token:
                QMessageBox.warning(self, "需要创作连接", "请先连接管理员创作模式。")
                return
            self.admin_interactions_button.setEnabled(False)
            self.status.setText("正在读取世界交互记录……")

            def loaded(result: dict[str, Any]) -> None:
                self.admin_interactions_button.setEnabled(
                    bool(self.account and self.account.get("is_admin") and self.api.management_token)
                )
                world = result.get("current_world") or {}
                interactions = result.get("interactions") or []
                if not interactions:
                    QMessageBox.information(
                        self,
                        "待回应和失败交互",
                        f"世界「{world.get('name', world_id)}」当前没有待回应、结算中或失败的交互。",
                    )
                    return

                labels = {"pending": "等待回应", "ready": "等待结算", "failed": "结算失败"}
                lines = []
                for item in interactions:
                    status = str(item.get("status", "unknown"))
                    remaining = int(item.get("seconds_remaining", 0))
                    attempts = int(item.get("resolution_attempts", 0))
                    detail = (
                        f"{labels.get(status, status)} · 目标：{item.get('target_name', '未知角色')}"
                        + (f" · 剩余 {remaining} 秒" if status == "pending" else "")
                        + (f" · 结算尝试 {attempts}/3" if attempts else "")
                        + f"\n行动：{item.get('action', '')}"
                        + f"\n回应要求：{item.get('prompt', '')}"
                    )
                    if item.get("response_text"):
                        detail += f"\n已收到回应：{item['response_text']}"
                    lines.append(detail)

                dialog = QDialog(self)
                dialog.setWindowTitle(f"待回应和失败交互 · {world.get('name', world_id)}")
                dialog.resize(760, 520)
                layout = QVBoxLayout(dialog)
                content = QTextEdit()
                content.setReadOnly(True)
                content.setPlainText("\n\n".join(lines))
                layout.addWidget(content)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                buttons.rejected.connect(dialog.reject)
                buttons.accepted.connect(dialog.accept)
                layout.addWidget(buttons)
                dialog.exec()

            def failed(message: str) -> None:
                self.admin_interactions_button.setEnabled(
                    bool(self.account and self.account.get("is_admin") and self.api.management_token)
                )
                QMessageBox.warning(self, "读取交互失败", message)

            self.run_task(
                lambda: self.api.request(
                    "GET",
                    f"/api/v1/worlds/{world_id}/admin/interactions",
                ),
                loaded,
                on_error=failed,
            )

        def generate_worldview(self) -> None:
            if self.world_generation_busy:
                return
            if not self.api.token or not self.account or not self.account.get("is_admin"):
                QMessageBox.warning(self, "需要管理员权限", "登录管理员账号并连接服务端后才能生成世界设定")
                return
            prompt = self.worldview_prompt.toPlainText().strip()
            if not prompt:
                QMessageBox.warning(self, "缺少提示词", "请先描述希望生成的世界")
                return
            if not self._setup():
                return
            request_id = str(uuid.uuid4())
            target_world_id = self.editing_world_id
            self.world_generation_busy = True
            self.generate_worldview_button.setEnabled(False)
            self.worldview_generation_status.setText("世界模型正在生成草案；这个过程不会让 Bot 玩家参演……")

            def generated(result: dict[str, Any]) -> None:
                self.world_generation_busy = False
                self.generate_worldview_button.setEnabled(True)
                if target_world_id is None:
                    self.world_name.setText(str(result["name"]))
                    self.generated_initial_state = dict(result.get("initial_state") or {})
                    self.generated_opening_narrative = str(result.get("opening_narrative", ""))
                    self.world_generation_request_id = request_id
                    destination = "创建世界后会将本次实际用量计入这个世界。"
                else:
                    self.generated_initial_state = {}
                    self.generated_opening_narrative = ""
                    self.world_generation_request_id = None
                    destination = "本次实际用量已记入目标世界账本。保存后在下一次推演生效。"
                self.shallow_worldview.setPlainText(str(result["shallow_worldview"]))
                self.deep_worldview.setPlainText(str(result.get("deep_worldview", "")))
                usage = int(result.get("actual_tokens", 0))
                if target_world_id is None:
                    try:
                        unassigned = int(self.api.request("GET", "/api/v1/admin/world-generation-usage").get("unassigned_world_model_tokens", usage))
                    except RuntimeError:
                        unassigned = usage
                    destination += f"当前尚未归属的生成用量：{unassigned:,} Token。"
                self.worldview_generation_status.setText(
                    f"草案已生成，本请求实际消耗 {usage:,} Token。请检查并编辑浅层/深层设定。{destination}"
                )

            def failed(message: str) -> None:
                self.world_generation_busy = False
                self.generate_worldview_button.setEnabled(True)
                self.worldview_generation_status.setText("生成失败；可以检查模型配置后重新生成。")
                QMessageBox.warning(self, "世界观生成失败", message)

            self.run_task(
                lambda: self.api.request(
                    "POST",
                    "/api/v1/worlds/generate",
                    {"prompt": prompt, "request_id": request_id, "world_id": target_world_id},
                ),
                generated,
                on_error=failed,
            )

        def create_world(self) -> None:
            if not self.api.token:
                QMessageBox.warning(self, "尚未登录", "请先登录管理员账号")
                return
            try:
                result = self.api.request(
                    "POST",
                    "/api/v1/worlds",
                    {
                        "name": self.world_name.text(),
                        "mode": "flowing" if self.world_mode.currentIndex() == 1 else "static",
                        "settings": self._collect_world_settings() | {"opening_narrative": self.generated_opening_narrative},
                        "state": self.generated_initial_state,
                        "generation_request_id": self.world_generation_request_id,
                    },
                )
                self.generated_initial_state = {}
                self.generated_opening_narrative = ""
                self.world_generation_request_id = None
                self.editing_world_id = None
                self.worldview_generation_status.setText("世界已创建。世界屏幕会显示已生成的开场叙事；本次生成用量已计入世界模型账本。")
                QMessageBox.information(self, "创建成功", f"世界已创建：{result['name']}")
                self.refresh_worlds()
            except (RuntimeError, ValueError) as exc:
                QMessageBox.warning(self, "创建失败", str(exc))

    app = QApplication.instance() or QApplication(sys.argv)
    if sys.platform == "win32":
        from pathlib import Path
        import os
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    font = QFont()
    font.setFamilies(["Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "sans-serif"])
    font.setPointSize(10)
    app.setFont(font)
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QMainWindow { background: #eef3f1; }
        QWidget { color: #243b38; font-size: 14px; }
        QWidget#appRoot { background: #eef3f1; }
        QLabel { background: transparent; }
        QLabel#brand { font-size: 28px; font-weight: 700; color: #14584f; }
        QLabel#tagline { color: #718581; font-size: 12px; }
        QLabel#connectionStatus { color: #536d68; font-size: 12px; padding: 5px 10px; background: #e1eee9; border-radius: 10px; }
        QLabel#gameTitle { color: #183e38; font-size: 18px; font-weight: 650; }
        QLabel#sectionTitle { color: #315c55; font-size: 14px; font-weight: 650; padding: 1px 0; }
        QLabel#footerStatus { color: #71827f; font-size: 12px; padding: 2px 4px; }
        QWidget#connectionPanel, QWidget#contentSurface, QWidget#worldPanel, QWidget#chatPanel {
            background: #ffffff; border: 1px solid #dce7e3; border-radius: 12px;
        }
        QWidget#joinPage, QWidget#authorPage, QWidget#gamePage { background: #ffffff; }
        QWidget#pageStack { background: transparent; }
        QLineEdit, QTextEdit, QListWidget, QSpinBox, QDoubleSpinBox, QComboBox {
            background: #ffffff; color: #263d39; border: 1px solid #d4e0dc;
            border-radius: 8px; padding: 8px 10px; selection-background-color: #c6e7dc;
        }
        QTextEdit { padding: 10px; line-height: 1.45; }
        QLineEdit { min-height: 22px; }
        QLineEdit:focus, QTextEdit:focus, QListWidget:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
            border: 1px solid #2b8877;
        }
        QListWidget { alternate-background-color: #f7faf9; }
        QListWidget::item { padding: 8px 9px; border-radius: 6px; }
        QListWidget::item:selected { background: #e3f2ed; color: #174f45; }
        QPushButton {
            background: #e7f1ee; color: #205d52; border: 1px solid #d4e5df;
            border-radius: 8px; padding: 9px 14px; font-weight: 600;
        }
        QPushButton:hover { background: #d8ebe5; border-color: #b9d9cf; }
        QPushButton:pressed { background: #cbe2da; }
        QPushButton:disabled { background: #edf1f0; color: #a4b2af; border-color: #e3e9e7; }
        QPushButton#primaryButton, QPushButton#submitInteractionResponse { background: #176f60; color: #ffffff; border: 1px solid #176f60; }
        QPushButton#primaryButton:hover, QPushButton#submitInteractionResponse:hover { background: #125d50; border-color: #125d50; }
        QPushButton#primaryButton:disabled, QPushButton#submitInteractionResponse:disabled { background: #b7c9c3; color: #f4f7f6; border-color: #b7c9c3; }
        QCheckBox { spacing: 9px; }
        QCheckBox::indicator { width: 17px; height: 17px; }
        QSplitter::handle { background: #e7efec; width: 6px; margin: 4px 0; border-radius: 3px; }
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical { width: 10px; background: transparent; margin: 3px; }
        QScrollBar::handle:vertical { background: #cbd9d5; border-radius: 5px; min-height: 28px; }
        QScrollBar::handle:vertical:hover { background: #a9c1ba; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QToolTip { background: #183e38; color: #ffffff; border: none; padding: 6px 8px; }
        """
    )
    window = Window()
    return app, window


def main() -> None:
    app, window = create_application()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
