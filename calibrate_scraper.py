"""calibrate_scraper.py - Visual tool to tune scrape_intro.py crop regions.

Samples frames from a video, runs the badge-colour detector, and for every
frame that passes (or every N-th frame with --all) saves an annotated PNG
with the current LAYOUT crop-boxes drawn on it.

The annotated images are written to:
    outputs/<VIDEO_ID>/calibration/

Open them in any image viewer to see whether each box covers the right text.

Usage:
    # Annotate all detected intro frames
    python calibrate_scraper.py <video_id>

    # Annotate ALL sampled frames (even non-intro), useful if detector misses
    python calibrate_scraper.py <video_id> --all

    # Annotate the frame closest to a specific timestamp (seconds)
    python calibrate_scraper.py <video_id> --at 47.5

    # Override sample window (default 720 s)
    python calibrate_scraper.py <video_id> --window 300

    # Show dominant HSV colours in the badge area (helps tune thresholds)
    python calibrate_scraper.py <video_id> --hsv
"""

import argparse
from pathlib import Path

import cv2
import numpy as np

# Import the constants we want to calibrate
from scrape_intro import (
    LAYOUT,
    INTRO_WINDOW,
    SAMPLE_INTERVAL,
    _GOOD_LO, _GOOD_HI,
    _EVIL_LO1, _EVIL_HI1,
    _EVIL_LO2, _EVIL_HI2,
    is_intro_frame,
    _crop,
)

# Colours for each drawn box (BGR)
BOX_COLORS = {
    "badge_area":    (200, 200,   0),   # yellow
    "player_name":   (0,   200,   0),   # green
    "believed_role": (0,   200, 200),   # cyan
    "actual_role":   (200,   0, 200),   # magenta
}

LABEL_FONT  = cv2.FONT_HERSHEY_SIMPLEX
LABEL_SCALE = 0.45
LABEL_THICK = 1


def draw_regions(frame: np.ndarray) -> np.ndarray:
    """Return a copy of *frame* with all LAYOUT boxes drawn on it."""
    out = frame.copy()
    h, w = out.shape[:2]

    for name, (x1f, y1f, x2f, y2f) in LAYOUT.items():
        x1, y1 = int(w * x1f), int(h * y1f)
        x2, y2 = int(w * x2f), int(h * y2f)
        col = BOX_COLORS.get(name, (255, 255, 255))
        cv2.rectangle(out, (x1, y1), (x2, y2), col, 2)
        cv2.putText(out, name, (x1 + 3, max(y1 + 14, 14)),
                    LABEL_FONT, LABEL_SCALE, col, LABEL_THICK, cv2.LINE_AA)

    return out


def dominant_hsv(region: np.ndarray, n: int = 5) -> list[tuple]:
    """Return the *n* most common HSV pixel values in *region* (sampled)."""
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    flat = hsv.reshape(-1, 3)
    # Quantise to reduce noise
    flat = (flat // 10) * 10
    colours, counts = np.unique(flat, axis=0, return_counts=True)
    top_idx = np.argsort(counts)[::-1][:n]
    return [(tuple(colours[i]), int(counts[i])) for i in top_idx]


def find_video(out_dir: Path) -> Path | None:
    for ext in ("mp4", "webm", "mkv", "avi", "mp4.webm"):
        p = out_dir / f"video.{ext}"
        if p.exists():
            return p
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Visually calibrate scrape_intro.py crop regions"
    )
    ap.add_argument("video_id", help="YouTube video ID")
    ap.add_argument("--all",    action="store_true",
                    help="Annotate every sampled frame, not just intro detections")
    ap.add_argument("--at",     type=float, default=None,
                    help="Jump to a specific timestamp (seconds) and annotate that frame")
    ap.add_argument("--window", type=float, default=INTRO_WINDOW,
                    help=f"Sample window in seconds (default: {INTRO_WINDOW})")
    ap.add_argument("--hsv",    action="store_true",
                    help="Print dominant HSV values in the badge area for each frame")
    args = ap.parse_args()

    out_dir  = Path(f"outputs/{args.video_id}")
    cal_dir  = out_dir / "calibration"
    cal_dir.mkdir(parents=True, exist_ok=True)

    video_path = find_video(out_dir)
    if video_path is None:
        print(f"No video file found in {out_dir}/")
        return

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Cannot open {video_path}")
        return

    fps          = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step_frames  = max(1, int(fps * SAMPLE_INTERVAL))
    end_frame    = min(total_frames, int(fps * args.window))

    print(f"Video : {video_path.name}  ({fps:.1f} fps)")
    print(f"Output: {cal_dir}\n")

    # ── single-timestamp mode ─────────────────────────────────────────────────
    if args.at is not None:
        target = int(args.at * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        if not ok:
            print(f"Could not read frame at t={args.at:.1f}s")
            return
        ann = draw_regions(frame)
        fname = cal_dir / f"at_{args.at:.1f}s.png"
        cv2.imwrite(str(fname), ann)
        print(f"Saved annotated frame -> {fname}")

        if args.hsv:
            badge = _crop(frame, LAYOUT["badge_area"])
            print(f"\nTop HSV values in badge_area:")
            for hsv_val, cnt in dominant_hsv(badge):
                print(f"  H={hsv_val[0]:3d}  S={hsv_val[1]:3d}  V={hsv_val[2]:3d}  "
                      f"count={cnt}")
        cap.release()
        return

    # ── scan mode ─────────────────────────────────────────────────────────────
    saved = 0
    frame_no = 0
    while frame_no < end_frame:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ok, frame = cap.read()
        if not ok:
            break

        t = frame_no / fps
        detected = is_intro_frame(frame)

        if detected or args.all:
            tag = "INTRO" if detected else "frame"
            print(f"  t={t:6.1f}s  [{tag}]", end="")

            if args.hsv:
                badge = _crop(frame, LAYOUT["badge_area"])
                top   = dominant_hsv(badge, n=3)
                vals  = "  |  ".join(
                    f"H={v[0]:3d} S={v[1]:3d} V={v[2]:3d} (n={c})"
                    for v, c in top
                )
                print(f"  badge HSV: {vals}", end="")

            print()

            ann   = draw_regions(frame)
            fname = cal_dir / f"t{t:.1f}s_{'INTRO' if detected else 'frame'}.png"
            cv2.imwrite(str(fname), ann)

            # Also save individual crops for fine-grained inspection
            for region_name, region in LAYOUT.items():
                if region_name == "badge_area":
                    continue
                crop = _crop(frame, region)
                if crop.size > 0:
                    cv2.imwrite(
                        str(cal_dir / f"t{t:.1f}s_{region_name}.png"), crop
                    )

            saved += 1

        frame_no += step_frames

    cap.release()

    if saved == 0:
        print("\nNo frames matched. Try --all to annotate everything, or")
        print("--hsv to inspect badge-area colours and tune the HSV thresholds.")
    else:
        print(f"\nSaved {saved} annotated frame(s) to {cal_dir}/")
        print("\nRegion colours in annotated images:")
        for name, col in BOX_COLORS.items():
            r, g, b = col[2], col[1], col[0]
            print(f"  {name:<16s} = rgb({r},{g},{b})")
        print("\nAdjust the LAYOUT dict in scrape_intro.py until each box")
        print("tightly covers the intended text region.")


if __name__ == "__main__":
    main()
