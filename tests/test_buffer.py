"""
test_buffer.py — Precog Buffer Protection Tests

Tests the two buffer protection mechanisms in RollingLog:
  1. Count cap: buffer never exceeds MAX_ENTRIES
  2. Time window: entries older than ROLLING_WINDOW_HOURS are expired

Runs fast — no waiting, no live log sources. Uses synthetic entries
with artificially controlled timestamps to simulate aged entries
without actually waiting 48 hours.

Run from the project root:
  python3 tests/test_buffer.py
"""

import sys
import time
import tempfile
from pathlib import Path

# Allow import from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.rolling_log import RollingLog, LogEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_entry(age_hours: float = 0.0, source: str = "test") -> LogEntry:
    now_ns = time.time_ns()
    ts_ns = now_ns - int(age_hours * 3600 * 1_000_000_000)
    return LogEntry(
        timestamp_ns=ts_ns,
        source=source,
        raw=f"synthetic entry aged {age_hours:.1f} hours",
        priority=6,
    )


def make_rolling_log(window_hours: int = 48, max_entries: int = 100) -> RollingLog:
    tmp = Path(tempfile.mkdtemp())
    return RollingLog(
        window_hours=window_hours,
        max_entries=max_entries,
        log_file=tmp / "test_rolling.log",
        flush_interval=9999,
    )


def pass_fail(condition: bool, test_name: str, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{status}] {test_name}{detail_str}")
    return condition


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_count_cap():
    print("\nTest 1: Count cap")
    log = make_rolling_log(max_entries=100)
    cap = 100

    for i in range(cap):
        log.add(make_entry(age_hours=0))
    r1 = pass_fail(log.entry_count() == cap, "Buffer holds exactly max_entries", f"count={log.entry_count()}, cap={cap}")

    for i in range(50):
        log.add(make_entry(age_hours=0))
    r2 = pass_fail(log.entry_count() == cap, "Buffer does not exceed max_entries after overflow", f"count={log.entry_count()}, cap={cap}")

    for i in range(cap * 10):
        log.add(make_entry(age_hours=0))
    r3 = pass_fail(log.entry_count() == cap, "Buffer holds after 10x cap insertions", f"count={log.entry_count()}, cap={cap}")

    log.stop()
    return all([r1, r2, r3])


def test_time_window_expiry():
    print("\nTest 2: Time window expiry")
    log = make_rolling_log(window_hours=48, max_entries=10_000)

    for i in range(20):
        log.add(make_entry(age_hours=1))
    r1 = pass_fail(log.entry_count() == 20, "Fresh entries (1h old) are retained", f"count={log.entry_count()}, expected=20")

    for i in range(10):
        log.add(make_entry(age_hours=47.9))
    r2 = pass_fail(log.entry_count() == 30, "Entries just inside window boundary are retained", f"count={log.entry_count()}, expected=30")

    for i in range(20):
        log.add(make_entry(age_hours=48.1))
    r3 = pass_fail(log.entry_count() == 30, "Entries just outside window are expired on insert", f"count={log.entry_count()}, expected=30")

    for i in range(20):
        log.add(make_entry(age_hours=200))
    r4 = pass_fail(log.entry_count() == 30, "Very old entries (200h) are expired on insert", f"count={log.entry_count()}, expected=30")

    log.stop()
    return all([r1, r2, r3, r4])


def test_oldest_dropped_first():
    print("\nTest 3: Oldest entries dropped first at count cap")
    log = make_rolling_log(max_entries=10, window_hours=48)

    for age in range(10, 0, -1):
        log.add(make_entry(age_hours=float(age)))
    r1 = pass_fail(log.entry_count() == 10, "Buffer full at cap", f"count={log.entry_count()}")

    log.add(make_entry(age_hours=0.1))
    snapshot = log.snapshot()
    oldest_age_h = (time.time_ns() - min(e.timestamp_ns for e in snapshot)) / 3_600_000_000_000

    r2 = pass_fail(log.entry_count() == 10, "Count still at cap after adding one beyond cap", f"count={log.entry_count()}")
    r3 = pass_fail(oldest_age_h < 10.0, "Oldest remaining entry is younger than the evicted 10h entry", f"oldest remaining: {oldest_age_h:.1f}h")

    log.stop()
    return all([r1, r2, r3])


def test_mixed_aged_entries():
    print("\nTest 4: Mixed fresh and expired entries")
    log = make_rolling_log(window_hours=48, max_entries=10_000)

    fresh = 0
    for age in [1, 50, 2, 100, 47, 49, 3, 200, 48.1, 0.5]:
        log.add(make_entry(age_hours=age))
        if age < 48:
            fresh += 1

    r1 = pass_fail(log.entry_count() == fresh, "Only fresh entries survive mixed insert", f"count={log.entry_count()}, expected={fresh}")
    log.stop()
    return r1


def test_both_protections_together():
    print("\nTest 5: Count cap and time window active simultaneously")
    cap = 5
    log = make_rolling_log(window_hours=48, max_entries=cap)

    for i in range(3):
        log.add(make_entry(age_hours=50))
    r1 = pass_fail(log.entry_count() == 0, "Expired entries dropped, buffer empty", f"count={log.entry_count()}")

    for i in range(8):
        log.add(make_entry(age_hours=1))
    r2 = pass_fail(log.entry_count() == cap, "Fresh entries capped at max_entries", f"count={log.entry_count()}, cap={cap}")

    for i in range(5):
        log.add(make_entry(age_hours=50))
    r3 = pass_fail(log.entry_count() == cap, "Expired entries after cap hit don't change count", f"count={log.entry_count()}, cap={cap}")

    log.stop()
    return all([r1, r2, r3])


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("Precog — Buffer Protection Tests")
    print("=" * 55)

    results = [
        test_count_cap(),
        test_time_window_expiry(),
        test_oldest_dropped_first(),
        test_mixed_aged_entries(),
        test_both_protections_together(),
    ]

    total = len(results)
    passed = sum(results)
    failed = total - passed

    print("\n" + "=" * 55)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  —  {failed} FAILED  <<<")
    else:
        print("  —  all clear")
    print("=" * 55)

    sys.exit(0 if failed == 0 else 1)
