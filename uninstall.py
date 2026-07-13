#!/usr/bin/env python3
"""
uninstall.py — Precog Uninstaller

Removes the project-side symlinks that install.py created, and decides
what happens to the actual data in /var/lib/precog/ (baseline stats,
flagged alerts, rolling log buffer).

That data is never silently kept or silently destroyed. Every run either
prompts for an explicit decision, or requires an explicit command-line
flag — there is no path where data quietly lingers (or gets destroyed)
without someone having consciously chosen that outcome.

Usage:
  sudo python3 uninstall.py                 # interactive prompt
  sudo python3 uninstall.py --keep-data      # non-interactive, preserve
  sudo python3 uninstall.py --purge-data     # non-interactive, but still
                                              # requires typing DELETE to
                                              # confirm the destructive step
"""

import os
import sys
import shutil
import pwd
from pathlib import Path

SYSTEM_DATA_DIR = Path("/var/lib/precog")


def check_root():
    if os.geteuid() != 0:
        print("ERROR: uninstall.py must be run with sudo.")
        print("  sudo python3 uninstall.py")
        sys.exit(1)


def get_real_user():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        pw = pwd.getpwnam(sudo_user)
        return pw.pw_name, pw.pw_uid, pw.pw_gid
    return "root", 0, 0


def get_project_data_dir(username):
    if username == "root":
        home = Path("/root")
    else:
        home = Path("/home") / username
    return home / "Documents" / "precog" / "data"


def remove_symlinks(project_data_dir):
    """
    Removes the project-side symlinks under the user's home directory.
    Always safe — these are just links, not the actual data, and
    removing them never destroys anything in /var/lib/precog/.
    """
    if not project_data_dir.exists():
        print(f"  [OK] {project_data_dir} does not exist, nothing to remove.")
        return

    removed_any = False
    for item in project_data_dir.iterdir():
        if item.is_symlink():
            item.unlink()
            print(f"  [REMOVED] symlink {item}")
            removed_any = True

    if not removed_any:
        print(f"  [OK] No symlinks found in {project_data_dir}.")


def describe_system_data():
    """Returns a human readable summary of what's in SYSTEM_DATA_DIR."""
    if not SYSTEM_DATA_DIR.exists():
        return None

    lines = []
    total_size = 0
    for item in sorted(SYSTEM_DATA_DIR.iterdir()):
        if item.is_file():
            size = item.stat().st_size
            total_size += size
            lines.append(f"    {item.name} ({size} bytes)")

    if total_size < 1024:
        size_str = f"{total_size}B"
    elif total_size < 1024 * 1024:
        size_str = f"{total_size // 1024}KB"
    else:
        size_str = f"{total_size / (1024 * 1024):.1f}MB"

    return lines, size_str


def purge_confirmation() -> bool:
    """
    Requires the user to type DELETE in full, exactly, to confirm.
    Anything else (including empty input, y, yes, etc.) cancels the purge.
    """
    print()
    print("This will PERMANENTLY delete all baseline learning history,")
    print("flagged alerts, and archived alerts. This cannot be undone.")
    print()
    response = input("Type DELETE (all caps) to confirm, anything else to cancel: ")
    return response == "DELETE"


def purge_system_data():
    try:
        shutil.rmtree(SYSTEM_DATA_DIR)
        print(f"  [PURGED] {SYSTEM_DATA_DIR} and all its contents removed.")
    except OSError as e:
        print(f"  ERROR: could not remove {SYSTEM_DATA_DIR}: {e}")
        sys.exit(1)


def keep_system_data():
    print(f"  [KEPT] {SYSTEM_DATA_DIR} preserved for a future reinstall.")


def main():
    check_root()

    keep_flag = "--keep-data" in sys.argv
    purge_flag = "--purge-data" in sys.argv

    if keep_flag and purge_flag:
        print("ERROR: --keep-data and --purge-data are mutually exclusive.")
        sys.exit(1)

    username, uid, gid = get_real_user()
    project_data_dir = get_project_data_dir(username)

    print("=" * 60)
    print("Precog Uninstaller")
    print("=" * 60)
    print()

    print("Removing project symlinks...")
    remove_symlinks(project_data_dir)
    print()

    data_info = describe_system_data()

    if data_info is None:
        print(f"No data found at {SYSTEM_DATA_DIR} — nothing further to do.")
        print()
        print("Uninstall complete.")
        return

    lines, size_str = data_info
    print(f"Data found at {SYSTEM_DATA_DIR} ({size_str} total):")
    for line in lines:
        print(line)
    print()

    if purge_flag:
        if purge_confirmation():
            purge_system_data()
        else:
            print("Purge cancelled. Data has been left untouched.")
            keep_system_data()
    elif keep_flag:
        keep_system_data()
    else:
        if not sys.stdin.isatty():
            print("ERROR: No --keep-data or --purge-data flag given, and")
            print("this doesn't appear to be an interactive terminal.")
            print("Refusing to guess. Re-run with one of:")
            print("  sudo python3 uninstall.py --keep-data")
            print("  sudo python3 uninstall.py --purge-data")
            sys.exit(1)

        print("What should happen to this data?")
        print("  keep  — preserve it, in case you reinstall Precog later")
        print("  purge — permanently delete it")
        print()
        while True:
            choice = input("Type 'keep' or 'purge': ").strip().lower()
            if choice == "keep":
                keep_system_data()
                break
            elif choice == "purge":
                if purge_confirmation():
                    purge_system_data()
                else:
                    print("Purge cancelled. Data has been left untouched.")
                    keep_system_data()
                break
            else:
                print("Please type exactly 'keep' or 'purge'.")

    print()
    print("Uninstall complete.")


if __name__ == "__main__":
    main()
