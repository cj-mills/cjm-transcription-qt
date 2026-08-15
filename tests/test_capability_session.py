"""CapabilitySession contract tests — headless, no Qt, no real capabilities.

The seam's promises: loads happen on the loop thread (never the caller's),
directive order is sysmon -> ffmpeg -> vad -> picks, unloads run in REVERSE
load order, background drains CHAIN, and a stack open can never overlap a
pending drain (the VRAM double-book guard the Textual shell earned)."""

import threading
import time
from typing import Any, List, Optional

import pytest

from cjm_transcription_qt.capability_session import CapabilitySession


class FakeJob:
    def __init__(self, iid: str, reason: Optional[str]):
        self.capability_instance_id = iid
        self.block_reason = reason


class FakeQueue:
    """Stands in for the substrate JobQueue inside the session's loop."""

    instances: List["FakeQueue"] = []

    def __init__(self, deps=None, sysmon_capability_name=None):
        self.started = False
        self.stopped = False
        self.pending: List[FakeJob] = []
        self.cancelled: List[str] = []
        FakeQueue.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def get_pending(self):
        return self.pending

    async def cancel_composition(self, comp_id):
        self.cancelled.append(comp_id)


class FakeProbe:
    def __init__(self, manager, queue, cfg, source, ids, preprocessing_id=None):
        self.args = (manager, queue, cfg, source, list(ids), preprocessing_id)
        self.preprocess = False
        self.preprocessing_id = preprocessing_id
        self._rows = {}
        self.active_comp_id = None

    async def prepare(self):
        return 7

    async def compare(self, index):
        rows = [{"instance_id": iid, "text": f"seg{index}", "chars": 4}
                for iid in self.args[4]]
        self._rows[(index, self.preprocess)] = rows
        return rows

    async def cancel_active(self):
        return self.active_comp_id is not None


class Recorder:
    """Fake load_capabilities + unload log, with thread attribution."""

    def __init__(self, load_delay: float = 0.0):
        self.loads: List[List[Any]] = []
        self.load_threads: List[str] = []
        self.unloads: List[str] = []
        self.unload_times: List[float] = []
        self.load_delay = load_delay

    def loader(self, manager, directives):
        self.load_threads.append(threading.current_thread().name)
        if self.load_delay:
            time.sleep(self.load_delay)
        self.loads.append(list(directives))

    def make_manager(self, recorder):
        rec = self

        class FakeManager:
            def __init__(self, search_paths=None, sysmon_capability_name=None):
                pass

            def unload_capability(self, iid):
                rec.unloads.append(iid)
                rec.unload_times.append(time.monotonic())

        return FakeManager


class Cfg:
    ffmpeg_capability = "cjm-capability-ffmpeg"
    vad_capability = "cjm-capability-silero-vad"


@pytest.fixture()
def session(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr("cjm_transcription_qt.capability_session.CapabilityManager",
                        rec.make_manager(rec))
    monkeypatch.setattr("cjm_transcription_qt.capability_session.JobQueue", FakeQueue)
    sess = CapabilitySession("/tmp/manifests", sysmon_capability="cjm-capability-sysmon",
                             timeout=10.0, loader=rec.loader, probe_factory=FakeProbe)
    sess.start()
    yield sess, rec
    sess.close()


PICKS = [{"instance_id": "whisper-large"}, {"instance_id": "parakeet"}]
IDS = ["whisper-large", "parakeet"]


def open_stack(sess):
    return sess.open_stack(Cfg(), PICKS, "/media/a.mp3", IDS,
                           preprocessing_id="cjm-capability-demucs").result(10)


def test_open_stack_loads_in_order_off_the_caller_thread(session):
    sess, rec = session
    segs = open_stack(sess)
    assert segs == 7
    assert rec.loads == [["cjm-capability-sysmon", "cjm-capability-ffmpeg",
                          "cjm-capability-silero-vad", *PICKS]]
    # Blocking loads ride asyncio.to_thread off the session loop — never the
    # caller's thread, and never named the loop thread either.
    assert rec.load_threads[0] != threading.main_thread().name
    assert rec.load_threads[0] != "capability-session"
    assert sess.queue.started
    assert sess.probe.args[3] == "/media/a.mp3"
    assert sess.loaded_ids == ["cjm-capability-sysmon", "cjm-capability-ffmpeg",
                               "cjm-capability-silero-vad", *IDS]


def test_compare_resolves_rows_and_caches_on_probe(session):
    sess, _ = session
    open_stack(sess)
    rows = sess.compare(3).result(10)
    assert [r["instance_id"] for r in rows] == IDS
    assert (3, False) in sess.probe._rows
    sess.drop_row_cache(3)
    assert (3, False) not in sess.probe._rows


def test_blocked_reasons_snapshot(session):
    sess, _ = session
    assert sess.blocked_reasons().result(10) == []  # no queue yet: empty, no raise
    open_stack(sess)
    sess.queue.pending = [FakeJob("whisper-large", "waiting on 3.2GB VRAM"),
                          FakeJob("parakeet", None)]
    assert sess.blocked_reasons().result(10) == [("whisper-large",
                                                  "waiting on 3.2GB VRAM")]


def test_background_teardown_unloads_reversed_and_chains(session):
    sess, rec = session
    open_stack(sess)
    first_queue = sess.queue
    fut = sess.teardown_background()
    assert sess.queue is None and sess.manager is None and sess.probe is None
    fut.result(10)
    assert first_queue.stopped
    assert rec.unloads == ["parakeet", "whisper-large", "cjm-capability-silero-vad",
                           "cjm-capability-ffmpeg", "cjm-capability-sysmon"]
    assert not sess.unloading


def test_open_stack_awaits_pending_drain(session):
    sess, rec = session
    rec.load_delay = 0.05
    open_stack(sess)
    sess.teardown_background()
    open_stack(sess)  # must not overlap the drain
    # Every unload of the first stack completed before the second load recorded.
    assert len(rec.loads) == 2
    assert len(rec.unloads) == 5


def test_teardown_blocking_clears_attached_stack(session):
    sess, rec = session
    open_stack(sess)
    sess.teardown()
    assert sess.queue is None and sess.loaded_ids == []
    assert len(rec.unloads) == 5


def test_load_preprocessing_appends_for_reverse_unload(session):
    sess, rec = session
    open_stack(sess)
    sess.load_preprocessing("cjm-capability-demucs").result(10)
    assert sess.loaded_ids[-1] == "cjm-capability-demucs"
    sess.teardown()
    assert rec.unloads[0] == "cjm-capability-demucs"  # unloads first


def test_cancel_active_dispatches(session):
    sess, _ = session
    open_stack(sess)
    assert sess.cancel_active().result(10) is False
    sess.probe.active_comp_id = "comp-1"
    assert sess.cancel_active().result(10) is True
