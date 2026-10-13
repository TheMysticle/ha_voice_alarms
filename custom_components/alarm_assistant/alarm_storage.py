"""Alarm storage and management using SQLite."""
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class AlarmStorage:
    """Singleton class for managing alarm storage in SQLite."""

    _instance = None
    _db_path: str | None = None

    @classmethod
    def configure(cls, db_path: str) -> None:
        """Set the database location (must be outside the integration folder)."""
        cls._db_path = db_path

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_db()
        return cls._instance

    def _init_db(self):
        """Initialize the SQLite database."""
        legacy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alarms.db")
        db_path = self._db_path or legacy_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # Migration: move DB from the integration folder (wiped by updates)
        if db_path != legacy_path and not os.path.exists(db_path) and os.path.exists(legacy_path):
            try:
                shutil.move(legacy_path, db_path)
                logger.info("Moved alarms.db from %s to %s", legacy_path, db_path)
            except OSError as err:
                logger.warning("Could not migrate legacy alarms.db: %s", err)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                time TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                repeat_days TEXT,
                sound TEXT,
                created_at INTEGER NOT NULL,
                media_player TEXT,
                led_effect TEXT
            )
        """)
        self._conn.commit()

        # Migration: add media_player column for existing databases
        try:
            self._conn.execute("ALTER TABLE alarms ADD COLUMN media_player TEXT")
            self._conn.commit()
            logger.info("Migrated alarms table: added media_player column")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add led_effect column (the ring animation for this alarm)
        try:
            self._conn.execute("ALTER TABLE alarms ADD COLUMN led_effect TEXT")
            self._conn.commit()
            logger.info("Migrated alarms table: added led_effect column")
        except sqlite3.OperationalError:
            pass  # Column already exists

        logger.info("Alarm database initialized at %s", db_path)

    def add_alarm(
        self,
        name: str,
        time: str,
        repeat_days: list[str] | None = None,
        sound: str = "default",
        media_player: str | None = None,
        led_effect: str | None = None,
    ) -> int:
        """
        Add a new alarm.

        Args:
            name: Name/label for the alarm
            time: Time in HH:MM format
            repeat_days: List of days (mon, tue, wed, thu, fri, sat, sun) or None for one-time
            sound: Sound to play
            media_player: Optional media player entity ID to play alarm on
            led_effect: Optional ring effect for this alarm (overrides the configured one)

        Returns:
            The ID of the created alarm
        """
        created_at = int(datetime.now().timestamp())
        repeat_days_json = json.dumps(repeat_days) if repeat_days else None

        cursor = self._conn.execute(
            """
            INSERT INTO alarms (name, time, enabled, repeat_days, sound, created_at, media_player, led_effect)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (name, time, repeat_days_json, sound, created_at, media_player, led_effect),
        )
        self._conn.commit()
        alarm_id = cursor.lastrowid
        logger.info("Created alarm: %s at %s (ID: %d)", name, time, alarm_id)
        return alarm_id

    def get_all_alarms(self) -> list[dict[str, Any]]:
        """Get all alarms."""
        cursor = self._conn.execute(
            """
            SELECT id, name, time, enabled, repeat_days, sound, created_at, media_player, led_effect
            FROM alarms
            ORDER BY time
            """
        )
        alarms = []
        for row in cursor.fetchall():
            repeat_days = json.loads(row[4]) if row[4] else None
            alarms.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "time": row[2],
                    "enabled": bool(row[3]),
                    "repeat_days": repeat_days,
                    "sound": row[5],
                    "created_at": row[6],
                    "media_player": row[7],
                    "led_effect": row[8],
                }
            )
        return alarms

    def get_enabled_alarms(self) -> list[dict[str, Any]]:
        """Get only enabled alarms."""
        return [alarm for alarm in self.get_all_alarms() if alarm["enabled"]]

    def delete_alarm(self, alarm_id: int) -> bool:
        """
        Delete an alarm by ID.

        Args:
            alarm_id: The ID of the alarm to delete

        Returns:
            True if alarm was deleted, False if not found
        """
        cursor = self._conn.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted alarm with ID: %d", alarm_id)
        else:
            logger.warning("Alarm with ID %d not found", alarm_id)
        return deleted

    def delete_alarm_by_name(self, name: str) -> int:
        """
        Delete alarm(s) by name.

        Args:
            name: The name of the alarm(s) to delete

        Returns:
            Number of alarms deleted
        """
        cursor = self._conn.execute(
            "DELETE FROM alarms WHERE name LIKE ?", (f"%{name}%",)
        )
        self._conn.commit()
        count = cursor.rowcount
        logger.info("Deleted %d alarm(s) matching name: %s", count, name)
        return count

    def delete_all_alarms(self) -> int:
        """
        Delete all alarms.

        Returns:
            Number of alarms deleted
        """
        cursor = self._conn.execute("DELETE FROM alarms")
        self._conn.commit()
        count = cursor.rowcount
        logger.info("Deleted all alarms (%d total)", count)
        return count

    def toggle_alarm(self, alarm_id: int, enabled: bool) -> bool:
        """
        Enable or disable an alarm.

        Args:
            alarm_id: The ID of the alarm
            enabled: True to enable, False to disable

        Returns:
            True if alarm was updated, False if not found
        """
        cursor = self._conn.execute(
            "UPDATE alarms SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, alarm_id),
        )
        self._conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            logger.info("Alarm %d %s", alarm_id, "enabled" if enabled else "disabled")
        return updated
