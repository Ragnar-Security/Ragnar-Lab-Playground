"""Tests for the KOTH SSE event bus.

These cover the three properties that failed in production with a full
classroom connected: a slow client must not be silently cut off the feed,
buffered state snapshots must not accumulate, and a frame must be
serialized once no matter how many subscribers there are.
"""

import json
import unittest

from app.koth_events import EventBus


class EventBusTests(unittest.TestCase):
    def test_publish_delivers_a_complete_sse_frame(self):
        bus = EventBus()
        sub = bus.subscribe()
        bus.publish('scan', {'iteration': 3})
        frames = sub.drain(timeout=0.1)
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith('event: scan\ndata: '))
        self.assertTrue(frames[0].endswith('\n\n'))
        body = frames[0].split('data: ', 1)[1].strip()
        self.assertEqual(json.loads(body), {'iteration': 3})

    def test_every_subscriber_gets_the_identical_frame_object(self):
        # The frame is built once per publish, not once per subscriber.
        # With 60 open tabs the old bus ran json.dumps 60 times per tick.
        bus = EventBus()
        subs = [bus.subscribe() for _ in range(5)]
        bus.publish('scan', {'boxes': [{'id': 1}]})
        frames = [s.drain(timeout=0.1)[0] for s in subs]
        first = frames[0]
        for f in frames[1:]:
            self.assertIs(f, first)

    def test_slow_subscriber_loses_frames_not_its_subscription(self):
        # Regression: the old bus removed a subscriber whose queue filled.
        # Its HTTP connection stayed open, so the browser never saw an
        # error and never reconnected — the scoreboard froze for good.
        bus = EventBus(buffer=4)
        sub = bus.subscribe()
        for i in range(10):
            bus.publish('flag', {'n': i})
        self.assertEqual(bus.subscriber_count, 1,
                         'a slow subscriber must stay subscribed')
        frames = sub.drain(timeout=0.1)
        self.assertEqual(len(frames), 4)
        self.assertGreater(sub.dropped, 0)
        # The frames kept are the NEWEST ones; a scoreboard that resumes
        # should resume at current state, not at ancient state.
        self.assertIn('"n": 9', frames[-1])

    def test_scan_frames_coalesce_to_the_latest(self):
        # A scan is a complete snapshot, so buffering several is waste —
        # and at 22 boxes each one is kilobytes of retained payload.
        bus = EventBus(buffer=8)
        sub = bus.subscribe()
        for i in range(5):
            bus.publish('scan', {'iteration': i}, coalesce_key='scan')
        frames = sub.drain(timeout=0.1)
        self.assertEqual(len(frames), 1)
        self.assertIn('"iteration": 4', frames[0])
        self.assertEqual(sub.dropped, 0, 'coalescing is not a drop')

    def test_coalescing_preserves_interleaved_log_events(self):
        # Claims and flags are one-shot log lines; they must survive a
        # burst of snapshots that supersede each other.
        bus = EventBus(buffer=8)
        sub = bus.subscribe()
        bus.publish('scan', {'iteration': 1}, coalesce_key='scan')
        bus.publish('claim', {'box_name': 'web-01'})
        bus.publish('scan', {'iteration': 2}, coalesce_key='scan')
        bus.publish('flag', {'flag_name': 'root'})
        frames = sub.drain(timeout=0.1)
        self.assertEqual(len(frames), 3)
        joined = ''.join(frames)
        self.assertIn('web-01', joined)
        self.assertIn('root', joined)
        self.assertIn('"iteration": 2', joined)
        self.assertNotIn('"iteration": 1', joined)

    def test_drain_returns_empty_on_timeout(self):
        bus = EventBus()
        sub = bus.subscribe()
        self.assertEqual(sub.drain(timeout=0.01), [])

    def test_unsubscribe_stops_delivery_and_is_idempotent(self):
        bus = EventBus()
        sub = bus.subscribe()
        bus.unsubscribe(sub)
        bus.unsubscribe(sub)  # double-unsubscribe must not raise
        bus.publish('scan', {'iteration': 1})
        self.assertEqual(sub.drain(timeout=0.01), [])
        self.assertEqual(bus.subscriber_count, 0)

    def test_unserializable_payload_is_dropped_not_raised(self):
        # The scoring tick publishes inside the scheduler thread; an
        # exception there would kill the tick, not just the frame.
        bus = EventBus()
        sub = bus.subscribe()
        bus.publish('scan', {'bad': object()})
        self.assertEqual(sub.drain(timeout=0.01), [])
        self.assertEqual(bus.subscriber_count, 1)

    def test_stats_report_slow_subscribers(self):
        bus = EventBus(buffer=2)
        healthy = bus.subscribe()
        slow = bus.subscribe()
        for i in range(5):
            bus.publish('flag', {'n': i})
        healthy.drain(timeout=0.1)
        stats = bus.stats()
        self.assertEqual(stats['subscribers'], 2)
        self.assertEqual(stats['published'], 5)
        self.assertEqual(stats['slow_subscribers'], 2)
        self.assertGreater(stats['total_dropped_frames'], 0)
        self.assertEqual(stats['max_queue_depth'], slow.depth)


if __name__ == '__main__':
    unittest.main()
