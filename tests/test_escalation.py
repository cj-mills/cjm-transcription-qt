"""The `i` import's respine routing (0b4d5cfa (1)): the console-script ladder, the
respine-chunk argv, and the refusal classes the app answers — pure, no Qt."""

from pathlib import Path

from cjm_transcription_qt.escalation import (classify_refusal, DECOMP_CORE_ENV, DECOMP_CORE_SCRIPT,
                                             refusal_line, resolve_decomp_core, respine_argv)


def test_resolve_decomp_core_sibling_env_then_none(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    assert resolve_decomp_core(envs_root=str(tmp_path)) is None
    b = tmp_path / DECOMP_CORE_ENV / "bin"
    b.mkdir(parents=True)
    (b / DECOMP_CORE_SCRIPT).write_text("#!/bin/sh\n")
    assert resolve_decomp_core(envs_root=str(tmp_path)) == str(b / DECOMP_CORE_SCRIPT)
    monkeypatch.setattr("shutil.which", lambda cmd: "/on/path/" + cmd)
    assert resolve_decomp_core(envs_root=str(tmp_path)) == "/on/path/" + DECOMP_CORE_SCRIPT


def test_respine_argv_shape_and_strand():
    g = ["--manifests-dir", ".cjm/manifests", "--graph-capability", "cjm-capability-graph-sqlite",
         "--graph-db-path", "/g.db"]
    a = respine_argv("src-1", 3, "/tmp/p.txt", "gemini-3.8-flash", "sha256:tpl", g, "/ws/runs")
    assert a[:5] == ["respine-chunk", "--source", "src-1", "--chunk", "3"]
    assert "--text-file" in a and a[a.index("--model-id") + 1] == "gemini-3.8-flash"
    assert a[a.index("--prompt-hash") + 1] == "sha256:tpl" and a[a.index("--runs-dir") + 1] == "/ws/runs"
    assert a[-6:] == g and "--strand" not in a
    b = respine_argv("src-1", 3, "/tmp/p.txt", "m", "h", g, "/ws/runs", strand=True)
    assert "--strand" in b and b.index("--strand") < b.index("--manifests-dir")


def test_refusal_classes():
    dep = ("[INFO] noise\nREFUSED: the chunk's old segments carry dependents — 28 event insert(s) + 6 speaker "
           "assignment(s) transferable by time; 32 would STRAND (14 grouping). Pass --strand …\n")
    assert classify_refusal(dep) == "dependents" and refusal_line(dep).startswith("the chunk's old segments")
    assert classify_refusal("REFUSED: source 0b87 has no decomposed spine\n") == "no-spine"
    assert classify_refusal("REFUSED: no decomp manifest records skeleton sha256:… for source x\n") == "no-spine"
    assert classify_refusal("REFUSED: spine event-carve · abcd is RETIRED — …\n") == "no-spine"
    assert classify_refusal("REFUSED: 1 event insert(s) transferable — but no chunk-scoped transfer is registered\n") == "dependents"
    assert classify_refusal("REFUSED: something else entirely\n") == "other"
    assert classify_refusal("respined chunk 3 …\nderived manifest: /x\n") is None and refusal_line("") is None
