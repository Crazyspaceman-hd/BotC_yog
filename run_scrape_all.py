"""One-off: re-run scrape_intro on every video that has a video file.
Initialises OCR once and reuses the reader across all videos.
"""
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scrape_intro

EXTS = ("mp4", "webm", "mkv", "avi", "mp4.webm")

# ── Build video list ───────────────────────────────────────────────────────────
playlist = json.load(open("playlist.json"))
entries = playlist if isinstance(playlist, list) else playlist.get("entries", playlist.get("videos", []))

videos = [
    e["id"] for e in entries
    if any((Path("outputs") / e["id"] / f"video.{ext}").exists() for ext in EXTS)
]

print(f"Will scrape {len(videos)} videos...\n", flush=True)

# ── Initialise OCR once ────────────────────────────────────────────────────────
print("--- Initialising OCR (once) ---", flush=True)
backend, reader = scrape_intro._init_ocr()
if backend:
    scrape_intro.test_ocr(backend, reader)
print(f"OCR backend: {backend}\n", flush=True)

# ── Load shared vocab ──────────────────────────────────────────────────────────
players_global = scrape_intro.load_lines(Path("players.txt"))

# ── Process each video ────────────────────────────────────────────────────────
ok, skipped, failed = 0, 0, []

for i, vid in enumerate(videos, 1):
    print(f"\n[{i}/{len(videos)}] {vid}", flush=True)
    out_dir  = Path(f"outputs/{vid}")
    out_json = out_dir / "intro_roster.json"

    # Find video file
    video_path = None
    for ext in EXTS:
        p = out_dir / f"video.{ext}"
        if p.exists():
            video_path = p
            break
    if video_path is None:
        print(f"  [SKIP] No video file found", flush=True)
        skipped += 1
        continue

    # Load per-video roles if available, else fall back to global
    roles_file = out_dir / "roles.txt"
    if not roles_file.exists():
        roles_file = Path("roles.txt")
    roles = scrape_intro.load_lines(roles_file)
    print(f"  {video_path.name}  |  {len(players_global)} players, {len(roles)} roles", flush=True)

    try:
        raw     = scrape_intro.scrape_video(video_path, players_global, roles,
                                             debug_dir=None,
                                             backend=backend, reader=reader)
        entries = scrape_intro.deduplicate(raw)

        print(f"\n  => {len(entries)} player(s) found:", flush=True)
        for e in entries:
            deception = (f"  <- believed {e['believed_role']}"
                         if e["actual_role"] != e["believed_role"] else "")
            print(f"     t={e['frame_time']:6.1f}s  {e['name']:<20s}  "
                  f"actual: {e['actual_role']}{deception}", flush=True)

        output = {
            "source":     "video_ocr",
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
            "video_id":   vid,
            "players":    entries,
        }
        out_json.write_text(
            json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Wrote {len(entries)} entries -> {out_json}", flush=True)
        ok += 1

    except Exception as e:
        print(f"  ERROR: {e}", flush=True)
        traceback.print_exc()
        failed.append(vid)

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"Done.  {ok} OK  |  {skipped} skipped (no video)  |  {len(failed)} failed")
if failed:
    print("Failed:", failed)
