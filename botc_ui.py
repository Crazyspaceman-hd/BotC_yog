"""botc_ui.py — Shared display constants for the BotC Streamlit apps.

Imported by both explore.py (full editor) and explore_public.py (read-only viewer).
Add new roles, verdicts, or colour tweaks here once and both apps pick them up.
"""

# ── colour palettes ───────────────────────────────────────────────────────────

_SPK_PALETTE = [
    "#dbeafe", "#fef9c3", "#dcfce7", "#ffe4e6",
    "#e0f2fe", "#fde8d0", "#ede9fe", "#d1fae5", "#fee2e2",
]

VERDICT_BG = {
    "TRUE":           "#dcfce7",
    "HONEST MISTAKE": "#fef9c3",
    "LIE":            "#ffe4e6",
    "UNVERIFIED":     "#e5e7eb",
}

VERDICT_ICON = {
    "TRUE": "✓", "HONEST MISTAKE": "~", "LIE": "✗", "UNVERIFIED": "?",
}

_STATUS_BG = {
    "analyzed":    "#dcfce7",
    "patched":     "#d1fae5",
    "merged":      "#fef9c3",
    "diarized":    "#fde8d0",
    "transcribed": "#e0f2fe",
    "downloaded":  "#dbeafe",
    "pending":     "#e5e7eb",
}

# Source-badge styles: (icon, background-colour hex)
# Keys cover both apps — unused keys are silently ignored.
_SOURCE_STYLE = {
    "manual":           ("🟢", "#dcfce7"),
    "scraped":          ("📷", "#e0f2fe"),
    "auto":             ("🟡", "#fef9c3"),
    "auto_timing":      ("⏱️",  "#fef3c7"),
    "scraped_unlinked": ("⚠️",  "#fde8d0"),
    "unlinked":         ("🔴", "#ffe4e6"),
    "system":           ("🎭", "#f0f0f0"),
}

# ── role classification ───────────────────────────────────────────────────────

# Known evil roles (demons + minions) — used to split good/evil stats
_EVIL_ROLES = {
    # Demons
    "imp", "ojo", "vigormortis", "no dashii", "vortox", "fang gu",
    "al-hadikhia", "lil' monsta", "lil monsta", "pukka", "po", "lleech",
    "shabaloth", "zombuul", "legion", "riot",
    # Minions
    "poisoner", "spy", "scarlet woman", "baron", "godfather", "assassin",
    "devil's advocate", "devils advocate", "evil twin", "witch", "cerenovus",
    "pit-hag", "pit hag", "fearmonger", "marionette", "organ grinder",
    "mezepheles", "harpy",
}


def _team(role: str) -> str:
    return "Evil" if str(role).strip().lower() in _EVIL_ROLES else "Good"


# ── heat-map colour endpoints ─────────────────────────────────────────────────

_REDS_HIGH  = (220,  38,  38)   # red-600   (high-lie end of scale)
_BLUES_HIGH = ( 37,  99, 235)   # blue-600  (high-truth end of scale)
