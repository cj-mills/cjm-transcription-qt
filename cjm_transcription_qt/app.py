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

from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncio

from cjm_substrate_qt_kit.keys import bind
from cjm_substrate_qt_kit.style import apply_row_style
from cjm_substrate_tui_kit.form import ConfigForm
from cjm_transcription_core.cli import expand_sources
from cjm_transcription_core.models import PipelineConfig
from cjm_transcription_tui.candidates import (candidate_directives, model_axis,
                                              spec_string, transcription_manifests)
from cjm_transcription_tui.results import RunIndex
from cjm_transcription_tui.sources import CollectionField, SourceBrowser
from cjm_transcription_tui.state import save_state
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QPlainTextEdit, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

from .capability_session import CapabilitySession
from .panes import (candidate_rows, compare_header, compare_rows, config_header,
                    config_rows, cwd_label, drill_header, drill_source_rows,
                    entry_rows, run_rows, segment_text, selection_html)
from .player import SegmentPlayer

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
    "results_drill": "j/k source · [ ] segment · p play · b runs list · q quit",
}


class TranscriptionWindow(QMainWindow):
    """Run-setup window; exit with `plan` set = the CLI hands off headless."""

    stack_opened = Signal(object)   # loop-thread Future -> Qt thread (queued)
    compare_done = Signal(object)
    preproc_done = Signal(object)
    hash_done = Signal(object)
    unload_done = Signal(object)
    blocked_read = Signal(object)

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
        self.bookmarks: List[str] = list(initial_bookmarks or [])
        self.player: Optional[SegmentPlayer] = None
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
        self.setCentralWidget(self.stack)

        # Status chips: journal + speakers ALWAYS visible (drive-1 discipline),
        # the unload chip while a background drain holds VRAM.
        self.chip_journal = QLabel()
        self.chip_speakers = QLabel()
        self.chip_unload = QLabel()
        for chip in (self.chip_journal, self.chip_speakers, self.chip_unload):
            self.statusBar().addPermanentWidget(chip)

    def _bind_keys(self) -> None:
        for key, fn in (("J", lambda: self.move_cursor(1)),
                        ("K", lambda: self.move_cursor(-1)),
                        ("Return", self.on_select),
                        ("Space", self.on_select),
                        ("Backspace", self.on_updir),
                        ("A", self.on_key_a),
                        ("L", self.on_mark_light),
                        ("C", self.on_config),
                        ("N", self.on_next_stage),
                        ("B", self.on_prev_stage),
                        ("[", lambda: self.on_segment(-1)),
                        ("]", lambda: self.on_segment(1)),
                        ("R", self.on_rerun),
                        ("P", self.on_play),
                        ("D", self.on_preprocess),
                        ("S", self.on_diarization),
                        ("V", self.on_results),
                        ("X", self.on_collection_none),
                        ("H", self.on_hash_check),
                        ("M", self.on_bookmark),
                        ("'", self.on_jump_bookmark),
                        ("Escape", self.on_cancel),
                        ("Q", self.on_quit)):
            bind(self, key, fn)

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
            self.res_text.setPlainText(
                segment_text(seg, (min(self.results_seg, len(segs) - 1),
                                   len(segs))))

    def _paint_results(self) -> None:
        if self.results_run is None:
            self.res_head.setText(f"<b>Past runs ({len(self.run_index.runs)})"
                                  f"</b> &nbsp;·&nbsp; {self.run_index.runs_dir}/")
            self._populate(self.res_list, run_rows(self.run_index))
            self.res_text.setPlainText("")
        else:
            m = self.run_index.runs[self.results_run]
            self.res_head.setText(drill_header(m, self.run_index))
            self._populate(self.res_list, drill_source_rows(m))
            self._paint_transcript()

    def _refresh_status(self) -> None:
        if self.graph_capability:
            self.chip_journal.setText(f"<span style='color:#3f9d55'> journal→"
                                      f"{self.graph_capability} </span>")
        else:
            self.chip_journal.setText("<b><span style='color:#c74a3c'> NOT "
                                      "JOURNALED </span></b>")
        if self.diarization_capability and self.diarization_enabled:
            self.chip_speakers.setText(f"<span style='color:#3f9d55'> speakers→"
                                       f"{self.diarization_capability} </span>")
        elif self.diarization_capability:
            self.chip_speakers.setText("<span style='color:#b9770e'> speakers "
                                       "OFF </span>")
        else:
            self.chip_speakers.setText("<span style='color:#8a9299'> no "
                                       "diarization </span>")
        self.chip_unload.setText(
            "<span style='color:#b9770e'> unloading previous stack… </span>"
            if self.sess.unloading else "")
        hints_key = ("results_drill" if self.stage == "results"
                     and self.results_run is not None else self.stage)
        if self.error:
            msg = f"⚠ {self.error}"
        elif self.busy:
            msg = self.busy
        elif self.notice:
            msg = self.notice
        else:
            msg = f"{self.stage.upper()}  ·  {HINTS[hints_key]}"
        self.statusBar().showMessage(msg)

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
            self.player = SegmentPlayer(self)
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
