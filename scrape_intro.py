"""scrape_intro.py - Extract player intro roster from BotC video UI overlays.

Reads:  outputs/<VIDEO_ID>/video.mp4  (also tries .webm, .mkv, .avi)
Writes: outputs/<VIDEO_ID>/intro_roster.json
        outputs/<VIDEO_ID>/intro_debug/  (optional debug crops, --debug flag)

During the intro phase of each BotC game the Yogscast Minecraft mod shows a
full-screen overlay for each player:

  Left panel  : username  +  the role the player is TOLD they have (believed)
  Right panel : the ACTUAL role (differs when the player is deceived,
                e.g. Marionette, Drunk, Lunatic, Spy, Recluse)

Algorithm:
  1. Sample one frame per SAMPLE_INTERVAL seconds for the first INTRO_WINDOW s
  2. Detect intro-overlay frames via HSV colour analysis of the two team badges
     (blue "GOOD TEAM" badge + red "EVIL TEAM" badge, top-left corner)
  3. OCR three text regions per detected frame (name / believed role / actual role)
  4. Fuzzy-match OCR results against players.txt and roles.txt
  5. Deduplicate per player, write intro_roster.json

OCR backend priority:  easyocr (GPU)  >  pytesseract  >  debug-frame-only mode

Usage:
    python scrape_intro.py [video_id]
    python scrape_intro.py [video_id] --debug   # also save cropped debug images
    python scrape_intro.py [video_id] --force   # overwrite existing output
"""

import argparse
import difflib
import json
import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


# ── OCR backend setup ────────────────────────────────────────────────────────

def _init_ocr():
    """
    Return (backend_name, reader_or_None).
    Tries easyocr (GPU → CPU fallback), then pytesseract.
    Catches ALL exceptions so callers always get a clean (name, obj) tuple.
    """
    try:
        import easyocr
        # Try GPU first; if CUDA is unavailable fall back to CPU silently
        for use_gpu in (True, False):
            try:
                print(f"  Initialising easyocr ({'GPU' if use_gpu else 'CPU'})...")
                reader = easyocr.Reader(["en"], gpu=use_gpu, verbose=False)
                print(f"  easyocr ready ({'GPU' if use_gpu else 'CPU'})")
                return "easyocr", reader
            except Exception as exc:
                print(f"  easyocr {'GPU' if use_gpu else 'CPU'} failed: {exc}")
    except ImportError:
        print("  easyocr not installed  (pip install easyocr)")
    except Exception as exc:
        print(f"  easyocr import error: {exc}")

    try:
        import pytesseract
        # Quick smoke-test so we know it's actually runnable
        pytesseract.get_tesseract_version()
        print("  pytesseract ready")
        return "pytesseract", None
    except ImportError:
        print("  pytesseract not installed  (pip install pytesseract)")
    except Exception as exc:
        print(f"  pytesseract not usable: {exc}")

    print("  [ERROR] No OCR backend available - install easyocr or pytesseract+Tesseract")
    return None, None


def test_ocr(backend: str, reader) -> bool:
    """
    Smoke-test: render white text on a black image and try to read it back.
    Returns True if at least one of the test words is recovered.
    """
    test_words = ["RAVENKEEPER", "BEN"]
    img = np.zeros((60, 300, 3), dtype=np.uint8)
    cv2.putText(img, "BEN  RAVENKEEPER", (5, 42),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 2, cv2.LINE_AA)
    result = ocr_crop(img, backend, reader)
    ok = any(w.lower() in result.lower() for w in test_words)
    status = "PASS" if ok else "FAIL (no expected word found)"
    print(f"  OCR smoke-test -> '{result}'  [{status}]")
    return ok


# ── constants ────────────────────────────────────────────────────────────────

INTRO_WINDOW    = 300.0   # seconds to sample (12 min covers most BotC intros)
SAMPLE_INTERVAL = 1.0     # seconds between frame checks
MIN_FRAME_GAP   = 5.0     # seconds gap before treating a new frame as a new player
MATCH_THRESHOLD = 0.62    # minimum fuzzy-match score to accept a name/role
CUTOFF_BUFFER   = 90.0    # extra seconds to scan after all players are found
                           # (raised from 10s: newer nameplate-card format has
                           # ~60s gaps between individual player intro cards)

# Roles where the game mechanically lies to the player about their own identity.
# Only these roles can have believed_role != actual_role in the output.
DECEPTIVE_ROLES = {"drunk", "lunatic", "marionette"}

# Normalised crop regions as (x1, y1, x2, y2) fractions of frame dimensions.
# Calibrated against the Yogscast BotC Minecraft mod reference screenshot (480×270 px).
#
# Left panel layout (approx pixels in ref image):
#   [avatar 8-78, y145-195] | [BEN  80-160, y148-163] | [MARIONETTE 170-234, y148-163]
#   Description text below the nameplate strip.
# Right panel layout:
#   Dark header with [RAVENKEEPER 268-455, y42-59]
#
# Run  python calibrate_scraper.py <id>  to get annotated frames for fine-tuning.
LAYOUT = {
    "badge_area":    (0.00, 0.00, 0.10, 0.26),  # top-left: GOOD TEAM (blue) + EVIL TEAM (red)
    "player_name":   (0.08, 0.42, 0.20, 0.48),  # left nameplate — username  ("BEN")
    "believed_role": (0.20, 0.42, 0.50, 0.48),  # left nameplate — role told ("MARIONETTE")
    "actual_role":   (0.72, 0.42, 0.98, 0.48),  # right panel header         ("RAVENKEEPER")
    "desc_box":      (0.00, 0.50, 0.22, 0.72),  # role description card (cream bg, always present
                                                 # in storyteller-POV intro frames)
}

# HSV thresholds for the "GOOD TEAM" badge (bright blue)
_GOOD_LO = np.array([95,  60,  60],  dtype=np.uint8)
_GOOD_HI = np.array([138, 255, 255], dtype=np.uint8)

# HSV thresholds for the "EVIL TEAM" badge (red wraps around hue=0 in HSV)
_EVIL_LO1 = np.array([0,   90,  90],  dtype=np.uint8)
_EVIL_HI1 = np.array([12,  255, 255], dtype=np.uint8)
_EVIL_LO2 = np.array([163, 90,  90],  dtype=np.uint8)
_EVIL_HI2 = np.array([180, 255, 255], dtype=np.uint8)

# HSV thresholds for the nameplate-card format (no team badges).
# The nameplate background bleeds strongly-saturated red into the badge area
# (H≈0, S>100) — distinct from the desaturated blue-green sky/terrain (S<80)
# seen in gameplay frames.
_NAMEPLATE_LO1 = np.array([0,   100, 100], dtype=np.uint8)
_NAMEPLATE_HI1 = np.array([12,  255, 255], dtype=np.uint8)
_NAMEPLATE_LO2 = np.array([163, 100, 100], dtype=np.uint8)
_NAMEPLATE_HI2 = np.array([180, 255, 255], dtype=np.uint8)

# HSV thresholds for the cream/off-white role-description box
# (bottom-left panel, always present in storyteller-POV intro frames).
# Cream = low saturation (<70) + high value (>170).  Gameplay content at the
# same screen position is colourful (S much higher) so this fires cleanly.
_DESC_LO = np.array([0,   0, 170], dtype=np.uint8)
_DESC_HI = np.array([180, 70, 255], dtype=np.uint8)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _crop(frame: np.ndarray, region: tuple) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = region
    return frame[int(h * y1): int(h * y2), int(w * x1): int(w * x2)]


def is_intro_frame(frame: np.ndarray) -> bool:
    """Return True when a player intro card is visible.

    Three independent checks — any one suffices:

    1. Full-screen overlay (older videos): GOOD TEAM (blue) + EVIL TEAM (red)
       badges present in the top-left badge area.

    2. Nameplate-card badge bleed (mid-era videos): strongly-saturated red
       (H≈0, S>100) bleeds from the nameplate background into the badge area.

    3. Description box (most reliable — storyteller-POV / newer videos): the
       cream/off-white role-description card at x=0-22%, y=50-72% is only
       present during player intro cards regardless of camera perspective.
       Measured cream fraction (S<70, V>170) consistently reads 0.10-0.20 on
       intro frames and <0.02 on gameplay frames across tested videos.
    """
    # ── Checks 1 & 2: badge area colour (older / mid-era formats) ────────────
    badge = _crop(frame, LAYOUT["badge_area"])
    if badge.size > 0:
        hsv   = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)
        total = badge.shape[0] * badge.shape[1]

        blue = cv2.inRange(hsv, _GOOD_LO, _GOOD_HI)
        red  = (cv2.inRange(hsv, _EVIL_LO1, _EVIL_HI1) |
                cv2.inRange(hsv, _EVIL_LO2, _EVIL_HI2))

        # Check 1: full-screen overlay — both team badges present
        if (float(blue.sum()) / 255.0 / total > 0.010 and
                float(red.sum()) / 255.0 / total > 0.002):
            return True

        # Check 2: nameplate badge bleed — strongly-saturated red only
        strong_red = (cv2.inRange(hsv, _NAMEPLATE_LO1, _NAMEPLATE_HI1) |
                      cv2.inRange(hsv, _NAMEPLATE_LO2, _NAMEPLATE_HI2))
        if float(strong_red.sum()) / 255.0 / total > 0.05:
            return True

    # ── Check 3: cream role-description box ───────────────────────────────────
    desc = _crop(frame, LAYOUT["desc_box"])
    if desc.size > 0:
        dhsv  = cv2.cvtColor(desc, cv2.COLOR_BGR2HSV)
        total = desc.shape[0] * desc.shape[1]
        cream = cv2.inRange(dhsv, _DESC_LO, _DESC_HI)
        if float(cream.sum()) / 255.0 / total > 0.08:
            return True

    return False


def _upscale(crop: np.ndarray, scale: int = 3) -> np.ndarray:
    """Upscale a BGR crop for OCR (keeps colour channels)."""
    return cv2.resize(crop, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)


def _preprocess_gray(crop: np.ndarray) -> np.ndarray:
    """Upscale + grayscale + CLAHE — for pytesseract."""
    up    = _upscale(crop)
    gray  = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _ocr_easyocr(crop: np.ndarray, reader) -> str:
    # Pass upscaled BGR — easyocr detects coloured text better in colour
    up      = _upscale(crop)
    results = reader.readtext(up, detail=1, paragraph=False)
    # Filter very low-confidence detections, join the rest
    texts = [r[1] for r in results if r[2] > 0.20]
    return " ".join(texts).strip()


def _ocr_pytesseract(crop: np.ndarray) -> str:
    import pytesseract
    from PIL import Image
    img = Image.fromarray(_preprocess_gray(crop))
    return pytesseract.image_to_string(
        img, config="--oem 3 --psm 7 -c tessedit_char_blacklist=|{}<>"
    ).strip()


def ocr_crop(crop: np.ndarray, backend: str, reader) -> str:
    """Run OCR on *crop*; return text or '' on any failure."""
    if crop is None or crop.size == 0:
        return ""
    try:
        if backend == "easyocr":
            return _ocr_easyocr(crop, reader)
        elif backend == "pytesseract":
            return _ocr_pytesseract(crop)
    except Exception as exc:
        print(f"    [OCR error] {type(exc).__name__}: {exc}")
    return ""


def best_match(raw: str, candidates: list[str],
               threshold: float = MATCH_THRESHOLD) -> str | None:
    """Fuzzy-match *raw* OCR text against *candidates*; return best or None."""
    if not raw or not candidates:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    raw_n   = norm(raw)
    cands_n = [norm(c) for c in candidates]

    best_s, best_i = 0.0, -1
    for i, cn in enumerate(cands_n):
        score = difflib.SequenceMatcher(None, raw_n, cn).ratio()
        if score > best_s:
            best_s, best_i = score, i

    if best_s >= threshold and best_i >= 0:
        return candidates[best_i]
    return None


# ── main scraping logic ──────────────────────────────────────────────────────

def scrape_video(
    video_path: Path,
    players:    list[str],
    roles:      list[str],
    debug_dir:  Path | None = None,
    backend:    str | None  = None,
    reader      = None,
) -> list[dict]:
    """
    Sample intro frames, detect overlay, OCR text regions.
    Returns raw detection dicts: {name, believed_role, actual_role, frame_time}.
    Pass pre-initialised (backend, reader) to avoid re-loading the model.
    """
    if backend is None:
        backend, reader = _init_ocr()
    if backend is None:
        print("  [WARNING] No OCR backend available — saving debug frames only.")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps          = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step_frames  = max(1, int(fps * SAMPLE_INTERVAL))
    end_frame    = min(total_frames, int(fps * INTRO_WINDOW))

    print(f"  Video : {video_path.name}  ({fps:.1f} fps, {total_frames} frames)")
    print(f"  Scan  : every {SAMPLE_INTERVAL}s for first {INTRO_WINDOW:.0f}s "
          f"({end_frame} frames to check)")
    if backend:
        print(f"  OCR   : {backend}")
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Debug : {debug_dir}")

    detections:    list[dict] = []
    last_det_time: float      = -MIN_FRAME_GAP - 1.0
    found_names:   set[str]   = set()
    last_intro_t:  float      = -1.0   # timestamp of most recent intro-overlay frame
    frame_no = 0

    while frame_no < end_frame:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ok, frame = cap.read()
        if not ok:
            break

        t = frame_no / fps

        # Inactivity cutoff: if we've seen at least one player and then gone
        # CUTOFF_BUFFER seconds with no intro frames, the intro phase is over.
        if (last_intro_t >= 0
                and found_names
                and t - last_intro_t >= CUTOFF_BUFFER):
            print(f"  No intro frames for {CUTOFF_BUFFER:.0f}s"
                  f" (last at t={last_intro_t:.1f}s) -> end of intro phase")
            break

        if is_intro_frame(frame):
            last_intro_t = t   # keep alive as long as overlay is visible

            # Log detection stats so thresholds can be tuned if needed
            badge = _crop(frame, LAYOUT["badge_area"])
            desc  = _crop(frame, LAYOUT["desc_box"])
            b_r = r_r = cream_r = 0.0
            if badge.size > 0:
                hsv  = cv2.cvtColor(badge, cv2.COLOR_BGR2HSV)
                tot  = badge.shape[0] * badge.shape[1]
                blue = cv2.inRange(hsv, _GOOD_LO, _GOOD_HI)
                red  = (cv2.inRange(hsv, _EVIL_LO1, _EVIL_HI1) |
                        cv2.inRange(hsv, _EVIL_LO2, _EVIL_HI2))
                b_r  = float(blue.sum()) / 255.0 / tot
                r_r  = float(red.sum())  / 255.0 / tot
            if desc.size > 0:
                dhsv   = cv2.cvtColor(desc, cv2.COLOR_BGR2HSV)
                dtot   = desc.shape[0] * desc.shape[1]
                cream  = cv2.inRange(dhsv, _DESC_LO, _DESC_HI)
                cream_r = float(cream.sum()) / 255.0 / dtot
            print(f"  t={t:6.1f}s  badge blue={b_r:.3f} red={r_r:.3f}  desc_cream={cream_r:.3f}", end="")

            # Skip frames that are still showing the same intro card
            if t - last_det_time < MIN_FRAME_GAP:
                print("  [dup skip]")
                frame_no += step_frames
                continue

            print(f"  -> OCR", end="")

            if debug_dir:
                cv2.imwrite(str(debug_dir / f"frame_{t:.1f}s.png"), frame)

            name_raw = bel_raw = act_raw = ""
            print()   # end the badge-score line

            if backend:
                name_crop = _crop(frame, LAYOUT["player_name"])
                bel_crop  = _crop(frame, LAYOUT["believed_role"])
                act_crop  = _crop(frame, LAYOUT["actual_role"])

                name_raw = ocr_crop(name_crop, backend, reader)
                bel_raw  = ocr_crop(bel_crop,  backend, reader)
                act_raw  = ocr_crop(act_crop,  backend, reader)

                if debug_dir:
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_name.png"),    name_crop)
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_believed.png"), bel_crop)
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_actual.png"),   act_crop)

                print(f"    raw OCR -> name={name_raw!r}  "
                      f"believed={bel_raw!r}  actual={act_raw!r}")

            name      = best_match(name_raw, players)
            bel_left  = best_match(bel_raw,  roles)   # left panel
            act_right = best_match(act_raw,  roles)   # right panel

            # How the two panels map to actual/believed roles varies by role type:
            #
            # DECEPTIVE roles (Drunk, Lunatic, Marionette):
            #   Left panel  = player's TRUE game role  (e.g. Drunk)
            #   Right panel = the FALSE role the player is told (e.g. Empath)
            #   → actual = left, believed = right
            #
            # All other roles (e.g. Spy, normal Townsfolk):
            #   Left panel  = cover alias / only role  (e.g. Investigator for Spy)
            #   Right panel = true identity (e.g. Spy), or empty for non-deceptive
            #   → actual = right (if present), else left; believed = actual (no deception)
            if bel_left and bel_left.lower() in DECEPTIVE_ROLES:
                actual_role   = bel_left
                believed_role = act_right or bel_left
            elif act_right:
                actual_role   = act_right
                believed_role = act_right
            else:
                actual_role   = bel_left
                believed_role = bel_left

            if name_raw and (name or believed_role):
                deception = (f"  [DECEIVED: believed={believed_role}]"
                             if actual_role and believed_role
                             and actual_role != believed_role else "")
                print(f"    -> name={name!r}  actual={actual_role!r}{deception}")
                detections.append({
                    "name":          name or name_raw,
                    "believed_role": (believed_role or bel_raw or "unknown").lower(),
                    "actual_role":   (actual_role   or act_raw or "unknown").lower(),
                    "frame_time":    round(t, 2),
                })
                last_det_time = t
                if name:
                    found_names.add(name.lower())
            else:
                print("  ->  no player/role matched, skipping")

        frame_no += step_frames

    cap.release()
    return detections


def deduplicate(detections: list[dict]) -> list[dict]:
    """
    One entry per player.  Priority order:
      1. Entries with deception (actual ≠ believed) — most complete
      2. Among non-deceptive entries: last occurrence wins (later frames
         are more stable; the right panel text often clears up over time)
    """
    by_name: dict[str, dict] = {}
    for d in detections:
        key = (d["name"] or "").strip().lower() or f"_t{d['frame_time']}"
        if key not in by_name:
            by_name[key] = d
        else:
            existing = by_name[key]
            new_deceptive = d["actual_role"] != d["believed_role"]
            old_deceptive = existing["actual_role"] != existing["believed_role"]
            if new_deceptive and not old_deceptive:
                # New entry has deception info — upgrade
                by_name[key] = d
            elif not old_deceptive and not new_deceptive:
                # Neither is deceptive: take the latest (most stable reading)
                by_name[key] = d
            # else: old is deceptive, new is not — keep old
    return list(by_name.values())


# ── CLI entry point ──────────────────────────────────────────────────────────

def main(video_id: str = "lF96Jd3Eaeg",
         debug: bool = False,
         force: bool = False,
         test_only: bool = False) -> None:
    print("=== scrape_intro.py ===\n")

    # Initialise OCR once so the smoke-test uses the same instance
    backend, reader = _init_ocr()

    print("\n--- OCR smoke-test ---")
    if backend:
        test_ocr(backend, reader)
    else:
        print("  (no backend — skipping smoke-test)")
    print()

    if test_only:
        return

    out_dir  = Path(f"outputs/{video_id}")
    out_json = out_dir / "intro_roster.json"

    if out_json.exists() and not force:
        print("  intro_roster.json already exists — skipping "
              "(use --force to overwrite)")
        return

    # Find video file — try common container extensions
    video_path = None
    for ext in ("mp4", "webm", "mkv", "avi", "mp4.webm"):
        p = out_dir / f"video.{ext}"
        if p.exists():
            video_path = p
            break
    if video_path is None:
        print(f"  [SKIP] No video file found in {out_dir}/ — run download first")
        return

    players    = load_lines(Path("players.txt"))
    roles_file = out_dir / "roles.txt"
    if not roles_file.exists():
        roles_file = Path("roles.txt")
    roles = load_lines(roles_file)
    print(f"  Loaded {len(players)} players, {len(roles)} roles\n")

    debug_dir = out_dir / "intro_debug" if debug else None
    raw       = scrape_video(video_path, players, roles, debug_dir,
                             backend=backend, reader=reader)
    entries   = deduplicate(raw)

    print(f"\n=== Scraped {len(entries)} player introduction(s) ===")
    for e in entries:
        deception = (f"  <- believed {e['believed_role']}"
                     if e["actual_role"] != e["believed_role"] else "")
        print(f"  t={e['frame_time']:6.1f}s  {e['name']:<20s}  "
              f"actual: {e['actual_role']}{deception}")

    output = {
        "source":     "video_ocr",
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "video_id":   video_id,
        "players":    entries,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nWrote {len(entries)} entries -> {out_json}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape player intro roster from BotC video UI overlays"
    )
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg",
                    help="YouTube video ID")
    ap.add_argument("--debug", action="store_true",
                    help="Save cropped debug images to outputs/<id>/intro_debug/")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing intro_roster.json")
    ap.add_argument("--test", action="store_true",
                    help="Initialise OCR, run smoke-test, then exit (no video needed)")
    args = ap.parse_args()
    main(args.video_id, debug=args.debug, force=args.force, test_only=args.test)
