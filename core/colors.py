"""
colors.py - reads the [colors] section of precog.conf and provides
ANSI 24-bit true-color escape codes for each output category.
"""

import configparser
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "precog.conf"

RESET = "\x1b[0m"

NAMED_COLORS = {
    "black":   (0, 0, 0),
    "red":     (255, 0, 0),
    "green":   (0, 200, 0),
    "yellow":  (230, 230, 0),
    "blue":    (60, 120, 255),
    "magenta": (255, 0, 255),
    "cyan":    (0, 200, 200),
    "white":   (255, 255, 255),
}

CATEGORIES = ["base_system", "boot_entry", "tier1_pattern", "tier2_corr", "tier3_triage", "aggregate"]


def _hex_to_rgb(hex_str):
    hex_str = hex_str.strip().lstrip("#")
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return (r, g, b)


def _resolve_color(value):
    value = value.strip()
    if value.startswith("#"):
        return _hex_to_rgb(value)
    lowered = value.lower()
    if lowered in NAMED_COLORS:
        return NAMED_COLORS[lowered]
    raise ValueError(f"Unrecognized color value: '{value}'")


def _rgb_to_ansi(rgb):
    r, g, b = rgb
    return f"\x1b[38;2;{r};{g};{b}m"


class ColorScheme:
    """
    Loads the active [colors] section from precog.conf and exposes
    a colorize() method for wrapping text in the right ANSI codes.
    """

    def __init__(self, config_path=None):
        self.config_path = config_path or CONFIG_PATH
        self._codes = {}
        self._load()

    def _load(self):
        parser = configparser.ConfigParser()
        parser.read(self.config_path)

        if "colors" not in parser:
            # No config found or [colors] missing - fall back to plain
            # text, no coloring, rather than crashing.
            self._codes = {cat: "" for cat in CATEGORIES}
            return

        section = parser["colors"]
        for cat in CATEGORIES:
            if cat in section:
                try:
                    rgb = _resolve_color(section[cat])
                    self._codes[cat] = _rgb_to_ansi(rgb)
                except ValueError:
                    self._codes[cat] = ""
            else:
                self._codes[cat] = ""

    def colorize(self, text, category):
        """
        Wraps text in the ANSI color code for the given category.
        Falls back to plain text if the category is unknown or has
        no color configured.
        """
        code = self._codes.get(category, "")
        if not code:
            return text
        return f"{code}{text}{RESET}"
