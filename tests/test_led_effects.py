"""led_effects is pure; load it by path so the package's HA imports are not needed."""
import importlib.util
import pathlib

import pytest

_P = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "alarm_assistant" / "led_effects.py"
_spec = importlib.util.spec_from_file_location("led_effects", _P)
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

ECHO = ["None", "Pulse", "Slow pulse", "Fast pulse", "Spin"]


def test_a_listed_effect_is_sent_as_the_light_spells_it():
    assert E.effect_to_send("fast PULSE", ECHO) == "Fast pulse"


def test_a_light_without_effects_never_gets_an_effect_field():
    for lst in (None, [], ()):
        assert E.effect_to_send("Pulse", lst) is None
        assert "effect" not in E.alarm_turn_on("light.x", [255, 0, 0], 255, "Pulse", lst)


def test_an_effect_the_light_lacks_falls_back_to_clearing_not_to_an_error():
    # Naming it would make light.turn_on fail and could lose the colour with it.
    assert E.effect_to_send("Rainbow", ECHO) == "None"
    assert E.effect_to_send("Rainbow", ["Colorloop", "Strobe"]) is None


@pytest.mark.parametrize("asked", [None, "", "none", "None", "off", "solid"])
def test_a_solid_alarm_clears_a_running_effect(asked):
    assert E.effect_to_send(asked, ECHO) == "None"
    assert E.wants_effect(asked) is False


def test_the_none_entry_is_found_without_regard_to_case():
    assert E.effect_to_send(None, ["none", "Pulse"]) == "none"


def test_asking_for_none_by_name_never_returns_a_real_effect():
    assert E.effect_to_send("None", ["Pulse", "None"]) == "None"


def test_alarm_turn_on_carries_colour_brightness_and_effect():
    d = E.alarm_turn_on("light.ring", [255, 0, 0], 255, "Fast pulse", ECHO)
    assert d == {"entity_id": "light.ring", "rgb_color": [255, 0, 0],
                 "brightness": 255, "effect": "Fast pulse"}


def test_snapshot_copies_the_colour_so_later_changes_cannot_alter_it():
    attrs = {"brightness": 128, "rgb_color": (1, 2, 3), "effect": "Spin"}
    s = E.snapshot("on", attrs)
    assert s == {"state": "on", "brightness": 128, "rgb_color": [1, 2, 3], "effect": "Spin"}
    assert isinstance(s["rgb_color"], list)


def test_restore_off_is_turn_off():
    svc, data = E.restore_service("light.ring", {"state": "off"}, ECHO)
    assert (svc, data) == ("turn_off", {"entity_id": "light.ring"})


def test_restore_puts_back_the_previous_effect():
    saved = {"state": "on", "brightness": 90, "rgb_color": [0, 255, 0], "effect": "Spin"}
    svc, data = E.restore_service("light.ring", saved, ECHO)
    assert svc == "turn_on"
    assert data == {"entity_id": "light.ring", "brightness": 90,
                    "rgb_color": [0, 255, 0], "effect": "Spin"}


@pytest.mark.parametrize("before", [None, "None", ""])
def test_restore_clears_the_alarms_own_effect_when_there_was_none(before):
    saved = {"state": "on", "brightness": 90, "rgb_color": [0, 255, 0], "effect": before}
    _, data = E.restore_service("light.ring", saved, ECHO)
    assert data["effect"] == "None"


def test_restore_on_a_plain_light_sends_no_effect():
    saved = {"state": "on", "brightness": 90, "rgb_color": [0, 255, 0], "effect": None}
    _, data = E.restore_service("light.lamp", saved, None)
    assert "effect" not in data
