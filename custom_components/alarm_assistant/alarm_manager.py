"""Alarm manager for scheduling and triggering alarms."""
import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .alarm_storage import AlarmStorage
from .const import (
    CONF_ALARM_SOUND,
    CONF_ALARM_VOLUME,
    CONF_AUTO_DISMISS_DURATION,
    CONF_LED_COLOR,
    CONF_LED_EFFECT,
    CONF_LED_ENTITY,
    CONF_MEDIA_PLAYER,
    DEFAULT_AUTO_DISMISS_DURATION,
    DEFAULT_LED_COLOR,
    DEFAULT_LED_EFFECT,
    DOMAIN,
    RINGING_CHECK_INTERVAL,
    RINGING_RESTART_GRACE,
)
from . import led_effects

_LOGGER = logging.getLogger(__name__)


class AlarmManager:
    """Manages alarm scheduling and triggering."""

    def __init__(self, hass: HomeAssistant):
        """Initialize the alarm manager."""
        self.hass = hass
        AlarmStorage.configure(hass.config.path(".storage", "alarm_assistant_alarms.db"))
        self.storage = AlarmStorage()
        self._scheduled_timers = {}
        self._auto_dismiss_timers = {}
        self._ringing_watchdogs = {}
        self._led_unavailable_warned = False
        self._running = False

    async def start(self):
        """Start the alarm manager and schedule all alarms."""
        if self._running:
            return

        self._running = True
        _LOGGER.info("Starting alarm manager")

        alarms = self.storage.get_enabled_alarms()
        for alarm in alarms:
            await self._schedule_alarm(alarm)

        _LOGGER.info("Alarm manager started with %d active alarms", len(alarms))

    async def stop(self):
        """Stop the alarm manager and cancel all scheduled alarms."""
        _LOGGER.info("Stopping alarm manager")
        self._running = False

        for timer_cancel in self._scheduled_timers.values():
            timer_cancel()
        self._scheduled_timers.clear()

        for timer_cancel in self._auto_dismiss_timers.values():
            timer_cancel()
        self._auto_dismiss_timers.clear()

        for task in self._ringing_watchdogs.values():
            task.cancel()
        self._ringing_watchdogs.clear()

    async def _schedule_alarm(self, alarm: dict):
        """Schedule a single alarm."""
        alarm_id = alarm["id"]
        time_str = alarm["time"]
        repeat_days = alarm.get("repeat_days")

        try:
            hour, minute = map(int, time_str.split(":"))
            next_trigger = self._calculate_next_trigger(hour, minute, repeat_days)

            if next_trigger:
                async def _handle_trigger(now, alarm=alarm):
                    await self._trigger_alarm(alarm)

                timer_cancel = async_track_point_in_time(
                    self.hass, _handle_trigger, next_trigger
                )
                self._scheduled_timers[alarm_id] = timer_cancel
                _LOGGER.info(
                    "Scheduled alarm %d (%s) for %s",
                    alarm_id,
                    alarm["name"],
                    next_trigger,
                )

        except Exception as e:
            _LOGGER.error("Error scheduling alarm %d: %s", alarm_id, e)

    def _calculate_next_trigger(
        self, hour: int, minute: int, repeat_days: list[str] | None
    ) -> datetime | None:
        """Calculate the next trigger time for an alarm."""
        now = dt_util.now()
        today_start = dt_util.start_of_local_day()
        alarm_time = today_start.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if repeat_days:
            day_map = {
                "mon": 0, "tue": 1, "wed": 2, "thu": 3,
                "fri": 4, "sat": 5, "sun": 6,
            }
            target_days = [day_map[day.lower()] for day in repeat_days if day.lower() in day_map]

            if not target_days:
                return None

            for days_ahead in range(8):
                check_time = today_start + timedelta(days=days_ahead)
                check_time = check_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

                if check_time.weekday() in target_days and check_time > now:
                    return check_time

            return None
        else:
            if alarm_time > now:
                return alarm_time
            else:
                tomorrow_time = today_start + timedelta(days=1)
                alarm_time = tomorrow_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
                return alarm_time

    async def _trigger_alarm(self, alarm: dict):
        """Trigger an alarm (play sound, send notification, etc.)."""
        alarm_id = alarm["id"]
        alarm_name = alarm["name"]
        alarm_sound = alarm.get("sound", "default")
        alarm_media_player = alarm.get("media_player")

        _LOGGER.info("Triggering alarm %d: %s", alarm_id, alarm_name)

        try:
            config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
            effective_media_player = alarm_media_player or config_data.get(CONF_MEDIA_PLAYER)

            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            if "ringing_alarms" not in self.hass.data[DOMAIN]:
                self.hass.data[DOMAIN]["ringing_alarms"] = {}

            self.hass.data[DOMAIN]["ringing_alarms"][alarm_id] = {
                "media_player": effective_media_player,
                "led_effect": alarm.get("led_effect"),
            }

            # Save the current volume BEFORE playing the alarm
            await self._save_volume_state(effective_media_player)

            # Play alarm sound
            await self._play_alarm_sound(alarm_sound, media_player_override=alarm_media_player)

            # Activate LED ring
            await self._set_alarm_led(alarm_id)

            if effective_media_player:
                self._start_ringing_watchdog(
                    alarm_id, alarm_sound, effective_media_player
                )

            await self._send_notification(alarm_name, alarm_id)
            await self._schedule_auto_dismiss(alarm_id)

            if not alarm.get("repeat_days"):
                self.storage.toggle_alarm(alarm_id, False)
                _LOGGER.info("Disabled one-time alarm %d", alarm_id)
            else:
                await self._schedule_alarm(alarm)

        except Exception as e:
            _LOGGER.error("Error triggering alarm %d: %s", alarm_id, e)

    async def _save_volume_state(self, media_player: str | None = None):
        """Save the current volume state of the media player before changing it."""
        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        player = media_player or config_data.get(CONF_MEDIA_PLAYER)
        if not player:
            return

        saved_volumes = self.hass.data.setdefault(DOMAIN, {}).setdefault("saved_volumes", {})
        # Only save if not already saved (to avoid overwriting with the alarm volume on loop)
        if player not in saved_volumes:
            state = self.hass.states.get(player)
            if state and "volume_level" in state.attributes:
                current_vol = state.attributes.get("volume_level")
                if current_vol is not None:
                    saved_volumes[player] = current_vol
                    _LOGGER.debug("Saved pre-alarm volume %s for %s", current_vol, player)

    async def _restore_volume_state(self, media_player: str | None = None):
        """Restore the media player volume back to its pre-alarm level."""
        saved_volumes = self.hass.data.get(DOMAIN, {}).get("saved_volumes", {})
        if not saved_volumes:
            return

        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        target_player = media_player or config_data.get(CONF_MEDIA_PLAYER)

        players_to_restore = [target_player] if target_player else list(saved_volumes.keys())

        for player in list(players_to_restore):
            if player in saved_volumes:
                prev_volume = saved_volumes.pop(player, None)
                if prev_volume is not None:
                    try:
                        await self.hass.services.async_call(
                            "media_player",
                            "volume_set",
                            {"entity_id": player, "volume_level": prev_volume},
                            blocking=False,
                        )
                        _LOGGER.info("Restored pre-alarm volume for %s to %s", player, prev_volume)
                    except Exception as e:
                        _LOGGER.warning("Could not restore volume for %s: %s", player, e)

    async def _play_alarm_sound(self, sound: str, media_player_override: str | None = None):
        """Play the alarm sound using a media player."""
        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        media_player = media_player_override or config_data.get(CONF_MEDIA_PLAYER)
        volume = config_data.get(CONF_ALARM_VOLUME, 0.5)

        if not media_player:
            _LOGGER.warning("No media player configured for alarm sounds")
            return

        try:
            if sound == "custom":
                custom_path = config_data.get("custom_sound_path")
                sound_file = custom_path if custom_path else "/local/alarm_sounds/custom.mp3"
            else:
                sound_map = {
                    "default": "/local/alarm_sounds/default.mp3",
                    "gentle": "/local/alarm_sounds/gentle.mp3",
                    "beep": "/local/alarm_sounds/beep.mp3",
                    "chime": "/local/alarm_sounds/chime.mp3",
                    "bell": "/local/alarm_sounds/bell.mp3",
                }
                sound_file = sound_map.get(sound, sound_map["default"])

            # Set alarm volume
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": media_player, "volume_level": volume},
                blocking=True,
            )

            # Play sound
            await self.hass.services.async_call(
                "media_player",
                "play_media",
                {
                    "entity_id": media_player,
                    "media_content_id": sound_file,
                    "media_content_type": "music",
                },
                blocking=False,
            )

            _LOGGER.info("Playing alarm sound: %s on %s", sound, media_player)

        except Exception as e:
            _LOGGER.error("Error playing alarm sound: %s", e)

    def _is_ringing(self, alarm_id: int) -> bool:
        """Return True if the alarm is still in the ringing state."""
        return alarm_id in self.hass.data.get(DOMAIN, {}).get("ringing_alarms", {})

    def _satellite_busy(self, media_player: str) -> bool:
        """Return True if a voice satellite on the same device is mid-interaction."""
        ent_reg = er.async_get(self.hass)
        mp_entry = ent_reg.async_get(media_player)
        if not mp_entry or not mp_entry.device_id:
            return False
        for entry in er.async_entries_for_device(ent_reg, mp_entry.device_id):
            if entry.domain != "assist_satellite":
                continue
            state = self.hass.states.get(entry.entity_id)
            if state and state.state in ("listening", "processing", "responding"):
                return True
        return False

    def _start_ringing_watchdog(self, alarm_id: int, sound: str, media_player: str):
        """Start a watchdog that keeps the alarm sound playing while ringing."""
        self.cancel_ringing_watchdog(alarm_id)
        self._ringing_watchdogs[alarm_id] = self.hass.async_create_background_task(
            self._ringing_watchdog(alarm_id, sound, media_player),
            f"{DOMAIN}_ringing_watchdog_{alarm_id}",
        )

    def cancel_ringing_watchdog(self, alarm_id: int):
        """Cancel the ringing watchdog for an alarm."""
        task = self._ringing_watchdogs.pop(alarm_id, None)
        if task:
            task.cancel()

    async def _ringing_watchdog(self, alarm_id: int, sound: str, media_player: str):
        """Restart alarm playback whenever it stops while the alarm is ringing."""
        try:
            led_refreshed = False
            while self._is_ringing(alarm_id):
                await asyncio.sleep(RINGING_CHECK_INTERVAL)
                if not self._is_ringing(alarm_id):
                    break

                state = self.hass.states.get(media_player)
                if state and state.state in ("playing", "buffering"):
                    if not led_refreshed:
                        await self._set_alarm_led(alarm_id)
                        led_refreshed = True
                    continue
                led_refreshed = False

                await asyncio.sleep(RINGING_RESTART_GRACE)
                while self._is_ringing(alarm_id) and self._satellite_busy(media_player):
                    await asyncio.sleep(RINGING_CHECK_INTERVAL)
                if not self._is_ringing(alarm_id):
                    break

                _LOGGER.debug("Restarting alarm sound for alarm %d", alarm_id)
                await self._play_alarm_sound(sound, media_player_override=media_player)

                if not self._is_ringing(alarm_id):
                    await self.hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {"entity_id": media_player},
                        blocking=False,
                    )
                    break
        finally:
            if self._ringing_watchdogs.get(alarm_id) is asyncio.current_task():
                self._ringing_watchdogs.pop(alarm_id, None)

    async def _send_notification(self, alarm_name: str, alarm_id: int):
        """Send a notification for the alarm."""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Alarm",
                    "message": f"Alarm '{alarm_name}' is ringing!",
                    "notification_id": f"alarm_{alarm_id}",
                },
                blocking=False,
            )
        except Exception as e:
            _LOGGER.error("Error sending alarm notification: %s", e)

    async def _schedule_auto_dismiss(self, alarm_id: int):
        """Schedule automatic dismissal of a ringing alarm."""
        from homeassistant.helpers.event import async_call_later

        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        auto_dismiss_minutes = config_data.get(
            CONF_AUTO_DISMISS_DURATION, DEFAULT_AUTO_DISMISS_DURATION
        )

        async def auto_dismiss_callback(now):
            _LOGGER.info("Auto-dismissing alarm %d after %d minutes", alarm_id, auto_dismiss_minutes)

            self.cancel_ringing_watchdog(alarm_id)

            ringing_alarms = self.hass.data.get(DOMAIN, {}).get("ringing_alarms", {})
            alarm_data = ringing_alarms.pop(alarm_id, {})
            media_player = alarm_data.get("media_player") or config_data.get(CONF_MEDIA_PLAYER)

            if media_player:
                try:
                    await self.hass.services.async_call(
                        "media_player",
                        "media_stop",
                        {"entity_id": media_player},
                        blocking=False,
                    )
                except Exception as e:
                    _LOGGER.warning("Could not stop media player during auto-dismiss: %s", e)

            try:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": f"alarm_{alarm_id}"},
                    blocking=False,
                )
            except Exception as e:
                _LOGGER.warning("Could not dismiss notification during auto-dismiss: %s", e)

            # Restore LED & Volume if no more ringing alarms
            if not self.hass.data.get(DOMAIN, {}).get("ringing_alarms"):
                await self._restore_led_state()
                await self._restore_volume_state(media_player)

            self._auto_dismiss_timers.pop(alarm_id, None)

        cancel_timer = async_call_later(
            self.hass, auto_dismiss_minutes * 60, auto_dismiss_callback
        )
        self._auto_dismiss_timers[alarm_id] = cancel_timer
        _LOGGER.info("Scheduled auto-dismiss for alarm %d in %d minutes", alarm_id, auto_dismiss_minutes)

    async def _save_led_state(self):
        """Save the current LED ring state before changing it."""
        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        led_entity = config_data.get(CONF_LED_ENTITY)
        if not led_entity:
            return

        state = self.hass.states.get(led_entity)
        if state:
            saved = led_effects.snapshot(state.state, state.attributes)
            self.hass.data.setdefault(DOMAIN, {})["saved_led_state"] = saved
            _LOGGER.debug("Saved LED state: %s", saved)

    def _led_effect_for(self, alarm_id: int | None, config_data: dict) -> str | None:
        """The effect this ring should show: the alarm's own, else the configured one."""
        ringing = self.hass.data.get(DOMAIN, {}).get("ringing_alarms", {})
        own = ringing.get(alarm_id, {}).get("led_effect") if alarm_id is not None else None
        if own:
            return own
        return config_data.get(CONF_LED_EFFECT, DEFAULT_LED_EFFECT)

    async def _set_alarm_led(self, alarm_id: int | None = None):
        """Set the LED ring to the alarm colour and effect."""
        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        led_entity = config_data.get(CONF_LED_ENTITY)
        if not led_entity:
            return

        state = self.hass.states.get(led_entity)
        if state is None or state.state == "unavailable":
            if not self._led_unavailable_warned:
                _LOGGER.warning(
                    "LED ring entity %s is not available, cannot set alarm color",
                    led_entity,
                )
                self._led_unavailable_warned = True
            return
        self._led_unavailable_warned = False

        if "saved_led_state" not in self.hass.data.get(DOMAIN, {}):
            await self._save_led_state()

        led_color = config_data.get(CONF_LED_COLOR, DEFAULT_LED_COLOR)
        data = led_effects.alarm_turn_on(
            led_entity,
            led_color,
            255,
            self._led_effect_for(alarm_id, config_data),
            state.attributes.get("effect_list"),
        )

        try:
            await self.hass.services.async_call(
                "light", "turn_on", data, blocking=False,
            )
            _LOGGER.info(
                "Set LED ring to alarm color %s, effect %s",
                led_color, data.get("effect", "(none)"),
            )
        except Exception as e:
            _LOGGER.warning("Could not set LED ring: %s", e)

    async def _restore_led_state(self):
        """Restore the LED ring to its previous state, effect included."""
        config_data = self.hass.data.get(DOMAIN, {}).get("config", {})
        led_entity = config_data.get(CONF_LED_ENTITY)
        if not led_entity:
            return

        saved = self.hass.data.get(DOMAIN, {}).pop("saved_led_state", None)
        if not saved:
            return

        state = self.hass.states.get(led_entity)
        effect_list = state.attributes.get("effect_list") if state else None
        service, data = led_effects.restore_service(led_entity, saved, effect_list)

        try:
            await self.hass.services.async_call(
                "light", service, data, blocking=False,
            )
            _LOGGER.info("Restored LED ring state")
        except Exception as e:
            _LOGGER.warning("Could not restore LED ring: %s", e)

    async def reschedule_all(self):
        """Reschedule all alarms (useful after configuration changes)."""
        _LOGGER.info("Rescheduling all alarms")

        for timer_cancel in self._scheduled_timers.values():
            timer_cancel()
        self._scheduled_timers.clear()

        alarms = self.storage.get_enabled_alarms()
        for alarm in alarms:
            await self._schedule_alarm(alarm)