"""botc_ui.py — Shared display constants for the BotC Streamlit apps.

Imported by both explore.py (full editor) and explore_public.py (read-only viewer).
Add new roles, verdicts, or colour tweaks here once and both apps pick them up.
"""

from pipeline_utils import normalize_role

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

# Full role registry — single source of truth for team + type.
# Keys are normalized (lowercase, spaces).  Values are (team, type).
# type values: Townsfolk | Outsider | Minion | Demon | Traveller | Fabled
# To add a new role: add one entry here; build_db.py seeds the DB automatically.
_ROLES: dict[str, tuple[str, str]] = {
    # ── Townsfolk (Good) ──────────────────────────────────────────────────────
    "acrobat":          ("Good", "Townsfolk"),
    "alchemist":        ("Good", "Townsfolk"),
    "alsaahir":         ("Good", "Townsfolk"),
    "amnesiac":         ("Good", "Townsfolk"),
    "artist":           ("Good", "Townsfolk"),
    "atheist":          ("Good", "Townsfolk"),
    "balloonist":       ("Good", "Townsfolk"),
    "banshee":          ("Good", "Townsfolk"),
    "bounty hunter":    ("Good", "Townsfolk"),
    "cannibal":         ("Good", "Townsfolk"),
    "chambermaid":      ("Good", "Townsfolk"),
    "chef":             ("Good", "Townsfolk"),
    "choirboy":         ("Good", "Townsfolk"),
    "clockmaker":       ("Good", "Townsfolk"),
    "courtier":         ("Good", "Townsfolk"),
    "cult leader":      ("Good", "Townsfolk"),
    "dreamer":          ("Good", "Townsfolk"),
    "empath":           ("Good", "Townsfolk"),
    "engineer":         ("Good", "Townsfolk"),
    "exorcist":         ("Good", "Townsfolk"),
    "farmer":           ("Good", "Townsfolk"),
    "fisherman":        ("Good", "Townsfolk"),
    "flowergirl":       ("Good", "Townsfolk"),
    "fool":             ("Good", "Townsfolk"),
    "fortune teller":   ("Good", "Townsfolk"),
    "gambler":          ("Good", "Townsfolk"),
    "general":          ("Good", "Townsfolk"),
    "gossip":           ("Good", "Townsfolk"),
    "grandmother":      ("Good", "Townsfolk"),
    "high priestess":   ("Good", "Townsfolk"),
    "huntsman":         ("Good", "Townsfolk"),
    "innkeeper":        ("Good", "Townsfolk"),
    "investigator":     ("Good", "Townsfolk"),
    "juggler":          ("Good", "Townsfolk"),
    "king":             ("Good", "Townsfolk"),
    "knight":           ("Good", "Townsfolk"),
    "leech":            ("Good", "Townsfolk"),
    "librarian":        ("Good", "Townsfolk"),
    "magician":         ("Good", "Townsfolk"),
    "mathematician":    ("Good", "Townsfolk"),
    "mayor":            ("Good", "Townsfolk"),
    "minstrel":         ("Good", "Townsfolk"),
    "monk":             ("Good", "Townsfolk"),
    "nightwatchman":    ("Good", "Townsfolk"),
    "night watchman":   ("Good", "Townsfolk"),  # alias (space variant)
    "noble":            ("Good", "Townsfolk"),
    "oracle":           ("Good", "Townsfolk"),
    "pacifist":         ("Good", "Townsfolk"),
    "philosopher":      ("Good", "Townsfolk"),
    "pixie":            ("Good", "Townsfolk"),
    "poppy grower":     ("Good", "Townsfolk"),
    "preacher":         ("Good", "Townsfolk"),
    "princess":         ("Good", "Townsfolk"),
    "professor":        ("Good", "Townsfolk"),
    "ravenkeeper":      ("Good", "Townsfolk"),
    "sage":             ("Good", "Townsfolk"),
    "sailor":           ("Good", "Townsfolk"),
    "savant":           ("Good", "Townsfolk"),
    "seamstress":       ("Good", "Townsfolk"),
    "shugenja":         ("Good", "Townsfolk"),
    "slayer":           ("Good", "Townsfolk"),
    "snake charmer":    ("Good", "Townsfolk"),
    "soldier":          ("Good", "Townsfolk"),
    "steward":          ("Good", "Townsfolk"),
    "tea lady":         ("Good", "Townsfolk"),
    "town crier":       ("Good", "Townsfolk"),
    "undertaker":       ("Good", "Townsfolk"),
    "village idiot":    ("Good", "Townsfolk"),
    "virgin":           ("Good", "Townsfolk"),
    "washerwoman":      ("Good", "Townsfolk"),
    "wizard":           ("Good", "Townsfolk"),
    # ── Outsiders (Good) ──────────────────────────────────────────────────────
    "barber":           ("Good", "Outsider"),
    "butler":           ("Good", "Outsider"),
    "damsel":           ("Good", "Outsider"),
    "drunk":            ("Good", "Outsider"),
    "golem":            ("Good", "Outsider"),
    "goon":             ("Good", "Outsider"),
    "hatter":           ("Good", "Outsider"),
    "heretic":          ("Good", "Outsider"),
    "hermit":           ("Good", "Outsider"),
    "klutz":            ("Good", "Outsider"),
    "lunatic":          ("Good", "Outsider"),
    "moonchild":        ("Good", "Outsider"),
    "mutant":           ("Good", "Outsider"),
    "ogre":             ("Good", "Outsider"),
    "plague doctor":    ("Good", "Outsider"),
    "politician":       ("Good", "Outsider"),
    "puzzlemaster":     ("Good", "Outsider"),
    "recluse":          ("Good", "Outsider"),
    "saint":            ("Good", "Outsider"),
    "snitch":           ("Good", "Outsider"),
    "sweetheart":       ("Good", "Outsider"),
    "tinker":           ("Good", "Outsider"),
    "zealot":           ("Good", "Outsider"),
    # ── Minions (Evil) ────────────────────────────────────────────────────────
    "assassin":         ("Evil", "Minion"),
    "baron":            ("Evil", "Minion"),
    "boomdandy":        ("Evil", "Minion"),
    "cerenovus":        ("Evil", "Minion"),
    "devil's advocate": ("Evil", "Minion"),
    "devils advocate":  ("Evil", "Minion"),  # alias (no apostrophe)
    "evil twin":        ("Evil", "Minion"),
    "fearmonger":       ("Evil", "Minion"),
    "goblin":           ("Evil", "Minion"),
    "godfather":        ("Evil", "Minion"),
    "harpy":            ("Evil", "Minion"),
    "lycanthrope":      ("Evil", "Minion"),
    "marionette":       ("Evil", "Minion"),
    "mastermind":       ("Evil", "Minion"),
    "mezepheles":       ("Evil", "Minion"),
    "organ grinder":    ("Evil", "Minion"),
    "pit-hag":          ("Evil", "Minion"),
    "pit hag":          ("Evil", "Minion"),  # alias (no hyphen)
    "poisoner":         ("Evil", "Minion"),
    "psychopath":       ("Evil", "Minion"),
    "scarlet woman":    ("Evil", "Minion"),
    "spy":              ("Evil", "Minion"),
    "summoner":         ("Evil", "Minion"),
    "vizier":           ("Evil", "Minion"),
    "widow":            ("Evil", "Minion"),
    "witch":            ("Evil", "Minion"),
    "xaan":             ("Evil", "Minion"),
    # ── Demons (Evil) ─────────────────────────────────────────────────────────
    "al-hadikhia":      ("Evil", "Demon"),
    "cacklejack":       ("Evil", "Demon"),
    "fang gu":          ("Evil", "Demon"),
    "imp":              ("Evil", "Demon"),
    "kazali":           ("Evil", "Demon"),
    "legion":           ("Evil", "Demon"),
    "leviathan":        ("Evil", "Demon"),
    "lil' monsta":      ("Evil", "Demon"),
    "lil monsta":       ("Evil", "Demon"),   # alias (no apostrophe)
    "lleech":           ("Evil", "Demon"),
    "lord of typhon":   ("Evil", "Demon"),
    "no dashii":        ("Evil", "Demon"),
    "ojo":              ("Evil", "Demon"),
    "po":               ("Evil", "Demon"),
    "pukka":            ("Evil", "Demon"),
    "riot":             ("Evil", "Demon"),
    "shabaloth":        ("Evil", "Demon"),
    "vigormortis":      ("Evil", "Demon"),
    "vortox":           ("Evil", "Demon"),
    "yaggababble":      ("Evil", "Demon"),
    "zombuul":          ("Evil", "Demon"),
    # ── Travellers (mostly Good; Evil ones explicit) ───────────────────────────
    "apprentice":       ("Good", "Traveller"),
    "barista":          ("Good", "Traveller"),
    "beggar":           ("Good", "Traveller"),
    "bishop":           ("Good", "Traveller"),
    "boffin":           ("Evil", "Traveller"),
    "bone collector":   ("Good", "Traveller"),
    "bureaucrat":       ("Good", "Traveller"),
    "butcher":          ("Good", "Traveller"),
    "deviant":          ("Good", "Traveller"),
    "gangster":         ("Good", "Traveller"),
    "gnome":            ("Good", "Traveller"),
    "gunslinger":       ("Good", "Traveller"),
    "harlot":           ("Good", "Traveller"),
    "judge":            ("Good", "Traveller"),
    "matron":           ("Good", "Traveller"),
    "scapegoat":        ("Good", "Traveller"),
    "thief":            ("Good", "Traveller"),
    "voudon":           ("Good", "Traveller"),
    # ── Fabled (always Good) ──────────────────────────────────────────────────
    "angel":            ("Good", "Fabled"),
    "buddhist":         ("Good", "Fabled"),
    "deus ex fiasco":   ("Good", "Fabled"),
    "djinn":            ("Good", "Fabled"),
    "doomsayer":        ("Good", "Fabled"),
    "duchess":          ("Good", "Fabled"),
    "ferryman":         ("Good", "Fabled"),
    "fibbin":           ("Good", "Fabled"),
    "fiddler":          ("Good", "Fabled"),
    "hell's librarian": ("Good", "Fabled"),
    "revolutionary":    ("Good", "Fabled"),
    "sentinel":         ("Good", "Fabled"),
    "spirit of ivory":  ("Good", "Fabled"),
    "toymaker":         ("Good", "Fabled"),
    "big wig":          ("Good", "Fabled"),
    "bootlegger":       ("Good", "Fabled"),
    "gardener":         ("Good", "Fabled"),
    "hindu":            ("Good", "Fabled"),
    "pope":             ("Good", "Fabled"),
    "storm catcher":    ("Good", "Fabled"),
    "tor":              ("Good", "Fabled"),
    "ventriloquist":    ("Good", "Fabled"),
    "zenomancer":       ("Good", "Fabled"),
}

# Derived set — kept for any code that accesses _EVIL_ROLES directly
_EVIL_ROLES = frozenset(name for name, (team, _) in _ROLES.items() if team == "Evil")


def _team(role: str) -> str:
    """Return 'Evil' or 'Good' for a role name (any casing / separator)."""
    info = _ROLES.get(normalize_role(role))
    return info[0] if info else "Good"


def _role_type(role: str) -> str:
    """Return role type: Townsfolk | Outsider | Minion | Demon | Traveller | Fabled | Unknown."""
    info = _ROLES.get(normalize_role(role))
    return info[1] if info else "Unknown"


# ── heat-map colour endpoints ─────────────────────────────────────────────────

_REDS_HIGH  = (220,  38,  38)   # red-600   (high-lie end of scale)
_BLUES_HIGH = ( 37,  99, 235)   # blue-600  (high-truth end of scale)
