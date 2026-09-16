"""The Qt transcription-workflow shell: the same three-stage run setup as the
Textual TUI (SOURCES -> CANDIDATES -> COMPARE, plus the config sub-view and the
past-runs RESULTS view), repainted with real typography and handed the same
exit contract — confirm leaves a run plan for the CLI's headless hand-off, so
TUI-launched and Qt-launched runs stay byte-identical to hand-launched ones.

Shell-only by design (DEC dcf8a712): every stateful decision lives in the
imported spine (SourceBrowser / CollectionField / candidate directives /
SegmentProbe / RunIndex / ConfigForm) or in CapabilitySession, whose loop
thread owns the capability stack. This module materializes row dicts from
panes.py into Qt widgets and forwards key gestures — the Textual key
vocabulary carries over verbatim (j/k, enter, a, l, c, n, b, [ ], r, p, d, s,
v, x, h, m, ', escape, q). Text entry rides modal QInputDialogs, so the
single-letter shortcuts never fight a focused editor.

Async discipline (the c4b0d6e5 seam): stack loads, compares, preprocessing
loads, hash checks, and unload drains all resolve as loop-thread Futures landing
here through QUEUED Signals — the paint thread never blocks on capability work.
The blocked-reason QTimer mirrors the Textual _watch_blocked poll at the same
2s cadence."""

import asyncio
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cjm_substrate_qt_kit.findbar import FindBar
from cjm_substrate_qt_kit.keyhints import KeyHintsOverlay
from cjm_substrate_qt_kit.keymap import KeymapRegistry
from cjm_substrate_qt_kit.player import SpanPlayer
from cjm_substrate_qt_kit.statusstrip import StatusStrip
from cjm_substrate_qt_kit.style import apply_row_style
from cjm_substrate_qt_kit.theme import style_text_pane
from cjm_substrate_tui_kit.form import ConfigForm
from cjm_transcription_core.candidates import (candidate_directives, model_axis, spec_string,
                                               transcription_manifests)
from cjm_transcript_graph_schema.schema import source_node_id
from cjm_transcription_core.chunk import (DEFAULT_ESCALATION_MODEL_ID, flagged_chunks,
                                          render_escalation_prompt)
from cjm_transcription_core.cli import expand_sources
from cjm_transcription_core.models import PipelineConfig
from cjm_transcription_core.results import RunIndex
from cjm_transcription_core.sources import CollectionField, SourceBrowser
from cjm_transcription_core.state import save_state
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPlainTextEdit, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

from .capability_session import CapabilitySession
from .escalation import classify_refusal, refusal_line, resolve_decomp_core, respine_argv
from .panes import (candidate_rows, compare_header, compare_rows, config_header, config_rows,
                    cwd_label, drill_header, drill_source_rows, entry_rows, flag_chip, run_rows,
                    segment_text, selection_html)

HINTS = {
    "sources": "enter descend/toggle · a folder-source · c collection · x none · "
               "s speakers · v runs · h hash · m/' bookmark · backspace up · "
               "n next · q quit",
    "candidates": "enter/space toggle · c config · s speakers · n compare · "
                  "b back · q quit",
    "config": "enter edit/cycle · b back to candidates · q quit",
    "compare": "[ ] segment · p play · d preprocess · l lightweight · a accuracy · "
               "r re-probe · escape cancel · enter confirm+run · b back · q quit",
    "results": "enter open run · j/k walk · b back · q quit",
    "results_drill": "j/k source · [ ] segment · , . flagged · p play · t re-transcribe · "
                     "e escalate · i import paste · b runs list · q quit",
}


class TranscriptionWindow(QMainWindow):
    """Run-setup window; exit with `plan` set = the CLI hands off headless."""

    stack_opened = Signal(object)   # loop-thread Future -> Qt thread (queued)
    compare_done = Signal(object)
    preproc_done = Signal(object)
    hash_done = Signal(object)
    unload_done = Signal(object)
    blocked_read = Signal(object)
    chunk_done = Signal(object)     # worker-thread chunk verb (rerun / import) -> Qt thread

    def __init__(self, manifests_dir: str,
                 *, start_dir: str = ".",
                 initial_sources: Optional[List[str]] = None,
                 sysmon_capability: Optional[str] = None,
                 graph_capability: Optional[str] = None,
                 graph_db_path: Optional[str] = None,
                 initial_picks: Optional[List[str]] = None,
                 preprocessing_capability: Optional[str] = None,
                 diarization_capability: Optional[str] = None,
                 initial_bookmarks: Optional[List[str]] = None,
                 runs_dir: str = "runs",
                 max_segment_duration: float = 220.0):
        super().__init__()
        self.setWindowTitle("cjm transcription setup (qt)")
        self.resize(1080, 780)
        self.plan: Optional[Dict[str, Any]] = None  # set by confirm; CLI reads it
        self.manifests_dir = manifests_dir
        self.sysmon_capability = sysmon_capability
        self.graph_capability = graph_capability
        self.graph_db_path = graph_db_path
        self.max_segment_duration = max_segment_duration
        self.browser = SourceBrowser(start_dir)
        self.collection = CollectionField()
        for p in initial_sources or []:
            self.browser.toggle(Path(p))
        self.candidates = candidate_directives(manifests_dir)
        self.manifest_code = transcription_manifests(manifests_dir)
        self.cand_picked: List[int] = [i for i, c in enumerate(self.candidates)
                                       if c["default"]]
        if initial_picks:
            wanted = set(initial_picks)
            restored = [i for i, c in enumerate(self.candidates)
                        if c["instance_id"] in wanted]
            if restored:
                self.cand_picked = restored
        self.stage = "sources"
        self.busy: Optional[str] = None
        self._busy_base: Optional[str] = None  # blocked-suffix base text
        self.error: Optional[str] = None
        self.notice: Optional[str] = None
        self.form: Optional[ConfigForm] = None
        self.form_cand: Optional[int] = None
        self.seg_index = 0
        self.seg_count = 0
        self.rows: List[Dict[str, Any]] = []
        self.marks: Dict[str, Optional[str]] = {"lightweight": None, "accuracy": None}
        self.preprocessing_capability = preprocessing_capability
        self.preprocess_loaded = False
        self.diarization_capability = diarization_capability
        self.diarization_enabled = diarization_capability is not None
        self.run_index = RunIndex(runs_dir)
        self._run_counts: Dict[str, int] = {}
        self.results_run: Optional[int] = None
        self.results_seg = 0
        # The flagged-chunk lane (cf0b91d6 part 4): the drilled run's census
        # index, (source index, segment index) -> flagged rows, manifest order.
        self.flags: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        self.flag_keys: List[Tuple[int, int]] = []
        self.flags_all: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}  # incl. escalated (covered) chunks
        self.escalated_keys: List[Tuple[int, int]] = []
        self.last_prompt_hash: str = ""
        self._pending_import: Optional[Dict[str, Any]] = None  # a respine-routed import awaiting the verb's answer (0b4d5cfa (1))
        self.bookmarks: List[str] = list(initial_bookmarks or [])
        self.player: Optional[SpanPlayer] = None
        self.sess = CapabilitySession(manifests_dir,
                                      sysmon_capability=sysmon_capability)
        self.sess.start()
        self._build_widgets()
        self._bind_keys()
        self.stack_opened.connect(self._on_stack_opened)
        self.compare_done.connect(self._on_compare_done)
        self.preproc_done.connect(self._on_preproc_done)
        self.hash_done.connect(self._on_hash_done)
        self.unload_done.connect(lambda _f: self._refresh_status())
        self.blocked_read.connect(self._on_blocked_read)
        self.chunk_done.connect(self._on_chunk_done)
        self.blocked_timer = QTimer(self)
        self.blocked_timer.setInterval(2000)
        self.blocked_timer.timeout.connect(self._poll_blocked)
        self._blocked_inflight = False
        self.run_index.load()
        self._run_counts = self.run_index.counts_by_path()
        self._show_stage("sources")

    # ---- widget tree ----------------------------------------------------

    def _build_widgets(self) -> None:
        def page(*widgets) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8)
            for child in widgets:
                lay.addWidget(child)
            return w

        def label() -> QLabel:
            lab = QLabel()
            lab.setTextFormat(Qt.RichText)
            lab.setWordWrap(True)
            return lab

        def rowlist() -> QListWidget:
            lst = QListWidget()
            lst.setWordWrap(True)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            return lst

        def text_pane() -> QPlainTextEdit:
            pane = QPlainTextEdit()
            pane.setReadOnly(True)
            style_text_pane(pane, live=True)
            return pane

        self.src_cwd = label()
        self.src_list = rowlist()
        self.src_list.currentRowChanged.connect(self._sync_browser_cursor)
        self.src_list.itemActivated.connect(lambda _i: self.on_select())
        self.src_selection = label()
        self.sources_page = page(self.src_cwd, self.src_list, self.src_selection)

        self.cand_head = label()
        self.cand_head.setText("<b>Candidate (capability, MODEL) instances — "
                               "manifest-derived</b>")
        self.cand_list = rowlist()
        self.cand_list.itemActivated.connect(lambda _i: self.on_select())
        self.candidates_page = page(self.cand_head, self.cand_list)

        self.cfg_head = label()
        self.cfg_list = rowlist()
        self.cfg_list.currentRowChanged.connect(self._paint_config_help)
        self.cfg_help = label()
        self.config_page = page(self.cfg_head, self.cfg_list, self.cfg_help)

        self.cmp_head = label()
        self.cmp_busy = label()   # FULL busy/error text wraps here (5a682f6f)
        self.cmp_list = rowlist()
        self.cmp_list.currentRowChanged.connect(lambda _r: self._paint_transcript())
        self.cmp_text = text_pane()
        cmp_split = QSplitter(Qt.Vertical)
        cmp_split.addWidget(self.cmp_list)
        cmp_split.addWidget(self.cmp_text)
        cmp_split.setStretchFactor(0, 1)
        cmp_split.setStretchFactor(1, 2)
        self.compare_page = page(self.cmp_head, self.cmp_busy, cmp_split)

        self.res_head = label()
        self.res_list = rowlist()
        self.res_list.itemActivated.connect(lambda _i: self.on_select())
        self.res_list.currentRowChanged.connect(self._on_results_row_changed)
        self.res_text = text_pane()
        res_split = QSplitter(Qt.Vertical)
        res_split.addWidget(self.res_list)
        res_split.addWidget(self.res_text)
        res_split.setStretchFactor(0, 1)
        res_split.setStretchFactor(1, 2)
        self.results_page = page(self.res_head, res_split)

        self.stack = QStackedWidget()
        for p in (self.sources_page, self.candidates_page, self.config_page,
                  self.compare_page, self.results_page):
            self.stack.addWidget(p)
        self.findbar = FindBar(self.res_text)  # focus-follow covers cmp_text too
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.stack, 1)
        outer.addWidget(self.findbar)
        self.strip = StatusStrip()
        outer.addWidget(self.strip)
        self.hints_overlay = KeyHintsOverlay(self)
        self.setCentralWidget(central)
        # Status chips: journal + speakers ALWAYS visible (drive-1 discipline),
        # the unload chip while a background drain holds VRAM — all on the
        # strip's chip row now (DEC 2a42c028 — QStatusBar retired).

    def _bind_keys(self) -> None:
        # Kit KeymapRegistry (adoption rung caa33c98): declarative table =
        # discovery surface; guard_text_entry keeps character verbs out of the
        # FindBar field.
        self.keymap = KeymapRegistry(self)
        add = self.keymap.add
        add("next", "Next row", "J", lambda: self.move_cursor(1), group="Rows")
        add("prev", "Previous row", "K", lambda: self.move_cursor(-1),
            group="Rows")
        add("select", "Select / descend", "Return", self.on_select,
            group="Rows")
        add("select-space", "Select (Space)", "Space", self.on_select,
            group="Rows")
        add("updir", "Up a directory", "Backspace", self.on_updir,
            group="Rows")
        add("add", "Add / assign", "A", self.on_key_a, group="Rows")
        add("mark-light", "Mark light", "L", self.on_mark_light, group="Rows")
        add("config", "Config page", "C", self.on_config, group="Stage")
        add("next-stage", "Next stage", "N", self.on_next_stage, group="Stage")
        add("prev-stage", "Previous stage", "B", self.on_prev_stage,
            group="Stage")
        add("rerun", "Re-run", "R", self.on_rerun, group="Stage")
        add("preprocess", "Preprocess", "D", self.on_preprocess, group="Stage")
        add("diarization", "Diarization", "S", self.on_diarization,
            group="Stage")
        add("results", "Results view", "V", self.on_results, group="Stage")
        add("segment-prev", "Previous segment", "[",
            lambda: self.on_segment(-1), group="Audio")
        add("segment-next", "Next segment", "]", lambda: self.on_segment(1),
            group="Audio")
        add("play", "Play", "P", self.on_play, group="Audio")
        # The flagged-chunk lane (cf0b91d6 part 4; the 65791933 mark-jump shape).
        add("flag-next", "Next flagged chunk", ".", lambda: self.on_flag(1),
            group="Audio")
        add("flag-prev", "Previous flagged chunk", ",", lambda: self.on_flag(-1),
            group="Audio")
        add("chunk-rerun", "Re-transcribe this chunk", "T", self.on_chunk_rerun,
            group="Stage")
        add("escalate", "Escalate chunk (copy prompt, open audio)", "E",
            self.on_escalate, group="Stage")
        add("import-paste", "Import a pasted transcript for this chunk", "I",
            self.on_import_transcript, group="Stage")
        add("collection-none", "Clear collection", "X", self.on_collection_none,
            group="App")
        add("hash-check", "Hash check", "H", self.on_hash_check, group="App")
        add("bookmark", "Bookmark", "M", self.on_bookmark, group="App")
        add("jump-bookmark", "Jump to bookmark", "'", self.on_jump_bookmark,
            group="App")
        add("cancel", "Cancel", "Escape", self.on_cancel, group="App")
        add("quit", "Quit", "Q", self.on_quit, group="App")
        add("keys", "Keyboard hints", "?", self.hints_overlay.toggle,
            group="App")
        add("find", "Find in text pane", "Ctrl+F", self.open_find)
        add("find-next", "Find next", "F3", self.findbar.next)
        add("find-previous", "Find previous", "Shift+F3", self.findbar.previous)
        self.keymap.guard_text_entry()

    def open_find(self) -> None:
        """Ctrl+F: find over the visible text pane (kit FindBar; focus-follow
        re-attaches when the user clicks into another searchable pane)."""
        pane = self.cmp_text if self.stage == "compare" else self.res_text
        self.findbar.attach(pane)
        self.findbar.open()

    # ---- painting -------------------------------------------------------

    def _populate(self, lst: QListWidget, rows: List[Dict[str, Any]],
                  keep_row: Optional[int] = None) -> None:
        """Materialize row dicts; UserRole carries each row's spine index."""
        lst.blockSignals(True)   # repaint is not a cursor gesture
        lst.clear()
        for r in rows:
            item = QListWidgetItem(str(r.get("text", "")))
            apply_row_style(item, r.get("style"))
            item.setData(Qt.UserRole, r.get("index"))
            lst.addItem(item)
        if lst.count():
            row = keep_row if keep_row is not None else 0
            lst.setCurrentRow(max(0, min(row, lst.count() - 1)))
        lst.blockSignals(False)

    def _active_list(self) -> QListWidget:
        return {"sources": self.src_list, "candidates": self.cand_list,
                "config": self.cfg_list, "compare": self.cmp_list,
                "results": self.res_list}[self.stage]

    def _show_stage(self, stage: str) -> None:
        self.stage = stage
        pages = {"sources": self.sources_page, "candidates": self.candidates_page,
                 "config": self.config_page, "compare": self.compare_page,
                 "results": self.results_page}
        self._paint_stage()
        self.stack.setCurrentWidget(pages[stage])
        self._active_list().setFocus()
        if stage == "compare":
            self.blocked_timer.start()
        else:
            self.blocked_timer.stop()
        self._refresh_status()

    def _paint_stage(self) -> None:
        if self.stage == "sources":
            self._paint_sources()
        elif self.stage == "candidates":
            keep = self.cand_list.currentRow()
            self._populate(self.cand_list,
                           candidate_rows(self.candidates, self.cand_picked,
                                          self.manifest_code),
                           keep_row=keep if keep >= 0 else None)
        elif self.stage == "config":
            self._paint_config()
        elif self.stage == "compare":
            self._paint_compare()
        elif self.stage == "results":
            self._paint_results()

    def _paint_sources(self) -> None:
        self.src_cwd.setText(cwd_label(self.browser, self.bookmarks))
        rows = entry_rows(self.browser, self.bookmarks, self._run_counts)
        self._populate(self.src_list, rows, keep_row=self.browser.cursor)
        self.src_selection.setText(selection_html(self.browser, self.collection))

    def _sync_browser_cursor(self, row: int) -> None:
        if self.stage == "sources" and row >= 0:
            idx = self.src_list.item(row).data(Qt.UserRole)
            if idx is not None:
                self.browser.cursor = idx

    def _paint_config(self) -> None:
        cand = self.candidates[self.form_cand] if self.form_cand is not None else {}
        self.cfg_head.setText(config_header(cand))
        keep = self.cfg_list.currentRow()
        self._populate(self.cfg_list, config_rows(self.form),
                       keep_row=keep if keep >= 0 else None)
        self._paint_config_help(self.cfg_list.currentRow())

    def _paint_config_help(self, row: int) -> None:
        """The focused field's schema description as the help line."""
        text = ""
        if (self.form is not None and row is not None and 0 <= row
                and row < len(self.form.fields)):
            text = self.form.fields[row].description or ""
        self.cfg_help.setText(f"<span style='color:#8a9299'>{text}</span>")

    def _paint_compare(self) -> None:
        self.cmp_head.setText(compare_header(self._probe_source(), self.seg_index,
                                             self.seg_count, self.sess.probe))
        if self.error:
            self.cmp_busy.setText(f"<span style='color:#c74a3c'>"
                                  f"{self.error}</span>")
        elif self.busy:
            self.cmp_busy.setText(f"<span style='color:#b9770e'>"
                                  f"{self.busy}</span>")
        else:
            self.cmp_busy.setText("")
        keep = self.cmp_list.currentRow()
        self._populate(self.cmp_list, compare_rows(self.rows, self.marks),
                       keep_row=keep if keep >= 0 else None)
        self._paint_transcript()

    def _paint_transcript(self) -> None:
        if self.stage == "compare":
            i = self.cmp_list.currentRow()
            text = (self.rows[i]["text"] or "(empty transcript)"
                    if 0 <= i < len(self.rows) else "")
            self.cmp_text.setPlainText(text)
        elif self.stage == "results" and self.results_run is not None:
            seg = self._results_segment()
            if seg is None:
                self.res_text.setPlainText("")
                return
            srcs = self.run_index.runs[self.results_run]["sources"]
            i = self.res_list.currentRow()
            segs = (srcs[i].get("segments") or []) if 0 <= i < len(srcs) else []
            pos = min(self.results_seg, len(segs) - 1)
            self.res_text.setPlainText(
                segment_text(seg, (pos, len(segs)),
                             flags=self.flags_all.get((i, int(seg.get("index", pos))))))

    def _paint_results(self, keep_row: bool = False) -> None:
        """`keep_row` = an in-drill repaint (a flag jump, a landed chunk verb)
        keeps the sources cursor; a fresh drill never inherits the RUNS-list
        row as a sources row."""
        if self.results_run is None:
            self.res_head.setText(f"<b>Past runs ({len(self.run_index.runs)})"
                                  f"</b> &nbsp;·&nbsp; {self.run_index.runs_dir}/")
            self._populate(self.res_list, run_rows(self.run_index))
            self.res_text.setPlainText("")
        else:
            m = self.run_index.runs[self.results_run]
            head = drill_header(m, self.run_index)
            cur = self._current_chunk()
            chip = flag_chip(self.flag_keys.index(cur) if cur in self.flag_keys else None,
                             len(self.flag_keys), escalated=len(self.escalated_keys))
            self.res_head.setText(head + (f" &nbsp; {chip}" if chip else ""))
            row = self.res_list.currentRow()
            self._populate(self.res_list, drill_source_rows(m),
                           keep_row=row if (keep_row and row >= 0) else None)
            self._paint_transcript()

    def _refresh_status(self) -> None:
        """Footer decomposition (DEC 2a42c028): stage/journal/speakers/unload
        chips, the error/busy/notice ladder on the readout, the stage legend
        on the hint line, the ?-overlay tracking the registry."""
        if self.graph_capability:
            journal = (f"<span style='color:#3f9d55'> journal→"
                       f"{self.graph_capability} </span>")
        else:
            journal = ("<b><span style='color:#c74a3c'> NOT "
                       "JOURNALED </span></b>")
        if self.diarization_capability and self.diarization_enabled:
            speakers = (f"<span style='color:#3f9d55'> speakers→"
                        f"{self.diarization_capability} </span>")
        elif self.diarization_capability:
            speakers = "<span style='color:#b9770e'> speakers OFF </span>"
        else:
            speakers = "<span style='color:#8a9299'> no diarization </span>"
        chips = [("stage", self.stage.upper()), ("journal", journal),
                 ("speakers", speakers)]
        if self.sess.unloading:
            chips.append(("unload", "<span style='color:#b9770e'> unloading "
                                    "previous stack… </span>"))
        self.strip.set_chips(chips)
        hints_key = ("results_drill" if self.stage == "results"
                     and self.results_run is not None else self.stage)
        if self.error:
            self.strip.set_readout(f"⚠ {self.error}", role="warn")
        elif self.busy:
            self.strip.set_readout(self.busy)
        elif self.notice:
            self.strip.set_readout(self.notice)
        else:
            self.strip.clear_readout()
        self.strip.set_hints(HINTS[hints_key] + " · ? keys")
        self.hints_overlay.set_entries(self.keymap.entries())

    # ---- gestures (stage-dispatched, busy-gated like the Textual actions) ----

    def move_cursor(self, delta: int) -> None:
        if self.busy:
            return
        lst = self._active_list()
        if lst.count():
            lst.setCurrentRow(max(0, min(lst.count() - 1,
                                         lst.currentRow() + delta)))
        if self.stage == "results" and self.results_run is not None:
            self.results_seg = 0
            self._stop_player()
            self._paint_transcript()
        if self.stage == "sources":
            self._paint_sources_selection_only()

    def _paint_sources_selection_only(self) -> None:
        self.src_selection.setText(selection_html(self.browser, self.collection))

    def on_select(self) -> None:
        if self.busy:
            return
        if self.stage == "sources":
            self.browser.enter()
            self._paint_sources()
        elif self.stage == "candidates":
            idx = self._current_index(self.cand_list)
            if idx is None:
                return
            if idx in self.cand_picked:
                self.cand_picked.remove(idx)
            else:
                self.cand_picked.append(idx)
            self._paint_stage()
        elif self.stage == "config":
            self._config_edit()
        elif self.stage == "results":
            if self.results_run is None and self.run_index.runs:
                idx = self._current_index(self.res_list)
                if idx is None:
                    return
                self.results_run = idx
                self.results_seg = 0
                self._index_flags()
                self._paint_results()
                self._refresh_status()
        elif self.stage == "compare":
            self._confirm()

    def _current_index(self, lst: QListWidget) -> Optional[int]:
        item = lst.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def on_updir(self) -> None:
        if self.stage == "sources" and not self.busy:
            self.browser.up()
            self._paint_sources()

    def on_key_a(self) -> None:
        if self.busy:
            return
        if self.stage == "sources":
            self.browser.add_folder()
            self._paint_sources()
        elif self.stage == "compare" and self.rows:
            i = self.cmp_list.currentRow()
            if 0 <= i < len(self.rows):
                self.marks["accuracy"] = self.rows[i]["instance_id"]
                self._paint_compare()

    def on_mark_light(self) -> None:
        if self.stage == "compare" and self.rows and not self.busy:
            i = self.cmp_list.currentRow()
            if 0 <= i < len(self.rows):
                self.marks["lightweight"] = self.rows[i]["instance_id"]
                self._paint_compare()

    def on_diarization(self) -> None:
        if not self.diarization_capability:
            self.notice = ("no diarization capability installed (diarize "
                           "surface) — pass --diarization-capability or "
                           "install one")
            self._refresh_status()
            return
        self.diarization_enabled = not self.diarization_enabled
        self._refresh_status()

    def on_collection_none(self) -> None:
        if self.stage == "sources" and not self.busy:
            self.collection.toggle_off()
            self._paint_sources_selection_only()

    def on_config(self) -> None:
        """c: candidates -> config sub-view; sources -> the collection editor."""
        if self.busy:
            return
        if self.stage == "sources":
            self._collection_editor()
            return
        if self.stage != "candidates":
            return
        idx = self._current_index(self.cand_list)
        if idx is None:
            return
        cand = self.candidates[idx]
        code = self.manifest_code.get(cand["capability"], {})
        axis = model_axis(code)
        skip = (axis["key"],) if axis else ()
        form = ConfigForm.from_schema(code.get("config_schema"), skip=skip)
        if not form.fields:
            self.error = f"{cand['capability']} has no configurable fields"
            self._refresh_status()
            return
        form.apply(cand.get("config") or {})
        self.form = form
        self.form_cand = idx
        self.error = None
        self._show_stage("config")

    def _collection_editor(self) -> None:
        """Modal collection-title entry: prefill = committed title or a single
        folder's proposal; recent manifest titles ride the dialog label
        (title identity makes retyping one SELECT the existing node)."""
        recent = self.run_index.collection_titles()[:5]
        label = "collection title (empty = auto):"
        if recent:
            label += "\nrecent: " + " · ".join(recent)
        text, ok = QInputDialog.getText(self, "collection", label,
                                        text=self.collection.prefill(
                                            self.browser.selected))
        if not ok:
            return
        self.collection.set_named(text)
        self._paint_sources_selection_only()

    def _config_edit(self) -> None:
        """enter on a config row: closed sets cycle in place; open kinds take a
        modal value dialog (parse failures stay in-status, value untouched)."""
        if self.form is None:
            return
        i = self.cfg_list.currentRow()
        if not (0 <= i < len(self.form.fields)):
            return
        field = self.form.fields[i]
        if field.cycle():
            self.error = None
            self._paint_config()
            return
        text, ok = QInputDialog.getText(self, "value",
                                        f"{field.title}:", text=field.render())
        if not ok:
            return
        try:
            field.parse(text)
        except ValueError as e:
            self.error = f"{field.title}: {e}"
            self._refresh_status()
            return
        self.error = None
        self._paint_config()
        self._refresh_status()

    def _commit_form(self) -> None:
        if self.form is None or self.form_cand is None:
            return
        cand = self.candidates[self.form_cand]
        code = self.manifest_code.get(cand["capability"], {})
        axis = model_axis(code)
        cfg = cand.get("config") or {}
        axis_part = ({axis["key"]: cfg[axis["key"]]}
                     if axis and axis["key"] in cfg else {})
        cand["config"] = {**axis_part, **self.form.overrides()}
        self.form = None
        self.form_cand = None

    def on_next_stage(self) -> None:
        if self.busy:
            return
        self.error = None
        if self.stage == "sources":
            if not self.browser.selected:
                self.error = "select at least one source first"
            else:
                self._show_stage("candidates")
                return
        elif self.stage == "candidates":
            if not self.cand_picked:
                self.error = "pick at least one candidate instance"
            else:
                self._enter_compare()
                return
        self._refresh_status()

    def on_prev_stage(self) -> None:
        if self.busy:
            return
        self.error = None
        if self.stage == "candidates":
            self.browser.refresh()  # external changes show on re-entry
            self._show_stage("sources")
        elif self.stage == "config":
            self._commit_form()
            self._show_stage("candidates")
        elif self.stage == "compare":
            self._stop_player()
            fut = self.sess.teardown_background()  # b returns INSTANTLY
            if fut is not None:
                fut.add_done_callback(self.unload_done.emit)
            self.rows = []
            self._show_stage("candidates")
        elif self.stage == "results":
            self._stop_player()
            if self.results_run is not None:
                self.results_run = None
                self._paint_results()
                self._refresh_status()
            else:
                self.browser.refresh()
                self._show_stage("sources")

    # ---- results view ---------------------------------------------------

    def on_results(self) -> None:
        if self.stage != "sources" or self.busy:
            return
        self.notice = None
        self.error = None
        self.run_index.load()   # a run finished elsewhere shows w/o restart
        self._run_counts = self.run_index.counts_by_path()
        self.results_run = None
        self._show_stage("results")

    def _on_results_row_changed(self, _row: int) -> None:
        if self.stage == "results" and self.results_run is not None:
            self.results_seg = 0
            self._stop_player()
            self._paint_transcript()

    def _results_segment(self) -> Optional[Dict[str, Any]]:
        if self.results_run is None:
            return None
        srcs = self.run_index.runs[self.results_run]["sources"]
        i = self.res_list.currentRow()
        if not srcs or not (0 <= i < len(srcs)):
            return None
        segs = srcs[i].get("segments") or []
        if not segs:
            return None
        return segs[max(0, min(self.results_seg, len(segs) - 1))]

    def on_segment(self, delta: int) -> None:
        if self.stage == "results" and self.results_run is not None:
            srcs = self.run_index.runs[self.results_run]["sources"]
            i = self.res_list.currentRow()
            segs = (srcs[i].get("segments") or []) if 0 <= i < len(srcs) else []
            if segs:
                self.results_seg = max(0, min(self.results_seg + delta,
                                              len(segs) - 1))
                self._stop_player()
                self._paint_transcript()
            return
        if self.stage == "compare" and self.seg_count and not self.busy:
            self.seg_index = max(0, min(self.seg_index + delta,
                                        self.seg_count - 1))
            self._stop_player()
            self._kick_compare()

    def on_rerun(self) -> None:
        if self.stage == "compare" and self.sess.probe is not None and not self.busy:
            self.sess.drop_row_cache(self.seg_index)
            self._kick_compare()

    # ---- the flagged-chunk lane (cf0b91d6 part 4; rulings 8a9b9639 · 9ffce5f7) ----

    def _index_flags(self) -> None:
        """Census the drilled run FROM ITS MANIFEST (no graph needed): total
        failures first — runaway loops (degenerate-tail markers on new runs,
        oversized / implausible words-per-second text on old ones) and extreme
        two-transcriber disagreement — keyed (source index, segment index)."""
        self.flags, self.flag_keys = {}, []
        self.flags_all, self.escalated_keys = {}, []
        if self.results_run is None:
            return
        try:
            self.flags_all = flagged_chunks(self.run_index.runs[self.results_run],
                                            include_escalated=True)
        except Exception as e:  # a foreign / pre-0.2.0 manifest censuses as clean
            self.notice = f"flag census unavailable: {e}"
            self.flags_all = {}
        # A chunk the operator already escalated (an external variant beside the
        # flagged one) is COVERED: shown as such, skipped by the jump.
        self.escalated_keys = [k for k, rows in self.flags_all.items()
                               if all(r.get("escalated") for r in rows)]
        self.flags = {k: rows for k, rows in self.flags_all.items()
                      if k not in set(self.escalated_keys)}
        self.flag_keys = list(self.flags)

    def _current_chunk(self) -> Optional[Tuple[int, int]]:
        """(source index, segment `index`) under the results-drill cursor."""
        if self.stage != "results" or self.results_run is None:
            return None
        i = self.res_list.currentRow()
        seg = self._results_segment()
        if i < 0 or seg is None:
            return None
        return (i, int(seg.get("index", self.results_seg)))

    def _drilled_manifest(self) -> Optional[Dict[str, Any]]:
        if self.results_run is None:
            return None
        return self.run_index.runs[self.results_run]

    def _goto_chunk(self, key: Tuple[int, int]) -> None:
        """Move the drill cursor to (source index, segment index)."""
        si, idx = key
        m = self._drilled_manifest()
        if m is None:
            return
        segs = (m["sources"][si].get("segments") or []) if 0 <= si < len(m["sources"]) else []
        pos = next((p for p, s in enumerate(segs) if int(s.get("index", p)) == idx), 0)
        self._stop_player()
        if self.res_list.currentRow() != si:
            self.res_list.setCurrentRow(si)   # -> _on_results_row_changed resets results_seg
        self.results_seg = pos
        self._paint_results(keep_row=True)

    def on_flag(self, direction: int) -> None:
        """, / . : jump to the previous / next flagged chunk (wraps; any chunk
        stays escalatable — the lane is a jump aid, never a filter)."""
        if self.stage != "results" or self.results_run is None or self.busy:
            return
        if not self.flag_keys:
            self.notice = "no flagged chunks in this run"
            self._refresh_status()
            return
        cur = self._current_chunk()
        keys = self.flag_keys
        if cur in keys:
            j = (keys.index(cur) + direction) % len(keys)
        else:
            later = [k for k in keys if cur is None or k > cur]
            j = keys.index(later[0]) if (direction > 0 and later) else (
                keys.index([k for k in keys if cur is None or k < cur][-1])
                if direction < 0 and any(cur is None or k < cur for k in keys) else 0)
        self._goto_chunk(keys[j])
        rows = self.flags.get(keys[j]) or []
        self.notice = (f"flagged {j + 1}/{len(keys)}: "
                       + " · ".join(f"{r.get('transcriber')} {','.join(r.get('reasons') or [])}" for r in rows))
        self._refresh_status()

    def _run_chunk_verb(self, argv: List[str], label: str,
                        exe: Optional[List[str]] = None) -> None:
        """Run a chunk verb (rerun-chunk / add-transcript, or decomp-core's
        respine-chunk via `exe`) in a worker thread as a subprocess; the result
        lands on the Qt thread through the queued chunk_done signal. The verb
        is the same headless CLI a hand-launched run uses — the app never
        reimplements a landing (the hand-off principle, carried to the lane).
        `exe` (default: THIS interpreter's transcription-core CLI) is the
        resolved console script of another core's env (the hub's ladder)."""
        if self.busy:
            return
        self.error = None
        self.busy = f"{label}…"
        self._refresh_status()
        cmd = list(exe) if exe else [sys.executable, "-m", "cjm_transcription_core.cli"]

        def work() -> None:
            try:
                proc = subprocess.run(cmd + argv, capture_output=True, text=True)
                self.chunk_done.emit((label, proc.returncode, proc.stdout, proc.stderr))
            except Exception as e:  # never strand the busy gate
                self.chunk_done.emit((label, -1, "", str(e)))
        threading.Thread(target=work, name="chunk-verb", daemon=True).start()

    def _on_chunk_done(self, payload) -> None:
        label, code, out, err = payload
        self.busy = None
        pending, self._pending_import = self._pending_import, None
        if pending is not None and code == 2:
            # The respine verb REFUSED (0b4d5cfa (5)): dependents want the operator's
            # say-so to strand; no live spine means the plain landing still applies.
            kind = classify_refusal(out)
            why = refusal_line(out) or "refused"
            if kind == "dependents":
                ans = QMessageBox.question(
                    self, "Respine chunk — dependents",
                    f"{why}\n\nStrand the non-transferable corrections and respine anyway?\n"
                    "(No = land the transcript only; the chunk keeps its current segments.)",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ans == QMessageBox.Yes:
                    self._pending_import = None
                    self._run_chunk_verb(pending["respine_argv"] + ["--strand"], pending["label"] + " (strand)",
                                         exe=pending["exe"])
                    return
                self.notice = "respine declined — landing the transcript only"
                self._run_chunk_verb(pending["add_argv"], pending["add_label"])
                return
            if kind == "no-spine":
                self.notice = f"no live spine to respine into ({why[:80]}) — landing the transcript only"
                self._run_chunk_verb(pending["add_argv"], pending["add_label"])
                return
            self.error = f"{label} refused: {why}"
            self._refresh_status()
            return
        lines = [l for l in (out or "").splitlines() if l.strip()]
        derived = next((l.split(":", 1)[1].strip() for l in lines
                        if l.startswith("derived manifest:")), None)
        if code == 0 and derived is None:
            # A zero exit with NO landing is a failure (the 2026-09-12 sighting:
            # `python -m` on a module without a main guard exits 0 doing nothing).
            code = -2
            err = "the verb produced no landing (no 'derived manifest:' line in its output)"
        if code == 0:
            self.notice = f"{label}: {lines[-3] if len(lines) >= 3 else (lines[-1] if lines else 'done')}"
        else:
            tail = [l for l in (err or "").splitlines() if l.strip()]
            self.error = f"{label} failed ({code}): {tail[-1] if tail else (lines[-1] if lines else 'no output')}"
        # The derived manifest is a NEW run: re-index, then MOVE the drill onto it
        # (the chain continues from the newest derived manifest — a second
        # landing must derive from the first, and the operator sees what landed);
        # on failure the drilled run stays put.
        cur = self._current_chunk()
        keep = self._drilled_manifest()
        self.run_index.load()
        self._run_counts = self.run_index.counts_by_path()
        target = None
        if derived is not None:
            want = Path(derived).name
            target = next((i for i, m in enumerate(self.run_index.runs)
                           if Path(str(m.get("_path") or "")).name == want), None)
        if target is None and keep is not None:
            target = next((i for i, m in enumerate(self.run_index.runs)
                           if m.get("run_id") == keep.get("run_id")), None)
        self.results_run = target
        if self.stage == "results" and self.results_run is not None:
            self._index_flags()
            if cur is not None:
                self._goto_chunk(cur)
            else:
                self._paint_results(keep_row=True)
            if derived is not None and code == 0:
                m = self.run_index.runs[self.results_run]
                self.notice += f" — now drilling {m.get('run_id')}"
        self._refresh_status()

    def _graph_argv(self, m: Dict[str, Any]) -> List[str]:
        """The landing's graph target flags: the app's explicit settings win,
        else the parent manifest's recorded emission target (the CLI defaults
        the same way; passing them keeps the hand-off reproducible)."""
        rec = m.get("graph") or {}
        cap = self.graph_capability or rec.get("capability")
        db = self.graph_db_path or rec.get("db_path")
        argv: List[str] = ["--manifests-dir", self.manifests_dir]
        if cap:
            argv += ["--graph-capability", str(cap)]
        if db:
            argv += ["--graph-db-path", str(db)]
        return argv

    def on_chunk_rerun(self) -> None:
        """t: re-transcribe THIS chunk with one of the run's transcribers (cache
        bypassed) — lands a new variant that SUPERSEDES the prior one and writes
        a derived manifest (rerun-chunk)."""
        cur = self._current_chunk()
        m = self._drilled_manifest()
        if cur is None or m is None or self.busy:
            return
        names = list((m.get("config") or {}).get("transcriber_capabilities") or self.run_index.transcribers(m))
        if not names:
            self.error = "this run names no transcribers"
            self._refresh_status()
            return
        flagged = [r.get("transcriber") for r in self.flags.get(cur) or []]
        default = next((n for n in names if n in flagged), names[0])
        pick, ok = QInputDialog.getItem(self, "Re-transcribe chunk", "Transcriber:", names,
                                        names.index(default), False)
        if not ok or not pick:
            return
        si, idx = cur
        argv = ["rerun-chunk", "--manifest", str(m.get("_path") or ""), "--transcriber", str(pick),
                "--source", str(si), "--segment", str(idx), "--reason", "operator"] + self._graph_argv(m)
        self._run_chunk_verb(argv, f"re-transcribing segment {idx} with {pick}")

    def on_escalate(self) -> None:
        """e: the copy-paste escalation gesture (f304d31d first cut): the prompt
        WITH CONTEXT goes to the clipboard, the chunk's audio folder opens for
        the upload; the model's answer comes back through `i`. Any chunk, not
        only flagged ones (8a9b9639 (2))."""
        cur = self._current_chunk()
        m = self._drilled_manifest()
        seg = self._results_segment()
        if cur is None or m is None or seg is None:
            return
        try:
            rendered = render_escalation_prompt(m, cur[0], cur[1])
        except Exception as e:
            self.error = f"prompt render failed: {e}"
            self._refresh_status()
            return
        QApplication.clipboard().setText(rendered["prompt"])
        self.last_prompt_hash = rendered["prompt_hash"]
        wav = str(seg.get("model_input_path") or "")
        if wav and Path(wav).exists():
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(wav).parent)))
            where = ("; audio folder opened" if opened
                     else f"; audio at {Path(wav).parent} (folder open unsupported here)")
        else:
            where = "; audio not on disk (capability cache cleaned?)"
        self.notice = (f"prompt copied ({rendered['prompt_hash'][:15]}…)" + where
                       + " — paste the model's transcript with i")
        self._refresh_status()

    def on_import_transcript(self) -> None:
        """i: land a pasted external transcript for THIS chunk as a third
        transcriber (<model id>/manual) with the prompt hash the last `e`
        rendered. ROUTED through decomp-core's respine-chunk when its console
        script resolves (0b4d5cfa (1)): the same landing, then the chunk is
        re-derived INTO the source's live spine; the verb's refusals come back
        as questions (dependents -> strand?) or as the plain landing (no live
        spine). Without the decomp script the plain add-transcript runs."""
        cur = self._current_chunk()
        m = self._drilled_manifest()
        if cur is None or m is None or self.busy:
            return
        model_id, ok = QInputDialog.getText(self, "Import pasted transcript", "External model id:",
                                            text=DEFAULT_ESCALATION_MODEL_ID)
        if not ok or not model_id.strip():
            return
        text, ok = QInputDialog.getMultiLineText(self, "Import pasted transcript",
                                                 "Paste the model's transcript for this chunk:")
        if not ok or not text.strip():
            return
        prompt_hash = self.last_prompt_hash or render_escalation_prompt(m, cur[0], cur[1])["prompt_hash"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="chunk-paste-", delete=False,
                                          encoding="utf-8")
        tmp.write(text)
        tmp.close()
        si, idx = cur
        graph_argv = self._graph_argv(m)
        add_argv = ["add-transcript", "--manifest", str(m.get("_path") or ""), "--source", str(si),
                    "--segment", str(idx), "--model-id", model_id.strip(), "--text-file", tmp.name,
                    "--prompt-hash", prompt_hash, "--text-source", "paste", "--reason", "escalation"] \
            + graph_argv
        add_label = f"landing {model_id.strip()} transcript on segment {idx}"
        exe = resolve_decomp_core()
        content_hash = str((m["sources"][si] if 0 <= si < len(m.get("sources") or []) else {}).get("content_hash") or "")
        if exe is None or not content_hash:
            self._run_chunk_verb(add_argv, add_label)
            return
        rargv = respine_argv(source_node_id(content_hash), idx, tmp.name, model_id.strip(), prompt_hash,
                             graph_argv, str(Path(str(m.get("_path") or "")).parent))
        label = f"respining segment {idx} from {model_id.strip()}"
        self._pending_import = {"add_argv": add_argv, "add_label": add_label, "respine_argv": rargv,
                                "label": label, "exe": [exe]}
        self._run_chunk_verb(rargv, label, exe=[exe])

    # ---- bookmarks + hash check ----------------------------------------

    def on_bookmark(self) -> None:
        if self.stage != "sources":
            return
        key = str(self.browser.cwd)
        if key in self.bookmarks:
            self.bookmarks.remove(key)
            self.notice = f"bookmark removed: {key}"
        else:
            self.bookmarks.append(key)
            self.notice = f"bookmarked ★ {key}"
        save_state(self.manifests_dir, bookmarks=self.bookmarks)
        self._paint_sources()
        self._refresh_status()

    def on_jump_bookmark(self) -> None:
        if self.stage != "sources" or not self.bookmarks:
            return
        cur = str(self.browser.cwd)
        order = sorted(self.bookmarks)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        target = Path(nxt)
        if not target.is_dir():
            self.notice = f"bookmark missing on disk: {nxt}"
            self._refresh_status()
            return
        self.browser.cwd = target
        self.browser.cursor = 0
        self.browser.refresh()
        self.notice = None
        self._paint_sources()
        self._refresh_status()

    def on_hash_check(self) -> None:
        """h: content-identity check off-thread (a large file never freezes
        paint — the session loop hosts the to_thread hash)."""
        if self.stage != "sources" or self.busy:
            return
        target = self.browser.focused()
        if target is None or target.is_dir():
            self.error = "focus a file to hash-check"
            self._refresh_status()
            return
        self.notice = None
        self.error = None
        self.busy = f"hashing {target.name}..."
        self._hash_target = target   # hash_check's result carries no path
        self._refresh_status()
        fut = self.sess.submit(asyncio.to_thread(self.run_index.hash_check,
                                                 str(target)))
        fut.add_done_callback(self.hash_done.emit)

    def _on_hash_done(self, fut) -> None:
        self.busy = None
        try:
            res = fut.result()
        except Exception as e:
            self.error = f"hash-check failed: {e}"
            self._refresh_status()
            return
        target = self._hash_target
        matches = res["matches"]
        name = target.name if target else "file"
        if not matches:
            self.notice = f"{name}: no prior run of this content"
        else:
            here = str(target.resolve()) if target else None
            moved = sum(1 for hit in matches
                        if str(Path(hit["source_path"]).resolve()) != here)
            note = f", {moved} under a different path" if moved else ""
            self.notice = (f"{name}: content in {len(matches)} prior "
                           f"run(s){note} — latest {matches[0]['run_id']}")
        self._refresh_status()

    # ---- playback -------------------------------------------------------

    def _stop_player(self) -> None:
        if self.player is not None:
            self.player.stop()

    def on_play(self) -> None:
        if self.player is not None and self.player.playing:
            self.player.stop()
            return
        wav: Optional[str] = None
        if self.stage == "compare" and self.sess.probe is not None:
            wav = self.sess.probe.wav_path(self.seg_index)
            if wav is None:
                self.error = "no probed audio for this segment yet"
                self._refresh_status()
                return
        elif self.stage == "results" and self.results_run is not None:
            seg = self._results_segment()
            if seg is None:
                return
            wav = seg.get("model_input_path") or ""
            if not wav or not Path(wav).exists():
                self.error = ("segment audio not on disk (capability cache "
                              "cleaned?)")
                self._refresh_status()
                return
        else:
            return
        if self.player is None:
            self.player = SpanPlayer(self)
        self.player.play(wav)
        err = self.player.error_text()
        if err:
            self.error = f"playback unavailable: {err}"
            self._refresh_status()

    # ---- the compare stage (the seam) -----------------------------------

    def _probe_source(self) -> Optional[str]:
        try:
            return expand_sources(self.browser.selected)[0]
        except (SystemExit, IndexError):
            return None

    def _picked_directives(self) -> List[Dict[str, Any]]:
        return [self.candidates[i] for i in sorted(self.cand_picked)]

    def _enter_compare(self) -> None:
        source = self._probe_source()
        if source is None:
            self.error = "no media file in the selection"
            self._refresh_status()
            return
        self.rows = []
        self.marks = {"lightweight": None, "accuracy": None}
        self.preprocess_loaded = False
        picks = self._picked_directives()
        ids = [d["instance_id"] for d in picks]
        self.busy = (f"loading {len(picks)} candidate instance(s) — model "
                     f"loads can take a while...")
        self._busy_base = self.busy
        self._show_stage("compare")
        cfg = PipelineConfig(transcriber_capabilities=ids, assume_yes=True,
                             max_segment_duration=self.max_segment_duration)
        fut = self.sess.open_stack(cfg, picks, source, ids,
                                   preprocessing_id=self.preprocessing_capability)
        fut.add_done_callback(self.stack_opened.emit)

    def _on_stack_opened(self, fut) -> None:
        try:
            self.seg_count = fut.result()
        except Exception as e:
            self.busy = None
            self.error = f"stack open failed: {e}"
            drain = self.sess.teardown_background()
            if drain is not None:
                drain.add_done_callback(self.unload_done.emit)
            self._show_stage("candidates")
            return
        self.seg_index = min(self.seg_index, max(0, self.seg_count - 1))
        self.busy = None
        if self.seg_count == 0:
            self.error = "no segments cut from the probe source"
            self._paint_compare()
            self._refresh_status()
            return
        self._kick_compare()

    def _kick_compare(self) -> None:
        probe = self.sess.probe
        if probe is None:
            return
        self.error = None   # a new run owns the error slot (stress-drive 2)
        self.busy = (f"transcribing segment {self.seg_index + 1}/{self.seg_count} "
                     f"across {len(probe.transcriber_ids)} candidate(s)"
                     + (" with preprocessing" if probe.preprocess else "")
                     + "...")
        self._busy_base = self.busy
        self._paint_compare()
        self._refresh_status()
        fut = self.sess.compare(self.seg_index)
        fut.add_done_callback(self.compare_done.emit)

    def _on_compare_done(self, fut) -> None:
        self.busy = None
        self._busy_base = None
        try:
            self.rows = fut.result()
        except Exception as e:
            self.error = f"probe failed: {e}"
        if self.stage == "compare":
            self._paint_compare()
            self._refresh_status()

    def _poll_blocked(self) -> None:
        """The 2s blocked-reason read (Textual _watch_blocked, timer-inverted)."""
        if self.stage != "compare" or self.busy is None or self._blocked_inflight:
            return
        self._blocked_inflight = True
        self.sess.blocked_reasons().add_done_callback(self.blocked_read.emit)

    def _on_blocked_read(self, fut) -> None:
        self._blocked_inflight = False
        if self.busy is None or self._busy_base is None:
            return
        try:
            blocked = fut.result()
        except Exception:
            return
        if blocked:
            detail = " · ".join(f"{iid} {reason}" for iid, reason in blocked)
            self.busy = f"{self._busy_base} ⏳ blocked: {detail}"
        else:
            self.busy = self._busy_base
        if self.stage == "compare":
            self._paint_compare()
            self._refresh_status()

    def on_preprocess(self) -> None:
        """d: the preprocessing A/B (5aba2ab6) — first toggle lazy-loads."""
        probe = self.sess.probe
        if self.stage != "compare" or probe is None or self.busy:
            return
        pid = probe.preprocessing_id
        if not pid:
            self.error = "no source_separation capability installed"
            self._refresh_status()
            return
        if not self.preprocess_loaded:
            self.busy = f"loading {pid} (first toggle of this stack)..."
            self._busy_base = self.busy
            self._paint_compare()
            self._refresh_status()
            fut = self.sess.load_preprocessing(pid)
            fut.add_done_callback(self.preproc_done.emit)
            return
        self._flip_preprocess()

    def _on_preproc_done(self, fut) -> None:
        self.busy = None
        try:
            fut.result()
        except BaseException as e:  # SystemExit on a missing manifest included
            self.error = f"preprocessing load failed: {e}"
            self._paint_compare()
            self._refresh_status()
            return
        self.preprocess_loaded = True
        self._flip_preprocess()

    def _flip_preprocess(self) -> None:
        probe = self.sess.probe
        if probe is None:
            return
        probe.preprocess = not probe.preprocess
        self._stop_player()   # the audible referent changed renditions
        self._kick_compare()

    def on_cancel(self) -> None:
        """escape: cancel the in-flight probe composition (30057f10)."""
        if self.stage == "compare" and self.sess.probe is not None and self.busy:
            self.sess.cancel_active()

    # ---- confirm + exits -------------------------------------------------

    def _confirm(self) -> None:
        light = self.marks["lightweight"]
        acc = self.marks["accuracy"]
        if not light or not acc:
            self.error = ("mark BOTH lightweight (l) and accuracy (a) before "
                          "confirming")
            self._refresh_status()
            return
        probe = self.sess.probe
        by_id = {d["instance_id"]: d for d in self._picked_directives()}
        pair = [by_id[light]] + ([by_id[acc]] if acc != light else [])
        self.plan = {
            "sources": list(self.browser.selected),
            "transcribers": [spec_string(d) for d in pair],
            "lightweight": light,
            "accuracy": acc,
            "max_segment_duration": self.max_segment_duration,
            "sysmon_capability": self.sysmon_capability,
            "graph_capability": self.graph_capability,
            "graph_db_path": self.graph_db_path,
            "manifests_dir": self.manifests_dir,
            "preprocessing_capability": (probe.preprocessing_id
                                         if (probe is not None
                                             and probe.preprocess) else None),
            "diarization_capability": (self.diarization_capability
                                       if self.diarization_enabled else None),
            "collection": self.collection.plan_value(),
            "picked_instance_ids": [d["instance_id"]
                                    for d in self._picked_directives()],
            "last_cwd": str(self.browser.cwd),
        }
        self.busy = "unloading the comparison stack..."
        self._refresh_status()
        self.close()

    def on_quit(self) -> None:
        save_state(self.manifests_dir, last_cwd=str(self.browser.cwd))
        self.close()

    def closeEvent(self, event) -> None:
        """ANY exit path: stop audio, drain the stack blocking (the hand-off
        needs the VRAM actually free), stop the loop thread."""
        if self.player is not None:
            self.player.close()
            self.player = None
        self.blocked_timer.stop()
        self.sess.close()
        super().closeEvent(event)
