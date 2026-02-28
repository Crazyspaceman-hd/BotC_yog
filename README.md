# BotC_yog

Automated pipeline for downloading, transcribing, diarizing, and analysing Yogscast Blood on the Clocktower (BotC) YouTube videos.

---

## How it works

The pipeline runs seven sequential steps per video:

| # | Step | What it does |
|---|------|--------------|
| 1 | **download** | Downloads audio and video from YouTube via yt-dlp, resamples audio to 16 kHz mono for diarization |
| 2 | **transcribe** | Transcribes audio to timestamped segments using faster-whisper |
| 3 | **diarize** | Labels each audio segment with a speaker ID using NVIDIA NeMo |
| 4 | **merge** | Combines Whisper transcription with NeMo speaker labels into a single CSV |
| 5 | **patch** | Fuzzy-matches player names in the transcript to fix Whisper transcription errors |
| 6 | **scrape** | OCRs the intro UI overlay to extract each player's name and role assignment |
| 7 | **analyze** | Cross-references statements against roles to detect lies, writes results to CSV |

All outputs land in `outputs/<video_id>/`.

---

## Prerequisites

Install these before anything else:

- **Python 3.10+**
- **ffmpeg** — must be on your `PATH` (used for audio extraction and resampling)
- **yt-dlp** — installed via `requirements_pipeline.txt`, but must also be available as a CLI command for member-only video auth
- **Tesseract** or **easyocr** — optional, required for the `scrape` step
- **CUDA-capable GPU** — optional but strongly recommended for the `diarize` step (NeMo is very slow on CPU)

---

## Installation

Two separate virtual environments are used to avoid dependency conflicts between the main pipeline and NeMo:

```bash
# Main pipeline (download, transcribe, merge, patch, scrape, analyze)
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements_pipeline.txt

# Web UI only (Streamlit apps)
# Can share .venv above, or install separately:
pip install -r requirements.txt

# Diarization only (heavy GPU dependencies)
python -m venv .venv_botc
.venv_botc\Scripts\activate       # Windows
pip install -r d_requirements.txt
```

Use `.venv` for all steps except `diarize`. Switch to `.venv_botc` when running `diarize_nemo.py` or the `diarize` step through `run_pipeline.py`.

---

## First-time setup

Fetch the playlist metadata before processing any videos. This writes `playlist.json` to the project root and tracks the processing status of each video.

```bash
python fetch_playlist.py --url "https://www.youtube.com/playlist?list=<PLAYLIST_ID>"
```

On subsequent runs the URL is cached in `playlist.json`, so you can just run:

```bash
python fetch_playlist.py
```

---

## Running the pipeline

### Single video — all steps

```bash
python run_pipeline.py <video_id>
```

### Single video — specific steps only

```bash
python run_pipeline.py <video_id> --steps download transcribe
python run_pipeline.py <video_id> --steps diarize merge patch
```

Available step names: `download` `transcribe` `diarize` `merge` `patch` `scrape` `analyze`

### Force re-run (overwrite existing outputs)

```bash
python run_pipeline.py <video_id> --force
python run_pipeline.py <video_id> --steps transcribe --force
```

### Process all pending videos in the playlist

```bash
python run_pipeline.py --all
python run_pipeline.py --all --steps transcribe merge
python run_pipeline.py --all --force
```

### Member-only videos

Option A — let yt-dlp pull cookies from your browser:

```bash
python run_pipeline.py <video_id> --browser chrome
python run_pipeline.py <video_id> --browser firefox
python run_pipeline.py <video_id> --browser edge
```

Option B — place a `cookies.txt` file (Netscape format) in the project root. It is detected automatically for all yt-dlp calls.

---

## Pipeline steps reference

> **Audio format warning:** The `download` step produces two files:
> - `audio.wav` — full-quality stereo (44.1 kHz, 2-channel)
> - `audio_16k.wav` — **16 kHz mono** (required by NeMo diarization)
>
> `diarize_nemo.py` reads `audio_16k.wav` directly. **Providing stereo audio breaks diarization entirely.** If you ever supply audio manually, convert it first:
> ```bash
> ffmpeg -i input.wav -ac 1 -ar 16000 outputs/<video_id>/audio_16k.wav
> ```

| Step | Primary script | Output file |
|------|---------------|-------------|
| download | yt-dlp (orchestrated by run_pipeline.py) | `audio.wav`, `audio_16k.wav`, `video.mp4` |
| transcribe | `transcribe.py` | `whisper_segments.jsonl` |
| diarize | `diarize_nemo.py` | `diarization.rttm` |
| merge | `merge_segments.py` | `segments.csv` |
| patch | `patch_transcript.py` | `segments_patched.csv` |
| scrape | `scrape_intro.py` | `intro_roster.json` |
| analyze | `analyze_roles.py` | `lie_analysis.csv` |

---

## Running individual scripts

Each script can also be run standalone, outside of `run_pipeline.py`.

### fetch_playlist.py

```bash
python fetch_playlist.py --url "https://www.youtube.com/playlist?list=<ID>"  # first run
python fetch_playlist.py                                                       # subsequent runs
```

### transcribe.py

```bash
python transcribe.py <video_id>
```

### diarize_nemo.py

```bash
# Activate .venv_botc first
python diarize_nemo.py <video_id>
```

### merge_segments.py

```bash
python merge_segments.py <video_id>
```

### patch_transcript.py

```bash
python patch_transcript.py <video_id>
```

### scrape_intro.py

```bash
python scrape_intro.py <video_id>
python scrape_intro.py <video_id> --debug   # also save cropped debug images to intro_debug/
python scrape_intro.py <video_id> --force   # overwrite existing intro_roster.json
```

### analyze_roles.py

```bash
python analyze_roles.py <video_id>
```

### calibrate_scraper.py

Visual tool for tuning the OCR crop regions in `scrape_intro.py`. Writes annotated PNG images to `outputs/<video_id>/calibration/`.

```bash
python calibrate_scraper.py <video_id>              # annotate detected intro frames
python calibrate_scraper.py <video_id> --all        # annotate all sampled frames
python calibrate_scraper.py <video_id> --at 47.5    # frame closest to 47.5 s
python calibrate_scraper.py <video_id> --window 300 # sample only the first 300 s
python calibrate_scraper.py <video_id> --hsv        # print dominant HSV values in badge area
```

### build_db.py

Rebuilds `botc.db` from all pipeline outputs. Run this after any `analyze_roles.py` or `scrape_intro.py` run to sync the database used by the web UI. Also imports `roster_overrides.json` (manual role edits from `explore.py`) into the `speaker_map` table.

```bash
python build_db.py           # rebuild botc.db (default)
python build_db.py --db PATH # write to a custom DB path
```

---

## Web UI

Two Streamlit apps are available for exploring and editing results:

```bash
streamlit run explore.py        # full editor — changes are saved to disk
streamlit run explore_public.py # read-only — safe to share/publish
```

---

## Output folder layout

```
outputs/
└── <video_id>/
    ├── audio.wav               # full-quality stereo audio
    ├── audio_16k.wav           # 16 kHz mono (diarization input)
    ├── video.mp4               # downloaded video
    ├── whisper_segments.jsonl  # raw Whisper transcription
    ├── diarization.rttm        # NeMo speaker segments
    ├── segments.csv            # merged transcript + speaker labels
    ├── segments_patched.csv    # transcript after name/role patching
    ├── intro_roster.json       # OCR'd player roster from intro overlay
    ├── lie_analysis.csv        # final lie detection output
    ├── intro_debug/            # debug crops from scrape_intro (--debug)
    ├── calibration/            # annotated frames from calibrate_scraper
    └── nemo_work/              # NeMo intermediate files
```

---

## Configuration files

| File | Purpose |
|------|---------|
| `players.txt` | One Yogscast player name per line — used as Whisper `initial_prompt` and for fuzzy matching |
| `roles.txt` | One BotC role name per line — used for fuzzy matching in scrape and patch steps |
| `nemo_diar.yaml` | NeMo diarization hyperparameters (sample rate, clustering settings, model paths) |
| `cookies.txt` | Netscape-format cookies for member-only YouTube video access (auto-detected if present) |
| `playlist.json` | Cached playlist metadata and per-video processing status (written by `fetch_playlist.py`) |
| `botc.db` | SQLite database — rebuilt by `build_db.py`; read by the web UI (`explore_public.py`) |
