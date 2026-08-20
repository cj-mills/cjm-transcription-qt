"""One capability stack behind a private asyncio loop thread — the jobs+progress
seam for Qt shells (DEC dcf8a712, the c4b0d6e5 leg-2 novelty).

The substrate stack (CapabilityManager / JobQueue / SegmentProbe) is asyncio-
native; Qt's event loop is not. A CapabilitySession owns a daemon loop thread
where the whole stack lives, and exposes the stack verbs as concurrent Futures
the Qt shell resolves through queued Signals — the paint thread never blocks on
a model load, a compare fan-out, or an unload drain. Deliberately Qt-free
(stdlib + substrate only): testable headless, and the extraction candidate for
cjm-substrate-qt-kit once the decomp/correction migrations become the second
and third consumers.

Semantics carried over from the Textual shell verbatim: blocking model loads
ride asyncio.to_thread so the loop stays live for cancellation and the
blocked-reason snapshots; b-gesture teardowns CHAIN (each unload awaits its
predecessor, and a stack open awaits the whole chain — a reload can never race
the VRAM frees); unloads run in reverse load order.
"""

import asyncio
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cjm_substrate.core.manager import CapabilityManager
from cjm_substrate.core.queue import JobQueue
from cjm_substrate_qt_kit.loopthread import LoopThreadSession
from cjm_transcription_core.cli import load_capabilities
from cjm_transcription_core.probe import SegmentProbe


class CapabilitySession(LoopThreadSession):
    """The loop-thread seat for one comparison stack.

    start() spins the loop (kit LoopThreadSession); open_stack() builds
    manager+queue+probe ON the loop (awaiting any pending unload chain first);
    the fetch/act verbs return concurrent Futures; teardown_background()
    detaches the live stack and drains it behind the seat; close() tears
    everything down blocking (the exit path). One instance per window, like
    GraphSession."""

    thread_name = "capability-session"

    def __init__(self, manifests_dir: str,
                 *, sysmon_capability: Optional[str] = None,
                 timeout: float = 1800.0,  # model loads can take minutes cold
                 loader: Callable[..., Any] = load_capabilities,
                 probe_factory: Callable[..., Any] = SegmentProbe):
        super().__init__(timeout=timeout)
        self.manifests_dir = manifests_dir
        self.sysmon_capability = sysmon_capability
        self._loader = loader
        self._probe_factory = probe_factory
        self.manager: Optional[Any] = None
        self.queue: Optional[Any] = None
        self.probe: Optional[Any] = None
        self.loaded_ids: List[str] = []
        self._unload_chain: Optional[Future] = None

    # ---- stack lifecycle ------------------------------------------------

    def open_stack(self, cfg: Any, picks: List[Dict[str, Any]], source: str,
                   transcriber_ids: List[str],
                   preprocessing_id: Optional[str] = None) -> Future:
        """Stand the comparison stack up: await the unload chain, load the
        directives (sysmon first, then ffmpeg/vad, then the candidates —
        blocking loads on a worker thread so this loop keeps serving
        cancellation and blocked-reason reads), start the queue, cut the
        probe source. Resolves to the segment count."""
        return self.submit(self._open_stack(cfg, picks, source, transcriber_ids,
                                            preprocessing_id))

    async def _open_stack(self, cfg, picks, source, transcriber_ids,
                          preprocessing_id) -> int:
        await self._await_unloads()
        manager = CapabilityManager(search_paths=[Path(self.manifests_dir)],
                                    sysmon_capability_name=self.sysmon_capability)
        directives: List[Any] = (
            ([self.sysmon_capability] if self.sysmon_capability else [])
            + [cfg.ffmpeg_capability, cfg.vad_capability] + list(picks))
        await asyncio.to_thread(self._loader, manager, directives)
        self.manager = manager
        self.loaded_ids = [d["instance_id"] if isinstance(d, dict) else d
                           for d in directives]
        self.queue = JobQueue(deps=manager,
                              sysmon_capability_name=self.sysmon_capability)
        await self.queue.start()
        self.probe = self._probe_factory(manager, self.queue, cfg, source,
                                         transcriber_ids,
                                         preprocessing_id=preprocessing_id)
        return await self.probe.prepare()

    def load_preprocessing(self, pid: str) -> Future:
        """Lazy-load the preprocessing capability (first d toggle of a stack);
        appends to loaded_ids so reverse-order teardown unloads it first."""
        return self.submit(self._load_preprocessing(pid))

    async def _load_preprocessing(self, pid: str) -> None:
        await asyncio.to_thread(self._loader, self.manager, [pid])
        self.loaded_ids.append(pid)

    # ---- probe verbs ----------------------------------------------------

    def compare(self, index: int) -> Future:
        """Fan segment `index` out across the candidates (cached per
        (segment, preprocess) by the probe); resolves to the row dicts."""
        return self.submit(self.probe.compare(index))

    def drop_row_cache(self, index: int) -> None:
        """Forget the cached rows for `index` under the CURRENT toggle (the
        r re-probe gesture); the next compare recomputes."""
        if self.probe is not None:
            self.probe._rows.pop((index, self.probe.preprocess), None)

    def cancel_active(self) -> Future:
        """Cancel the in-flight probe composition (escape); resolves True when
        a cancel was dispatched."""
        return self.submit(self.probe.cancel_active())

    def blocked_reasons(self) -> Future:
        """Snapshot of (instance_id, block_reason) for pending jobs — the read
        model behind the shell's QTimer poll (the Textual _watch_blocked's
        2s cadence, inverted: the shell owns the timer, the loop owns the
        read). Resolves to a possibly-empty list; never raises."""
        return self.submit(self._blocked_reasons())

    async def _blocked_reasons(self) -> List[Tuple[str, str]]:
        if self.queue is None:
            return []
        try:
            pending = self.queue.get_pending()
        except Exception:
            return []
        return [(j.capability_instance_id, j.block_reason)
                for j in pending if getattr(j, "block_reason", None)]

    # ---- teardown (the chained-unload contract) -------------------------

    def _detach(self):
        """Hand the live stack to a teardown path and clear the session state."""
        queue, manager, ids = self.queue, self.manager, self.loaded_ids
        self.queue = None
        self.manager = None
        self.probe = None
        self.loaded_ids = []
        return queue, manager, ids

    @property
    def unloading(self) -> bool:
        """True while a background unload chain is draining (the status chip)."""
        return self._unload_chain is not None and not self._unload_chain.done()

    def teardown_background(self) -> Optional[Future]:
        """Detach the stack and drain it behind the seat (the b gesture).

        Back-to-back b/n gestures chain: each drain awaits its predecessor,
        and open_stack/close await the whole chain — VRAM frees can never
        race a reload. Returns the chain Future (None when nothing was up)
        so the shell can hang a done-signal on it for the chip repaint."""
        queue, manager, ids = self._detach()
        if queue is None and manager is None:
            return None
        prev = self._unload_chain
        self._unload_chain = self.submit(self._drain(prev, queue, manager, ids))
        return self._unload_chain

    async def _drain(self, prev, queue, manager, ids) -> None:
        if prev is not None:
            try:
                await asyncio.wrap_future(prev)
            except Exception:
                pass
        await self._teardown_of(queue, manager, ids)

    async def _teardown_of(self, queue, manager, ids) -> None:
        """Stop a queue, then unload its instances in reverse load order."""
        if queue is not None:
            try:
                await queue.stop()
            except Exception:
                pass
        if manager is not None:
            for iid in reversed(ids):
                try:
                    await asyncio.to_thread(manager.unload_capability, iid)
                except Exception:
                    pass

    def teardown(self) -> None:
        """Blocking full teardown (exit paths): drain the chain, then whatever
        is still attached."""
        queue, manager, ids = self._detach()
        chain = self._unload_chain
        if chain is not None:
            try:
                chain.result(self.timeout)
            except Exception:
                pass
            self._unload_chain = None
        if queue is not None or manager is not None:
            self.call(self._teardown_of(queue, manager, ids))

    async def _await_unloads(self) -> None:
        chain = self._unload_chain
        if chain is not None:
            try:
                await asyncio.wrap_future(chain)
            except Exception:
                pass

    def close(self) -> None:
        """Full stop: teardown, then the loop thread (window close)."""
        if not self.running:
            return
        self.teardown()
        super().close()
