"""Escalation routing for the transcription app's `i` import (ruling 0b4d5cfa (1): an
escalation from EITHER seat respines when a live spine exists). Pure, Qt-free: the
console-script resolution across core envs (the hub's ladder — each core's script
lives only in its own env), the respine-chunk argv, and the refusal classes the
app answers (dependents -> ask to strand; no live spine -> the plain landing)."""

import shutil
import sys
from pathlib import Path
from typing import List, Optional

DECOMP_CORE_SCRIPT = "cjm-transcript-decomp-core"   # The verb host's console script
DECOMP_CORE_ENV = "cjm-transcript-decomp-core"      # Its conda env (env name = core repo name)


def resolve_decomp_core(
    envs_root: Optional[str] = None,  # Conda envs dir override (tests; None = derive from this interpreter)
) -> Optional[str]:  # Absolute executable path, or None = not installed anywhere we know
    """PATH first (an env that installs everything wins), then the sibling env's bin
    with the envs root derived from sys.prefix's parent — never a hardcoded path
    (6dfe00e9 discipline). None = the caller falls back to the plain landing."""
    found = shutil.which(DECOMP_CORE_SCRIPT)
    if found:
        return found
    root = Path(envs_root) if envs_root else Path(sys.prefix).parent
    cand = root / DECOMP_CORE_ENV / "bin" / DECOMP_CORE_SCRIPT
    return str(cand) if cand.exists() else None


def respine_argv(
    source_id: str,        # The Source node id (recomputed from the manifest's content hash)
    chunk_index: int,      # The coarse chunk's manifest index
    text_file: str,        # The paste on disk
    model_id: str,         # The external model id
    prompt_hash: str,      # The prompt template's hash
    graph_argv: List[str], # The app's --manifests-dir / --graph-capability / --graph-db-path flags
    runs_dir: str,         # Where the run manifests live (the drilled manifest's directory)
    strand: bool = False,  # Strand the non-transferable dependents (after the operator said so)
) -> List[str]:  # The respine-chunk argv (without the executable)
    """The verb's argv: land the paste + respine the chunk in the live spine."""
    argv = ["respine-chunk", "--source", str(source_id), "--chunk", str(int(chunk_index)),
            "--text-file", str(text_file), "--model-id", str(model_id), "--prompt-hash", str(prompt_hash),
            "--text-source", "paste", "--reason", "escalation", "--runs-dir", str(runs_dir)]
    if strand:
        argv.append("--strand")
    return argv + list(graph_argv)


def refusal_line(stdout: str) -> Optional[str]:  # The verb's 'REFUSED: …' line, or None
    """The refusal the verb printed (exit 2 prints exactly one)."""
    for line in (stdout or "").splitlines():
        if line.startswith("REFUSED:"):
            return line[len("REFUSED:"):].strip()
    return None


def classify_refusal(stdout: str) -> Optional[str]:  # "dependents" | "no-spine" | "other" | None
    """Which answer a refusal wants: DEPENDENTS = ask the operator to strand and re-run;
    NO-SPINE = the source is not decomposed (or its manifest lineage is missing) — the
    plain landing still applies; OTHER = show it. None = no refusal in the output."""
    r = refusal_line(stdout)
    if r is None:
        return None
    low = r.lower()
    if "carry dependents" in low or "would strand" in low or "no chunk-scoped transfer" in low:
        return "dependents"
    if ("no decomposed spine" in low or "no decomp manifest" in low or "retired" in low
            or "legacy" in low or "no live segments" in low):
        return "no-spine"
    return "other"
