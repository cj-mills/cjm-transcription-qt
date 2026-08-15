# cjm-transcription-qt

<!-- generated from the context graph by `cjm-context-graph readme` — do not edit by hand; edit the graph (the urge to hand-edit = move it on-graph) -->

Qt shell for the transcription-workflow setup app — the first workflow TUI migrated to the PySide6 lane (DEC c4b0d6e5 leg 2). Imports the pure spine of cjm-transcription-tui (sources browser, candidate directives, segment probe, run index, sidecar state) and shares its CLI ladder (build_parser/resolve_settings/hand_off), so Qt-confirmed runs hand off to cjm-transcription-core byte-identically. Long-running capability jobs with progress run under Qt via CapabilitySession — a private asyncio loop thread owning CapabilityManager/JobQueue/SegmentProbe, resolving to the shell through queued Signals. Audio = QMediaPlayer.

## Modules

- **`cjm_transcription_qt`** — Qt shell for the transcription-workflow setup app — the first workflow TUI on the PySide6 lane (DEC dcf8a712).
- **`cjm_transcription_qt.app`** — The Qt transcription-workflow shell: the same three-stage run setup as the
- **`cjm_transcription_qt.capability_session`** — One capability stack behind a private asyncio loop thread — the jobs+progress
- **`cjm_transcription_qt.cli`** — Console-script driver for the Qt shell: the SAME argument surface,
- **`cjm_transcription_qt.panes`** — Pure row/label builders for the Qt shell — the paint logic, Qt-free.
- **`cjm_transcription_qt.player`** — Segment playback via QMediaPlayer — the Qt lane's audio answer (DEC dcf8a712).

## API

### `cjm_transcription_qt.app`

- `TranscriptionWindow` _class_ — Run-setup window; exit with `plan` set = the CLI hands off headless.

### `cjm_transcription_qt.capability_session`

- `CapabilitySession` _class_ — The loop-thread seat for one comparison stack.

### `cjm_transcription_qt.cli`

- `main` _function_ — Resolve the shared setup surface, run the Qt setup window, hand off.

### `cjm_transcription_qt.panes`

- `candidate_rows` _function_ — Manifest-derived (capability, MODEL) space: group-header rows interleave
- `compare_header` _function_ — Source name + segment position + the preprocessing A/B state.
- `compare_rows` _function_ — One row per candidate: L/A marks, char count, CR-7 profile numbers.
- `config_header` _function_
- `config_rows` _function_ — One row per config field: modified star, title, current value.
- `cwd_label` _function_ — The header line above the listing (HTML: bold path + bookmark star).
- `drill_header` _function_ — Run id + transcribers + the collection convergence audit (d544e250):
- `drill_source_rows` _function_ — One row per source of a drilled run, chain + diarization outcome
- `entry_rows` _function_ — One row per browser entry: pick state, dir slash, bookmark star, and the
- `run_rows` _function_ — Past runs newest-first: id, timestamp, source count, transcribers.
- `segment_text` _function_ — A drilled segment as PLAIN text: header line, then one block per
- `selection_html` _function_ — The Selected block + Collection line (ae3464fc) as one HTML fragment.

### `cjm_transcription_qt.player`

- `SegmentPlayer` _class_ — Play/stop one WAV at a time; p toggles (press again to cut playback).

## Dependencies

**Depends on:** `PySide6`, `cjm-substrate`, `cjm-substrate-qt-kit`, `cjm-substrate-tui-kit`, `cjm-transcription-core`, `cjm-transcription-tui`
