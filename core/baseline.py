"""
baseline.py — Precog Core Module
Baseline learning: catalogues what "normal" looks like for this system
so the anomaly detector has something meaningful to compare against.

Design principles:
- Storage first: compact statistical summaries, not raw data
- Useful immediately: seeds from journalctl history on first run, monitoring
  starts at once with a confidence indicator rather than a waiting period
- Two modes: standard (minimal footprint) and power (full keyword tracking)
- Honest about confidence: reports what it knows and how certain it is

The baseline stores per-source activity in 168 time buckets (7 days x 24
hours). Each bucket holds mean event count, standard deviation, and sample
count — three numbers, nothing more. Keyword tracking adds hit counts and
last-seen timestamps for a curated or user-defined keyword list.

Confidence grows from whatever the seeded history provides toward 100%
as direct observation accumulates. The tool is useful from minute one.
"""

import json
import math
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rolling_log import LogEntry


# ---------------------------------------------------------------------------
# Default keyword watchlist (standard mode)
# Drawn directly from the detection targets list.
# Focused on the most common precursor signals — not exhaustive.
#
# NOTE: These are starting-point defaults, not fixed truth. Real-world
# use will surface false positives specific to your own system (e.g.
# NetworkManager DHCP chatter routinely matching "failed"/"timeout" —
# confirmed via live testing). Once enough flagged.log history has
# accumulated, prune or weight these per your actual environment rather
# than treating this list as authoritative. See keyword weighting item
# in project context notes — intentionally deferred until real data
# exists to tune against.
# ---------------------------------------------------------------------------

DEFAULT_KEYWORDS = [
    "buffer overrun",
    "buffer overflow",
    "disk full",
    "no space left",
    "oom-kill",
    "killed process",
    "out of memory",
    "killed process",
    "authentication failure",
    "failed password",
    "segfault",
    "segmentation fault",
    "i/o error",
    "error",
    "errno",
    "timeout",
    "corrupted",
    "error",
    "critical",
    "failed",
]

# Boot journal specific keywords — always watched regardless of mode
BOOT_KEYWORDS = [
    "failed to start",
    "dependency failed",
    "timed out",
    "start limit hit",
]


# ---------------------------------------------------------------------------
# Confidence calculation constants
# ---------------------------------------------------------------------------

MIN_DAYS_FOR_FULL_CONFIDENCE = 14
SEED_CONFIDENCE_WEIGHT = 0.40
DIRECT_CONFIDENCE_WEIGHT = 0.60
MONITORING_ACTIVE_THRESHOLD = 0.10
# ---------------------------------------------------------------------------
# TimeBucket — compact stats for one hour-of-day / day-of-week slot
# ---------------------------------------------------------------------------

class TimeBucket:
    """
    Compact statistical summary for a single time slot.

    Tracks event frequency using Welford's online algorithm for computing
    mean and variance in a single pass without storing raw values.
    This keeps memory and storage minimal regardless of sample count.

    slot_key: "dow_hour" e.g. "1_09" = Monday 9am
    """

    __slots__ = ["count", "mean", "M2"]

    def __init__(self):
        self.count = 0
        self.mean  = 0.0
        self.M2    = 0.0

    def update(self, events_this_hour: int) -> None:
        """
        Add one hourly observation using Welford's online algorithm.
        Safe to call once per hour per source per bucket.
        """
        self.count += 1
        delta = events_this_hour - self.mean
        self.mean += delta / self.count
        delta2 = events_this_hour - self.mean
        self.M2 += delta * delta2

    @property
    def stddev(self) -> float:
        if self.count < 2:
            return 0.0
        return math.sqrt(self.M2 / (self.count - 1))

    def is_anomalous(self, events_this_hour: int, sensitivity: float = 2.0) -> bool:
        """
        Returns True if events_this_hour is more than sensitivity standard
        deviations above the mean. Requires at least 7 samples before
        making any judgment.
        """
        if self.count < 7:
            return False
        threshold = self.mean + (sensitivity * self.stddev)
        return events_this_hour > threshold

    def to_dict(self) -> dict:
        return {"count": self.count, "mean": self.mean, "M2": self.M2}

    @staticmethod
    def from_dict(d: dict) -> "TimeBucket":
        b = TimeBucket()
        b.count = d["count"]
        b.mean  = d["mean"]
        b.M2    = d["M2"]
        return b


# ---------------------------------------------------------------------------
# KeywordStat — tracks frequency of a single keyword in a single source
# ---------------------------------------------------------------------------

class KeywordStat:
    """
    Tracks how often a keyword appears in a specific log source.
    Stores total hit count and the timestamp of the most recent hit.
    Storage: two numbers per keyword per source — minimal footprint.
    """

    __slots__ = ["hits", "last_seen_ns"]

    def __init__(self):
        self.hits         = 0
        self.last_seen_ns = 0

    def record_hit(self, timestamp_ns: int) -> None:
        self.hits += 1
        self.last_seen_ns = max(self.last_seen_ns, timestamp_ns)

    def to_dict(self) -> dict:
        return {"hits": self.hits, "last_seen_ns": self.last_seen_ns}

    @staticmethod
    def from_dict(d: dict) -> "KeywordStat":
        ks = KeywordStat()
        ks.hits          = d["hits"]
        ks.last_seen_ns  = d["last_seen_ns"]
        return ks


# ---------------------------------------------------------------------------
# BaselineConfig — mode and keyword list
# ---------------------------------------------------------------------------

class BaselineConfig:
    """
    Configuration for the baseline module.
    Eventually loaded from precog.conf — hardcoded defaults for now.

    power_mode: if True, tracks user_keywords in addition to defaults.
    user_keywords: additional keywords for power mode users.
    sensitivity: standard deviations above mean to trigger anomaly flag.
    """

    def __init__(
        self,
        power_mode: bool = False,
        user_keywords: list[str] = None,
        sensitivity: float = 2.0,
        base_keywords: list[str] = None,
    ):
        self.power_mode    = power_mode
        self.user_keywords = [k.lower() for k in (user_keywords or [])]
        self.sensitivity   = sensitivity
        self.base_keywords = (
            [k.lower() for k in base_keywords]
            if base_keywords is not None
            else list(DEFAULT_KEYWORDS)
        )

    @property
    def active_keywords(self) -> list[str]:
        keywords = list(self.base_keywords) + list(BOOT_KEYWORDS)
        if self.power_mode and self.user_keywords:
            keywords += self.user_keywords
        return list(dict.fromkeys(keywords))

    def storage_estimate(self, source_count: int) -> str:
        buckets_per_source = 168
        bytes_per_bucket   = 60
        keyword_count      = len(self.active_keywords)
        bytes_per_keyword  = 50

        total = (source_count * buckets_per_source * bytes_per_bucket +
                 source_count * keyword_count * bytes_per_keyword)

        if total < 1024:
            return f"{total}B"
        elif total < 1024 * 1024:
            return f"{total // 1024}KB"
        else:
            return f"{total / (1024 * 1024):.1f}MB"
# ---------------------------------------------------------------------------
# BaselineStore — owns all baseline data and persistence
# ---------------------------------------------------------------------------

class BaselineStore:
    """
    Central store for all baseline data.

    Owns:
    - Time buckets: {source: {slot_key: TimeBucket}}
    - Keyword stats: {source: {keyword: KeywordStat}}
    - Confidence metadata: first_seen_ns, direct_observation_days, seed_days

    Persists to /var/lib/precog/baseline.json via atomic write.
    Reports confidence and storage estimates on demand.
    Thread safe — all public methods acquire self._lock.
    """

    def __init__(self, data_dir: Path = None, config: BaselineConfig = None):
        self.data_dir      = data_dir or Path("/var/lib/precog")
        self.config        = config or BaselineConfig()
        self.baseline_file = self.data_dir / "baseline.json"
        self._lock         = threading.Lock()

        self._buckets:  dict[str, dict[str, TimeBucket]]  = {}
        self._keywords: dict[str, dict[str, KeywordStat]] = {}

        self._first_seen_ns:           int   = 0
        self._direct_observation_days: float = 0.0
        self._seed_days:               float = 0.0
        self._seeded:                  bool  = False
        self._notice_acknowledged:     bool  = False

        # Explicit chmod here (not just relying on install.py) so
        # permissions are correct even if precog.py is ever run
        # directly on a fresh system without install.py having run
        # first — baseline.json can reflect real keyword-match
        # activity and shouldn't be readable by every user.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self._load()
        if self.baseline_file.exists():
            self.baseline_file.chmod(0o600)

    # --- Confidence --------------------------------------------------------

    @property
    def confidence(self) -> float:
        seed_contrib   = min(self._seed_days / MIN_DAYS_FOR_FULL_CONFIDENCE, 1.0)
        direct_contrib = min(
            self._direct_observation_days / MIN_DAYS_FOR_FULL_CONFIDENCE, 1.0
        )
        return (seed_contrib * SEED_CONFIDENCE_WEIGHT +
                direct_contrib * DIRECT_CONFIDENCE_WEIGHT)

    @property
    def confidence_pct(self) -> int:
        return int(self.confidence * 100)

    @property
    def monitoring_active(self) -> bool:
        return self.confidence >= MONITORING_ACTIVE_THRESHOLD

    @property
    def notice_acknowledged(self) -> bool:
        return self._notice_acknowledged

    def acknowledge_notice(self) -> None:
        with self._lock:
            self._notice_acknowledged = True
            self._save()

    def first_run_notice(self) -> str:
        seed_days = int(self._seed_days)
        conf = self.confidence_pct
        return (
            f"Precog has seeded its baseline from {seed_days} days of existing "
            f"logs (confidence: {conf}%). Monitoring is active now. Prediction "
            f"confidence will improve over the next 7-14 days as Precog observes "
            f"your system directly. This message will not appear again."
        )

    # --- Data update -------------------------------------------------------

    def record_hourly_bucket(
        self, source: str, slot_key: str, event_count: int
    ) -> None:
        with self._lock:
            if source not in self._buckets:
                self._buckets[source] = {}
            if slot_key not in self._buckets[source]:
                self._buckets[source][slot_key] = TimeBucket()
            self._buckets[source][slot_key].update(event_count)

    def record_keyword_hit(
        self, source: str, keyword: str, timestamp_ns: int
    ) -> None:
        with self._lock:
            if source not in self._keywords:
                self._keywords[source] = {}
            if keyword not in self._keywords[source]:
                self._keywords[source][keyword] = KeywordStat()
            self._keywords[source][keyword].record_hit(timestamp_ns)

            if self._first_seen_ns == 0 or timestamp_ns < self._first_seen_ns:
                self._first_seen_ns = timestamp_ns

    def update_direct_observation(self, days: float) -> None:
        with self._lock:
            self._direct_observation_days = days
            self._save()

    def set_seed_days(self, days: float) -> None:
        with self._lock:
            self._seed_days = days
            self._seeded = True
            self._save()

    # --- Query -------------------------------------------------------------

    def is_anomalous(
        self, source: str, slot_key: str, event_count: int
    ) -> bool:
        if not self.monitoring_active:
            return False
        with self._lock:
            bucket = self._buckets.get(source, {}).get(slot_key)
            if bucket is None:
                return False
            return bucket.is_anomalous(event_count, self.config.sensitivity)

    def keyword_hits(self, source: str, keyword: str) -> int:
        with self._lock:
            return self._keywords.get(source, {}).get(
                keyword, KeywordStat()
            ).hits

    def sources(self) -> list[str]:
        with self._lock:
            return list(set(list(self._buckets.keys()) + list(self._keywords.keys())))

    def storage_size(self) -> str:
        try:
            size = self.baseline_file.stat().st_size
            if size < 1024:
                return f"{size}B"
            elif size < 1024 * 1024:
                return f"{size // 1024}KB"
            else:
                return f"{size / (1024*1024):.1f}MB"
        except FileNotFoundError:
            return "0B"

    # --- Persistence -------------------------------------------------------

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        tmp = self.baseline_file.with_suffix(".tmp")
        try:
            data = {
                "meta": {
                    "first_seen_ns":           self._first_seen_ns,
                    "direct_observation_days": self._direct_observation_days,
                    "seed_days":               self._seed_days,
                    "seeded":                  self._seeded,
                    "notice_acknowledged":     self._notice_acknowledged,
                    "power_mode":              self.config.power_mode,
                },
                "buckets": {
                    source: {
                        slot: bucket.to_dict()
                        for slot, bucket in slots.items()
                    }
                    for source, slots in self._buckets.items()
                },
                "keywords": {
                    source: {
                        kw: stat.to_dict()
                        for kw, stat in kws.items()
                    }
                    for source, kws in self._keywords.items()
                },
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            tmp.rename(self.baseline_file)
            # Enforce permissions on every save, not just at __init__ —
            # rename() doesn't guarantee the umask-derived mode of the
            # temp file matches what we want long-term.
            self.baseline_file.chmod(0o600)
        except OSError as e:
            print(f"[precog] WARNING: could not save baseline: {e}")

    def _load(self) -> None:
        if not self.baseline_file.exists():
            return
        if self.baseline_file.stat().st_size == 0:
            return
        try:

            with open(self.baseline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("meta", {})
            self._first_seen_ns           = meta.get("first_seen_ns", 0)
            self._direct_observation_days = meta.get("direct_observation_days", 0.0)
            self._seed_days               = meta.get("seed_days", 0.0)
            self._seeded                  = meta.get("seeded", False)
            self._notice_acknowledged     = meta.get("notice_acknowledged", False)

            for source, slots in data.get("buckets", {}).items():
                self._buckets[source] = {
                    slot: TimeBucket.from_dict(b)
                    for slot, b in slots.items()
                }

            for source, kws in data.get("keywords", {}).items():
                self._keywords[source] = {
                    kw: KeywordStat.from_dict(s)
                    for kw, s in kws.items()
                }

            print(f"[precog] Baseline loaded — confidence: "
                  f"{self.confidence_pct}%, size: {self.storage_size()}")

        except (OSError, json.JSONDecodeError, KeyError) as e:
            print(f"[precog] WARNING: could not load baseline: {e}")
# ---------------------------------------------------------------------------
# BaselineCollector — ingests LogEntry objects, updates the store
# ---------------------------------------------------------------------------

class BaselineCollector:
    """
    Processes LogEntry objects and updates the BaselineStore.

    Two responsibilities:
    1. Keyword scanning — checks each entry's raw text against the active
       keyword list and records hits immediately.
    2. Hourly bucket updates — counts events per source per hour and
       flushes to the store at the end of each hour.

    Call process(entry) for each new log entry.
    Call start_flush_loop() to have hourly flush happen automatically.
    """

    def __init__(self, store: BaselineStore):
        self.store    = store
        self._lock    = threading.Lock()
        self._counts: dict[str, int] = {}
        self._current_hour_slot: str = ""
        self._stop_event = threading.Event()
        self._start_ns   = time.time_ns()

    def process(self, entry: LogEntry) -> None:
        """
        Process one log entry. Updates keyword stats and hourly counts.
        Thread safe — safe to call from multiple watcher threads.
        """
        dt = datetime.fromtimestamp(
            entry.timestamp_ns / 1_000_000_000, tz=timezone.utc
        )
        slot_key  = f"{dt.weekday()}_{dt.hour:02d}"
        raw_lower = entry.raw.lower()

        # Keyword scan
        for keyword in self.store.config.active_keywords:
            if keyword in raw_lower:
                self.store.record_keyword_hit(
                    entry.source, keyword, entry.timestamp_ns
                )

        # Hourly count
        with self._lock:
            if slot_key != self._current_hour_slot:
                if self._current_hour_slot and self._counts:
                    self._flush_counts(self._current_hour_slot)
                self._current_hour_slot = slot_key
                self._counts = {}

            self._counts[entry.source] = (
                self._counts.get(entry.source, 0) + 1
            )

    def flush(self) -> None:
        """Flush current hourly counts to the store."""
        with self._lock:
            if self._current_hour_slot and self._counts:
                self._flush_counts(self._current_hour_slot)
                self._counts = {}

        elapsed_days = (
            (time.time_ns() - self._start_ns) / 1_000_000_000 / 86400
        )
        self.store.update_direct_observation(elapsed_days)

    def _flush_counts(self, slot_key: str) -> None:
        for source, count in self._counts.items():
            self.store.record_hourly_bucket(source, slot_key, count)

    def start_flush_loop(self) -> None:
        """Start background thread that flushes and saves every hour."""
        t = threading.Thread(
            target=self._flush_loop,
            daemon=True,
            name="precog-baseline-flush"
        )
        t.start()

    def _flush_loop(self) -> None:
        while not self._stop_event.wait(timeout=3600):
            self.flush()
            self.store.save()

    def stop(self) -> None:
        self._stop_event.set()
        self.flush()
        self.store.save()
# ---------------------------------------------------------------------------
# JournalSeeder — reads journalctl history to seed baseline on first run
# ---------------------------------------------------------------------------

class JournalSeeder:
    """
    Reads all available journalctl history and feeds it through a
    BaselineCollector to seed the baseline on first run.

    Runs synchronously — call seed() and wait for it to complete before
    starting live monitoring. On a system with several weeks of journal
    history this may take 10-30 seconds.
    """

    def __init__(self, collector: BaselineCollector, store: BaselineStore):
        self.collector = collector
        self.store     = store

    def seed(self) -> float:
        """
        Read all available journal history and feed it through the collector.
        Returns the number of days of history found.
        """
        print("[precog] Seeding baseline from journal history...")

        cmd = ["journalctl", "--output=json", "--no-pager"]
        earliest_ns = None
        entry_count = 0

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("[precog] WARNING: journalctl not found, skipping seed.")
            return 0.0

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_us_str = record.get("__REALTIME_TIMESTAMP", "")
                try:
                    ts_ns = int(ts_us_str) * 1000
                except (ValueError, TypeError):
                    continue

                if earliest_ns is None or ts_ns < earliest_ns:
                    earliest_ns = ts_ns

                unit = record.get(
                    "_SYSTEMD_UNIT",
                    record.get("SYSLOG_IDENTIFIER", "unknown")
                )
                message      = record.get("MESSAGE", "")
                priority_str = record.get("PRIORITY", "")
                try:
                    priority = int(priority_str)
                except (ValueError, TypeError):
                    priority = None

                entry = LogEntry(
                    timestamp_ns=ts_ns,
                    source="journalctl",
                    raw=f"{unit}: {message}",
                    priority=priority,
                )
                self.collector.process(entry)
                entry_count += 1

                if entry_count % 10000 == 0:
                    print(f"[precog] Seeding... {entry_count} entries processed")

        finally:
            proc.terminate()

        if earliest_ns is not None:
            seed_days = (time.time_ns() - earliest_ns) / 1_000_000_000 / 86400
        else:
            seed_days = 0.0

        self.collector.flush()
        self.store.set_seed_days(seed_days)
        self.store.save()

        print(f"[precog] Seeding complete — {entry_count} entries, "
              f"{seed_days:.1f} days of history, "
              f"confidence: {self.store.confidence_pct}%")

        return seed_days

