"""NES controller shift-register behavior."""

from __future__ import annotations

from .constants import Button


class Controller:
    """An eight-button controller as seen through the $4016/$4017 serial bus."""

    def __init__(self) -> None:
        self.buttons = 0
        self.shift = 0
        self.strobe = False

    def set_button(self, button: Button, pressed: bool) -> None:
        if pressed:
            self.buttons |= int(button)
        else:
            self.buttons &= ~int(button)
        if self.strobe:
            self.shift = self.buttons

    def set_buttons(self, buttons: int) -> None:
        self.buttons = buttons & 0xFF
        if self.strobe:
            self.shift = self.buttons

    def write_strobe(self, value: int) -> None:
        new_strobe = bool(value & 1)
        if new_strobe or self.strobe:
            self.shift = self.buttons
        self.strobe = new_strobe

    def read(self) -> int:
        if self.strobe:
            return self.buttons & 1
        result = self.shift & 1
        self.shift = ((self.shift >> 1) | 0x80) & 0xFF
        return result

    def get_state(self) -> dict:
        return {
            "buttons": self.buttons,
            "shift": self.shift,
            "strobe": self.strobe,
        }

    def set_state(self, state: dict) -> None:
        self.buttons = int(state["buttons"]) & 0xFF
        self.shift = int(state["shift"]) & 0xFF
        self.strobe = bool(state["strobe"])

