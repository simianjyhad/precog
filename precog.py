#!/usr/bin/env python3
"""
precog.py — Precog Main Entry Point

Wires together the three core modules:
  - rolling_log.py  : 48 hour buffer of raw log entries
  - baseline.py     : learns what "normal" looks like, scans for keywords
  - alert.py        : tracks threshold crossings, flags entries for attention

Flow:
  1. On startup, load (or create) the baseline store.
  2. If the baseline has never been seeded, run JournalSeeder to read
     all available journalctl history and seed it immediately.
  3. If the first-run notice hasn't been shown, print it once.
  4. Start watchers (journalctl + auth.log) feeding into the rolling log.
  5. Every entry that comes in is processed by the baseline collector
     (keyword scan + hourly bucket counting) and checked for alert
     worthy keyword hits, which get evaluated by the alert tracker.
  6. A status loop prints buffer size, confidence, and any new flags
     every 10 seconds.
  7. Ctrl+C triggers a clean shutdown — all watchers stop, baseline
     flushes, alert state saves.

Run directly:
  python3 precog.py
"""

import sys
import signal
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "core"))

from core.rolling_log import RollingLog, WatcherManager, LogEntry
from core.baseline import (
    BaselineStore, BaselineCollector, BaselineConfig, JournalSeeder
)
from core.alert import AlertTracker, AlertLevel, AlertThreshold, thresholds_from_config
from core.colors import ColorScheme
from core.config import PrecogConfig, ConfigError


# ---------------------------------------------------------------------------
# Critical keywords — these trigger TRIAGE (immediate) rather than
# PATTERN_WATCH (3 of 5 within 10 minutes) when matched.
# ---------------------------------------------------------------------------

CRITICAL_KEYWORDS = {
    "oom-kill",
    "out of memory",
    "segfault",
    "segmentation fault",
    "corrupted",
    "no space left",
    "disk full",
}


# ---------------------------------------------------------------------------
# AlertBridge — connects keyword hits found by the baseline collector
# to the alert tracker's threshold evaluation.
# ---------------------------------------------------------------------------

class AlertBridge:
    """
    Decides which alert tier a keyword match should be evaluated against,
    and calls AlertTracker.evaluate() accordingly.
    """

    LEVEL_TO_CATEGORY = {
        AlertLevel.TRIAGE: "tier3_triage",
        AlertLevel.PATTERN_WATCH: "tier1_pattern",
    }

    def __init__(
        self,
        tracker: AlertTracker,
        colors: ColorScheme = None,
        critical_keywords: set = None,
        noisy_keywords: dict = None,
    ):
        self.tracker = tracker
        self.colors = colors or ColorScheme()
        # Falls back to the module-level CRITICAL_KEYWORDS constant if
        # not provided, so existing callers are unaffected.
        self.critical_keywords = (
            critical_keywords if critical_keywords is not None else CRITICAL_KEYWORDS
        )
        # keyword -> (required_count, window_seconds). Keywords here get
        # their own stricter threshold instead of the standard Pattern
        # Watch tier (3 of 5 within 10 minutes) — for things that are
        # individually meaningful but tend to recur as routine noise.
        self.noisy_keywords = noisy_keywords or {}

    def consider(self, entry: LogEntry, keyword: str) -> None:
        """
        Called once per keyword match found during baseline keyword scanning.
        """
        pattern_key = f"{entry.source}:{keyword}"

        custom_threshold = None

        if keyword in self.critical_keywords:
            level = AlertLevel.TRIAGE
            note = f"Critical keyword '{keyword}' detected"
        elif keyword in self.noisy_keywords:
            level = AlertLevel.PATTERN_WATCH
            count, window = self.noisy_keywords[keyword]
            note = f"Noisy keyword '{keyword}' recurring ({count} within {window}s)"
            custom_threshold = AlertThreshold(
                required_count=count, total_samples=count, window_seconds=window
            )
        else:
            level = AlertLevel.PATTERN_WATCH
            note = f"Keyword '{keyword}' recurring"

        flagged = self.tracker.evaluate(
            entry, level, pattern_key, note=note, custom_threshold=custom_threshold
        )
        if flagged:
            line = (f"[precog] ALERT ({level.name}): {entry.source} — "
                     f"{entry.raw[:80]}")
            category = self.LEVEL_TO_CATEGORY.get(level)
            if category:
                line = self.colors.colorize(line, category)
            print(line)

# ---------------------------------------------------------------------------
# Precog — top level orchestrator
# ---------------------------------------------------------------------------

class Precog:
    """
    Owns and coordinates all core components. Call run() to start
    monitoring; it blocks until interrupted.
    """

    def __init__(self):
        config_path = Path(__file__).parent / "config" / "precog.conf"
        thresholds = None
        base_keywords = None
        critical_keywords = None
        try:
            self.config = PrecogConfig(config_path)
            thresholds = thresholds_from_config(self.config)
            base_keywords = self.config.keywords["watched"]
            critical_keywords = set(self.config.keywords["critical"])
            print(f"[precog] Loaded config from {config_path}")
        except ConfigError as e:
            self.config = None
            print(f"[precog] WARNING: could not load {config_path}: {e}")
            print("[precog] Falling back to built-in defaults.")

        keyword_exclusions = (
            self.config.keyword_exclusions if self.config is not None else {}
        )
        self.baseline_config = BaselineConfig(
            power_mode=False,
            base_keywords=base_keywords,
            keyword_exclusions=keyword_exclusions,
        )
        self.baseline_store  = BaselineStore(config=self.baseline_config)
        self.collector        = BaselineCollector(self.baseline_store)
        self.rolling_log      = RollingLog()
        self.alert_tracker    = AlertTracker(
            thresholds=thresholds, data_dir=Path("/var/lib/precog")
        )
        noisy_keywords = (
            self.config.noisy_keywords if self.config is not None else {}
        )
        self.alert_bridge     = AlertBridge(
            self.alert_tracker,
            critical_keywords=critical_keywords,
            noisy_keywords=noisy_keywords,
        )
        self.watcher_manager  = WatcherManager(self.rolling_log)

        self.retention_cutoff_days = (
            self.config.retention["flagged_log_cutoff_days"]
            if self.config is not None
            else 7
        )

        # Startup archive pass — catches up immediately on a long-idle
        # system rather than waiting for the first periodic check.
        archived = self.alert_tracker.archive_old_entries(
            cutoff_days=self.retention_cutoff_days
        )
        if archived:
            print(f"[precog] Archived {archived} flagged entries older than "
                  f"{self.retention_cutoff_days} days on startup.")

        self._stop_event = False
        self._archive_stop_event = threading.Event()

    # --- Startup -------------------------------------------------------

    def first_run_check(self) -> None:
        """
        On first run (baseline never seeded), read journalctl history
        to seed the baseline immediately rather than starting cold.
        """
        if not self.baseline_store._seeded:
            seeder = JournalSeeder(self.collector, self.baseline_store)
            seeder.seed()

        if not self.baseline_store.notice_acknowledged:
            print("\n" + "=" * 60)
            print(self.baseline_store.first_run_notice())
            print("=" * 60 + "\n")
            self.baseline_store.acknowledge_notice()

    # --- Entry processing ------------------------------------------------

    def process_entry(self, entry: LogEntry) -> None:
        """
        Called for every new log entry.
        """
        raw_lower = entry.raw.lower()
        matched_keywords = [
            kw for kw in self.baseline_config.active_keywords
            if kw in raw_lower and not self.baseline_config.is_excluded(kw, raw_lower)
        ]

        self.collector.process(entry)

        for keyword in matched_keywords:
            self.alert_bridge.consider(entry, keyword)

    # --- Status reporting --------------------------------------------------

    def print_status(self) -> None:
        buffer_count = self.rolling_log.entry_count()
        confidence   = self.baseline_store.confidence_pct
        flagged      = self.alert_tracker.flagged_count()
        active       = "yes" if self.baseline_store.monitoring_active else "no"

        ts = time.strftime("%H:%M:%S")
        print(f"[precog] {ts} — buffer: {buffer_count} entries | "
              f"confidence: {confidence}% | monitoring active: {active} | "
              f"flagged: {flagged}")

    # --- Run loop ------------------------------------------------------

    def _start_archive_loop(self) -> None:
        """
        Background thread that re-runs the archive pass once a day
        while Precog is running, so retention doesn't depend solely
        on restarts to keep flagged.log lean.
        """
        def _loop():
            while not self._archive_stop_event.wait(timeout=86400):
                archived = self.alert_tracker.archive_old_entries(
                    cutoff_days=self.retention_cutoff_days
                )
                if archived:
                    print(f"[precog] Archived {archived} flagged entries "
                          f"older than {self.retention_cutoff_days} days.")

        t = threading.Thread(
            target=_loop,
            daemon=True,
            name="precog-archive-loop"
        )
        t.start()

    def run(self) -> None:
        print("[precog] Starting up...")

        self.first_run_check()

        self.watcher_manager.add_journal_watcher()
        if Path("/var/log/auth.log").exists():
            self.watcher_manager.add_file_watcher("/var/log/auth.log")

        self.watcher_manager.start_all()
        self.collector.start_flush_loop()
        self._start_archive_loop()

        print("[precog] Monitoring active — press Ctrl+C to stop.\n")

        def shutdown(sig, frame):
            print("\n[precog] Shutting down...")
            self._archive_stop_event.set()
            self.watcher_manager.stop_all()
            self.collector.stop()
            # Flagged alerts are intentionally left on disk across restarts —
            # no clear_flagged() call here.
            print(f"[precog] Final buffer size: {self.rolling_log.entry_count()} entries")
            print(f"[precog] Final confidence: {self.baseline_store.confidence_pct}%")
            print(f"[precog] Total flagged alerts: {self.alert_tracker.flagged_count()}")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        last_processed_count = 0
        try:
            while True:
                time.sleep(10)

                snapshot = self.rolling_log.snapshot()
                new_entries = snapshot[last_processed_count:]
                for entry in new_entries:
                    self.process_entry(entry)
                last_processed_count = len(snapshot)

                self.print_status()

        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# Standalone boot log viewer — does not touch baseline, alerts, or config.
# Just runs journalctl -b, colorizes it consistently with the rest of
# Precog's output, prints it, and exits. No monitoring is started.
# ---------------------------------------------------------------------------

def show_boot_log():
    import subprocess
    colors = ColorScheme()
    try:
        proc = subprocess.run(
            ["journalctl", "-b", "--no-pager"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("[precog] ERROR: journalctl not found.")
        sys.exit(1)

    if proc.returncode != 0:
        print(f"[precog] ERROR: journalctl exited with code {proc.returncode}")
        if proc.stderr:
            print(proc.stderr.strip())
        sys.exit(1)

    try:
        for line in proc.stdout.splitlines():
            print(colors.colorize(line, "base_system"))
    except BrokenPipeError:
        # Reader (e.g. `head`, `less`) closed the pipe early — not an
        # error from our side, just means the reader got what it needed.
        # Silence the noisy traceback and exit cleanly.
        sys.stderr.close()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--show-boot-log" in sys.argv:
        show_boot_log()
    else:
        precog = Precog()
        precog.run()
