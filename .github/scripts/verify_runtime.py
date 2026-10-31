"""Run an isolated desktop/HTTP check against release sources and a native package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def verify_native_package(package: Path, output: Path) -> dict:
    checksum = package.with_name(package.name + ".sha256")
    expected = checksum.read_text(encoding="ascii").split()[0]
    with package.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    require(actual == expected, "Release archive SHA-256 does not match")

    unpacked = output / "unpacked"
    unpacked.mkdir()
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-x", "-k", str(package), str(unpacked)], check=True)
        candidates = list(unpacked.rglob("VirtualDomain.app/Contents/MacOS/VirtualDomain"))
    elif sys.platform.startswith("linux"):
        subprocess.run(["tar", "-xzf", str(package), "-C", str(unpacked)], check=True)
        candidates = list(unpacked.glob("VirtualDomain/VirtualDomain"))
    else:
        raise ValueError("Native release check is configured for Linux and macOS")
    require(len(candidates) == 1, f"Expected one application executable: {candidates}")
    executable = candidates[0]
    require(os.access(executable, os.X_OK), "Archive lost the executable permission")
    log_path = output / "native-client.log"
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(executable)], cwd=unpacked, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            # Catch missing shared libraries, Qt plugins and immediate startup crashes.
            while time.monotonic() - started < 8:
                require(
                    process.poll() is None,
                    f"Packaged client exited during startup ({process.returncode}); "
                    f"see {log_path.name}",
                )
                time.sleep(0.1)
        finally:
            stop_process(process)
    return {
        "status": "passed",
        "sha256": actual,
        "startup_seconds": round(time.monotonic() - started, 2),
        "scope": "native executable startup; interaction is checked using release sources",
    }


def verify_application(source: Path, output: Path) -> dict:
    sys.path[:0] = [str(source / "server/src"), str(source / "client/src")]
    from http.server import ThreadingHTTPServer
    from unittest.mock import patch

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    from virtual_domain_client.app import ApiClient, create_application
    from virtual_domain_server.core.providers import ModelConfig, ModelResponse
    from virtual_domain_server.core.service import VirtualDomainService
    from virtual_domain_server.core.simulation import WorldSimulationEngine
    from virtual_domain_server.server.builtin import ApiHandler

    class FixtureProvider:
        config = ModelConfig("fixture", "offline-runtime-check")

        def __init__(self, *, chat: bool = False) -> None:
            self.chat = chat
            self.calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            text = (
                "我正在小镇广场散步。"
                if self.chat
                else json.dumps(
                    {
                        "narrative": "你走到小镇广场，向面包摊主挥了挥手。",
                        "state_patch": {"location": "小镇广场"},
                    },
                    ensure_ascii=False,
                )
            )
            return ModelResponse(text, 10, 5, self.config.model)

    world_model = FixtureProvider()
    chat_model = FixtureProvider(chat=True)
    database = output / "data/世界存档.sqlite3"
    database.parent.mkdir()

    def start_server():
        service = VirtualDomainService(
            str(database),
            simulation_engine=WorldSimulationEngine(world_model, max_retries=0),
            chat_provider=chat_model,
        )
        handler = type(
            "RuntimeCheckHandler",
            (ApiHandler,),
            {"service": service, "bot_bridge": None, "model_settings": None},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return service, server, thread

    def stop_server(service, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        require(not thread.is_alive(), "HTTP server did not stop")
        service.close()

    service, server, thread = start_server()
    app = window = None
    errors = []

    def warning(_parent, title, text, *_args, **_kwargs):
        errors.append(f"{title}: {text}")
        return QMessageBox.StandardButton.Ok

    def wait_until(predicate, description):
        deadline = time.monotonic() + 25
        while not predicate():
            app.processEvents()
            require(not errors, "; ".join(errors))
            require(time.monotonic() < deadline, f"Timed out: {description}")
            time.sleep(0.01)
        app.processEvents()
        require(not errors, "; ".join(errors))

    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        admin = ApiClient(base_url)
        require(
            admin.request("GET", "/api/v1/health")["status"] == "ok",
            "Health check failed",
        )
        admin.register(
            "runtime-admin",
            "offline-test-password",
            initialization_code=service.initialization_code,
        )
        admin.connect_management(service.management_pairing_code)
        world = admin.request(
            "POST",
            "/api/v1/worlds",
            {
                "name": "跨平台·小镇世界",
                "mode": "static",
                "settings": {
                    "shallow_worldview": "一座宁静的小镇，有广场和面包摊。",
                    "deep_worldview": "钟楼地下存放着未公开的档案。",
                    "bot_name": "泠",
                    "bot_persona": "温和、独立的小镇旅人。",
                    "player_persona": "谨慎、好奇，习惯先观察。",
                    "bot_turn_enabled": False,
                },
            },
        )
        world_id = world["id"]
        admin.request("POST", f"/api/v1/worlds/{world_id}/current", {})
        invitation = admin.request(
            "POST", f"/api/v1/worlds/{world_id}/invitations", {}
        )
        player = ApiClient(base_url)
        account = player.register(
            "runtime-player",
            "offline-test-password",
            invitation_code=invitation["code"],
        )
        require(not account["is_admin"], "Player unexpectedly has management rights")
        require(player.registered_world_id == world_id, "Invitation selected wrong world")

        with patch.object(QMessageBox, "warning", side_effect=warning):
            app, window = create_application()
            window.url.setText(base_url)
            window.username.setText("runtime-player")
            window.password.setText("offline-test-password")
            window.show()
            QTest.mouseClick(window.login_button, Qt.MouseButton.LeftButton)
            require(window.account is not None, "Desktop login failed")
            window.worlds.setCurrentRow(window.world_ids.index(world_id))
            QTest.mouseClick(window.join_button, Qt.MouseButton.LeftButton)
            wait_until(
                lambda: window.current_world_id == world_id
                and window.game_state_ready
                and not window.workers,
                "open gameplay screen",
            )
            require(window.stack.currentWidget() == window.game_page, "Wrong game page")
            require(window.gameplay_allowed, "Action controls are disabled")
            window.action_input.setText("我走到广场，向摊主挥手。")
            QTest.keyClick(window.action_input, Qt.Key.Key_Return)
            wait_until(
                lambda: not window.play_busy
                and not window.workers
                and not window.action_input.text(),
                "submit action and refresh world",
            )
            require(
                "小镇广场" in window.world_screen.toPlainText(),
                "World screen did not show the simulated action",
            )
            version_after_action = window.current_state_version
            window.chat_input.setText("泠，你在做什么？")
            QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
            wait_until(
                lambda: not window.play_busy
                and not window.workers
                and not window.chat_input.text(),
                "send game chat",
            )
            require("散步" in window.game_chat.toPlainText(), "Bot reply did not render")
            require(
                window.current_state_version == version_after_action,
                "Out-of-world chat unexpectedly advanced world state",
            )
            require(
                window.grab().save(str(output / "gameplay.png")),
                "Could not save desktop evidence",
            )
            quota = player.request("GET", f"/api/v1/worlds/{world_id}/quota")
            require(quota["world_used"] == 15, "World fixture usage was not recorded")
            require(quota["bot_used"] == 15, "Bot fixture usage was not recorded")
            backup = admin.request("POST", "/api/v1/admin/backups", {})
            require(bool(backup["id"]), "Backup file was not created")
            window.leave_game()
            wait_until(
                lambda: window.current_world_id is None and not window.workers,
                "leave world",
            )
            window.close()
            app.processEvents()
        stop_server(service, server, thread)
        service = server = thread = None

        # Reopen the same on-disk database and read it through a fresh HTTP server.
        service, server, thread = start_server()
        player = ApiClient(f"http://127.0.0.1:{server.server_port}")
        player.login("runtime-player", "offline-test-password")
        restored = player.request("GET", f"/api/v1/worlds/{world_id}")
        require(restored["name"] == "跨平台·小镇世界", "World name was corrupted")
        events = player.request("GET", f"/api/v1/worlds/{world_id}/events")["events"]
        require(any("小镇广场" in event["content"] for event in events), "Action was lost")
        require(any("散步" in event["content"] for event in events), "Chat was lost")
        require(
            player.request("GET", f"/api/v1/worlds/{world_id}/quota") == quota,
            "Usage changed after database reopen",
        )
        return {
            "status": "passed",
            "qt_platform": app.platformName(),
            "checks": [
                "administrator registration and management pairing over HTTP",
                "world creation and player invitation",
                "desktop login and gameplay screen",
                "desktop action submission and world rendering",
                "desktop bot chat independent of world progression",
                "separate fixture-token ledgers and manual backup",
                "Chinese paths, persisted accounts, events and usage after database reopen",
            ],
            "fixture_world_calls": world_model.calls,
            "fixture_bot_calls": chat_model.calls,
            "external_model_calls": 0,
        }
    finally:
        if window is not None:
            window.timer.stop()
            if window.workers:
                # Do not destroy a Qt window while its worker threads are running.
                deadline = time.monotonic() + 30
                while window.workers and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(0.01)
            window.current_world_id = None
            window.close()
            app.processEvents()
        if service is not None:
            stop_server(service, server, thread)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "external_model_calls": 0,
        "limitations": [
            "Packaged executable check covers startup, not external GUI automation.",
            "Desktop interaction uses the release source on the runner's display.",
            "No real model, AstrBot, QQ, macOS Gatekeeper or other Linux distributions.",
        ],
    }
    try:
        report["application"] = verify_application(args.source.resolve(), output)
        report["native_package"] = (
            verify_native_package(args.package.resolve(), output)
            if args.package
            else {"status": "not_requested"}
        )
        report["status"] = "passed"
        return 0
    except Exception:
        report["error"] = traceback.format_exc()
        traceback.print_exc()
        return 1
    finally:
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
