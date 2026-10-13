"""World time and human presence have independent persistent clocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import WorldMode, WorldStatus
from .state_schema import validate_state_schema


def timestamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def seconds_since(now: datetime, value: str) -> float:
    return max(0.0, (now - datetime.fromisoformat(value)).total_seconds())


def world_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows may lack the optional IANA database. Shanghai has used +08:00
        # without daylight saving since 1991, so this fallback is exact today.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        if name == "UTC":
            return UTC
        raise ValueError("不支持的时区，请安装 tzdata 或使用 UTC/Asia/Shanghai")


def validate_settings(settings: dict) -> None:
    validate_state_schema(settings.get("state_schema", {}))
    ranges = {
        "time_scale": (0.1, 10),
        "routine_threshold_seconds": (1, 365 * 86400),
        "offline_interval_seconds": (60, 365 * 86400),
        "offline_daily_questions": (0, 10000),
        "offline_daily_actions": (0, 10000),
        "interaction_timeout_seconds": (30, 3600),
    }
    for key, (low, high) in ranges.items():
        value = settings.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high):
            raise ValueError(f"{key} 必须在 {low} 至 {high} 之间")
        if value is not None and key != "time_scale" and not isinstance(value, int):
            raise ValueError(f"{key} 必须是整数")
    for key in ("bot_turn_enabled", "offline_bot_enabled", "allow_death", "allow_permanent_negative", "allow_long_term"):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError(f"{key} 必须是开关值")
    role_mode = settings.get("role_mode", "self")
    if not isinstance(role_mode, str) or role_mode not in {"self", "character"}:
        raise ValueError("role_mode 必须是 self 或 character")
    if role_mode == "character":
        names = [settings.get("player_character_name"), settings.get("bot_character_name")]
        personas = [settings.get("player_character_persona"), settings.get("bot_character_persona")]
        if any(not isinstance(value, str) or not value.strip() for value in names):
            raise ValueError("操控角色模式必须填写用户和 Bot 的角色名")
        if names[0].strip() == names[1].strip():
            raise ValueError("用户与 Bot 不能绑定同一个角色")
        if any(not isinstance(value, str) or not value.strip() for value in personas):
            raise ValueError("操控角色模式必须分别填写两个角色的人设")
    for key in ("player_character_name", "bot_character_name", "player_character_persona", "bot_character_persona"):
        if key in settings and not isinstance(settings[key], str):
            raise ValueError(f"{key} 必须是文本")
    world_timezone(str(settings.get("timezone", "Asia/Shanghai")))


class WorldRuntime:
    def __init__(self, service) -> None:
        self.service = service
        self.store = service.store

    def advance_clock(self, world, now: datetime) -> dict:
        with self.store.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO world_runtime(world_id,anchor_at) VALUES(?,?)", (world.id, timestamp(now)))
            row = conn.execute("SELECT * FROM world_runtime WHERE world_id=?", (world.id,)).fetchone()
            delta = 0.0
            if world.mode == WorldMode.FLOWING and world.current and world.status == WorldStatus.ACTIVE and not self.service.quota_snapshot(world.id).exhausted():
                delta = seconds_since(now, row["anchor_at"])
            total = float(row["active_seconds"]) + delta
            conn.execute("UPDATE world_runtime SET anchor_at=?, active_seconds=? WHERE world_id=?", (timestamp(now), total, world.id))
            return dict(row) | {"anchor_at": timestamp(now), "active_seconds": total}

    def presence(self, world_id: str, now: datetime) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT p.account_id,p.display_name,p.last_seen,a.activity_at,a.transport_at,a.disconnected,a.interaction_seconds FROM participants p LEFT JOIN player_activity a ON a.world_id=p.world_id AND a.account_id=p.account_id WHERE p.world_id=? AND p.kind='human'", (world_id,),
        ).fetchall()
        result = []
        with self.store.transaction() as conn:
            for row in rows:
                activity = row["activity_at"] or row["last_seen"]
                transport = row["transport_at"] or row["last_seen"]
                inactive = seconds_since(now, activity) if activity else 301
                disconnected = bool(row["disconnected"]) or not transport or seconds_since(now, transport) >= 45
                online = not disconnected and inactive < 300
                conn.execute("UPDATE participants SET online=? WHERE world_id=? AND account_id=?", (int(online), world_id, row["account_id"]))
                result.append({"account_id": row["account_id"], "display_name": row["display_name"], "online": online, "idle_seconds": int(inactive), "last_interaction_seconds": float(row["interaction_seconds"] or 0)})
        return result

    def touch(self, world_id: str, account_id: str, now: datetime, *, active: bool) -> None:
        with self.store.transaction() as conn:
            existing = conn.execute("SELECT 1 FROM player_activity WHERE world_id=? AND account_id=?", (world_id, account_id)).fetchone()
            if not existing:
                conn.execute("INSERT INTO player_activity(world_id,account_id,activity_at,transport_at) VALUES(?,?,?,?)", (world_id, account_id, timestamp(now), timestamp(now)))
            elif active:
                conn.execute("UPDATE player_activity SET activity_at=?,transport_at=?,disconnected=0 WHERE world_id=? AND account_id=?", (timestamp(now), timestamp(now), world_id, account_id))
            else:
                conn.execute("UPDATE player_activity SET transport_at=?,disconnected=0 WHERE world_id=? AND account_id=?", (timestamp(now), world_id, account_id))

    def time_context(self, world, now: datetime, *, trigger: str) -> dict:
        runtime = self.advance_clock(world, now)
        if world.mode != WorldMode.FLOWING:
            return {}
        players = self.presence(world.id, now)
        threshold = int(world.settings.get("routine_threshold_seconds", 1800))
        elapsed = max(0, runtime["active_seconds"] - runtime["settled_seconds"])
        role_mode = world.settings.get("role_mode", "self")
        player_profile = world.settings.get("player_character_persona") if role_mode == "character" else world.settings.get("player_persona")
        bot_profile = world.settings.get("bot_character_persona") if role_mode == "character" else world.settings.get("bot_persona")
        routine = [{"account_id": player["account_id"], "online": player["online"], "away_seconds": max(0, runtime["active_seconds"] - player["last_interaction_seconds"]), "may_routine": bool(player_profile) and runtime["active_seconds"] - player["last_interaction_seconds"] > threshold} for player in players]
        return {"trigger": trigger, "real_elapsed_seconds": elapsed, "world_elapsed_seconds": elapsed * float(world.settings.get("time_scale", 1)), "active_seconds": runtime["active_seconds"], "routine_threshold_seconds": threshold, "players": routine, "bot_may_routine": bool(bot_profile) and runtime["active_seconds"] - runtime["bot_interaction_seconds"] > threshold}

