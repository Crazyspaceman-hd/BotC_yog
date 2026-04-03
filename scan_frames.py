"""scan_frames.py — N0 (frame_scan): Visual game-phase signal extraction.

Samples video.mp4 at regular intervals and detects two persistent UI elements
using HSV colour thresholding on normalised crop regions (resolution-independent):

  header_bar   — Compact player role-bar at top-centre of screen.
                 Absent in building interiors and during night role-card views.
                 Detected by: blue/red nameplate colour spread horizontally
                 across >= 3 of 6 column buckets in the top strip.

  votes_table  — Nomination voting table (floating white text, far right).
                 Visible only while nominations/voting is active.
                 Persists across multiple nominations in the same day.
                 Detected by: white-pixel density in the far-right column.

Both detectors reuse the same HSV ranges tuned in scrape_intro.py for the
Good (blue) / Evil (red) team badges — no separate calibration needed.

Output:
    outputs/<video_id>/frame_scan.json

    {
      "video_id": "...",
      "sample_interval_s": 30,
      "total_duration_s": 3942,
      "frames": [
        {"t": 0.0,  "header_visible": false, "votes_visible": false},
        {"t": 30.0, "header_visible": true,  "votes_visible": false},
        ...
      ]
    }

Usage:
    python scan_frames.py <video_id>
    python scan_frames.py <video_id> --interval 15
    python scan_frames.py <video_id> --force
    python scan_frames.py <video_id> --debug-t 2400   # annotated frame dump
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

# ── Crop regions (normalised x1,y1,x2,y2 fractions of frame dimensions) ──────
# Header bar: thin top strip, centre-width only.
# Excludes the far-left/right margins where per-player night role-cards appear.
_HDR_X1, _HDR_Y1, _HDR_X2, _HDR_Y2 = 0.12, 0.02, 0.88, 0.10

# Votes table: far-right column, mid-height.
# Inventory UI and role-cards never reach this x > 0.83 region.
_VOT_X1, _VOT_Y1, _VOT_X2, _VOT_Y2 = 0.83, 0.15, 1.00, 0.45

# Minimum fraction of 6 horizontal column-buckets that must carry signal for
# the header to be considered visible.  Role-cards cluster in ≤ 2 left columns.
_HDR_MIN_COLS = 3
_HDR_MIN_FRAC = 0.02   # total coloured-pixel fraction within the strip

# White-pixel fraction threshold for the votes region.
_VOT_WHITE_THRESH = 0.030

# ── HSV thresholds (same as scrape_intro.py) ──────────────────────────────────
_GOOD_LO  = np.array([95,  60,  60],  dtype=np.uint8)
_GOOD_HI  = np.array([138, 255, 255], dtype=np.uint8)
_EVIL_LO1 = np.array([0,   90,  90],  dtype=np.uint8)
_EVIL_HI1 = np.array([12,  255, 255], dtype=np.uint8)
_EVIL_LO2 = np.array([163, 90,  90],  dtype=np.uint8)
_EVIL_HI2 = np.array([180, 255, 255], dtype=np.uint8)


def _crop(frame: np.ndarray, x1: float, y1: float,
          x2: float, y2: float) -> np.ndarray:
    h, w = frame.shape[:2]
    return frame[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]


def detect_header(frame: np.ndarray) -> tuple[bool, float, int]:
    """Return (visible, colour_frac, active_cols_of_6)."""
    crop = _crop(frame, _HDR_X1, _HDR_Y1, _HDR_X2, _HDR_Y2)
    hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, _GOOD_LO, _GOOD_HI)
    red  = (cv2.inRange(hsv, _EVIL_LO1, _EVIL_HI1) |
            cv2.inRange(hsv, _EVIL_LO2, _EVIL_HI2))
    mask = ((blue > 0) | (red > 0)).astype(np.uint8)
    frac = mask.sum() / mask.size
    cols = np.array_split(mask, 6, axis=1)
    active = sum(1 for c in cols if c.sum() > 0)
    visible = frac >= _HDR_MIN_FRAC and active >= _HDR_MIN_COLS
    return visible, float(frac), active


def detect_votes(frame: np.ndarray) -> tuple[bool, float]:
    """Return (visible, white_frac)."""
    crop  = _crop(frame, _VOT_X1, _VOT_Y1, _VOT_X2, _VOT_Y2)
    gray  = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    white = (gray > 200).sum() / gray.size
    return white >= _VOT_WHITE_THRESH, float(white)


def scan(video_id: str, interval_s: float = 30.0,
         force: bool = False, debug_t: float | None = None) -> None:
    out_dir  = Path("outputs") / video_id
    out_json = out_dir / "frame_scan.json"

    # Accept any video file the download step may have left: video.mp4, video.webm, etc.
    video = next(
        (p for p in sorted(out_dir.glob("video.*"))
         if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".avi", ".mov"}),
        None,
    )
    if video is None:
        print(f"  [SKIP] no video file for {video_id}")
        return

    if out_json.exists() and not force:
        print(f"  [SKIP] frame_scan.json already exists for {video_id} (--force to redo)")
        return

    cap  = cv2.VideoCapture(str(video))
    fps  = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = total_frames / fps
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"  Video: {w}x{h}, {duration:.0f}s ({duration/60:.1f}m), "
          f"sampling every {interval_s:.0f}s")

    if debug_t is not None:
        _dump_debug_frame(cap, debug_t, out_dir, w, h)
        cap.release()
        return

    timestamps = list(np.arange(0.0, duration, interval_s))
    results: list[dict] = []
    t0 = time.time()

    for i, t in enumerate(timestamps):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            continue

        hdr_vis, hdr_frac, hdr_cols = detect_header(frame)
        vot_vis, vot_frac           = detect_votes(frame)

        results.append({
            "t":              round(t, 1),
            "header_visible": bool(hdr_vis),
            "votes_visible":  bool(vot_vis),
        })

        if (i + 1) % 20 == 0 or i == len(timestamps) - 1:
            pct  = (i + 1) / len(timestamps) * 100
            elapsed = time.time() - t0
            eta  = elapsed / (i + 1) * (len(timestamps) - i - 1)
            print(f"    [{i+1:4d}/{len(timestamps)}] {pct:5.1f}% | "
                  f"t={t:.0f}s hdr={'Y' if hdr_vis else 'n'}({hdr_cols}/6) "
                  f"votes={'Y' if vot_vis else 'n'} | ETA {eta:.0f}s",
                  flush=True)

    cap.release()

    payload = {
        "video_id":         video_id,
        "sample_interval_s": interval_s,
        "total_duration_s": round(duration, 1),
        "frames":           results,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    hdr_count  = sum(1 for r in results if r["header_visible"])
    vot_count  = sum(1 for r in results if r["votes_visible"])
    elapsed    = time.time() - t0
    print(f"  Done in {elapsed:.1f}s — {len(results)} frames sampled")
    print(f"  header_visible: {hdr_count}/{len(results)} samples")
    print(f"  votes_visible:  {vot_count}/{len(results)} samples")
    print(f"  Written -> {out_json}")


def _dump_debug_frame(cap: cv2.VideoCapture, t: float,
                      out_dir: Path, w: int, h: int) -> None:
    """Save an annotated frame with crop-region overlays for calibration."""
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ret, frame = cap.read()
    if not ret:
        print(f"  Could not seek to t={t}s")
        return

    hdr_vis, hdr_frac, hdr_cols = detect_header(frame)
    vot_vis, vot_frac           = detect_votes(frame)
    annotated = frame.copy()

    def rect(x1f, y1f, x2f, y2f, colour, label):
        x1, y1 = int(w * x1f), int(h * y1f)
        x2, y2 = int(w * x2f), int(h * y2f)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(annotated, label, (x1 + 4, y1 + 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2, cv2.LINE_AA)

    hdr_col = (0, 255, 0) if hdr_vis else (0, 80, 255)
    vot_col = (0, 255, 0) if vot_vis else (0, 80, 255)
    rect(_HDR_X1, _HDR_Y1, _HDR_X2, _HDR_Y2, hdr_col,
         f"HEADER {'YES' if hdr_vis else 'NO'} frac={hdr_frac:.3f} cols={hdr_cols}/6")
    rect(_VOT_X1, _VOT_Y1, _VOT_X2, _VOT_Y2, vot_col,
         f"VOTES {'YES' if vot_vis else 'NO'} white={vot_frac:.3f}")

    debug_path = out_dir / "calibration" / f"scan_debug_{int(t)}s.jpg"
    debug_path.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(debug_path), annotated)
    print(f"  Debug frame -> {debug_path}")
    print(f"  header: {'VISIBLE' if hdr_vis else 'hidden'} "
          f"(frac={hdr_frac:.4f}, cols={hdr_cols}/6)")
    print(f"  votes:  {'VISIBLE' if vot_vis else 'hidden'} "
          f"(white={vot_frac:.4f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_id",    help="YouTube video ID")
    ap.add_argument("--interval",  type=float, default=30.0,
                    help="Seconds between frame samples (default: 30)")
    ap.add_argument("--force",     action="store_true",
                    help="Re-run even if frame_scan.json already exists")
    ap.add_argument("--debug-t",   type=float, metavar="T",
                    help="Dump a single annotated debug frame at timestamp T (seconds)")
    args = ap.parse_args()
    scan(args.video_id, interval_s=args.interval,
         force=args.force, debug_t=args.debug_t)
