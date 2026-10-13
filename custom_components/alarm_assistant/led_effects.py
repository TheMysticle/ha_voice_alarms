"""Choosing and restoring a light effect for the alarm ring (pure, no HA imports).

An alarm used to turn the satellite's ring a colour and nothing else. A light
with effects can do more, and a light WITHOUT them must keep working, so every
decision here is made against the effect list the light itself reports:

* an effect is only ever sent if the light lists it (matched without regard to
  case), because a light.turn_on naming an effect the light does not have is an
  error that can take the colour with it;
* a solid alarm on a light that has effects first CLEARS whatever effect is
  running, otherwise a colour change is painted under a pulse nobody asked for;
* restoring puts back the effect the light had before the alarm, or clears the
  alarm's own.

The effect list is the `effect_list` attribute of the light's state, which Home
Assistant fills for ESPHome lights (and EchoMuse's ring) from the device.
"""
from __future__ import annotations

from typing import Any, Iterable

# What a light calls "no effect". ESPHome lists it first; HA shows it as the
# way to clear one. Compared without case.
NONE_NAMES = {"none", "off", "solid", ""}

# Effects EchoMuse's ring offers, for the setup form. Other lights bring their
# own list; the form accepts any text, and the light decides at ring time.
SUGGESTED = ["Pulse", "Fast pulse", "Slow pulse", "Spin", "None"]


def _match(name: str, effect_list: Iterable[str]) -> str | None:
    want = name.strip().lower()
    for e in effect_list:
        if isinstance(e, str) and e.strip().lower() == want:
            return e
    return None


def wants_effect(requested: str | None) -> bool:
    """Whether `requested` names an effect, as opposed to none at all."""
    return bool(requested) and requested.strip().lower() not in NONE_NAMES


def effect_to_send(requested: str | None, effect_list: Iterable[str] | None) -> str | None:
    """The `effect` to put in light.turn_on, or None to send no effect field.

    A light with no effect list gets no effect field whatever was asked. With a
    list, a requested effect it has is sent as the light spells it; "no effect"
    (or one the light lacks) sends the light's own "None" entry when it has
    one, which clears a running effect, and nothing when it has not.
    """
    effects = [e for e in (effect_list or []) if isinstance(e, str)]
    if not effects:
        return None
    if wants_effect(requested):
        found = _match(requested, effects)
        if found is not None and found.strip().lower() not in NONE_NAMES:
            return found
    return _match("none", effects)


def alarm_turn_on(entity_id: str, rgb: list[int], brightness: int,
                  requested_effect: str | None,
                  effect_list: Iterable[str] | None) -> dict[str, Any]:
    """The light.turn_on data for the ringing alarm."""
    data: dict[str, Any] = {"entity_id": entity_id, "rgb_color": list(rgb),
                            "brightness": brightness}
    effect = effect_to_send(requested_effect, effect_list)
    if effect is not None:
        data["effect"] = effect
    return data


def snapshot(state_value: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """What to remember about a light before the alarm takes it."""
    rgb = attributes.get("rgb_color")
    return {
        "state": state_value,
        "brightness": attributes.get("brightness"),
        "rgb_color": list(rgb) if rgb is not None else None,
        "effect": attributes.get("effect"),
    }


def restore_service(entity_id: str, saved: dict[str, Any],
                    effect_list: Iterable[str] | None) -> tuple[str, dict[str, Any]]:
    """(service, data) that put the light back as `snapshot` found it."""
    if saved.get("state") == "off":
        return "turn_off", {"entity_id": entity_id}
    data: dict[str, Any] = {"entity_id": entity_id}
    if saved.get("brightness") is not None:
        data["brightness"] = saved["brightness"]
    if saved.get("rgb_color") is not None:
        data["rgb_color"] = list(saved["rgb_color"])
    effect = effect_to_send(saved.get("effect"), effect_list)
    if effect is not None:
        data["effect"] = effect
    return "turn_on", data
