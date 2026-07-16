"""
test_alert.py — Precog Alert System Tests

Tests the alert flagging infrastructure in alert.py:
  1. TRIAGE fires immediately on first hit
  2. PATTERN_WATCH requires 3 of 5 within window
  3. CORRELATION requires 2 of 3 within window
  4. Hits outside the time window don't count toward threshold
  5. Window resets after threshold fires — no immediate re-fire
  6. get_flagged() filters correctly by tier
  7. clear_flagged() removes entries correctly
  8. Persistence — flagged entries survive a tracker restart

Run from project root:
  python3 tests/test_alert.py
"""

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from core.alert import (
    AlertLevel, AlertThreshold, AlertTracker, FlaggedEntry, DEFAULT_THRESHOLDS
)
from core.rolling_log import LogEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_entry(age_seconds: float = 0.0, source: str = "test") -> LogEntry:
    now_ns = time.time_ns()
    ts_ns = now_ns - int(age_seconds * 1_000_000_000)
    return LogEntry(
        timestamp_ns=ts_ns,
        source=source,
        raw=f"synthetic entry aged {age_seconds:.1f}s",
        priority=3,
    )


def make_tracker(
    pattern_count: int = 3,
    pattern_samples: int = 5,
    pattern_window: int = 600,
    corr_count: int = 2,
    corr_samples: int = 3,
    corr_window: int = 300,
) -> AlertTracker:
    """Create a tracker with a temp data dir and configurable thresholds."""
    tmp = Path(tempfile.mkdtemp())
    thresholds = {
        AlertLevel.PATTERN_WATCH: AlertThreshold(
            required_count=pattern_count,
            total_samples=pattern_samples,
            window_seconds=pattern_window,
        ),
        AlertLevel.CORRELATION: AlertThreshold(
            required_count=corr_count,
            total_samples=corr_samples,
            window_seconds=corr_window,
        ),
        AlertLevel.TRIAGE: AlertThreshold(
            required_count=1,
            total_samples=1,
            window_seconds=0,
        ),
    }
    return AlertTracker(thresholds=thresholds, data_dir=tmp)


def pass_fail(condition: bool, test_name: str, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{status}] {test_name}{detail_str}")
    return condition


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_triage_fires_immediately():
    """TRIAGE should flag on the very first hit, no window needed."""
    print("\nTest 1: TRIAGE fires immediately")
    tracker = make_tracker()

    entry = make_entry()
    result = tracker.evaluate(entry, AlertLevel.TRIAGE, "test:critical")

    r1 = pass_fail(result is not None, "First hit returns a FlaggedEntry")
    r2 = pass_fail(
        result is not None and result.level == AlertLevel.TRIAGE,
        "FlaggedEntry has correct tier",
        f"level={result.level if result else None}"
    )
    r3 = pass_fail(
        tracker.flagged_count() == 1,
        "Tracker records one flagged entry",
        f"count={tracker.flagged_count()}"
    )
    return all([r1, r2, r3])


def test_pattern_watch_threshold():
    """PATTERN_WATCH should not fire until required_count hits within window."""
    print("\nTest 2: PATTERN_WATCH threshold (3 of 5)")
    tracker = make_tracker(pattern_count=3, pattern_samples=5, pattern_window=600)
    key = "journalctl:buffer_overrun"

    # First two hits — should not fire
    for i in range(2):
        result = tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, key)
        if result is not None:
            pass_fail(False, f"Hit {i+1} should not fire yet", "fired early")
            return False

    r1 = pass_fail(tracker.flagged_count() == 0, "No flag after 2 hits")

    # Third hit — should fire
    result = tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, key)
    r2 = pass_fail(result is not None, "Third hit fires the threshold")
    r3 = pass_fail(
        tracker.flagged_count() == 1,
        "One flagged entry recorded",
        f"count={tracker.flagged_count()}"
    )
    return all([r1, r2, r3])


def test_correlation_threshold():
    """CORRELATION should not fire until required_count hits within window."""
    print("\nTest 3: CORRELATION threshold (2 of 3)")
    tracker = make_tracker(corr_count=2, corr_samples=3, corr_window=300)
    key = "multi:disk+kernel"

    # First hit — should not fire
    result = tracker.evaluate(make_entry(), AlertLevel.CORRELATION, key)
    r1 = pass_fail(result is None, "First hit does not fire")

    # Second hit — should fire
    result = tracker.evaluate(make_entry(), AlertLevel.CORRELATION, key)
    r2 = pass_fail(result is not None, "Second hit fires the threshold")
    r3 = pass_fail(
        result is not None and result.level == AlertLevel.CORRELATION,
        "FlaggedEntry has correct tier"
    )
    return all([r1, r2, r3])


def test_hits_outside_window_dont_count():
    """Hits older than window_seconds should not count toward the threshold."""
    print("\nTest 4: Hits outside time window don't count")

    # Use a very short window — 5 seconds
    tracker = make_tracker(pattern_count=3, pattern_samples=5, pattern_window=5)
    key = "test:aged_hits"

    # Two hits aged 10 seconds — outside the 5 second window
    for i in range(2):
        old_entry = make_entry(age_seconds=10.0)
        tracker.evaluate(old_entry, AlertLevel.PATTERN_WATCH, key)

    r1 = pass_fail(tracker.flagged_count() == 0, "Aged hits don't trigger flag")

    # One fresh hit — should not fire alone (need 3)
    result = tracker.evaluate(make_entry(age_seconds=0), AlertLevel.PATTERN_WATCH, key)
    r2 = pass_fail(result is None, "One fresh hit after aged hits doesn't fire")

    return all([r1, r2])


def test_window_resets_after_fire():
    """After threshold fires, window resets — same pattern shouldn't immediately re-fire."""
    print("\nTest 5: Window resets after threshold fires")
    tracker = make_tracker(pattern_count=3, pattern_samples=5, pattern_window=600)
    key = "test:reset_check"

    # Fire the threshold
    for i in range(3):
        tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, key)

    r1 = pass_fail(tracker.flagged_count() == 1, "Threshold fired once")

    # Immediately evaluate again — should not re-fire (window was reset)
    result = tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, key)
    r2 = pass_fail(result is None, "Does not immediately re-fire after reset")
    r3 = pass_fail(tracker.flagged_count() == 1, "Still only one flagged entry")

    return all([r1, r2, r3])


def test_get_flagged_filters_by_tier():
    """get_flagged() should correctly filter by AlertLevel."""
    print("\nTest 6: get_flagged() filters by tier")
    tracker = make_tracker()

    # Fire one TRIAGE
    tracker.evaluate(make_entry(), AlertLevel.TRIAGE, "test:triage")

    # Fire one PATTERN_WATCH (3 hits needed)
    for i in range(3):
        tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, "test:pattern")

    total = tracker.flagged_count()
    triage_count = tracker.flagged_count(AlertLevel.TRIAGE)
    pattern_count = tracker.flagged_count(AlertLevel.PATTERN_WATCH)
    corr_count = tracker.flagged_count(AlertLevel.CORRELATION)

    r1 = pass_fail(total == 2, "Total flagged count is 2", f"total={total}")
    r2 = pass_fail(triage_count == 1, "One TRIAGE entry", f"count={triage_count}")
    r3 = pass_fail(pattern_count == 1, "One PATTERN_WATCH entry", f"count={pattern_count}")
    r4 = pass_fail(corr_count == 0, "Zero CORRELATION entries", f"count={corr_count}")

    return all([r1, r2, r3, r4])


def test_clear_flagged():
    """clear_flagged() should remove entries correctly."""
    print("\nTest 7: clear_flagged()")
    tracker = make_tracker()

    # Fire TRIAGE twice (resets between fires so need 2 separate evaluations)
    tracker.evaluate(make_entry(), AlertLevel.TRIAGE, "test:triage_a")
    tracker.evaluate(make_entry(), AlertLevel.TRIAGE, "test:triage_b")

    # Fire one PATTERN_WATCH
    for i in range(3):
        tracker.evaluate(make_entry(), AlertLevel.PATTERN_WATCH, "test:pattern")

    r1 = pass_fail(tracker.flagged_count() == 3, "Three flagged entries before clear")

    # Clear only TRIAGE
    cleared = tracker.clear_flagged(AlertLevel.TRIAGE)
    r2 = pass_fail(cleared == 2, "Cleared 2 TRIAGE entries", f"cleared={cleared}")
    r3 = pass_fail(tracker.flagged_count() == 1, "One entry remains", f"count={tracker.flagged_count()}")
    r4 = pass_fail(
        tracker.flagged_count(AlertLevel.PATTERN_WATCH) == 1,
        "Remaining entry is PATTERN_WATCH"
    )

    # Clear all
    tracker.clear_flagged()
    r5 = pass_fail(tracker.flagged_count() == 0, "All cleared")

    return all([r1, r2, r3, r4, r5])


def test_persistence():
    """Flagged entries should survive a tracker restart."""
    print("\nTest 8: Persistence across restart")

    tmp = Path(tempfile.mkdtemp())
    thresholds = {
        AlertLevel.TRIAGE: AlertThreshold(1, 1, 0),
        AlertLevel.PATTERN_WATCH: AlertThreshold(3, 5, 600),
        AlertLevel.CORRELATION: AlertThreshold(2, 3, 300),
    }

    # First tracker instance — fire two flags
    tracker1 = AlertTracker(thresholds=thresholds, data_dir=tmp)
    tracker1.evaluate(make_entry(), AlertLevel.TRIAGE, "test:persist_a")
    tracker1.evaluate(make_entry(), AlertLevel.TRIAGE, "test:persist_b")

    r1 = pass_fail(tracker1.flagged_count() == 2, "Two flags before restart")

    # Second tracker instance — same data dir, should load from disk
    tracker2 = AlertTracker(thresholds=thresholds, data_dir=tmp)
    r2 = pass_fail(
        tracker2.flagged_count() == 2,
        "Two flags loaded after restart",
        f"count={tracker2.flagged_count()}"
    )
    r3 = pass_fail(
        tracker2.flagged_count(AlertLevel.TRIAGE) == 2,
        "Loaded flags have correct tier"
    )

    return all([r1, r2, r3])


def test_dedup_prevents_double_flagging():
    """
    The same LogEntry (same timestamp_ns) evaluated twice under the
    same pattern_key should only ever be flagged once — both within a
    single tracker instance, and across a restart where the tracker
    reloads its dedup keys from disk.
    """
    print("\nTest 9: Dedup prevents double-flagging")
    tmp = Path(tempfile.mkdtemp())
    thresholds = {
        AlertLevel.TRIAGE: AlertThreshold(1, 1, 0),
        AlertLevel.PATTERN_WATCH: AlertThreshold(3, 5, 600),
        AlertLevel.CORRELATION: AlertThreshold(2, 3, 300),
    }

    # Reuse the SAME entry object (same timestamp_ns) for every call —
    # this is what actually exercises the dedup key (timestamp_ns, pattern_key),
    # rather than accidentally testing with two different timestamps.
    entry = make_entry()

    tracker1 = AlertTracker(thresholds=thresholds, data_dir=tmp)
    first_flag = tracker1.evaluate(entry, AlertLevel.TRIAGE, "test:dedup_a")
    r1 = pass_fail(first_flag is not None, "First evaluation flags normally")

    second_flag = tracker1.evaluate(entry, AlertLevel.TRIAGE, "test:dedup_a")
    r2 = pass_fail(
        second_flag is None,
        "Same entry re-evaluated in same run does NOT re-flag",
    )
    r3 = pass_fail(
        tracker1.flagged_count() == 1,
        "Only one flagged entry exists after the duplicate attempt",
        f"count={tracker1.flagged_count()}"
    )

    # Restart — new tracker instance, same data dir, loads dedup keys from disk
    tracker2 = AlertTracker(thresholds=thresholds, data_dir=tmp)
    third_flag = tracker2.evaluate(entry, AlertLevel.TRIAGE, "test:dedup_a")
    r4 = pass_fail(
        third_flag is None,
        "Same entry after restart still does NOT re-flag (dedup survives restart)",
    )
    r5 = pass_fail(
        tracker2.flagged_count() == 1,
        "Still only one flagged entry after restart",
        f"count={tracker2.flagged_count()}"
    )

    return all([r1, r2, r3, r4, r5])


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("Precog — Alert System Tests")
    print("=" * 55)

    results = [
        test_triage_fires_immediately(),
        test_pattern_watch_threshold(),
        test_correlation_threshold(),
        test_hits_outside_window_dont_count(),
        test_window_resets_after_fire(),
        test_get_flagged_filters_by_tier(),
        test_clear_flagged(),
        test_persistence(),
        test_dedup_prevents_double_flagging(),
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
