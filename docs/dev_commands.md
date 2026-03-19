# Dev Commands — Quick Reference

## Per-video pipeline
```bash
python run_pipeline.py <video_id>                          # full pipeline
python run_pipeline.py <video_id> --steps transcribe merge patch analyze
python run_pipeline.py <video_id> --steps transcribe merge patch analyze --force
python run_pipeline.py --all                               # all pending public videos
```

## Intro-card scraping
```bash
python scrape_intro.py <video_id>                          # scrape one video
python scrape_intro.py <video_id> --force                  # re-scrape (skips manual_entry files)
python scrape_intro.py <video_id> --force-manual           # re-scrape, overwriting manual_entry too
python scrape_intro.py --all --force                       # re-scrape all (skips manual_entry)
# Note: video IDs starting with '-' require: python scrape_intro.py --force -- <video_id>
```

## Curation
```bash
python auto_assign_speakers.py                             # dry run
python auto_assign_speakers.py --apply                     # write roster_overrides.json
streamlit run fix_rosters.py                               # manual speaker/OCR fixes (MANUAL GATE)
python analyze_roles.py --all                              # re-run analysis after curation
python compare_rosters.py                                  # diff DB roster vs episode spreadsheet
```

## Dataset rebuild
```bash
python build_db.py                                         # rebuild botc.db from outputs/
python generate_landing.py                                 # rebuild landing.html
bash deploy_pages.sh                                       # push landing to gh-pages
python validate.py                                         # end-state check (28 checks)
python validate.py --strict                                # fail on warnings too
python validate.py --json                                  # machine-readable output
```

## Optional enrichments (N0/N1/N2/N2b/N3/N4/N5)
```bash
python scan_frames.py <video_id>                           # N0: visual phase signal extraction
python speaker_consistency.py <video_id>                   # N1: smooth diarization
python detect_phases.py <video_id>                         # N2: phase boundary detection
python detect_phases.py --force -- <video_id>              # N2: re-run (use -- for dash-prefix IDs e.g. -Ejl5ODVNg0)
python generate_context_segments.py <video_id>             # N2b: context labelling (run after N2)
python generate_context_segments.py --all                  # N2b: run on all videos with phase_labels
python generate_context_segments.py <video_id> --force     # N2b: re-run, overwriting existing output
python extract_claims.py <video_id>                        # N3: claim/event extraction
python extract_player_status.py <video_id>                 # N4: player death / status tracking
python extract_player_status.py <video_id> --force         # N4: re-run, overwriting existing output
python extract_player_status.py --all                      # N4: run on all analyzed videos
python extract_execution_context.py <video_id>             # N5: public execution-context events (run after N2b)
python extract_execution_context.py <video_id> --force     # N5: re-run, overwriting existing output
python extract_execution_context.py --all                  # N5: run on all analyzed videos
```

## Full orchestrated run
```bash
bash scripts/run_all.sh
```

## Utilities
```bash
python fetch_playlist.py                                   # refresh playlist.json
python retranscribe_all.py                                 # bulk re-transcribe
python batch_transcribe.py                                 # batch transcription with tracking
python batch_downstream.py                                 # batch downstream (merge/patch/analyze)
streamlit run explore.py                                   # full editor (local)
streamlit run explore_public.py                            # read-only viewer
gh pr create --base develop                                # open PR to develop
```
