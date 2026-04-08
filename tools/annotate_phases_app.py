"""
annotate_phases_app.py
======================
Lightweight human-in-the-loop phase annotation tool.

Lets you:
  • Select a video that has auto phase_labels.csv
  • Browse transcript segments by time
  • View current auto phase intervals and day events
  • Add / edit / delete manual phase intervals (Intro / Night / Day)
  • Add / edit / delete day-event markers
  • Seed from auto predictions (edit rather than label from scratch)
  • Save trusted ground-truth to data/annotations/phase_truth/<video_id>.json

Run:
    streamlit run tools/annotate_phases_app.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow imports from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st

from tools.build_annotation_bundle import build_bundle, list_annotatable_videos

TRUTH_DIR = REPO_ROOT / "data" / "annotations" / "phase_truth"
TRUTH_DIR.mkdir(parents=True, exist_ok=True)

VALID_PHASES = ["Intro", "Night", "Day"]
VALID_EVENTS = [
    "NominationStart",
    "VoteSequence",
    "ExecutionAnnouncement",
    "StorytellerInterruption",
    "DayEnd",
]


# ── persistence ───────────────────────────────────────────────────────────────

def truth_path(video_id: str) -> Path:
    return TRUTH_DIR / f"{video_id}.json"


def load_truth(video_id: str) -> dict | None:
    p = truth_path(video_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_truth(video_id: str, annotation: dict) -> None:
    p = truth_path(video_id)
    p.write_text(json.dumps(annotation, indent=2, ensure_ascii=False), encoding="utf-8")


# ── session state helpers ─────────────────────────────────────────────────────

def _init_state(bundle: dict) -> None:
    """Initialise working annotation in session state from saved truth or auto predictions."""
    vid = bundle["video_id"]

    # Only reinitialise when switching videos.
    if st.session_state.get("_loaded_video") == vid:
        return

    existing = load_truth(vid)
    if existing:
        phases = existing.get("phases", [])
        events = existing.get("events", [])
        source = "saved annotation"
    else:
        phases = [
            {
                "start": p["start"],
                "end": p["end"],
                "phase": p["phase"],
                "round": p["round"],
                "note": "",
            }
            for p in bundle["auto_phases"]
        ]
        events = [
            {
                "start": e["start"],
                "end": e["end"],
                "round": e["round"],
                "event": e["event"],
                "note": "",
            }
            for e in bundle["auto_day_events"]
        ]
        source = "seeded from auto"

    st.session_state["_loaded_video"] = vid
    st.session_state["phases"] = phases
    st.session_state["events"] = events
    st.session_state["_source"] = source
    st.session_state["_dirty"] = False


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


# ── main app ──────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="Phase Annotator", layout="wide")
    st.title("BotC Phase Annotation Tool")

    # ── Video selection ───────────────────────────────────────────────────────
    videos = list_annotatable_videos()
    if not videos:
        st.error("No videos with phase_labels.csv found in outputs/.")
        return

    video_options = {
        f"{v['video_id']}  —  {v['title'][:60]}": v["video_id"] for v in videos
    }
    saved_ids = {p.stem for p in TRUTH_DIR.glob("*.json")}
    labels_with_status = []
    for label, vid in video_options.items():
        marker = " ✓" if vid in saved_ids else ""
        labels_with_status.append(label + marker)

    chosen_label = st.sidebar.selectbox(
        "Select video", labels_with_status, index=0
    )
    # Strip the ✓ marker to look up the original key
    clean_label = chosen_label.replace(" ✓", "")
    video_id = video_options[clean_label]

    # ── Load bundle ───────────────────────────────────────────────────────────
    with st.spinner("Loading bundle…"):
        bundle = build_bundle(video_id)
    if bundle is None:
        st.error(f"Could not load outputs for {video_id}")
        return

    _init_state(bundle)

    duration = bundle["duration"]
    transcript = bundle["transcript"]

    st.sidebar.markdown(f"**Duration:** {_fmt_ts(duration)}")
    st.sidebar.markdown(f"**Segments:** {len(transcript)}")
    st.sidebar.markdown(f"**Winner:** {bundle['winner'] or '—'}")
    st.sidebar.caption(f"Source: {st.session_state['_source']}")

    if st.session_state["_dirty"]:
        st.sidebar.warning("Unsaved changes")

    # ── Save / Reset buttons ──────────────────────────────────────────────────
    col_save, col_reset = st.sidebar.columns(2)
    if col_save.button("💾 Save", use_container_width=True):
        annotation = _build_annotation_dict(video_id, bundle)
        save_truth(video_id, annotation)
        st.session_state["_dirty"] = False
        st.session_state["_source"] = "saved annotation"
        st.sidebar.success("Saved.")
    if col_reset.button("↺ Reset to auto", use_container_width=True):
        st.session_state["_loaded_video"] = None  # force reinit from auto
        st.rerun()

    # ── Layout: three columns ─────────────────────────────────────────────────
    col_left, col_mid, col_right = st.columns([2, 2, 3])

    # ── LEFT: Phase intervals ─────────────────────────────────────────────────
    with col_left:
        st.subheader("Phase Intervals")
        _render_phases_editor(duration)

    # ── MID: Day event markers ────────────────────────────────────────────────
    with col_mid:
        st.subheader("Day Event Markers")
        _render_events_editor(duration)

    # ── RIGHT: Transcript browser ─────────────────────────────────────────────
    with col_right:
        st.subheader("Transcript")
        _render_transcript(transcript, duration)

    # ── Auto predictions (collapsed reference) ───────────────────────────────
    with st.expander("Auto predictions (read-only reference)"):
        _render_auto_reference(bundle)


# ── phases editor ─────────────────────────────────────────────────────────────

def _render_phases_editor(duration: float) -> None:
    phases: list[dict] = st.session_state["phases"]

    # Add new phase row
    with st.expander("+ Add phase interval", expanded=False):
        c1, c2, c3, c4 = st.columns([1.5, 1.5, 1.5, 1])
        new_start = c1.number_input("Start (s)", 0.0, duration, 0.0, 1.0, key="np_start")
        new_end = c2.number_input("End (s)", 0.0, duration, min(60.0, duration), 1.0, key="np_end")
        new_phase = c3.selectbox("Phase", VALID_PHASES, key="np_phase")
        new_round = c4.number_input("Round", 0, 20, 0, key="np_round")
        new_note = st.text_input("Note", key="np_note")
        if st.button("Add phase", key="btn_add_phase"):
            if new_end > new_start:
                phases.append(
                    {
                        "start": round(new_start, 2),
                        "end": round(new_end, 2),
                        "phase": new_phase,
                        "round": int(new_round),
                        "note": new_note,
                    }
                )
                phases.sort(key=lambda x: x["start"])
                st.session_state["_dirty"] = True
                st.rerun()
            else:
                st.error("End must be after start.")

    # Render existing rows
    to_delete = []
    for i, p in enumerate(phases):
        with st.container():
            st.markdown(
                f"**{p['phase']}** &nbsp; R{p['round']} &nbsp; "
                f"`{_fmt_ts(p['start'])} – {_fmt_ts(p['end'])}`"
            )
            ec1, ec2, ec3, ec4, ec5 = st.columns([1.5, 1.5, 1.5, 1, 0.7])
            new_s = ec1.number_input(
                "Start", 0.0, float(p["end"]), float(p["start"]), 1.0,
                key=f"ps_{i}", label_visibility="collapsed"
            )
            new_e = ec2.number_input(
                "End", float(p["start"]), duration, float(p["end"]), 1.0,
                key=f"pe_{i}", label_visibility="collapsed"
            )
            new_ph = ec3.selectbox(
                "Phase", VALID_PHASES,
                index=VALID_PHASES.index(p["phase"]) if p["phase"] in VALID_PHASES else 0,
                key=f"pp_{i}", label_visibility="collapsed"
            )
            new_r = ec4.number_input(
                "Rnd", 0, 20, int(p["round"]),
                key=f"pr_{i}", label_visibility="collapsed"
            )
            if ec5.button("✕", key=f"pdel_{i}"):
                to_delete.append(i)

            new_note = st.text_input(
                "Note", p.get("note", ""),
                key=f"pn_{i}", label_visibility="collapsed",
                placeholder="optional note"
            )

            # Detect any change
            if (
                round(new_s, 2) != p["start"]
                or round(new_e, 2) != p["end"]
                or new_ph != p["phase"]
                or int(new_r) != p["round"]
                or new_note != p.get("note", "")
            ):
                phases[i] = {
                    "start": round(new_s, 2),
                    "end": round(new_e, 2),
                    "phase": new_ph,
                    "round": int(new_r),
                    "note": new_note,
                }
                st.session_state["_dirty"] = True

            st.divider()

    for i in sorted(to_delete, reverse=True):
        phases.pop(i)
        st.session_state["_dirty"] = True
    if to_delete:
        st.rerun()


# ── events editor ─────────────────────────────────────────────────────────────

def _render_events_editor(duration: float) -> None:
    events: list[dict] = st.session_state["events"]

    with st.expander("+ Add event marker", expanded=False):
        c1, c2, c3, c4 = st.columns([1.5, 1.5, 2, 1])
        ne_start = c1.number_input("Start (s)", 0.0, duration, 0.0, 1.0, key="ne_start")
        ne_end = c2.number_input("End (s)", 0.0, duration, min(30.0, duration), 1.0, key="ne_end")
        ne_event = c3.selectbox("Event", VALID_EVENTS, key="ne_event")
        ne_round = c4.number_input("Round", 0, 20, 1, key="ne_round")
        ne_note = st.text_input("Note", key="ne_note")
        if st.button("Add event", key="btn_add_event"):
            events.append(
                {
                    "start": round(ne_start, 2),
                    "end": round(ne_end, 2),
                    "round": int(ne_round),
                    "event": ne_event,
                    "note": ne_note,
                }
            )
            events.sort(key=lambda x: x["start"])
            st.session_state["_dirty"] = True
            st.rerun()

    to_delete = []
    for i, e in enumerate(events):
        st.markdown(
            f"**{e['event']}** &nbsp; R{e['round']} &nbsp; "
            f"`{_fmt_ts(e['start'])} – {_fmt_ts(e['end'])}`"
        )
        ec1, ec2, ec3, ec4, ec5 = st.columns([1.5, 1.5, 2, 1, 0.7])
        new_s = ec1.number_input(
            "Start", 0.0, duration, float(e["start"]), 1.0,
            key=f"es_{i}", label_visibility="collapsed"
        )
        new_e = ec2.number_input(
            "End", 0.0, duration, float(e["end"]), 1.0,
            key=f"ee_{i}", label_visibility="collapsed"
        )
        new_ev = ec3.selectbox(
            "Event", VALID_EVENTS,
            index=VALID_EVENTS.index(e["event"]) if e["event"] in VALID_EVENTS else 0,
            key=f"et_{i}", label_visibility="collapsed"
        )
        new_r = ec4.number_input(
            "Rnd", 0, 20, int(e["round"]),
            key=f"er_{i}", label_visibility="collapsed"
        )
        if ec5.button("✕", key=f"edel_{i}"):
            to_delete.append(i)

        new_note = st.text_input(
            "Note", e.get("note", ""),
            key=f"en_{i}", label_visibility="collapsed",
            placeholder="optional note"
        )

        if (
            round(new_s, 2) != e["start"]
            or round(new_e, 2) != e["end"]
            or new_ev != e["event"]
            or int(new_r) != e["round"]
            or new_note != e.get("note", "")
        ):
            events[i] = {
                "start": round(new_s, 2),
                "end": round(new_e, 2),
                "round": int(new_r),
                "event": new_ev,
                "note": new_note,
            }
            st.session_state["_dirty"] = True

        st.divider()

    for i in sorted(to_delete, reverse=True):
        events.pop(i)
        st.session_state["_dirty"] = True
    if to_delete:
        st.rerun()


# ── transcript browser ────────────────────────────────────────────────────────

def _render_transcript(transcript: list[dict], duration: float) -> None:
    if not transcript:
        st.info("No transcript segments found.")
        return

    c1, c2 = st.columns(2)
    t_start = c1.number_input("From (s)", 0.0, duration, 0.0, 30.0, key="tr_from")
    t_end = c2.number_input("To (s)", 0.0, duration, min(300.0, duration), 30.0, key="tr_to")
    filter_text = st.text_input("Filter text (case-insensitive)", key="tr_filter")

    visible = [
        r for r in transcript
        if r["start"] >= t_start and r["start"] <= t_end
        and (not filter_text or filter_text.lower() in r["text"].lower())
    ]

    st.caption(f"Showing {len(visible)} / {len(transcript)} segments")

    for r in visible:
        label = r.get("speaker", "")
        ts = _fmt_ts(r["start"])
        st.markdown(
            f"`{ts}` **{label}** — {r['text']}"
        )


# ── auto predictions reference ────────────────────────────────────────────────

def _render_auto_reference(bundle: dict) -> None:
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Auto phases**")
        for p in bundle["auto_phases"]:
            st.markdown(
                f"- `{_fmt_ts(p['start'])} – {_fmt_ts(p['end'])}` "
                f"**{p['phase']}** R{p['round']} "
                f"_(conf {p['confidence']:.2f})_ {p['evidence']}"
            )
    with col_b:
        st.markdown("**Auto day events**")
        for e in bundle["auto_day_events"]:
            st.markdown(
                f"- `{_fmt_ts(e['start'])}` **{e['event']}** R{e['round']} "
                f"_(conf {e['confidence']:.2f})_"
            )
        if bundle["frame_cues"]:
            st.markdown("**Frame cues (votes/header visible)**")
            for c in bundle["frame_cues"][:20]:
                flags = []
                if c["votes_visible"]:
                    flags.append("votes")
                if c["header_visible"]:
                    flags.append("header")
                st.markdown(f"- `{_fmt_ts(c['t'])}` {', '.join(flags)}")
            if len(bundle["frame_cues"]) > 20:
                st.caption(f"… {len(bundle['frame_cues']) - 20} more")


# ── annotation dict builder ───────────────────────────────────────────────────

def _build_annotation_dict(video_id: str, bundle: dict) -> dict:
    return {
        "video_id": video_id,
        "title": bundle["title"],
        "duration": bundle["duration"],
        "phases": st.session_state["phases"],
        "events": st.session_state["events"],
    }


if __name__ == "__main__":
    main()
