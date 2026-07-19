"""
rolling_log.py — Precog Core Module
Rolling log foundation: ingests log events from multiple sources,
maintains a 48-hour rolling window, enforces an entry count cap to
prevent buffer overrun, and persists the buffer to disk continuously.

This module is source-agnostic. It does not care where log entries
come from — journalctl, flat files, or future plugins all feed in
through the same interface.

All entries carry their native timestamp at source precision.
The rolling log does not impose or normalize timestamp resolution.
"""

import threading
import time
import json
import subprocess
import os
import select
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration defaults
# These can be overridden by precog.conf once config loading is implemented.
# ---------------------------------------------------------------------------

ROLLING_WINDOW_HOURS = 48          # How far back the rolling log reaches
MAX_ENTRIES = 100_000              # Hard cap — oldest entries drop if hit
DATA_DIR = Path("/var/lib/precog")
ROLLING_LOG_FILE = DATA_DIR / "rolling.log"
FLUSH_INTERVAL_SECONDS = 30        # How often the buffer is flushed to disk

# Log sources watched by default (Layer 1 core defaults).
# journalctl is the primary source. Flat files are secondary.
# Layer 2 (distro profile) and Layer 3 (DE addon) sources are added
# during first run setup — not hardcoded here.
DEFAULT_SOURCES = [
    {"type": "journalctl", "args": []},          # Full systemd journal
    {"type": "file", "path": "/var/log/auth.log"},
]


# ---------------------------------------------------------------------------
# LogEntry — the atomic unit of data in Precog
# ---------------------------------------------------------------------------

@dataclass
class LogEntry:
    """
    A single log event from any source.

    timestamp_ns: nanoseconds since epoch. Stored at native source
    precision — journalctl provides microseconds, flat files provide
    seconds. The rolling log stores whatever it receives and does not
    normalize. Display layer handles formatting.

    source: human readable label for the originating log source.
    priority: syslog priority integer (0=emerg, 7=debug). None if unknown.
    raw: the original unmodified log line exactly as received.
    """
    timestamp_ns: int       # nanoseconds since epoch (native source precision)
    source: str             # e.g. "journalctl", "auth.log"
    raw: str                # original log line, unmodified
    priority: Optional[int] = None   # syslog priority if available


def entry_to_dict(entry: LogEntry) -> dict:
    return asdict(entry)


def entry_from_dict(d: dict) -> LogEntry:
    return LogEntry(**d)


# ---------------------------------------------------------------------------
# RollingLog — the core buffer
# ---------------------------------------------------------------------------

class RollingLog:
    """
    Thread-safe rolling log buffer.

    Maintains a deque of LogEntry objects bounded by:
      - Time: entries older than ROLLING_WINDOW_HOURS are expired
      - Count: if MAX_ENTRIES is reached, oldest entry is dropped

    Both bounds are enforced on every insert. The buffer cannot overrun.

    Source-agnostic: accepts entries from any watcher without modification.
    """

    def __init__(
        self,
        window_hours: int = ROLLING_WINDOW_HOURS,
        max_entries: int = MAX_ENTRIES,
        log_file: Path = ROLLING_LOG_FILE,
        flush_interval: int = FLUSH_INTERVAL_SECONDS,
    ):
        self.window_hours = window_hours
        self.max_entries = max_entries
        self.log_file = log_file
        self.flush_interval = flush_interval

        self._buffer: deque[LogEntry] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # Ensure data directory exists
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing buffer from disk if present (survives restarts)
        self._load_from_disk()

        # Start background flush thread
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="precog-flush"
        )
        self._flush_thread.start()

    # --- Public interface ---------------------------------------------------

    def add(self, entry: LogEntry) -> None:
        """
        Add a log entry to the rolling buffer.
        Enforces both the time window and the count cap on every insert.
        This is the single entry point for all log sources and plugins.
        """
        now_ns = time.time_ns()
        cutoff_ns = now_ns - (self.window_hours * 3600 * 1_000_000_000)

        # Reject expired entries before they enter the buffer
        if entry.timestamp_ns < cutoff_ns:
            return

        with self._lock:
            # Count cap: if full, drop oldest before adding
            if len(self._buffer) >= self.max_entries:
                self._buffer.popleft()

            self._buffer.append(entry)

            # Time window: expire entries older than the rolling window.
            # Entries are appended in order so expiry is always from the left.
            while self._buffer and self._buffer[0].timestamp_ns < cutoff_ns:
                self._buffer.popleft()

    def snapshot(self) -> list[LogEntry]:
        """
        Return a copy of the current buffer contents.
        Safe to call from any thread. Does not modify the buffer.
        """
        with self._lock:
            return list(self._buffer)

    def entry_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def stop(self) -> None:
        """
        Signal the flush thread to stop and perform a final flush to disk.
        Call this on clean shutdown.
        """
        self._stop_event.set()
        self._flush_thread.join(timeout=10)
        self._flush_to_disk()

    # --- Persistence -------------------------------------------------------

    def _flush_to_disk(self) -> None:
        """
        Write the current buffer to disk as newline-delimited JSON.
        Each line is one LogEntry serialised to JSON.
        Writes to a temp file then renames atomically to avoid corruption.
        """
        tmp = self.log_file.with_suffix(".tmp")
        try:
            with self._lock:
                entries = list(self._buffer)

            with open(tmp, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry_to_dict(entry)) + "\n")

            tmp.rename(self.log_file)

        except OSError as e:
            print(f"[precog] WARNING: could not flush rolling log to disk: {e}")

    def _load_from_disk(self) -> None:
        """
        Load a previously persisted rolling log from disk on startup.
        Expired entries are discarded during load — the window is enforced
        at load time just as it is on insert.
        """
        if not self.log_file.exists():
            return

        now_ns = time.time_ns()
        cutoff_ns = now_ns - (self.window_hours * 3600 * 1_000_000_000)
        loaded = 0
        skipped = 0

        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = entry_from_dict(json.loads(line))
                        if entry.timestamp_ns >= cutoff_ns:
                            self._buffer.append(entry)
                            loaded += 1
                        else:
                            skipped += 1
                    except (json.JSONDecodeError, TypeError, KeyError):
                        # Malformed line — skip silently
                        skipped += 1

            print(f"[precog] Loaded {loaded} entries from disk "
                  f"({skipped} expired or malformed, discarded).")

        except OSError as e:
            print(f"[precog] WARNING: could not load rolling log from disk: {e}")

    def _flush_loop(self) -> None:
        """
        Background thread: flush buffer to disk every FLUSH_INTERVAL_SECONDS.
        """
        while not self._stop_event.wait(timeout=self.flush_interval):
            self._flush_to_disk()


# ---------------------------------------------------------------------------
# LogWatcher — reads from log sources and feeds into RollingLog
# ---------------------------------------------------------------------------

class JournalWatcher:
    """
    Watches the systemd journal via journalctl and feeds entries into
    a RollingLog. Runs in its own thread.

    Uses journalctl's JSON output format for structured field access.
    Requests microsecond-precision timestamps (__REALTIME_TIMESTAMP).
    """

    def __init__(
        self,
        rolling_log: RollingLog,
        extra_args: list[str] = None,
        boot_hook=None,
    ):
        self.rolling_log = rolling_log
        self.extra_args = extra_args or []
        # Optional callback: called with (record: dict, ts_ns: int) for
        # every entry seen. Lets a caller (precog.py) do boot-window
        # detection without rolling_log.py needing to know anything
        # about BaselineStore. None means "no boot detection wired up."
        self.boot_hook = boot_hook
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            daemon=True,
            name="precog-journal"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _watch(self) -> None:
        """
        Tail the journal continuously using journalctl -f --output=json.
        Parses each JSON line into a LogEntry and adds it to the rolling log.

        journalctl __REALTIME_TIMESTAMP is microseconds since epoch.
        Stored as nanoseconds (multiply by 1000) for uniform internal precision.
        """
        cmd = ["journalctl", "-f", "-n", "0", "--output=json"] + self.extra_args

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("[precog] ERROR: journalctl not found. "
                  "Is this a systemd system?")
            return

        print("[precog] JournalWatcher started.")

        try:
            while not self._stop_event.is_set():
                # Use select to avoid blocking indefinitely on readline
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if not ready:
                    continue

                line = proc.stdout.readline()
                if not line:
                    # journalctl exited unexpectedly
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # __REALTIME_TIMESTAMP is microseconds since epoch as a string
                ts_us_str = record.get("__REALTIME_TIMESTAMP", "")
                try:
                    ts_ns = int(ts_us_str) * 1000   # microseconds → nanoseconds
                except (ValueError, TypeError):
                    ts_ns = time.time_ns()           # fallback to now

                if self.boot_hook is not None:
                    self.boot_hook(record, ts_ns)

                priority_str = record.get("PRIORITY", "")
                try:
                    priority = int(priority_str)
                except (ValueError, TypeError):
                    priority = None

                # Reconstruct a readable raw line from the journal record
                unit = record.get("_SYSTEMD_UNIT", record.get("SYSLOG_IDENTIFIER", "unknown"))
                message = record.get("MESSAGE", "")
                raw = f"{unit}: {message}"

                entry = LogEntry(
                    timestamp_ns=ts_ns,
                    source="journalctl",
                    raw=raw,
                    priority=priority,
                )
                self.rolling_log.add(entry)

        finally:
            proc.terminate()
            print("[precog] JournalWatcher stopped.")


class FileWatcher:
    """
    Tails a flat log file and feeds new lines into a RollingLog.
    Runs in its own thread.

    Handles log rotation: if the file shrinks (inode replaced), reopens it.
    Timestamps from flat files are seconds precision at best. Stored as
    nanoseconds for uniform internal representation.
    """

    def __init__(self, rolling_log: RollingLog, path: str):
        self.rolling_log = rolling_log
        self.path = Path(path)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            daemon=True,
            name=f"precog-file-{self.path.name}"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _watch(self) -> None:
        """
        Tail a flat log file. Seeks to end on open so only new lines
        are captured (we don't want to flood the buffer with history
        on startup — journalctl history seeding is handled separately).

        Reopens the file if it shrinks, to handle log rotation.
        """
        if not self.path.exists():
            print(f"[precog] WARNING: log file not found, skipping: {self.path}")
            return

        print(f"[precog] FileWatcher started: {self.path}")

        try:
            f = open(self.path, "r", encoding="utf-8", errors="replace")
            f.seek(0, 2)    # Seek to end — tail only, no history flood
            inode = os.stat(self.path).st_ino

            while not self._stop_event.is_set():
                line = f.readline()

                if line:
                    raw = line.rstrip("\n")
                    entry = LogEntry(
                        timestamp_ns=time.time_ns(),    # flat files: use now
                        source=self.path.name,
                        raw=raw,
                        priority=None,
                    )
                    self.rolling_log.add(entry)
                else:
                    # No new line — check for rotation
                    try:
                        current_inode = os.stat(self.path).st_ino
                    except FileNotFoundError:
                        current_inode = None

                    if current_inode != inode:
                        # File was rotated — reopen
                        f.close()
                        time.sleep(0.5)
                        f = open(self.path, "r", encoding="utf-8", errors="replace")
                        inode = os.stat(self.path).st_ino
                        print(f"[precog] FileWatcher: reopened after rotation: {self.path}")
                    else:
                        time.sleep(0.5)

        except OSError as e:
            print(f"[precog] ERROR watching {self.path}: {e}")
        finally:
            try:
                f.close()
            except Exception:
                pass
            print(f"[precog] FileWatcher stopped: {self.path}")


# ---------------------------------------------------------------------------
# WatcherManager — starts and stops all watchers together
# ---------------------------------------------------------------------------

class WatcherManager:
    """
    Owns all active watchers and coordinates startup and shutdown.
    This is the plugin seam: future plugins register their own watchers
    here without touching core logic.
    """

    def __init__(self, rolling_log: RollingLog):
        self.rolling_log = rolling_log
        self._watchers = []

    def add_journal_watcher(self, extra_args: list[str] = None, boot_hook=None) -> None:
        self._watchers.append(JournalWatcher(self.rolling_log, extra_args, boot_hook=boot_hook))

    def add_file_watcher(self, path: str) -> None:
        self._watchers.append(FileWatcher(self.rolling_log, path))

    def start_all(self) -> None:
        for w in self._watchers:
            w.start()

    def stop_all(self) -> None:
        for w in self._watchers:
            w.stop()
        self.rolling_log.stop()


# ---------------------------------------------------------------------------
# Entry point — basic smoke test / live demo
# Runs if you execute this file directly: python3 rolling_log.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import signal

    print("[precog] Starting rolling log foundation — press Ctrl+C to stop.\n")

    rolling_log = RollingLog()
    manager = WatcherManager(rolling_log)

    # Wire up default sources (Layer 1)
    manager.add_journal_watcher()
    if Path("/var/log/auth.log").exists():
        manager.add_file_watcher("/var/log/auth.log")

    manager.start_all()

    def shutdown(sig, frame):
        print("\n[precog] Shutting down...")
        manager.stop_all()
        count = rolling_log.entry_count()
        print(f"[precog] Final buffer size: {count} entries.")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Status ticker — shows buffer size every 10 seconds
    try:
        while True:
            time.sleep(10)
            count = rolling_log.entry_count()
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[precog] {ts} UTC — buffer: {count} entries")
    except SystemExit:
        pass
