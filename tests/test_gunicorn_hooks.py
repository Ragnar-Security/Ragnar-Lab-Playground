"""Verify gunicorn lifecycle hooks call koth_scorer.stop_scheduler.

The bug we're guarding against: when a worker receives SIGTERM/SIGINT
during graceful shutdown, Python's atexit handlers shut down APScheduler's
ThreadPoolExecutor before the scheduler thread notices. The scheduler
keeps trying to submit ticks and spams "cannot schedule new futures
after shutdown" until the process exits. Wiring stop_scheduler() into
worker_int / worker_abort / worker_exit prevents that.
"""

import importlib
import sys
import unittest
from unittest.mock import MagicMock, patch


class GunicornHooksTestCase(unittest.TestCase):
    def setUp(self):
        # Re-import the gunicorn config fresh each test in case a prior
        # test patched globals on it.
        if 'gunicorn_conf' in sys.modules:
            del sys.modules['gunicorn_conf']
        # gunicorn.conf.py isn't a python package member; import by spec.
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            'gunicorn_conf',
            os.path.join(os.path.dirname(__file__), '..', 'gunicorn.conf.py'))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_worker_class_is_gevent(self):
        self.assertEqual(self.mod.worker_class, 'gevent')

    def test_workers_is_one(self):
        # APScheduler is in-process; multiple workers would race ticks.
        self.assertEqual(self.mod.workers, 1)

    def test_worker_int_calls_stop_scheduler(self):
        with patch('app.koth_scorer.stop_scheduler') as mock_stop:
            self.mod.worker_int(MagicMock())
            mock_stop.assert_called_once()

    def test_worker_abort_calls_stop_scheduler(self):
        with patch('app.koth_scorer.stop_scheduler') as mock_stop:
            self.mod.worker_abort(MagicMock())
            mock_stop.assert_called_once()

    def test_worker_exit_calls_stop_scheduler(self):
        with patch('app.koth_scorer.stop_scheduler') as mock_stop:
            self.mod.worker_exit(MagicMock(), MagicMock())
            mock_stop.assert_called_once()

    def test_hooks_swallow_stop_scheduler_failure(self):
        # If stop_scheduler raises, the hook must not propagate — we don't
        # want a buggy scheduler to block worker shutdown.
        with patch('app.koth_scorer.stop_scheduler',
                   side_effect=RuntimeError('boom')):
            # Each hook should swallow the exception cleanly.
            self.mod.worker_int(MagicMock())
            self.mod.worker_abort(MagicMock())
            self.mod.worker_exit(MagicMock(), MagicMock())

    def test_worker_recycling_is_disabled(self):
        # This worker is not stateless: it owns the KOTH APScheduler and
        # the in-memory SSE event bus, so a recycle drops every open
        # scoreboard stream and restarts the scoring loop mid-game. The
        # previous max_requests=5000 fired roughly every 17 minutes of
        # classroom play. Memory is now bounded by the event bus itself.
        self.assertEqual(self.mod.max_requests, 0)

    def test_single_gevent_worker(self):
        # A second worker means a second APScheduler racing the first and
        # SSE events fanning out to the wrong subscriber set.
        self.assertEqual(self.mod.workers, 1)
        self.assertEqual(self.mod.worker_class, 'gevent')


if __name__ == '__main__':
    unittest.main()
