import socket
import threading
import time
import unittest

from app.koth_scanner import probe_beacon, probe_beacons, probe_many, probe_port


class _DripListener:
    """A hostile box: accepts, then trickles bytes forever, no newline.

    Students have root on the KOTH boxes by design, so this is reachable
    by the intended adversary.
    """

    def __init__(self, interval=0.05):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.interval = interval
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                while not self._stop.is_set():
                    conn.sendall(b'A')
                    time.sleep(self.interval)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self.sock.close()


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Listener:
    """A one-shot TCP listener that optionally writes `payload` on accept.

    `payload=None` accepts and holds the connection without writing, which
    is the "service is up but serves no claim token" case.
    """

    def __init__(self, payload=None, accepts=1):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.payload = payload
        self._accepts = accepts
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        for _ in range(self._accepts):
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self.payload is not None:
                    conn.sendall(self.payload)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


class ProbePortTests(unittest.TestCase):
    def test_open_port_reports_open(self):
        listener = _Listener()
        try:
            self.assertEqual(probe_port('127.0.0.1', listener.port, timeout=1.0),
                             'open')
        finally:
            listener.close()

    def test_closed_port_reports_closed(self):
        self.assertEqual(probe_port('127.0.0.1', _free_port(), timeout=0.5),
                         'closed')

    def test_missing_host_reports_closed(self):
        # A box whose IP has not been discovered yet must read as closed
        # rather than raise inside the scoring tick.
        self.assertEqual(probe_port(None, 22), 'closed')
        self.assertEqual(probe_port('', 22), 'closed')


class ProbeBeaconTests(unittest.TestCase):
    def test_beacon_returns_the_served_token(self):
        listener = _Listener(payload=b'tok-abc123\n')
        try:
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=1.0)
        finally:
            listener.close()
        self.assertEqual(status, 'open')
        self.assertEqual(token, 'tok-abc123')

    def test_beacon_without_trailing_newline_still_parses(self):
        # `printf '%s' "$TOKEN" | nc -l` sends no newline.
        listener = _Listener(payload=b'tok-xyz')
        try:
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=1.0)
        finally:
            listener.close()
        self.assertEqual((status, token), ('open', 'tok-xyz'))

    def test_beacon_takes_only_the_first_line(self):
        listener = _Listener(payload=b'tok-first\ntok-second\n')
        try:
            _, token = probe_beacon('127.0.0.1', listener.port, timeout=1.0)
        finally:
            listener.close()
        self.assertEqual(token, 'tok-first')

    def test_beacon_caps_what_a_hostile_box_can_feed_us(self):
        # A root-owned box could otherwise stream megabytes at the scorer
        # once per tick. The cap is 256 bytes with no newline in sight.
        listener = _Listener(payload=b'A' * 10000)
        try:
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=1.0)
        finally:
            listener.close()
        self.assertEqual(status, 'open')
        self.assertLessEqual(len(token), 256)

    def test_slow_drip_box_cannot_hold_the_tick(self):
        # The byte cap bounds memory, not time. A box that trickles one
        # byte per interval and never sends a newline would otherwise hold
        # the greenlet for BEACON_MAX_BYTES x interval. Through a pool of
        # 16, 22 such boxes stall the whole scoring tick, and APScheduler
        # (max_instances=1) then silently drops every tick behind it:
        # frozen scoreboard, no scoring, for the entire class.
        listener = _DripListener(interval=0.05)
        try:
            t0 = time.monotonic()
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=0.5)
            elapsed = time.monotonic() - t0
        finally:
            listener.close()
        self.assertEqual(status, 'open')
        self.assertLess(
            elapsed, 2.0,
            'probe_beacon must honour an overall deadline, not a per-recv '
            'timeout — took %.1fs against a 0.5s budget' % elapsed)
        # Whatever it managed to read is still a usable (if bogus) line.
        self.assertTrue(token is None or len(token) <= 256)

    def test_open_but_silent_port_is_up_with_no_token(self):
        # Distinguishing this from 'closed' is what lets the scoreboard
        # show a box as alive but unclaimed.
        listener = _Listener(payload=None)
        try:
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=0.3)
        finally:
            listener.close()
        self.assertEqual(status, 'open')
        self.assertIsNone(token)

    def test_closed_port_has_no_token(self):
        self.assertEqual(probe_beacon('127.0.0.1', _free_port(), timeout=0.5),
                         ('closed', None))

    def test_binary_banner_is_up_but_not_a_claim(self):
        # Some other service answering on the claim port must not be
        # mistaken for a malformed token.
        listener = _Listener(payload=b'\x16\x03\x01\xff\xfe\n')
        try:
            status, token = probe_beacon('127.0.0.1', listener.port, timeout=1.0)
        finally:
            listener.close()
        self.assertEqual(status, 'open')
        self.assertIsNone(token)


class FanOutTests(unittest.TestCase):
    def test_probe_many_preserves_input_order(self):
        # The scorer zips results back onto (box, service) pairs, so an
        # out-of-order result would assign a status to the wrong service.
        listener = _Listener(accepts=4)
        closed = _free_port()
        targets = [('127.0.0.1', listener.port), ('127.0.0.1', closed),
                   ('127.0.0.1', listener.port), (None, 22)]
        try:
            statuses = probe_many(targets, timeout=1.0, concurrency=4)
        finally:
            listener.close()
        self.assertEqual(statuses, ['open', 'closed', 'open', 'closed'])

    def test_probe_many_uses_the_injected_prober(self):
        seen = []

        def fake(host, port, timeout=None):
            seen.append(port)
            return 'open'

        statuses = probe_many([('h', 1), ('h', 2)], prober=fake, concurrency=2)
        self.assertEqual(statuses, ['open', 'open'])
        self.assertEqual(sorted(seen), [1, 2])

    def test_a_raising_prober_degrades_to_closed(self):
        # One box that blows up must not abort scoring for the rest.
        def boom(host, port, timeout=None):
            if port == 2:
                raise RuntimeError('network gone')
            return 'open'

        statuses = probe_many([('h', 1), ('h', 2), ('h', 3)],
                              prober=boom, concurrency=3)
        self.assertEqual(statuses, ['open', 'closed', 'open'])

    def test_probe_beacons_degrades_to_closed_tuple(self):
        def boom(host, port, timeout=None):
            raise RuntimeError('nope')

        self.assertEqual(
            probe_beacons([('h', 1), ('h', 2)], prober=boom, concurrency=2),
            [('closed', None), ('closed', None)])

    def test_empty_target_list_is_a_no_op(self):
        self.assertEqual(probe_many([]), [])
        self.assertEqual(probe_beacons([]), [])


if __name__ == '__main__':
    unittest.main()
