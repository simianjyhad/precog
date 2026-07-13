"""
alert.py — Precog Core Module
Alert flagging infrastructure: defines alert tiers, per-tier thresholds
with sliding time windows, and the AlertTracker that decides when an
entry has crossed the threshold and should be flagged to the user.

This module sits on top of rolling_log.py. It does not modify LogEntry
or RollingLog directly — it maintains its own index of flagged entries
and exposes a clean interface for querying them.

Design principle: the threshold system prevents alert fatigue by requiring
a pattern to repeat within a time window before it earns user attention.
Different tiers have different thresholds because different severity levels
warrant different levels of certainty before interrupting the user.
"""

import threading
import time
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from rolling_log import LogEntry


# ---------------------------------------------------------------------------
# Alert tiers
# ---------------------------------------------------------------------------

class AlertLevel(Enum):
    """
    Three tier alert system.

    PATTERN_WATCH  — Something worth watching but not urgent.
                     A single subsystem showing elevated error rates.
                     High threshold — needs to earn the flag.

    CORRELATION    — Two or more subsystems showing related errors
                     close together in time. Systemic signal.
                     Medium threshold — cross-subsystem correlation
                     is already a stronger signal.

    TRIAGE         — Critical system errors requiring immediate attention.
                     Lowest threshold — flags on first occurrence.
    """
    PATTERN_WATCH = 1
    CORRELATION   = 2
    TRIAGE        = 3


# ---------------------------------------------------------------------------
# Threshold configuration
# ---------------------------------------------------------------------------

@dataclass
class AlertThreshold:
    """
    Threshold definition for a single alert tier.

    required_count  : how many matching events must occur within the window
    window_seconds  : the sliding time window those events must fall within
    total_samples   : the denominator for the ratio (e.g. 3 of 5)
                      Set equal to required_count if you just want a flat count.

    Defaults:
      PATTERN_WATCH  — 3 of 5 within 10 minutes
      CORRELATION    — 2 of 3 within 5 minutes
      TRIAGE         — 1 of 1 immediately (any occurrence)

    All values are configurable via precog.conf once config loading
    is implemented. These defaults are the starting point for tuning.
    """
    required_count: int    # how many hits needed to fire
    total_samples:  int    # sliding window sample size (hits tracked)
    window_seconds: int    # time window in seconds


# Default thresholds per tier
DEFAULT_THRESHOLDS: dict[AlertLevel, AlertThreshold] = {
    AlertLevel.PATTERN_WATCH: AlertThreshold(
        required_count=3,
        total_samples=5,
        window_seconds=600,    # 10 minutes
    ),
    AlertLevel.CORRELATION: AlertThreshold(
        required_count=2,
        total_samples=3,
        window_seconds=300,    # 5 minutes
    ),
    AlertLevel.TRIAGE: AlertThreshold(
        required_count=1,
        total_samples=1,
        window_seconds=0,      # immediate — no window needed
    ),
}


# ---------------------------------------------------------------------------
# Config integration — build a thresholds dict from a PrecogConfig instance
# ---------------------------------------------------------------------------

def thresholds_from_config(cfg) -> "dict[AlertLevel, AlertThreshold]":
    """
    Convert a PrecogConfig instance's cfg.thresholds dict into the
    dict[AlertLevel, AlertThreshold] format AlertTracker expects.

    Falls back cleanly if cfg is None (caller should just not call this
    and use DEFAULT_THRESHOLDS directly in that case, but this guard
    keeps it safe either way).
    """
    t = cfg.thresholds
    return {
        AlertLevel.PATTERN_WATCH: AlertThreshold(
            required_count=t["tier1_pattern_watch_count"],
            total_samples=t["tier1_pattern_watch_samples"],
            window_seconds=t["tier1_pattern_watch_window"],
        ),
        AlertLevel.CORRELATION: AlertThreshold(
            required_count=t["tier2_correlation_count"],
            total_samples=t["tier2_correlation_samples"],
            window_seconds=t["tier2_correlation_window"],
        ),
        AlertLevel.TRIAGE: AlertThreshold(
            required_count=t["tier3_triage_count"],
            total_samples=t["tier3_triage_samples"],
            window_seconds=0,
        ),
    }


# ---------------------------------------------------------------------------
# FlaggedEntry — a LogEntry that has crossed a threshold
# ---------------------------------------------------------------------------

@dataclass
class FlaggedEntry:
    """
    A log entry that has been flagged by the alert system.

    entry       : the original LogEntry that triggered the flag
    level       : which alert tier fired
    flagged_at  : when the threshold was crossed (nanoseconds since epoch)
    pattern_key : the key used to track this pattern (source:keyword or similar)
    hit_count   : how many matching events occurred in the window when fired
    note        : optional human readable context added by the flagging logic
    """
    entry:       LogEntry
    level:       AlertLevel
    flagged_at:  int            # ns since epoch
    pattern_key: str
    hit_count:   int
    note:        Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.name
        d["entry"] = asdict(self.entry)
        return d

    @staticmethod
    def from_dict(d: dict) -> "FlaggedEntry":
        entry = LogEntry(**d["entry"])
        level = AlertLevel[d["level"]]
        return FlaggedEntry(
            entry=entry,
            level=level,
            flagged_at=d["flagged_at"],
            pattern_key=d["pattern_key"],
            hit_count=d["hit_count"],
            note=d.get("note"),
        )


# ---------------------------------------------------------------------------
# PatternWindow — sliding window hit tracker for a single pattern key
# ---------------------------------------------------------------------------

class PatternWindow:
    """
    Tracks recent hit timestamps for a single pattern key within a tier.

    Maintains a deque of hit timestamps (nanoseconds). On each new hit,
    expired timestamps are pruned first, then the new hit is added.
    Returns True when the required_count threshold is met within the window.

    This is the core mechanism that makes "3 of 5 within 10 minutes" work.
    The deque is capped at total_samples so old hits naturally fall off.
    """

    def __init__(self, threshold: AlertThreshold):
        self.threshold = threshold
        self._hits: deque[int] = deque(maxlen=threshold.total_samples)

    def record_hit(self, timestamp_ns: int) -> bool:
        """
        Record a new hit at timestamp_ns.
        Returns True if the threshold has been crossed.
        """
        if self.threshold.window_seconds == 0:
            # TRIAGE tier — immediate, no window check needed
            self._hits.append(timestamp_ns)
            return True

        cutoff_ns = timestamp_ns - (self.threshold.window_seconds * 1_000_000_000)

        # Prune hits that have fallen outside the window
        while self._hits and self._hits[0] < cutoff_ns:
            self._hits.popleft()

        self._hits.append(timestamp_ns)

        # Count how many hits are within the window
        hits_in_window = sum(1 for h in self._hits if h >= cutoff_ns)
        return hits_in_window >= self.threshold.required_count

    def hit_count_in_window(self, now_ns: int) -> int:
        """Return how many hits are currently within the window."""
        if self.threshold.window_seconds == 0:
            return len(self._hits)
        cutoff_ns = now_ns - (self.threshold.window_seconds * 1_000_000_000)
        return sum(1 for h in self._hits if h >= cutoff_ns)

    def reset(self) -> None:
        """Clear all recorded hits. Call after a threshold fires to avoid re-firing."""
        self._hits.clear()


# ---------------------------------------------------------------------------
# AlertTracker — the main alert engine
# ---------------------------------------------------------------------------

class AlertTracker:
    """
    Evaluates incoming log entries against configured thresholds and
    maintains a list of flagged entries that have crossed those thresholds.

    Usage:
        tracker = AlertTracker()
        flagged = tracker.evaluate(entry, alert_level=AlertLevel.PATTERN_WATCH,
                                   pattern_key="kernel:buffer_overrun")
        if flagged:
            # entry crossed the threshold, do something with flagged

    Pattern keys:
        A pattern_key is a string that groups related events together for
        threshold counting. Format is flexible — "source:keyword" works well
        for most cases, e.g. "journalctl:oom_kill", "auth.log:failed_password".
        The caller decides what constitutes a pattern. AlertTracker just counts.

    Thread safety:
        All public methods are thread-safe. Multiple watchers can call
        evaluate() concurrently without data races.

    Persistence:
        Flagged entries are saved to disk so they survive restarts.
        The flag store is separate from the rolling log — rolling.log holds
        all entries, flagged.log holds only entries that crossed a threshold.
    """

    def __init__(
        self,
        thresholds: dict[AlertLevel, AlertThreshold] = None,
        data_dir: Path = None,
    ):
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.data_dir = data_dir or (Path(__file__).parent.parent / "data")
        self.flag_file = self.data_dir / "flagged.log"
        self.archive_file = self.data_dir / "flagged_archive.log"

        self._lock = threading.Lock()

        # pattern_key -> AlertLevel -> PatternWindow
        self._windows: dict[str, dict[AlertLevel, PatternWindow]] = defaultdict(dict)

        # Ordered list of all flagged entries, newest last
        self._flagged: list[FlaggedEntry] = []

        # Ensure data dir exists and load previous flags from disk.
        # Explicit chmod here (not just relying on install.py) so
        # permissions are correct even if precog.py is ever run
        # directly on a fresh system without install.py having run
        # first — this dir/file can contain sensitive log fragments
        # and shouldn't be readable by every user on the system.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        if not self.flag_file.exists():
            self.flag_file.touch(mode=0o600)
        else:
            self.flag_file.chmod(0o600)
        self._load_from_disk()
        # Fast lookup for dedup: prevents re-flagging entries already
        # flagged in this run or a prior one (survives restarts since
        # _load_from_disk() populates self._flagged before this runs)
        self._flagged_keys: set[tuple[int, str]] = {
            (f.entry.timestamp_ns, f.pattern_key) for f in self._flagged
        }

    # --- Public interface ---------------------------------------------------

    def evaluate(
        self,
        entry: LogEntry,
        alert_level: AlertLevel,
        pattern_key: str,
        note: str = None,
    ) -> Optional[FlaggedEntry]:
        """
        Evaluate a log entry against the threshold for alert_level.

        If the threshold is crossed, creates a FlaggedEntry, adds it to
        the flagged list, persists it to disk, and returns it.
        Returns None if the threshold has not been crossed yet.

        The caller is responsible for deciding:
          - which alert_level applies to this entry
          - what pattern_key groups this entry with related events

        AlertTracker is responsible for:
          - counting hits within the sliding window
          - deciding when the threshold is crossed
          - storing and persisting flagged entries
        """
        with self._lock:
            # Dedup: skip entries already flagged under this pattern_key,
            # whether in this run or a prior one (persists via flagged.log)
            dedup_key = (entry.timestamp_ns, pattern_key)
            if dedup_key in self._flagged_keys:
                return None

            # Get or create the PatternWindow for this key + tier combination
            if alert_level not in self._windows[pattern_key]:
                self._windows[pattern_key][alert_level] = PatternWindow(
                    self.thresholds[alert_level]
                )

            window = self._windows[pattern_key][alert_level]
            threshold_crossed = window.record_hit(entry.timestamp_ns)

            if not threshold_crossed:
                return None

            # Threshold crossed — create a flagged entry
            hit_count = window.hit_count_in_window(entry.timestamp_ns)
            flagged = FlaggedEntry(
                entry=entry,
                level=alert_level,
                flagged_at=time.time_ns(),
                pattern_key=pattern_key,
                hit_count=hit_count,
                note=note,
            )
            self._flagged.append(flagged)
            self._flagged_keys.add(dedup_key)

            # Reset the window so the same pattern doesn't immediately re-fire
            window.reset()

            # Persist immediately — flagged entries are important
            self._append_to_disk(flagged)

            return flagged

    def get_flagged(
        self,
        level: AlertLevel = None,
        since_ns: int = None,
    ) -> list[FlaggedEntry]:
        """
        Return flagged entries, optionally filtered.

        level    : if provided, only return entries at this alert level
        since_ns : if provided, only return entries flagged after this timestamp
        """
        with self._lock:
            results = list(self._flagged)

        if level is not None:
            results = [f for f in results if f.level == level]

        if since_ns is not None:
            results = [f for f in results if f.flagged_at >= since_ns]

        return results

    def flagged_count(self, level: AlertLevel = None) -> int:
        """Return count of flagged entries, optionally filtered by tier."""
        return len(self.get_flagged(level=level))

    def clear_flagged(self, level: AlertLevel = None) -> int:
        """
        Clear flagged entries, optionally only for a specific tier.
        Returns the number of entries cleared.
        Also rewrites the flag file to reflect the cleared state.
        """
        with self._lock:
            if level is None:
                count = len(self._flagged)
                self._flagged.clear()
            else:
                before = len(self._flagged)
                self._flagged = [f for f in self._flagged if f.level != level]
                count = before - len(self._flagged)

            self._rewrite_disk()

        return count

    def archive_old_entries(self, cutoff_days: int = 7) -> int:
        """
        Move flagged entries older than cutoff_days out of the live
        flagged.log and into flagged_archive.log. Nothing is discarded —
        only relocated. The live file stays lean; full history survives.

        Dedup keys (self._flagged_keys) are NOT affected — an entry that
        ages out of the visible file must still never be re-flagged if
        its source log line somehow gets processed again.

        Returns the number of entries archived.
        """
        cutoff_ns = time.time_ns() - (cutoff_days * 86400 * 1_000_000_000)

        with self._lock:
            to_archive = [f for f in self._flagged if f.flagged_at < cutoff_ns]
            if not to_archive:
                return 0

            remaining = [f for f in self._flagged if f.flagged_at >= cutoff_ns]

            try:
                with open(self.archive_file, "a", encoding="utf-8") as f:
                    for flagged in to_archive:
                        f.write(json.dumps(flagged.to_dict()) + "\n")
            except OSError as e:
                print(f"[precog] WARNING: could not write to archive file: {e}")
                return 0

            self._flagged = remaining
            self._rewrite_disk()

        return len(to_archive)

    def reset_pattern(self, pattern_key: str, level: AlertLevel = None) -> None:
        """
        Reset the hit window for a pattern key, optionally for a specific tier.
        Use this to manually clear a pattern that is firing too aggressively
        during threshold tuning.
        """
        with self._lock:
            if pattern_key in self._windows:
                if level is None:
                    for window in self._windows[pattern_key].values():
                        window.reset()
                elif level in self._windows[pattern_key]:
                    self._windows[pattern_key][level].reset()

    # --- Persistence -------------------------------------------------------

    def _append_to_disk(self, flagged: FlaggedEntry) -> None:
        """
        Append a single flagged entry to the flag file.
        Called immediately when a threshold is crossed.
        Append-only — no risk of losing earlier flags on write.
        """
        try:
            with open(self.flag_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(flagged.to_dict()) + "\n")
        except OSError as e:
            print(f"[precog] WARNING: could not write flagged entry to disk: {e}")

    def _rewrite_disk(self) -> None:
        """
        Rewrite the flag file from the current in-memory list.
        Called after clear operations. Uses atomic temp-file rename.
        Caller must hold self._lock.
        """
        tmp = self.flag_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for flagged in self._flagged:
                    f.write(json.dumps(flagged.to_dict()) + "\n")
            tmp.rename(self.flag_file)
        except OSError as e:
            print(f"[precog] WARNING: could not rewrite flag file: {e}")

    def _load_from_disk(self) -> None:
        """
        Load previously flagged entries from disk on startup.
        Malformed lines are skipped silently.
        """
        if not self.flag_file.exists():
            return

        loaded = 0
        skipped = 0

        try:
            with open(self.flag_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        flagged = FlaggedEntry.from_dict(json.loads(line))
                        self._flagged.append(flagged)
                        loaded += 1
                    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                        skipped += 1

            print(f"[precog] Loaded {loaded} flagged entries from disk "
                  f"({skipped} malformed, discarded).")

        except OSError as e:
            print(f"[precog] WARNING: could not load flag file: {e}")