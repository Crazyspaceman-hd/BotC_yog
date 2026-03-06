import argparse
import json
from pathlib import Path

import pandas as pd

from pipeline_utils import parse_rttm, best_speaker


def main(video_id: str) -> None:
    out_dir = Path(f"outputs/{video_id}")
    whisper = out_dir / "whisper_segments.jsonl"
    rttm = out_dir / "diarization.rttm"
    out = out_dir / "segments.csv"

    turns = parse_rttm(rttm)

    rows = []
    for line in whisper.read_text(encoding="utf-8").splitlines():
        seg = json.loads(line)
        spk = best_speaker(seg["start"], seg["end"], turns)
        rows.append({"start": seg["start"], "end": seg["end"], "speaker": spk, "text": seg["text"]})

    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    print("Wrote:", out)
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Merge Whisper segments with RTTM diarization")
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg",
                    help="YouTube video ID")
    main(ap.parse_args().video_id)
