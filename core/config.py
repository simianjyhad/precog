"""
core/config.py

Parser for precog.conf. Reads [thresholds], [keywords], and [colors]
sections and hands back clean, ready-to-use Python objects. Does not
apply colors to output itself - that's the terminal-application layer,
still to be designed (see context notes).

Usage:
    from core.config import PrecogConfig

    cfg = PrecogConfig("/path/to/precog.conf")
    cfg.thresholds["tier1_pattern_watch_count"]   # -> 3 (int)
    cfg.keywords["critical"]                       # -> list[str]
    cfg.keywords["watched"]                        # -> list[str]
    cfg.colors["tier3_triage"]                     # -> "#D55E00" or "red"
    cfg.resolve_color("tier3_triage")              # -> ANSI escape code
"""

import configparser
from pathlib import Path


# Named colors mapped to standard ANSI escape codes (foreground, 8-color).
_ANSI_NAMED_COLORS = {
    "black":   "\033[30m",
    "red":     "\033[31m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "blue":    "\033[34m",
    "magenta": "\033[35m",
    "cyan":    "\033[36m",
    "white":   "\033[37m",
}

ANSI_RESET = "\033[0m"


class ConfigError(Exception):
    """Raised when precog.conf is missing required sections/keys or
    contains a value that can't be parsed."""
    pass


class PrecogConfig:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ConfigError(f"Config file not found: {self.path}")

        parser = configparser.ConfigParser()
        # Preserve key case as-typed (colors/keywords keys are lowercase
        # by convention, but this avoids surprises if that ever changes).
        parser.optionxform = str
        read_ok = parser.read(self.path)
        if not read_ok:
            raise ConfigError(f"Could not read config file: {self.path}")

        self.thresholds = self._load_thresholds(parser)
        self.keywords = self._load_keywords(parser)
        self.colors = self._load_colors(parser)
        self.retention = self._load_retention(parser)
        self.keyword_exclusions = self._load_keyword_exclusions(parser)
        self.noisy_keywords = self._load_noisy_keywords(parser)

    # ------------------------------------------------------------------
    def _load_thresholds(self, parser):
        if "thresholds" not in parser:
            raise ConfigError("[thresholds] section missing from precog.conf")

        section = parser["thresholds"]
        required = [
            "tier1_pattern_watch_count",
            "tier1_pattern_watch_window",
            "tier2_correlation_count",
            "tier2_correlation_window",
            "tier3_triage_count",
        ]

        result = {}
        for key in required:
            if key not in section:
                raise ConfigError(f"[thresholds] missing required key: {key}")
            raw = section[key].strip()
            try:
                result[key] = int(raw)
            except ValueError:
                raise ConfigError(
                    f"[thresholds] {key} must be an integer, got: {raw!r}"
                )

        # Optional total_samples per tier (sliding window sample size,
        # e.g. "3 of 5"). Falls back to required_count if not present,
        # so older precog.conf files without these keys still work.
        sample_map = {
            "tier1_pattern_watch_count": "tier1_pattern_watch_samples",
            "tier2_correlation_count":   "tier2_correlation_samples",
            "tier3_triage_count":        "tier3_triage_samples",
        }
        for count_key, samples_key in sample_map.items():
            if samples_key in section:
                raw = section[samples_key].strip()
                try:
                    result[samples_key] = int(raw)
                except ValueError:
                    raise ConfigError(
                        f"[thresholds] {samples_key} must be an integer, got: {raw!r}"
                    )
            else:
                result[samples_key] = result[count_key]

        return result

    # ------------------------------------------------------------------
    def _load_keywords(self, parser):
        if "keywords" not in parser:
            raise ConfigError("[keywords] section missing from precog.conf")

        section = parser["keywords"]
        result = {}
        for key in ("critical", "watched"):
            if key not in section:
                raise ConfigError(f"[keywords] missing required key: {key}")
            raw = section[key]
            # Split on commas, strip whitespace, drop empty entries
            # (guards against trailing commas or accidental blank lines).
            items = [item.strip() for item in raw.split(",")]
            items = [item for item in items if item]
            if not items:
                raise ConfigError(f"[keywords] {key} is empty")
            result[key] = items
        return result

    # ------------------------------------------------------------------
    def _load_colors(self, parser):
        if "colors" not in parser:
            raise ConfigError("[colors] section missing from precog.conf")

        section = parser["colors"]
        required = [
            "base_system",
            "boot_entry",
            "tier1_pattern",
            "tier2_corr",
            "tier3_triage",
        ]

        result = {}
        for key in required:
            if key not in section:
                raise ConfigError(f"[colors] missing required key: {key}")
            result[key] = section[key].strip()
        return result

    # ------------------------------------------------------------------
    def _load_retention(self, parser):
        """
        Optional section — falls back to a 7 day default if [retention]
        or its key is missing entirely, rather than raising ConfigError.
        Unlike thresholds/keywords/colors, this section is new and
        shouldn't break loading of a precog.conf that predates it.
        """
        default = 7
        if "retention" not in parser:
            return {"flagged_log_cutoff_days": default}

        section = parser["retention"]
        if "flagged_log_cutoff_days" not in section:
            return {"flagged_log_cutoff_days": default}

        raw = section["flagged_log_cutoff_days"].strip()
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(
                f"[retention] flagged_log_cutoff_days must be an integer, got: {raw!r}"
            )
        return {"flagged_log_cutoff_days": value}

    # ------------------------------------------------------------------
    def _load_keyword_exclusions(self, parser):
        """
        Optional section — falls back to an empty dict if missing.
        Maps a keyword to a phrase that, if present in the same log
        line, means the match should be skipped (a false-positive
        guard for keywords that also appear in harmless boilerplate).
        """
        if "keyword_exclusions" not in parser:
            return {}

        section = parser["keyword_exclusions"]
        result = {}
        for key in section:
            result[key.strip().lower()] = section[key].strip().lower()
        return result

    # ------------------------------------------------------------------
    def _load_noisy_keywords(self, parser):
        """
        Optional section — falls back to an empty dict if missing.
        Maps a keyword to its own (required_count, window_seconds) pair,
        overriding the standard Pattern Watch threshold for that keyword
        specifically. Format in precog.conf: keyword = count, window
        """
        if "noisy_keywords" not in parser:
            return {}

        section = parser["noisy_keywords"]
        result = {}
        for key in section:
            raw = section[key].strip()
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) != 2:
                raise ConfigError(
                    f"[noisy_keywords] {key} must be 'count, window_seconds', "
                    f"got: {raw!r}"
                )
            try:
                count = int(parts[0])
                window = int(parts[1])
            except ValueError:
                raise ConfigError(
                    f"[noisy_keywords] {key} values must be integers, got: {raw!r}"
                )
            result[key.strip().lower()] = (count, window)
        return result

    # ------------------------------------------------------------------
    def resolve_color(self, category):
        """
        Return the ANSI escape code for a given color category
        (e.g. 'tier3_triage'). Accepts named colors (from
        _ANSI_NAMED_COLORS) or hex codes (#RRGGBB, converted to
        24-bit ANSI true-color escapes).
        """
        if category not in self.colors:
            raise ConfigError(f"Unknown color category: {category}")

        value = self.colors[category].lower()

        if value in _ANSI_NAMED_COLORS:
            return _ANSI_NAMED_COLORS[value]

        if value.startswith("#") and len(value) == 7:
            try:
                r = int(value[1:3], 16)
                g = int(value[3:5], 16)
                b = int(value[5:7], 16)
            except ValueError:
                raise ConfigError(f"Invalid hex color for {category}: {value}")
            return f"\033[38;2;{r};{g};{b}m"

        raise ConfigError(
            f"Unrecognized color value for {category}: {value!r} "
            f"(expected a name like 'red' or a hex code like '#FF8800')"
        )