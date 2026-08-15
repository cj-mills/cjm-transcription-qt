"""Segment playback via QMediaPlayer — the Qt lane's audio answer (DEC dcf8a712).

Replaces the kit ChunkPlayer (sounddevice/PortAudio) for the p verb: the probe
and the run manifests both point at standalone per-segment WAVs, so playback is
plain file playback — no chunk windows, no device probing, and none of the
ALSA fd-2 chatter the Textual shell had to mute (_quiet_fd2 retired). The
first QMediaPlayer outing on the lane; the correction redesign inherits it."""

from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class SegmentPlayer:
    """Play/stop one WAV at a time; p toggles (press again to cut playback)."""

    def __init__(self, parent=None):
        self._player = QMediaPlayer(parent)
        self._out = QAudioOutput(parent)
        self._player.setAudioOutput(self._out)

    @property
    def playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlayingState

    def play(self, path: str) -> None:
        """Start `path` from the top (stale audio under a fresh comparison
        would mismatch the transcript on screen — stop-then-play, always)."""
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def close(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())

    def error_text(self) -> Optional[str]:
        """The player's last error string, or None (surfaced in-status)."""
        if self._player.error() == QMediaPlayer.NoError:
            return None
        return self._player.errorString() or "playback error"
