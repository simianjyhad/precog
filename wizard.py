"""
wizard.py — Precog First-Run Setup Wizard

Asks a small set of questions to help tailor precog.conf to the
person's actual system and needs, rather than leaving everyone on
generic defaults. Run standalone or invoked from install.py.
"""

import sys


def ask_color_vision():
    print()
    print("Do you have any difficulty distinguishing certain colors?")
    print("  1) No difficulty — use the default palette")
    print("  2) Red-green color blindness (most common)")
    print("  3) Blue-yellow color blindness (rarer)")
    print("  4) Difficulty with similar hues/shades generally (any cause,")
    print("     including low vision or display limitations)")
    while True:
        choice = input("Choose 1-4: ").strip()
        if choice in ("1", "2", "3", "4"):
            return {"1": "default", "2": "deuteranopia", "3": "tritanopia", "4": "max_contrast"}[choice]
        print("Please enter a number from 1 to 4.")


def ask_custom_keywords():
    print()
    print("Would you like to track any custom keywords beyond the")
    print("built-in defaults? (e.g. a specific error message you know")
    print("matters on your system)")
    response = input("Add custom keywords? [y/N]: ").strip().lower()
    if response != "y":
        return []
    raw = input("Enter keywords, comma-separated: ").strip()
    return [k.strip() for k in raw.split(",") if k.strip()]


def ask_network_stack_watch():
    print()
    print("NOTE: this is NOT internet traffic monitoring, browsing")
    print("history, or anything like it. Precog never inspects network")
    print("traffic or what you do online. This only watches your own")
    print("system's log messages for signs of trouble between LOCAL")
    print("system services (e.g. NetworkManager, dbus) — the same kind")
    print("of log-based pattern watching Precog already does elsewhere,")
    print("just aimed at this one category of system message.")
    print()
    print("Would you like Precog to watch for recurring network-stack")
    print("issues (like persistent dbus/permission errors between")
    print("system services)? These are usually not urgent, but worth")
    print("knowing about if they keep happening.")
    response = input("Enable this? [y/N]: ").strip().lower()
    return response == "y"


def ask_desktop_environment():
    print()
    print("What desktop environment are you running?")
    print("  1) COSMIC")
    print("  2) GNOME")
    print("  3) KDE Plasma")
    print("  4) Something else / not sure")
    while True:
        choice = input("Choose 1-4: ").strip()
        if choice in ("1", "2", "3", "4"):
            return {"1": "cosmic", "2": "gnome", "3": "kde", "4": "other"}[choice]
        print("Please enter a number from 1 to 4.")


COLOR_PALETTES = {
    "default": {
        "base_system": "white", "boot_entry": "blue",
        "tier1_pattern": "yellow", "tier2_corr": "magenta", "tier3_triage": "red",
    },
    "deuteranopia": {
        "base_system": "white", "boot_entry": "#0072B2",
        "tier1_pattern": "#F0E442", "tier2_corr": "#E69F00", "tier3_triage": "#D55E00",
    },
    "tritanopia": {
        "base_system": "white", "boot_entry": "#CC79A7",
        "tier1_pattern": "#56B4E9", "tier2_corr": "#009E73", "tier3_triage": "#D55E00",
    },
    "max_contrast": {
        "base_system": "white", "boot_entry": "#404040",
        "tier1_pattern": "#FFFFFF", "tier2_corr": "#808080", "tier3_triage": "#000000",
    },
}

PALETTE_LABELS = {
    "default": "DEFAULT (normal color vision)",
    "deuteranopia": "DEUTERANOPIA / PROTANOPIA (red-green colorblindness, most common)",
    "tritanopia": "TRITANOPIA (blue-yellow colorblindness, rarer)",
    "max_contrast": "MAXIMUM CONTRAST (for difficulty distinguishing similar hues or\n#     shades, whether from colorblindness, low vision, or display\n#     limitations)",
}


def build_colors_section(active_palette):
    """
    Builds a complete [colors] section as a string, with the chosen
    palette active (uncommented) and the other three present but
    commented out, in the same style as the original hand-written file.
    """
    lines = []
    lines.append("[colors]")
    lines.append("# ----------------------------------------------------------------------")
    lines.append("# Color codes for terminal output.")
    lines.append("# Each entry accepts EITHER a name OR a hex code - pick whichever format")
    lines.append("# you're comfortable with on a given line, they can be mixed.")
    lines.append("#   Names: black, red, green, yellow, blue, magenta, cyan, white")
    lines.append("#   Hex:   #RRGGBB (e.g. #FF8800)")
    lines.append("#")
    lines.append("# HOW TO SWITCH PALETTES:")
    lines.append("# Only ONE section below should be active (uncommented) at a time.")
    lines.append("# To switch, comment out the active section's lines (add # to the start")
    lines.append("# of each) and uncomment the section you want instead (remove the #).")

    for palette_name in ("default", "deuteranopia", "tritanopia", "max_contrast"):
        lines.append(f"# --- {PALETTE_LABELS[palette_name]} " + "-" * 3)
        prefix = "" if palette_name == active_palette else "# "
        values = COLOR_PALETTES[palette_name]
        for key in ("base_system", "boot_entry", "tier1_pattern", "tier2_corr", "tier3_triage"):
            lines.append(f"{prefix}{key:<13} = {values[key]}")

    lines.append("# ----------------------------------------------------------------------")
    return "\n".join(lines) + "\n"


def apply_wizard_answers(answers, conf_path="config/precog.conf"):
    """
    Applies the wizard's answers to precog.conf. Makes a timestamped
    backup first. Returns a list of human-readable strings describing
    what was changed, for the caller to print/log.
    """
    import shutil
    import time as time_module

    if answers is None:
        return ["Wizard was skipped — no changes made to precog.conf."]

    backup_path = f"{conf_path}.bak.{int(time_module.time())}"
    shutil.copy2(conf_path, backup_path)

    with open(conf_path) as f:
        content = f.read()

    changes = [f"Backed up existing config to {backup_path}"]

    # --- Color palette -----------------------------------------------
    import re
    new_colors_section = build_colors_section(answers["color_vision"])
    pattern = re.compile(r"\[colors\].*?(?=\n\[)", re.DOTALL)
    if pattern.search(content):
        content = pattern.sub(new_colors_section.rstrip("\n") + "\n", content, count=1)
        changes.append(f"Set color palette to: {answers['color_vision']}")
    else:
        changes.append("WARNING: could not find [colors] section to update.")

    # --- Custom keywords -----------------------------------------------
    if answers["custom_keywords"]:
        kw_list = ", ".join(answers["custom_keywords"])
        watched_pattern = re.compile(r"(watched\s*=\s*)(.+)")
        match = watched_pattern.search(content)
        if match:
            content = watched_pattern.sub(
                lambda m: m.group(1) + m.group(2).rstrip() + ", " + kw_list,
                content, count=1
            )
            changes.append(f"Added custom keywords: {kw_list}")
        else:
            changes.append("WARNING: could not find 'watched' line to update.")

    # --- Network stack watch -----------------------------------------------
    if answers["watch_network_stack"]:
        noisy_pattern = re.compile(r"(\[noisy_keywords\].*?\n)(?=\n|\Z)", re.DOTALL)
        addition = "security policy denied = 5, 86400\n"
        if noisy_pattern.search(content):
            content = noisy_pattern.sub(lambda m: m.group(1) + addition, content, count=1)
            changes.append("Enabled network-stack recurring-issue watching "
                            "('security policy denied', 5 hits within 24 hours)")
        else:
            changes.append("WARNING: could not find [noisy_keywords] section to update.")

    # --- Desktop environment exclusions -----------------------------------------------
    if answers["desktop_environment"] == "cosmic":
        excl_pattern = re.compile(r"(\[keyword_exclusions\].*?\n)(?=\n|\Z)", re.DOTALL)
        addition = "error = sctk_adwaita\n"
        if excl_pattern.search(content):
            content = excl_pattern.sub(lambda m: m.group(1) + addition, content, count=1)
            changes.append("Added COSMIC-specific exclusion for known-benign "
                            "sctk_adwaita XDG warnings")
        else:
            changes.append("WARNING: could not find [keyword_exclusions] section to update.")

    with open(conf_path, "w") as f:
        f.write(content)

    return changes


def run_wizard():
    """
    Asks all wizard questions in sequence. Returns a dict of answers.
    Does not write any files itself — that's a separate step, so this
    function can be tested and reasoned about in isolation.
    """
    print("=" * 60)
    print("Precog First-Run Setup Wizard")
    print("=" * 60)
    print("A few quick questions to tailor Precog to your system.")
    print("Press Ctrl+C at any time to skip and use defaults instead.")

    answers = {}
    try:
        answers["color_vision"] = ask_color_vision()
        answers["custom_keywords"] = ask_custom_keywords()
        answers["watch_network_stack"] = ask_network_stack_watch()
        answers["desktop_environment"] = ask_desktop_environment()
    except KeyboardInterrupt:
        print()
        print("Wizard skipped. Default settings will be used.")
        return None

    return answers


if __name__ == "__main__":
    result = run_wizard()
    print()
    print("Answers collected:")
    print(result)
