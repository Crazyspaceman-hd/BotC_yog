# transcribe.py
import argparse
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel

# Tweak these if you want faster/slower
MODEL_SIZE = "small"   # change to "medium" later if you want
DEVICE = "cuda"
COMPUTE = "float16"


def fmt_ts(sec: float) -> str:
    m = int(sec // 60)
    s = sec - 60 * m
    return f"{m:02d}:{s:05.2f}"


def main(video_id: str) -> None:
    out_dir = Path(f"outputs/{video_id}")
    audio = out_dir / "audio.wav"
    out = out_dir / "whisper_segments.jsonl"

    out_dir.mkdir(parents=True, exist_ok=True)

    # Build initial_prompt from players.txt + per-game or global roles.txt
    _players_file = Path("players.txt")
    _roles_file = out_dir / "roles.txt"
    if not _roles_file.exists():
        _roles_file = Path("roles.txt")

    if _players_file.exists() and _roles_file.exists():
        _players = [l.strip() for l in _players_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        _roles   = [l.strip() for l in _roles_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        initial_prompt = (
            "Players: " + ", ".join(_players) + ". "
            "Roles: " + ", ".join(_roles) + "."
        )
        print(f"initial_prompt loaded ({len(_players)} players, {len(_roles)} roles)")
    else:
        initial_prompt = None
        print("No players.txt / roles.txt found — transcribing without initial_prompt.")

    print(f"Audio: {audio}")
    print(f"Out:   {out}")
    print(f"Model: {MODEL_SIZE} ({DEVICE}, {COMPUTE})")
    print("Starting transcription...\n")

    t0 = time.time()
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE)

    segments, info = model.transcribe(
        str(audio),
        vad_filter=True,
        beam_size=5,
        initial_prompt=initial_prompt,
    )

    count = 0
    last_print = time.time()

    with out.open("w", encoding="utf-8") as f:
        for seg in segments:
            rec = {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count += 1

            # Print progress every ~2 seconds
            now = time.time()
            if now - last_print >= 2.0:
                elapsed = now - t0
                audio_done = float(seg.end)
                speed = (audio_done / elapsed) if elapsed > 0 else 0.0
                eta = ((info.duration - audio_done) / speed) if speed > 0 else float("inf")
                print(
                    f"[{count:5d} seg] audio {fmt_ts(audio_done)} / {fmt_ts(info.duration)} "
                    f"({audio_done/info.duration*100:5.1f}%) | "
                    f"{speed:4.2f}x realtime | ETA ~ {fmt_ts(eta) if eta != float('inf') else '??:??.??'}",
                    flush=True,
                )
                last_print = now

    t1 = time.time()
    print(f"\nDone. Wrote {count} segments in {t1 - t0:.1f}s -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Transcribe audio with Whisper")
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg",
                    help="YouTube video ID")
    main(ap.parse_args().video_id)
