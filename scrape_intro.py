"""scrape_intro.py - Extract player intro roster from BotC video UI overlays.

Reads:  outputs/<VIDEO_ID>/video.mp4  (also tries .webm, .mkv, .avi)
Writes: outputs/<VIDEO_ID>/intro_roster.json
        outputs/<VIDEO_ID>/intro_debug/  (optional debug crops, --debug flag)

During the intro phase of each BotC game the Yogscast Minecraft mod shows a
full-screen overlay for each player from the Storyteller's POV:

  Left panel  : username  +  the player's ACTUAL role (always)
  Right panel : the player's BELIEVED role (only for deceptive roles:
                Drunk/Lunatic/Marionette), OR info about other players
                (demon bluffs, info-role reveals, role-pick prompts, etc.)
                In all non-deceptive cases the right panel must be ignored.

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
    python scrape_intro.py [video_id] --debug          # also save cropped debug images
    python scrape_intro.py [video_id] --force          # overwrite existing output
    python scrape_intro.py [video_id] --force-manual   # also overwrite manual_entry files
    python scrape_intro.py --all --force               # re-scrape all videos (skips manual_entry)
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
# Calibrated against 1920×1080 video frames from multiple episodes.
#
# Nameplate strip (left panel) measured across several videos:
#   - Text top:    y ≈ 0.35–0.37  (varies slightly by camera angle / animation frame)
#   - Text bottom: y ≈ 0.42–0.44
#   Using y = 0.34–0.46 gives ≥30 px headroom above and below at 1080p.
#
# Right panel header (believed role for deceptive players, bluff/info otherwise):
#   Same y band as the left nameplate strip — right card mirrors the left layout.
#
# Run  python calibrate_scraper.py <id>  to get annotated frames for fine-tuning.
LAYOUT = {
    "badge_area":    (0.00, 0.00, 0.10, 0.26),  # top-left: GOOD TEAM (blue) + EVIL TEAM (red)
    "player_name":   (0.08, 0.42, 0.20, 0.48),  # left nameplate — username  ("BEN")
    "believed_role": (0.20, 0.42, 0.50, 0.48),  # left nameplate — actual role ("IMP", "MAYOR"…)
    "actual_role":   (0.72, 0.42, 0.98, 0.48),  # right panel header (believed role / info)
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

        # Check 1: full-screen overlay — both team badges present.
        # Raised from (0.010, 0.002) to (0.06, 0.03): the genuine overlay has
        # large saturated badges (blue~0.13+, red~0.10+). Low values (blue<0.06)
        # are caused by Minecraft sky/terrain colour leaking into the badge area
        # on frames that are NOT actual intro cards.
        if (float(blue.sum()) / 255.0 / total > 0.06 and
                float(red.sum()) / 255.0 / total > 0.03):
            return True

        # Check 2: nameplate badge bleed — strongly-saturated red only.
        # Raised from 0.05 to 0.10: genuine nameplate bleed reads ~0.13-0.15;
        # Minecraft red-block/terrain leaks read ~0.09 and are false positives.
        strong_red = (cv2.inRange(hsv, _NAMEPLATE_LO1, _NAMEPLATE_HI1) |
                      cv2.inRange(hsv, _NAMEPLATE_LO2, _NAMEPLATE_HI2))
        if float(strong_red.sum()) / 255.0 / total > 0.10:
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
    """Upscale + grayscale + CLAHE — for pytesseract (general purpose)."""
    up    = _upscale(crop)
    gray  = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _preprocess_nameplate(crop: np.ndarray) -> np.ndarray:
    """Isolate white text on nameplate background before OCR.

    Good-team player nameplates use a blue (H≈90-135) background.
    Evil-team player nameplates use a red (H≈0-12 or 163-180) background.
    Both carry white text.  The surrounding area is Minecraft terrain which
    confuses pytesseract PSM 7 with texture patterns that look like letters.

    Strategy:
      1. Upscale 3× for better OCR resolution.
      2. HSV-mask both blue and red nameplate regions (union).
      3. Tight-crop: find the largest connected coloured blob and discard
         everything outside its bounding box.  The nameplate is one solid
         compact rectangle; terrain noise is small scattered fragments.
         This removes the main source of OCR garbage without needing to
         know anything about screen layout.  Falls back to the full crop
         if no blob meets the minimum area threshold.
      4. Dilate slightly to capture adjacent white text pixels.
      5. Within that mask, flag high-brightness (≥180 V) pixels as text.
      6. Output: black text on white background (for tesseract dark-on-light).
    """
    up  = _upscale(crop)
    hsv = cv2.cvtColor(up, cv2.COLOR_BGR2HSV)

    # Blue nameplate background (good-team cards)
    blue = cv2.inRange(hsv,
                       np.array([ 90,  50,  50], dtype=np.uint8),
                       np.array([135, 255, 255], dtype=np.uint8))

    # Red nameplate background (evil-team cards; hue wraps around 0 in HSV)
    red1 = cv2.inRange(hsv,
                       np.array([  0,  50,  50], dtype=np.uint8),
                       np.array([ 12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv,
                       np.array([163,  50,  50], dtype=np.uint8),
                       np.array([180, 255, 255], dtype=np.uint8))
    red = cv2.bitwise_or(red1, red2)

    # Union of blue and red nameplate regions
    colored = cv2.bitwise_or(blue, red)

    # ── Tight crop to nameplate blob ──────────────────────────────────────────
    # The nameplate is a wide horizontal bar that spans most of the crop's
    # height.  Minecraft terrain noise produces only short, squat blobs.
    # Strategy: filter for blobs that are at least 30 % of the crop height
    # (nameplate passes; terrain noise doesn't), then among those take the
    # largest-area blob.  Falls back to the full crop when no qualifying blob
    # is found so the old behaviour is preserved for edge cases.
    contours, _ = cv2.findContours(colored, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    MIN_BLOB_H = up.shape[0] * 0.30   # 30 % of crop height after 3× upscale
    tall_blobs = [c for c in contours
                  if cv2.boundingRect(c)[3] >= MIN_BLOB_H
                  and cv2.contourArea(c) > 100]
    if tall_blobs:
        # The nameplate is always at the LEFT edge of the crop region.
        # Minecraft terrain may produce larger blobs further right.
        # Select the tall blob with the smallest bx (leftmost).
        best = min(tall_blobs, key=lambda c: cv2.boundingRect(c)[0])
        bx, by, bw, bh = cv2.boundingRect(best)
        pad = 10   # pixels of padding (in 3× space)
        bx1 = max(0, bx - pad)
        by1 = max(0, by - pad)
        bx2 = min(up.shape[1], bx + bw + pad)
        by2 = min(up.shape[0], by + bh + pad)
        up      = up     [by1:by2, bx1:bx2]
        colored = colored[by1:by2, bx1:bx2]

    # Expand mask to capture adjacent white text pixels
    kernel      = np.ones((5, 5), np.uint8)
    colored_exp = cv2.dilate(colored, kernel, iterations=2)

    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)

    # White text pixels inside (or touching) the nameplate region
    text_mask = np.zeros_like(gray)
    text_mask[(colored_exp > 0) & (gray > 180)] = 255

    # Black text on white background (tesseract default expectation)
    result = cv2.bitwise_not(text_mask)
    clahe  = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    return clahe.apply(result)


def _ocr_easyocr(crop: np.ndarray, reader) -> str:
    # Pass upscaled BGR — easyocr detects coloured text better in colour
    up      = _upscale(crop)
    results = reader.readtext(up, detail=1, paragraph=False)
    # Filter very low-confidence detections, join the rest
    texts = [r[1] for r in results if r[2] > 0.20]
    return " ".join(texts).strip()


def _ocr_pytesseract(crop: np.ndarray, nameplate: bool = False) -> str:
    import pytesseract
    from PIL import Image
    preprocessed = _preprocess_nameplate(crop) if nameplate else _preprocess_gray(crop)
    # Guard: if the processed image is blank (no text found by the blue-mask
    # step), tesseract will hallucinate characters from noise.  Skip OCR.
    if nameplate and int(np.sum(preprocessed < 250)) < 100:
        return ""
    img = Image.fromarray(preprocessed)
    return pytesseract.image_to_string(
        img, config="--oem 3 --psm 7 -c tessedit_char_blacklist=|{}<>"
    ).strip()


def ocr_crop(crop: np.ndarray, backend: str, reader,
             nameplate: bool = False) -> str:
    """Run OCR on *crop*; return text or '' on any failure.

    Set *nameplate=True* for player-name and role crops so the blue-masking
    preprocessor is used (removes Minecraft background noise).
    """
    if crop is None or crop.size == 0:
        return ""
    try:
        if backend == "easyocr":
            return _ocr_easyocr(crop, reader)
        elif backend == "pytesseract":
            return _ocr_pytesseract(crop, nameplate=nameplate)
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
        # Coverage check: the OCR string must cover more than half the
        # candidate's length.  This prevents short garbled fragments (e.g.
        # "ine" from terrain noise) from fuzzy-matching long role names
        # (e.g. "tinker") purely by coincidence.
        if len(cn) > 0 and len(raw_n) <= len(cn) / 2:
            continue
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
    # Name persistence: when the name OCR is blank but the role OCR is clear,
    # attribute the role to the most recently confirmed player name (up to
    # NAME_PERSIST_S seconds ago).  This handles frames where the name is
    # momentarily unreadable but the role text is legible.
    # IMPORTANT: keep this well below MIN_FRAME_GAP (5 s) so persistence
    # does NOT bleed across consecutive player cards.  Each new card starts
    # ≈5 s after the previous detection, so 4 s expires in time.
    NAME_PERSIST_S = 4.0
    last_name:      str | None = None
    last_name_t:    float      = -NAME_PERSIST_S - 1.0
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

                # name uses standard CLAHE (no blue mask — avatar fills most of crop)
                # role crops use blue-mask preprocessing to reject Minecraft background
                name_raw = ocr_crop(name_crop, backend, reader, nameplate=False)
                bel_raw  = ocr_crop(bel_crop,  backend, reader, nameplate=True)
                act_raw  = ocr_crop(act_crop,  backend, reader, nameplate=True)

                if debug_dir:
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_name.png"),    name_crop)
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_believed.png"), bel_crop)
                    cv2.imwrite(str(debug_dir / f"t{t:.1f}_actual.png"),   act_crop)

                print(f"    raw OCR -> name={name_raw!r}  "
                      f"believed={bel_raw!r}  actual={act_raw!r}")

            name      = best_match(name_raw, players)
            bel_left  = best_match(bel_raw,  roles)   # left panel
            act_right = best_match(act_raw,  roles)   # right panel

            # Update name persistence tracker on a clean name read
            if name:
                last_name  = name
                last_name_t = t
            # Fall back to last confirmed name when OCR momentarily blanks —
            # the card is still showing the same player (e.g. parallax / fade).
            elif last_name and (t - last_name_t) <= NAME_PERSIST_S:
                name = last_name

            # The LEFT panel ALWAYS shows the player's true game role.
            # The RIGHT panel has two distinct meanings:
            #   a) DECEPTIVE roles (Drunk, Lunatic, Marionette):
            #        Left  = actual role  (e.g. "Drunk")
            #        Right = believed role (e.g. "Empath" — what they think they are)
            #   b) All other situations — right panel must be IGNORED:
            #        - Demon bluffs (3 good-role cards shown to the demon)
            #        - Info-role reveals (e.g. Investigator seeing "Spy")
            #        - Role-pick prompts (e.g. Boffin choosing a role)
            # Storing the right panel for non-deceptive roles would record
            # a bluff/info role instead of the player's real role.
            if bel_left and bel_left.lower() in DECEPTIVE_ROLES:
                actual_role   = bel_left
                believed_role = act_right or bel_left
            else:
                actual_role   = bel_left
                believed_role = bel_left

            # Require a confirmed player-name match (name is not None).
            # Entries where name OCR didn't fuzzy-match any known player are
            # almost certainly noise (bluff cards, info-target cards, etc.) and
            # storing them as phantom players corrupts the roster.
            if name and believed_role:
                deception = (f"  [DECEIVED: believed={believed_role}]"
                             if actual_role and believed_role
                             and actual_role != believed_role else "")
                print(f"    -> name={name!r}  actual={actual_role!r}{deception}")
                detections.append({
                    "name":          name,
                    "believed_role": (believed_role or "unknown").lower(),
                    # Never fall back to raw OCR text for stored roles — garbled OCR
                    # (e.g. "Ome Sige Or") is worse than "unknown" for downstream use.
                    "actual_role":   (actual_role or "unknown").lower(),
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
      2. Among non-deceptive entries: FIRST occurrence wins.
         Rationale: the player's own intro card always appears before any
         secondary cards (demon bluff cards, info-role target cards, etc.).
         Keeping the first reading avoids overwriting the correct demon role
         with a subsequent bluff role that also passes is_intro_frame().

    After per-player deduplication, a second pass collapses entries that
    share the same actual_role.  BotC roles are unique within a script, so
    two different "players" with the same role within a 60-second window are
    almost certainly the same card OCR'd with a garbled name on one frame.
    The entry with the earlier frame_time wins.
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
            # elif not old_deceptive and not new_deceptive: keep first (do nothing)
            # else: old is deceptive, new is not — keep old

    # Second pass: collapse duplicate roles (same actual_role, different player names).
    # Only suppress if the second detection occurs within 60s of the first — a tighter
    # window avoids suppressing genuinely distinct players with the same role across
    # different card appearances.
    by_role: dict[str, dict] = {}
    result: list[dict] = []
    ROLE_DEDUP_WINDOW_S = 60.0
    for d in sorted(by_name.values(), key=lambda x: x["frame_time"]):
        role = d["actual_role"].lower()
        if role == "unknown":
            result.append(d)
            continue
        if role not in by_role:
            by_role[role] = d
            result.append(d)
        else:
            prev = by_role[role]
            if d["frame_time"] - prev["frame_time"] <= ROLE_DEDUP_WINDOW_S:
                # Likely the same card read twice with different name OCR — suppress.
                print(f"    [role-dedup] dropping {d['name']!r}/{role} at t={d['frame_time']}"
                      f" (same role as {prev['name']!r} at t={prev['frame_time']})")
            else:
                # Far enough apart — probably a different game section; keep it.
                by_role[role] = d
                result.append(d)
    return result


# ── CLI entry point ──────────────────────────────────────────────────────────

_VIDEO_EXTS = ("mp4", "webm", "mkv", "avi", "mp4.webm")


def _scrape_one(video_id: str, backend, reader,
                debug: bool = False, force: bool = False,
                force_manual: bool = False) -> bool:
    """Scrape a single video. Returns True on success, False on skip."""
    out_dir  = Path(f"outputs/{video_id}")
    out_json = out_dir / "intro_roster.json"

    if out_json.exists() and not force:
        print(f"  [SKIP] intro_roster.json already exists (use --force to overwrite)")
        return False

    # Never overwrite manually-curated entries — they represent known-correct ground
    # truth that automated OCR cannot reliably reproduce.  Pass --force-manual to
    # override (intended only for explicit re-curation, not routine batch runs).
    if out_json.exists() and not force_manual:
        try:
            _existing = json.loads(out_json.read_text(encoding="utf-8"))
            if _existing.get("source") == "manual_entry":
                print(f"  [SKIP] intro_roster.json is a manual_entry — use --force-manual to overwrite")
                return False
        except Exception:
            pass  # malformed JSON: fall through and re-scrape

    video_path = None
    for ext in _VIDEO_EXTS:
        p = out_dir / f"video.{ext}"
        if p.exists():
            video_path = p
            break
    if video_path is None:
        print(f"  [SKIP] No video file found in {out_dir}/ — run download first")
        return False

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
    return True


def _init_ocr_with_test():
    """Initialise OCR and run smoke-test. Returns (backend, reader)."""
    backend, reader = _init_ocr()
    print("\n--- OCR smoke-test ---")
    if backend:
        test_ocr(backend, reader)
    else:
        print("  (no backend — skipping smoke-test)")
    print()
    return backend, reader


def main(video_id: str = "lF96Jd3Eaeg",
         debug: bool = False,
         force: bool = False,
         force_manual: bool = False,
         test_only: bool = False) -> None:
    print("=== scrape_intro.py ===\n")
    backend, reader = _init_ocr_with_test()
    if test_only:
        return
    _scrape_one(video_id, backend, reader, debug=debug, force=force,
                force_manual=force_manual)


def main_all(debug: bool = False, force: bool = False,
             force_manual: bool = False) -> None:
    """Scrape all videos that have a downloaded video file, reusing one OCR instance."""
    outputs = Path("outputs")
    video_ids = sorted(
        d.name for d in outputs.iterdir()
        if d.is_dir() and any((d / f"video.{ext}").exists() for ext in _VIDEO_EXTS)
    )
    print(f"=== scrape_intro.py --all ===\n")
    print(f"Found {len(video_ids)} video(s) with downloaded video files.")

    backend, reader = _init_ocr_with_test()
    print(f"OCR backend: {backend}\n")

    ok, skipped, errors = 0, 0, []
    for i, vid in enumerate(video_ids, 1):
        print(f"\n[{i}/{len(video_ids)}] {vid}")
        try:
            if _scrape_one(vid, backend, reader, debug=debug, force=force,
                           force_manual=force_manual):
                ok += 1
            else:
                skipped += 1
        except Exception as exc:
            print(f"  ERROR: {exc}")
            errors.append((vid, exc))

    print(f"\n{'=' * 60}")
    print(f"Done.  {ok} scraped  |  {skipped} skipped  |  {len(errors)} failed")
    if errors:
        print("Failed:", [v for v, _ in errors])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Scrape player intro roster from BotC video UI overlays"
    )
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg",
                    help="YouTube video ID (omit when using --all)")
    ap.add_argument("--all", action="store_true", dest="all_videos",
                    help="Scrape all videos that have a downloaded video file")
    ap.add_argument("--debug", action="store_true",
                    help="Save cropped debug images to outputs/<id>/intro_debug/")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing intro_roster.json (skips manual_entry files)")
    ap.add_argument("--force-manual", action="store_true", dest="force_manual",
                    help="Also overwrite intro_roster.json files with source=manual_entry")
    ap.add_argument("--test", action="store_true",
                    help="Initialise OCR, run smoke-test, then exit (no video needed)")
    args = ap.parse_args()
    if args.all_videos:
        main_all(debug=args.debug, force=args.force, force_manual=args.force_manual)
    else:
        main(args.video_id, debug=args.debug, force=args.force,
             force_manual=args.force_manual, test_only=args.test)
