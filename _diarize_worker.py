"""_diarize_worker.py - Run by diarize_nemo.py in an isolated venv.

Usage (internal — called by diarize_nemo.py):
    .venv_diar/Scripts/python _diarize_worker.py <video_id>
"""
import os
import sys
from pathlib import Path

import torch
from pyannote.audio import Pipeline

video_id = sys.argv[1]
audio    = Path(f"outputs/{video_id}/audio.wav")
out_rttm = Path(f"outputs/{video_id}/diarization.rttm")

if not audio.exists():
    sys.exit(f"Audio not found: {audio}")

hf_token = os.environ.get("HF_TOKEN", True)

print(f"Audio: {audio}")
print(f"Out:   {out_rttm}")
print("Loading pyannote/speaker-diarization-3.1 ...")

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    token=hf_token,
)

if torch.cuda.is_available():
    pipeline = pipeline.to(torch.device("cuda"))
    print("Running on GPU")
else:
    print("Running on CPU")

print("Running diarization ...\n")
diarization = pipeline({"uri": video_id, "audio": str(audio.resolve())})

out_rttm.parent.mkdir(parents=True, exist_ok=True)
with out_rttm.open("w") as f:
    diarization.write_rttm(f)

print(f"Wrote: {out_rttm}")
