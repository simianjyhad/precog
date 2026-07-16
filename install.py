#!/usr/bin/env python3
"""
install.py — Precog First Run Setup
Creates the protected data directory at /var/lib/precog/, sets correct
ownership and permissions, and creates symlinks in the project data/
directory so the user can find files where they expect them.

Must be run with sudo on first install:
  sudo python3 install.py

Safe to re-run — checks before creating or overwriting anything.
"""

import os
import sys
import pwd
import stat
from pathlib import Path

SYSTEM_DATA_DIR  = Path("/var/lib/precog")
SYSTEM_FILES     = ["rolling.log", "flagged.log", "baseline.json", "flagged_archive.log"]
PROJECT_DATA_DIR = None  # set after get_real_user() is called

def check_root():
    if os.geteuid() != 0:
        print("ERROR: install.py must be run with sudo.")
        print("  sudo python3 install.py")
        sys.exit(1)

def get_real_user():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        pw = pwd.getpwnam(sudo_user)
        return pw.pw_name, pw.pw_uid, pw.pw_gid
    return "root", 0, 0

def create_system_dir():
    if SYSTEM_DATA_DIR.exists():
        print(f"  [OK] {SYSTEM_DATA_DIR} already exists")
        return
    SYSTEM_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(SYSTEM_DATA_DIR, 0, 0)
    SYSTEM_DATA_DIR.chmod(0o755)
    print(f"  [CREATED] {SYSTEM_DATA_DIR} (root:root 755)")

def create_system_files():
    for filename in SYSTEM_FILES:
        fpath = SYSTEM_DATA_DIR / filename
        if fpath.exists():
            print(f"  [OK] {fpath} already exists")
            continue
        fpath.touch()
        os.chown(fpath, 0, 0)
        # 600, not 644 — these files can contain sensitive raw log
        # content (auth failures, keyword-matched fragments, etc.),
        # so only root should be able to read them, not every user
        # on the system. Precog itself runs under sudo, so this
        # doesn't affect normal functionality.
        fpath.chmod(0o600)
        print(f"  [CREATED] {fpath} (root:root 600)")

def create_project_data_dir(uid, gid):
    if PROJECT_DATA_DIR.exists():
        print(f"  [OK] {PROJECT_DATA_DIR} already exists")
        return
    PROJECT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(PROJECT_DATA_DIR, uid, gid)
    PROJECT_DATA_DIR.chmod(0o755)
    print(f"  [CREATED] {PROJECT_DATA_DIR}")

def create_symlinks(uid, gid):
    for filename in SYSTEM_FILES:
        link_path   = PROJECT_DATA_DIR / filename
        target_path = SYSTEM_DATA_DIR / filename
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink() and link_path.resolve() == target_path:
                print(f"  [OK] symlink already correct: {link_path}")
            else:
                print(f"  [SKIP] {link_path} exists and is not the expected symlink")
            continue
        link_path.symlink_to(target_path)
        os.lchown(link_path, uid, gid)
        print(f"  [LINKED] {link_path} -> {target_path}")

def verify():
    print("\nVerification:")
    all_ok = True
    if SYSTEM_DATA_DIR.is_dir():
        s = SYSTEM_DATA_DIR.stat()
        print(f"  [OK] {SYSTEM_DATA_DIR}  mode={oct(stat.S_IMODE(s.st_mode))} uid={s.st_uid}")
    else:
        print(f"  [FAIL] {SYSTEM_DATA_DIR} missing")
        all_ok = False
    for filename in SYSTEM_FILES:
        fpath = SYSTEM_DATA_DIR / filename
        if fpath.exists():
            s = fpath.stat()
            print(f"  [OK] {fpath}  mode={oct(stat.S_IMODE(s.st_mode))} uid={s.st_uid}")
        else:
            print(f"  [FAIL] {fpath} missing")
            all_ok = False
    for filename in SYSTEM_FILES:
        link_path = PROJECT_DATA_DIR / filename
        if link_path.is_symlink():
            print(f"  [OK] {link_path} -> {link_path.resolve()}")
        else:
            print(f"  [FAIL] symlink missing: {link_path}")
            all_ok = False
    if all_ok:
        print("\nInstall complete. Precog data directory is ready.")
    else:
        print("\nWARNING: Some items are missing. Re-run install.py.")

if __name__ == "__main__":
    print("Precog — First Run Install")
    print("=" * 40)
    check_root()
    username, uid, gid = get_real_user()
    PROJECT_DATA_DIR = Path(pwd.getpwnam(username).pw_dir) / "Documents" / "precog" / "data"
    print(f"Installing for user: {username} (uid={uid})\n")
    print("Creating system data directory:")
    create_system_dir()
    create_system_files()
    print("\nCreating project symlinks:")
    create_project_data_dir(uid, gid)
    create_symlinks(uid, gid)
    verify()

    if "--skip-wizard" not in sys.argv and sys.stdin.isatty():
        try:
            from wizard import run_wizard, apply_wizard_answers
            answers = run_wizard()
            conf_path = Path(__file__).parent / "config" / "precog.conf"
            changes = apply_wizard_answers(answers, conf_path=str(conf_path))
            print()
            print("Configuration changes:")
            for change in changes:
                print(f"  - {change}")
        except ImportError:
            print("\n[install] wizard.py not found — skipping setup wizard.")
    else:
        print("\nSkipping setup wizard (--skip-wizard given, or non-interactive session).")
