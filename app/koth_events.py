"""Pub/sub fan-out for the KOTH live scoreboard (SSE).

Three properties this has to hold at classroom scale (22 teams, 60+ open
tabs, one gevent worker):

1. **Serialize once.** The old bus handed every subscriber a dict and let
   the SSE generator `json.dumps` it per client — 60 redundant
   serializations of the same ~8 KB payload every tick. `publish()` now
   builds the complete `text/event-stream` frame once and hands the same
   immutable `str` to everyone.

2. **Bounded, and drop the stale thing — not the client.** The old bus
   used `queue.Queue(maxsize=100)` and *unsubscribed* any client whose
   queue filled. That client's HTTP connection stayed open, so the browser
   never reconnected: it just sat there receiving keepalive pings forever
   with a frozen scoreboard. Here a slow subscriber loses buffered frames,
   never its subscription.

3. **Coalesce snapshots.** A `scan` frame is a complete state snapshot, so
   buffering two of them is pure waste — the newer one fully supersedes
   the older. Frames published with a `coalesce_key` replace any buffered
   frame carrying the same key. That caps a stalled subscriber's retained
   bytes at roughly one snapshot plus a handful of log events, instead of
   `maxlen` full snapshots.
"""

import json
import logging
import threading
from collections import deque
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

# Per-subscriber buffer depth. Only non-coalesced frames (claim/flag/
# game_ended log lines) can actually stack up this far; `scan` collapses
# to one entry.
DEFAULT_BUFFER = 32


class Subscriber:
    """One connected SSE client's frame buffer."""

    __slots__ = ('_frames', '_lock', '_wakeup', 'dropped', 'created_at')

    def __init__(self, maxlen: int = DEFAULT_BUFFER):
        # (coalesce_key | None, frame_text)
        self._frames: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self.dropped = 0
        self.created_at = datetime.now()

    @property
    def depth(self) -> int:
        return len(self._frames)

    def offer(self, frame: str, coalesce_key: Optional[str]) -> None:
        with self._lock:
            if coalesce_key is not None:
                for i, (key, _) in enumerate(self._frames):
                    if key == coalesce_key:
                        self._frames[i] = (coalesce_key, frame)
                        self._wakeup.set()
                        return
            if len(self._frames) == self._frames.maxlen:
                # deque would evict silently; count it so /_diag can show
                # that a client is falling behind.
                self._frames.popleft()
                self.dropped += 1
            self._frames.append((coalesce_key, frame))
            self._wakeup.set()

    def drain(self, timeout: float) -> List[str]:
        """Block up to `timeout` seconds, then return every buffered frame.

        `_wakeup` is set and cleared under `_lock`, so a frame offered
        between the wait returning and the lock being taken is included in
        this drain rather than lost.
        """
        if not self._wakeup.wait(timeout):
            return []
        with self._lock:
            frames = [frame for _, frame in self._frames]
            self._frames.clear()
            self._wakeup.clear()
        return frames


class EventBus:
    def __init__(self, buffer: int = DEFAULT_BUFFER):
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()
        self._buffer = buffer
        self.published = 0

    # -- subscription ----------------------------------------------------
    def subscribe(self) -> Subscriber:
        sub = Subscriber(self._buffer)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # -- publishing ------------------------------------------------------
    def publish(self, event: str, payload: dict, *,
                coalesce_key: Optional[str] = None) -> None:
        """Fan `payload` out to every subscriber as one pre-built SSE frame.

        `coalesce_key` marks the frame as a snapshot that supersedes any
        buffered frame with the same key (see module docstring).
        """
        try:
            frame = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        except (TypeError, ValueError):
            logger.exception("KOTH event %r is not JSON-serializable; dropped", event)
            return
        with self._lock:
            subs = list(self._subs)
            self.published += 1
        for sub in subs:
            sub.offer(frame, coalesce_key)

    # -- observability ---------------------------------------------------
    def stats(self) -> dict:
        """Live numbers for the admin diagnostics endpoint."""
        with self._lock:
            subs = list(self._subs)
        return {
            'subscribers': len(subs),
            'published': self.published,
            'buffer_limit': self._buffer,
            'max_queue_depth': max((s.depth for s in subs), default=0),
            'total_dropped_frames': sum(s.dropped for s in subs),
            'slow_subscribers': sum(1 for s in subs if s.dropped),
        }


event_bus = EventBus()
