"""Console-script driver for the Qt shell: the SAME argument surface,
resolution ladder, and headless hand-off as every transcription shell — all
three imported from cjm_transcription_core.launch (build_parser /
resolve_settings / hand_off; absorbed there by spine absorption 12f342f1),
so the shells cannot drift on the reproducibility contract. Only the window
in the middle differs."""

import sys

from cjm_substrate_qt_kit.theme import apply_theme
from cjm_transcription_core.launch import build_parser, hand_off, resolve_settings
from PySide6.QtWidgets import QApplication

from .app import TranscriptionWindow


def main() -> int:  # Console-script entry point (cjm-transcription-qt)
    """Resolve the shared setup surface, run the Qt setup window, hand off."""
    parser = build_parser()
    parser.prog = "cjm-transcription-qt"
    args = parser.parse_args()
    s = resolve_settings(args)
    qapp = QApplication(sys.argv[:1])
    apply_theme(qapp)
    win = TranscriptionWindow(s["manifests_dir"], start_dir=s["start_dir"],
                              runs_dir=s["runs_dir"],
                              initial_sources=args.paths or None,
                              sysmon_capability=s["sysmon_capability"],
                              graph_capability=s["graph_capability"],
                              graph_db_path=s["graph_db_path"],
                              initial_picks=s["state"].get("picked_instance_ids"),
                              initial_bookmarks=s["state"].get("bookmarks"),
                              preprocessing_capability=s["preprocessing_capability"],
                              diarization_capability=s["diarization_capability"],
                              max_segment_duration=args.max_segment_duration)
    win.show()
    qapp.exec()
    return hand_off(win.plan, args)
