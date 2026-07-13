# Precog

Precog is a predictive log monitoring tool for Linux. It watches your
system's logs in real time, learns what "normal" looks like for your
specific machine, and flags patterns worth your attention before they
become full-blown problems — without drowning you in noise.

The name is a nod to Philip K. Dick.

## Why Precog?

Most log monitoring tools either dump raw output at you (useless unless
you already know what you're looking for) or wait until something has
already broken. Precog aims for the middle ground: it tracks recurring
patterns and correlations across your logs, and only surfaces something
once it's earned your attention — while still being useful from the
very first minute you run it.

## Core Design

- **Baseline learning** — Precog seeds itself from your existing
  journalctl history on first run, then keeps learning what's normal
  for your system over time. It reports its own confidence level
  honestly rather than pretending to know more than it does.
- **Three-tier alert system**:
  - **Pattern Watch** — a single subsystem showing a recurring issue.
    Requires repetition within a time window before it fires, so a
    one-off blip doesn't interrupt you.
  - **Correlation** — multiple subsystems showing related trouble
    close together in time. A stronger, more systemic signal.
  - **Triage** — critical keywords that fire immediately, no waiting
    period, because some things shouldn't wait to be flagged.
- **Config-driven, not hardcoded** — thresholds, watched keywords, and
  terminal colors all live in `config/precog.conf`, so you can tune
  Precog to your own system's normal chatter rather than living with
  someone else's defaults.
- **Accessibility-aware color palettes** — the default color scheme
  plus alternate palettes for deuteranopia/protanopia, tritanopia, and
  a maximum-contrast option for anyone who has trouble distinguishing
  similar hues or shades, whatever the cause.
- **Retention with nothing thrown away** — flagged entries older than
  a configurable cutoff move to an archive file rather than piling up
  forever in the live log. Full history is preserved, just relocated.

## Requirements

- Linux with journalctl available
- Python 3.10+
- Root/sudo access (Precog reads system logs and writes to
  /var/lib/precog/)

## Installation

    git clone <your-repo-url>
    cd precog
    sudo python3 install.py

install.py sets up /var/lib/precog/ for Precog's persistent data
(baseline stats, flagged alerts, rolling log buffer) with restrictive
permissions, since this data can include fragments of sensitive log
content (failed login attempts, matched keyword context, etc.) and
shouldn't be readable by every user on a multi-user system.

## Running

    sudo python3 precog.py

On first run, Precog seeds its baseline from your existing journalctl
history (this may take a little while on a system with a lot of log
history) and prints a one-time confidence notice. After that, it
watches journalctl and /var/log/auth.log (if present) live, and
prints a status line every 10 seconds along with any new alerts.

Press Ctrl+C to stop. Shutdown flushes the baseline, saves state, and
exits cleanly — nothing is lost between runs.

## Viewing the Boot Log

    python3 precog.py --show-boot-log

Prints the current boot's full journal output, colorized consistently
with the rest of Precog's terminal output. This is a standalone
diagnostic view — it does not touch the baseline, alerts, or config,
and does not start monitoring. Useful for a quick glance at what
happened during the last boot without needing to remember journalctl's
own syntax.

## Configuration

See config/precog.conf for the full set of tunable settings:

- [thresholds] — how many occurrences within what time window before
  each alert tier fires
- [keywords] — which terms are tracked, and which are severe enough
  to trigger an immediate Triage alert
- [colors] — terminal color scheme, including accessibility palettes
- [retention] — how long flagged entries stay in the live log before
  archiving

The shipped keyword list is a starting point based on common precursor
signals, not a fixed truth — every system's normal log chatter is
different, and you should expect to tune it once you've got some real
history to judge by.

## Testing

    cd tests
    python3 test_alert.py
    python3 test_baseline.py
    python3 test_buffer.py

## Project Structure

    precog/
    |-- precog.py          Main entry point
    |-- install.py          First-run setup (run with sudo)
    |-- config/
    |   |-- precog.conf     All tunable settings
    |-- core/
    |   |-- rolling_log.py  Rolling buffer of raw log entries
    |   |-- baseline.py     Baseline learning and confidence scoring
    |   |-- alert.py        Threshold tracking and alert flagging
    |   |-- config.py       precog.conf parser
    |   |-- colors.py       Terminal color scheme handling
    |-- tests/              Unit tests

## License

MIT License — see LICENSE for the full text. Fork it, use it, modify
it, just keep the copyright notice attached.
