# Dev Commands — Quick Reference

## Per-video pipeline
```bash
python run_pipeline.py <video_id>                          # full pipeline
python run_pipeline.py <video_id> --steps transcribe merge patch analyze
python run_pipeline.py <video_id> --steps transcribe merge patch analyze --force
python run_pipeline.py --all                               # all pending public videos
```

## Curation
```bash
python auto_assign_speakers.py                             # dry run
python auto_assign_speakers.py --apply                     # write roster_overrides.json
streamlit run fix_rosters.py                               # manual speaker/OCR fixes (MANUAL GATE)
python analyze_roles.py --all                              # re-run analysis after curation
```

## Dataset rebuild
```bash
python build_db.py                                         # rebuild botc.db from outputs/
python generate_landing.py                                 # rebuild landing.html
bash deploy_pages.sh                                       # push landing to gh-pages
python validate.py                                         # end-state check (25+ checks)
python validate.py --strict                                # fail on warnings too
```

## Optional enrichments (N1/N2/N3)
```bash
python speaker_consistency.py <video_id>                   # N1: smooth diarization
python detect_phases.py <video_id>                         # N2: phase boundary detection
python extract_claims.py <video_id>                        # N3: claim/event extraction
```

## Full orchestrated run
```bash
bash scripts/run_all.sh
```

## Utilities
```bash
python fetch_playlist.py                                   # refresh playlist.json
python retranscribe_all.py                                 # bulk re-transcribe
streamlit run explore.py                                   # full editor (local)
streamlit run explore_public.py                            # read-only viewer
gh pr create --base develop                                # open PR
```
