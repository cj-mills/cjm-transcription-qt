# cjm-transcription-qt

Qt shell for the transcription-workflow setup app — the first workflow TUI migrated to the PySide6 lane. Imports the pure spine of `cjm-transcription-tui` (sources browser, candidate directives, segment probe, run index, sidecar state) and repaints it with real typography; the confirmed run hands off to the headless `cjm-transcription-core` CLI exactly as the Textual shell does.

(README is projected from the context graph — `readme --write cjm-transcription-qt` regenerates it.)
