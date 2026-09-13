"""Pure-builder tests for panes.py — the paint logic, no Qt.

Real spine objects where cheap (SourceBrowser over a tmp tree, CollectionField,
kit ConfigForm), plain dicts for candidates/rows/manifests."""

from pathlib import Path

from cjm_substrate_tui_kit.form import ConfigForm
from cjm_transcription_core.sources import CollectionField, SourceBrowser

from cjm_transcription_qt.panes import (candidate_rows, compare_header,
                                        compare_rows, config_rows, drill_header,
                                        drill_source_rows, entry_rows, run_rows,
                                        segment_text, selection_html)


def make_tree(tmp_path: Path) -> Path:
    (tmp_path / "talks").mkdir()
    (tmp_path / "talks" / "a.mp3").write_bytes(b"x")
    (tmp_path / "music").mkdir()
    return tmp_path


def test_entry_rows_pick_state_and_run_chips(tmp_path):
    root = make_tree(tmp_path)
    b = SourceBrowser(str(root))
    rows = entry_rows(b, [], {})
    assert [r["text"] for r in rows] == ["[ ] music/", "[ ] talks/"]
    assert all(r["style"] == "blue" for r in rows)
    b.toggle(root / "talks")
    counts = {str((root / "talks" / "a.mp3").resolve()): 2}
    rows = entry_rows(b, [str((root / "music").resolve())], counts)
    assert rows[0]["text"] == "[ ] music/ ★"
    assert rows[1]["text"] == "[x] talks/  ·2 runs"
    assert rows[1]["style"] == "green"


def test_entry_rows_empty_dir_placeholder(tmp_path):
    (tmp_path / "empty").mkdir()
    b = SourceBrowser(str(tmp_path / "empty"))
    rows = entry_rows(b, [], {})
    assert rows[0]["index"] is None and rows[0]["style"] == "dim"


def test_selection_html_modes(tmp_path):
    root = make_tree(tmp_path)
    b = SourceBrowser(str(root))
    c = CollectionField()
    html = selection_html(b, c)
    assert "Selected (0, run order)" in html
    b.toggle(root / "talks")
    html = selection_html(b, c)
    assert "dir" in html and "talks" in html
    assert "auto: Talks" not in html  # prettify only capitalizes nothing here
    c.toggle_off()
    assert "--no-collection" in selection_html(b, c)


CANDS = [
    {"capability": "cjm-capability-whisper", "model": "large-v3",
     "instance_id": "whisper#large-v3", "default": True, "config": {}},
    {"capability": "cjm-capability-whisper", "model": "tiny",
     "instance_id": "whisper#tiny", "default": False,
     "config": {"beam": 8}},
    {"capability": "cjm-capability-parakeet", "model": None,
     "instance_id": "parakeet", "default": False, "config": {}},
]


def test_candidate_rows_groups_picks_and_cfg_chip():
    rows = candidate_rows(CANDS, [0], {})
    texts = [r["text"] for r in rows]
    assert texts[0] == "cjm-capability-whisper"          # group header
    assert rows[0]["index"] is None
    assert texts[1].startswith("[x] large-v3  (default)")
    assert rows[1]["style"] == "green"
    assert "[cfg]" in texts[2]                           # non-model config edit
    assert texts[3] == "cjm-capability-parakeet"
    assert "(no model axis)" in texts[4]


def test_config_rows_marks_modified():
    form = ConfigForm.from_schema({
        "type": "object",
        "properties": {"beam": {"type": "integer", "default": 5},
                       "lang": {"type": "string", "default": "en"}}})
    form.apply({"beam": 8})
    rows = config_rows(form)
    assert rows[0]["text"].startswith("* beam")
    assert rows[0]["style"] == "green"
    assert rows[1]["text"].startswith("  lang")


class FakeProbe:
    preprocessing_id = "cjm-capability-demucs"
    preprocess = True


def test_compare_header_and_rows():
    head = compare_header("/x/a.mp3", 2, 9, FakeProbe())
    assert "a.mp3" in head and "segment 3/9" in head and "demucs ON" in head
    rows = compare_rows(
        [{"instance_id": "w", "text": "t", "chars": 5,
          "profile": {"duration_s_mean": 1.5, "gpu_mb_peak": 900.0,
                      "rss_mb_peak": 2000.0, "samples": 4}},
         {"instance_id": "p", "text": "t2", "chars": 7}],
        {"lightweight": "w", "accuracy": "w"})
    assert rows[0]["text"].startswith("[L,A] w  5 chars  ~1.5s/seg")
    assert rows[0]["style"] == "green"
    assert rows[1]["text"].startswith("[ ] p")


class FakeRunIndex:
    runs_dir = "runs"

    def __init__(self, runs):
        self.runs = runs

    def transcribers(self, m):
        return list(m.get("_names") or [])


RUN = {"run_id": "run-42", "created_at": 1786000000.0, "_names": ["whisper"],
       "sources": [{"source_path": "/x/a.mp3", "chain": ["demucs", "ffmpeg"],
                    "segments": [{"start": 0, "end": 2.5,
                                  "transcripts": {"w": {"text": "hello"}}}],
                    "diarization": {"status": "ok", "speaker_count": 2,
                                    "turn_count": 9}}],
       "collections": [{"title": "Talks", "status": "confirmed"}],
       "graph": {"collections": [{"title": "Talks", "nodes_added": 0}]}}


def test_run_rows_and_drill():
    idx = FakeRunIndex([RUN])
    rows = run_rows(idx)
    assert "run-42" in rows[0]["text"] and "1 source(s)" in rows[0]["text"]
    head = drill_header(RUN, idx)
    assert "run-42" in head and "attached to existing" in head
    srows = drill_source_rows(RUN)
    assert "a.mp3" in srows[0]["text"]
    assert "demucs->ffmpeg" in srows[0]["text"]
    assert "2 speaker(s) · 9 turn(s)" in srows[0]["text"]


def test_segment_text_multi_and_flat():
    seg = RUN["sources"][0]["segments"][0]
    text = segment_text(seg, (0, 1))
    assert text.startswith("segment 1/1  [0.0s – 2.5s]")
    assert "── w ──" in text and "hello" in text
    flat = segment_text({"start": 0, "end": 1, "text": "flat"}, (0, 2))
    assert flat.endswith("flat")


def test_flag_line_chip_and_segment_text_verdict():
    """The flagged-chunk lane's paint (cf0b91d6 part 4): the verdict line names each
    flagged variant with its reasons and measures; the header chip shows k/K on a flagged
    chunk, the count off one, nothing when the run is clean; segment_text stays the same
    for an unflagged chunk."""
    from cjm_transcription_qt.panes import flag_chip, flag_line
    flags = [{"transcriber": "cjm-capability-voxtral-hf", "reasons": ["oversized", "implausible_rate"],
              "chars": 124835, "words_per_second": 57.5},
             {"transcriber": "whisper--small", "reasons": ["disagreement"], "chars": 2154, "words_per_second": 1.9}]
    line = flag_line(flags)
    assert line.startswith("⚑ flagged: cjm-capability-voxtral-hf oversized,implausible_rate (124835 ch, 57.5 w/s)")
    assert " · whisper--small disagreement (2154 ch, 1.9 w/s)" in line
    assert flag_line(None) == "" and flag_line([]) == ""
    assert flag_chip(None, 0) == "" and "3 flagged chunk(s)" in flag_chip(None, 3)
    assert "flagged 2/3" in flag_chip(1, 3)
    seg = RUN["sources"][0]["segments"][0]
    plain = segment_text(seg, (0, 1))
    flagged = segment_text(seg, (0, 1), flags=flags)
    assert plain == segment_text(seg, (0, 1), flags=None)
    assert flagged.splitlines()[1].startswith("⚑ flagged:") and flagged.splitlines()[0] == plain.splitlines()[0]
    assert flagged.endswith(plain.split("\n", 1)[1])
