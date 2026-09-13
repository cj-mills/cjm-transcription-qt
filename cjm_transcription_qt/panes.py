"""Pure row/label builders for the Qt shell — the paint logic, Qt-free.

Each function mirrors a Textual `_paint_*` region of cjm-transcription-tui's
app.py, re-expressed as row dicts ({"text", "style", ...}) and small HTML
fragments the shell materializes into QListWidgets and QLabels. No windowing
(visible_slice) — Qt lists scroll natively — and no Rich objects, so the
whole layer tests headless (the spine/lens discipline of DEC 8b9804c2 carried
to the workflow lane).

Style words match the workbench vocabulary (STYLE_COLORS there): green/blue/
yellow/red/cyan/dim + bold. Transcript prose NEVER passes through markdown —
the shell paints it setPlainText, which is the whole defang problem avoided.
"""

import html
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cjm_transcription_core.candidates import model_axis
from cjm_transcription_core.results import RunIndex


# ---- sources stage ------------------------------------------------------


def entry_rows(browser: Any, bookmarks: List[str],
               run_counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """One row per browser entry: pick state, dir slash, bookmark star, and the
    path-keyed prior-run chip (no hashing during browse — h is content truth)."""
    rows: List[Dict[str, Any]] = []
    keys = browser.entry_keys()
    sel = set(browser.selected)
    for i, entry in enumerate(browser.entries()):
        picked = keys[i] in sel
        name = entry.name + ("/" if entry.is_dir() else "")
        star = " ★" if entry.is_dir() and keys[i] in bookmarks else ""
        n = (RunIndex.dir_count(run_counts, keys[i]) if entry.is_dir()
             else run_counts.get(keys[i], 0))
        chip = f"  ·{n} run{'s' if n != 1 else ''}" if n else ""
        style = "green" if picked else ("blue" if entry.is_dir() else "")
        rows.append({"text": f"[{'x' if picked else ' '}] {name}{star}{chip}",
                     "style": style, "index": i})
    if not rows:
        rows.append({"text": "(no subdirectories or media files)",
                     "style": "dim", "index": None})
    return rows


def cwd_label(browser: Any, bookmarks: List[str]) -> str:
    """The header line above the listing (HTML: bold path + bookmark star)."""
    star = " <span style='color:#b9770e'>★</span>" \
        if str(browser.cwd) in bookmarks else ""
    return f"<b>{html.escape(str(browser.cwd))}</b>{star}"


def selection_html(browser: Any, collection: Any) -> str:
    """The Selected block + Collection line (ae3464fc) as one HTML fragment."""
    sel = browser.selected
    parts = [f"<b>Selected ({len(sel)}, run order):</b>"]
    for s in sel:
        kind = "dir " if Path(s).is_dir() else "file"
        parts.append(f"<span style='color:#3f9d55'>&nbsp;{kind}&nbsp; "
                     f"{html.escape(s)}</span>")
    summary, mode = collection.summary(sel)
    color = {"named": "#3f9d55", "auto": "#b9770e",
             "off": "#8a9299", "none": "#8a9299"}[mode]
    parts.append(f"<b>Collection:</b> <span style='color:{color}'>"
                 f"{html.escape(summary)}</span>")
    return "<br>".join(parts)


# ---- candidates + config stages -----------------------------------------


def candidate_rows(candidates: List[Dict[str, Any]], picked: List[int],
                   manifest_code: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Manifest-derived (capability, MODEL) space: group-header rows interleave
    with pickable instance rows; a [cfg] chip flags non-model config edits."""
    rows: List[Dict[str, Any]] = []
    last_cap = None
    for i, c in enumerate(candidates):
        if c["capability"] != last_cap:
            rows.append({"text": c["capability"], "style": "blue", "index": None})
            last_cap = c["capability"]
        label = c["model"] if c["model"] is not None else "(no model axis)"
        chunks = [f"[{'x' if i in picked else ' '}] {label}"]
        if c["default"]:
            chunks.append("(default)")
        chunks.append(f"-> {c['instance_id']}")
        axis = model_axis(manifest_code.get(c["capability"], {}))
        axis_key = axis["key"] if axis else None
        if any(k != axis_key for k in (c.get("config") or {})):
            chunks.append("[cfg]")
        rows.append({"text": "  ".join(chunks),
                     "style": "green" if i in picked else "", "index": i})
    if not candidates:
        rows.append({"text": "no transcription capabilities found",
                     "style": "red", "index": None})
    return rows


def config_rows(form: Any) -> List[Dict[str, Any]]:
    """One row per config field: modified star, title, current value."""
    rows: List[Dict[str, Any]] = []
    for i, (title, value, modified) in enumerate(form.rows()):
        star = "*" if modified else " "
        rows.append({"text": f"{star} {title}  =  {value}",
                     "style": "green" if modified else "", "index": i})
    if not rows:
        rows.append({"text": "(no configurable fields)", "style": "dim",
                     "index": None})
    return rows


def config_header(cand: Dict[str, Any]) -> str:
    label = cand.get("model") or cand.get("capability") or "?"
    return (f"<b>config · <span style='color:#2b8a9d'>{html.escape(str(label))}"
            f"</span></b> <span style='color:#8a9299'>-&gt; "
            f"{html.escape(str(cand.get('instance_id', '?')))}</span>")


# ---- compare stage ------------------------------------------------------


def compare_header(source: Optional[str], seg_index: int, seg_count: int,
                   probe: Any) -> str:
    """Source name + segment position + the preprocessing A/B state."""
    name = html.escape(Path(source).name if source else "?")
    parts = [f"<b>{name}</b>",
             f"<span style='color:#8a9299'>segment {seg_index + 1}/"
             f"{max(seg_count, 1)}</span>"]
    if probe is not None and probe.preprocessing_id:
        pre = html.escape(probe.preprocessing_id.removeprefix("cjm-capability-"))
        if probe.preprocess:
            parts.append(f"<b><span style='color:#3f9d55'>{pre} ON</span></b>")
        else:
            parts.append(f"<span style='color:#8a9299'>{pre} off</span>")
    return " &nbsp; ".join(parts)


def compare_rows(rows: List[Dict[str, Any]],
                 marks: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """One row per candidate: L/A marks, char count, CR-7 profile numbers."""
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        ms = [m for m, key in (("L", "lightweight"), ("A", "accuracy"))
              if marks.get(key) == row["instance_id"]]
        chunks = [f"[{','.join(ms) or ' '}] {row['instance_id']}",
                  f"{row['chars']} chars"]
        prof = row.get("profile") or {}
        if prof:
            chunks.append(f"~{prof['duration_s_mean']:.1f}s/seg  "
                          f"gpu {prof['gpu_mb_peak']:.0f}MB  "
                          f"rss {prof['rss_mb_peak']:.0f}MB  "
                          f"(n={prof['samples']})")
        out.append({"text": "  ".join(chunks),
                    "style": "green" if ms else "", "index": i})
    return out


# ---- results stage ------------------------------------------------------


def run_rows(run_index: Any) -> List[Dict[str, Any]]:
    """Past runs newest-first: id, timestamp, source count, transcribers."""
    rows: List[Dict[str, Any]] = []
    for i, m in enumerate(run_index.runs):
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(float(m.get("created_at") or 0)))
        text = f"{m['run_id']}  {when}  {len(m['sources'])} source(s)"
        names = run_index.transcribers(m)
        if names:
            text += "  " + ", ".join(names)
        if m.get("parent_run_id"):
            # A DERIVED manifest (a chunk re-run / external landing chain):
            # the newest one is the one to decompose from.
            landings = len((m.get("derivation") or {}).get("landings") or [])
            text += f"  ⤷ derived from {str(m['parent_run_id'])[:28]} ({landings} landing(s))"
        rows.append({"text": text, "style": "", "index": i})
    if not rows:
        rows.append({"text": "(no run manifests found — confirmed runs land here)",
                     "style": "dim", "index": None})
    return rows


def drill_header(manifest: Dict[str, Any], run_index: Any) -> str:
    """Run id + transcribers + the collection convergence audit (d544e250):
    nodes_added=0 = attached to a pre-existing collection, never hidden."""
    names = run_index.transcribers(manifest)
    parts = [f"<b>{html.escape(str(manifest['run_id']))}</b>"]
    if names:
        parts.append(f"<span style='color:#8a9299'>"
                     f"{html.escape(', '.join(names))}</span>")
    lines = [" &nbsp; ".join(parts)]
    recs = {r.get("title"): r
            for r in ((manifest.get("graph") or {}).get("collections") or [])}
    for decl in (manifest.get("collections") or []):
        title = str(decl.get("title") or "")
        line = (f"<b>collection: {html.escape(title)}</b> "
                f"<span style='color:#8a9299'>({decl.get('status')})</span>")
        rec = recs.get(title)
        if rec is not None and not rec.get("nodes_added"):
            line += (" <span style='color:#b9770e'>→ attached to existing"
                     "</span>")
        lines.append(line)
    return "<br>".join(lines)


def drill_source_rows(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per source of a drilled run, chain + diarization outcome
    (450e7c78: ok shows the yield, anything else shows the status)."""
    rows: List[Dict[str, Any]] = []
    for i, s in enumerate(manifest.get("sources") or []):
        segs = s.get("segments") or []
        chunks = [Path(s.get("source_path") or "?").name,
                  f"{len(segs)} segment(s)"]
        if s.get("chain"):
            chunks.append("->".join(s["chain"]))
        d = s.get("diarization") or {}
        if d.get("status") == "ok":
            chunks.append(f"{d.get('speaker_count')} speaker(s) · "
                          f"{d.get('turn_count')} turn(s)")
        elif d.get("status"):
            chunks.append(f"diarization: {d['status']}")
        rows.append({"text": "  ".join(chunks), "style": "", "index": i})
    return rows


def flag_line(flags: Optional[List[Dict[str, Any]]]) -> str:
    """The flagged-chunk lane's PLAIN-text verdict line for one chunk (cf0b91d6
    part 4): one clause per flagged variant — transcriber, reasons, the
    words/s and char size that earned them — or "" when the chunk is clean."""
    if not flags:
        return ""
    parts = []
    for f in flags:
        why = ",".join(f.get("reasons") or [])
        parts.append(f"{f.get('transcriber')} {why} "
                     f"({int(f.get('chars') or 0)} ch, {float(f.get('words_per_second') or 0):.1f} w/s)")
    head = ("✓ escalated (an external transcript covers this chunk) — was flagged: "
            if all(f.get("escalated") for f in flags) else "⚑ flagged: ")
    return head + " · ".join(parts)


def flag_chip(position: Optional[int], total: int, escalated: int = 0) -> str:
    """Header chip: 'flagged k/K' when the cursor sits on a flagged chunk,
    else the count alone; '' when the run has none; the escalated count
    (chunks already covered by an external transcript) rides beside it."""
    tail = f" <span style='color:#3f9d55'>· ✓ {escalated} escalated</span>" if escalated else ""
    if not total:
        return (f"<span style='color:#3f9d55'>✓ {escalated} escalated</span>" if escalated else "")
    if position is None:
        return f"<span style='color:#b9770e'>⚑ {total} flagged chunk(s)</span>" + tail
    return f"<span style='color:#b9770e'>⚑ flagged {position + 1}/{total}</span>" + tail


def segment_text(seg: Dict[str, Any], position: Tuple[int, int],
                 flags: Optional[List[Dict[str, Any]]] = None) -> str:
    """A drilled segment as PLAIN text: header line, the flag verdict when
    the chunk carries one, then one block per transcriber (or the flat
    0.1.x text)."""
    idx, total = position
    out = [f"segment {idx + 1}/{total}"
           f"  [{float(seg.get('start') or 0):.1f}s – "
           f"{float(seg.get('end') or 0):.1f}s]"]
    verdict = flag_line(flags)
    if verdict:
        out.append(verdict)
    texts = seg.get("transcripts")
    if isinstance(texts, dict) and texts:
        for tid, rec in texts.items():
            out.append(f"\n── {tid} ──")
            out.append((rec or {}).get("text") or "(empty)")
    else:
        out.append("")
        out.append(seg.get("text") or "(empty transcript)")
    return "\n".join(out)
