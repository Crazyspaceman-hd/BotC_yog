"""
scripts/run_dataset_refresh.py
------------------------------
Authoritative dataset-refresh path for BotC_yog.

Runs the cross-video curation and publish phase in dependency order:

    auto_assign_speakers  (heuristic speaker linking)
    --> MANUAL GATE: fix_rosters (if issues remain)
    --> analyze_roles --all  (re-run analysis after curation)
    --> build_db             (rebuild SQLite from all outputs)
    --> generate_landing     (rebuild landing.html)
    --> [deploy_pages.sh]    (optional gh-pages push)
    --> validate             (confirm end-state)

This script does NOT run per-video extraction (download/transcribe/diarize/
merge/patch/scrape). For that, use run_pipeline.py or scripts/run_all.sh.

Usage:
    python scripts/run_dataset_refresh.py
    python scripts/run_dataset_refresh.py --skip-assign   # speakers already curated
    python scripts/run_dataset_refresh.py --skip-analyze  # lies already up to date
    python scripts/run_dataset_refresh.py --skip-deploy   # don't push to gh-pages
    python scripts/run_dataset_refresh.py --no-pause      # non-interactive (CI mode)

Environment:
    Must be run from the project root directory (where playlist.json lives).
    Python must be the .venv interpreter:
        .venv/Scripts/python.exe scripts/run_dataset_refresh.py
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run(cmd: list[str], step_name: str) -> None:
    print(f"\n[refresh] --- {step_name} ---")
    print(f"[refresh] $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n[refresh] FAILED at step: {step_name} (exit {result.returncode})")
        print("[refresh] Fix the issue above and re-run. Steps before this one do not need to be repeated.")
        sys.exit(result.returncode)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-assign",   action="store_true", help="Skip auto_assign_speakers (speakers already curated)")
    ap.add_argument("--skip-analyze",  action="store_true", help="Skip analyze_roles --all (lie analysis already current)")
    ap.add_argument("--skip-deploy",   action="store_true", help="Skip deploy_pages.sh (don't push to gh-pages)")
    ap.add_argument("--no-pause",      action="store_true", help="Skip the manual-gate pause (non-interactive/CI mode)")
    args = ap.parse_args()

    print("[refresh] BotC_yog Dataset Refresh")
    print("[refresh] " + "=" * 50)
    print(f"[refresh] Root: {ROOT}")
    print(f"[refresh] Python: {PYTHON}")

    # ── Step 1: Heuristic speaker assignment ──────────────────────────────────
    if not args.skip_assign:
        run([PYTHON, "auto_assign_speakers.py"], "auto_assign_speakers (dry run)")

        if not args.no_pause:
            print()
            print("[refresh] >>> MANUAL GATE <<<")
            print("[refresh] Review the speaker assignment proposals above.")
            print("[refresh]   Apply CERTAIN+HIGH:  python auto_assign_speakers.py --apply")
            print("[refresh]   Apply +MEDIUM:        python auto_assign_speakers.py --apply --include-medium")
            print("[refresh]   Fix remaining:        streamlit run fix_rosters.py")
            print("[refresh]   Then re-run this script with --skip-assign.")
            print("[refresh]")
            print("[refresh] Press Enter to continue with current state, or Ctrl+C to stop.")
            try:
                input()
            except KeyboardInterrupt:
                print("\n[refresh] Stopped at manual gate.")
                sys.exit(0)
        else:
            print("[refresh] --no-pause: skipping manual gate (CI mode)")
    else:
        print("[refresh] Skipping auto_assign_speakers (--skip-assign)")

    # ── Step 2: Re-run lie detection ──────────────────────────────────────────
    if not args.skip_analyze:
        run([PYTHON, "analyze_roles.py", "--all"], "analyze_roles --all")
    else:
        print("\n[refresh] Skipping analyze_roles --all (--skip-analyze)")

    # ── Step 3: Rebuild DB ────────────────────────────────────────────────────
    run([PYTHON, "build_db.py"], "build_db")

    # ── Step 4: Regenerate landing page ──────────────────────────────────────
    run([PYTHON, "generate_landing.py"], "generate_landing")

    # ── Step 5: Deploy (optional) ─────────────────────────────────────────────
    if not args.skip_deploy:
        deploy_sh = ROOT / "deploy_pages.sh"
        if deploy_sh.exists():
            run(["bash", str(deploy_sh)], "deploy_pages.sh")
        else:
            print("\n[refresh] deploy_pages.sh not found — skipping deploy")
    else:
        print("\n[refresh] Skipping deploy (--skip-deploy)")

    # ── Step 6: Validate ──────────────────────────────────────────────────────
    run([PYTHON, "validate.py"], "validate")

    print()
    print("[refresh] Dataset refresh complete.")
    print("[refresh] If any WARNs remain, see the manual gate items in validate output above.")


if __name__ == "__main__":
    main()
