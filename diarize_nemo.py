# diarize_nemo.py
import argparse
import json
import time
import threading
from pathlib import Path

import torch
import nemo.collections.asr.data.audio_to_label as atl
from omegaconf import OmegaConf
from nemo.collections.asr.models import ClusteringDiarizer

CFG_YAML = Path("nemo_diar.yaml")

# --- Patch NeMo VAD collate to force waveform tensors to 1D -------------------
_orig_vad_collate = atl._vad_frame_seq_collate_fn

def _patched_vad_collate(self, batch):
    fixed = []
    for item in batch:
        # Some collate inputs can be non-(tuple/list) in edge cases; pass through.
        if not isinstance(item, (tuple, list)) or len(item) == 0:
            fixed.append(item)
            continue

        sig = item[0]

        if torch.is_tensor(sig):
            # Normalize shapes like [1, T] or [T, 1] -> [T]
            if sig.dim() == 2 and (sig.size(0) == 1 or sig.size(1) == 1):
                sig = sig.squeeze()

            # If still not 1D, flatten defensively
            if sig.dim() > 1:
                sig = sig.reshape(-1)

            item = list(item)
            item[0] = sig
            item = tuple(item)

        fixed.append(item)

    return _orig_vad_collate(self, fixed)

atl._vad_frame_seq_collate_fn = _patched_vad_collate
# -----------------------------------------------------------------------------


def _heartbeat(stop_evt: threading.Event, work_dir: Path) -> None:
    while not stop_evt.is_set():
        newest = None
        newest_mtime = -1
        for p in work_dir.rglob("*"):
            try:
                if p.is_file():
                    mt = p.stat().st_mtime
                    if mt > newest_mtime:
                        newest_mtime = mt
                        newest = p
            except FileNotFoundError:
                pass
        if newest:
            print(f"…working (latest file: {newest.relative_to(work_dir)})", flush=True)
        else:
            print("…working", flush=True)
        stop_evt.wait(10)


def main(video_id: str) -> None:
    out_dir = Path(f"outputs/{video_id}")
    audio = out_dir / "audio_16k.wav"
    work = out_dir / "nemo_work"
    out_rttm = out_dir / "diarization.rttm"

    work.mkdir(parents=True, exist_ok=True)

    manifest = {
        "audio_filepath": str(audio),
        "offset": 0,
        "duration": None,
        "label": "infer",
        "text": "",
        "num_speakers": 9,
        "rttm_filepath": None,
        "uem_filepath": None,
    }
    (work / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    print(f"Audio:  {audio}")
    print(f"Work:   {work}")
    print(f"Config: {CFG_YAML}")
    print("Starting NeMo diarization (heartbeat every 10s)...\n")

    cfg = OmegaConf.load(str(CFG_YAML))

    cfg.device = "cuda"
    cfg.num_workers = 0
    cfg.diarizer.manifest_filepath = str(work / "manifest.json")
    cfg.diarizer.out_dir = str(work)

    cfg.diarizer.oracle_vad = False
    cfg.diarizer.vad.model_path = "vad_multilingual_marblenet"
    cfg.diarizer.speaker_embeddings.model_path = "titanet_large"
    cfg.diarizer.clustering.parameters.oracle_num_speakers = False

    stop = threading.Event()
    t = threading.Thread(target=_heartbeat, args=(stop, work), daemon=True)
    t.start()

    t0 = time.time()
    try:
        diarizer = ClusteringDiarizer(cfg=cfg)
        diarizer.diarize()
    finally:
        stop.set()
        t.join(timeout=1)

    print(f"\nFinished diarization in {time.time() - t0:.1f}s")

    rttms = list(work.glob("**/*.rttm"))
    if not rttms:
        raise RuntimeError("No RTTM produced. Check NeMo logs above.")

    rttms.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    rttms[0].replace(out_rttm)
    print("Wrote:", out_rttm)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Diarize audio with NeMo")
    ap.add_argument("video_id", nargs="?", default="lF96Jd3Eaeg", help="YouTube video ID")
    main(ap.parse_args().video_id)