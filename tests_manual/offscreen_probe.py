"""Offscreen paint probe: stand the window up with an empty manifests dir and
walk the cheap gestures (no capability loads) — the paint-path verification
layer pytest cannot give (67335f7d), Qt edition. Run from a NEUTRAL cwd:

    QT_QPA_PLATFORM=offscreen python offscreen_probe.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cjm_transcription_qt.app import TranscriptionWindow  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qt-probe-"))
    manifests = tmp / "manifests"
    manifests.mkdir()
    media = tmp / "media"
    media.mkdir()
    (media / "clip.mp3").write_bytes(b"\x00")
    app = QApplication(sys.argv[:1])
    win = TranscriptionWindow(str(manifests), start_dir=str(tmp),
                              runs_dir=str(tmp / "runs"))
    win.show()
    assert win.stage == "sources"
    assert win.src_list.count() >= 1, "browser rows painted"
    # descend into media/, toggle the file, walk back up (rows are sorted, so
    # find the media/ row rather than assuming it is first)
    media_row = next(i for i in range(win.src_list.count())
                     if "media/" in win.src_list.item(i).text())
    win.src_list.setCurrentRow(media_row)
    win.on_select()                      # enter media/
    assert win.browser.cwd == media
    win.on_select()                      # toggle clip.mp3
    assert win.browser.selected, "file toggled into selection"
    assert "Selected (1" in win.src_selection.text()
    win.on_updir()
    # n -> candidates (none installed: red placeholder row, no picks)
    win.on_next_stage()
    assert win.stage == "candidates"
    assert win.cand_list.count() == 1
    win.on_next_stage()                  # no picks -> error in status
    assert win.error == "pick at least one candidate instance"
    win.on_prev_stage()
    assert win.stage == "sources"
    # results view (empty runs dir -> placeholder)
    win.on_results()
    assert win.stage == "results"
    assert win.res_list.count() == 1
    win.on_prev_stage()
    assert win.stage == "sources"
    # collection toggle + status chips painted
    win.on_collection_none()
    assert "--no-collection" in win.src_selection.text()
    assert "NOT JOURNALED" in win.chip_journal.text()
    assert "no diarization" in win.chip_speakers.text()
    win.on_quit()
    app.processEvents()
    print("offscreen probe OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
