"""Bounded concurrent map, correct under gevent and under plain threads.

The production worker runs `gevent.monkey.patch_all()` (run_prod.py), so a
`ThreadPoolExecutor` here is really a greenlet pool and a socket timeout
yields instead of blocking. Tests and `flask run` are NOT monkey-patched,
so the same helper has to work with real threads.

Why this exists: the KOTH scoring tick used to do every Proxmox
guest-agent read and every TCP service probe one after another. With 22
boxes and a 2 s probe timeout a single tick could outrun its own 12 s
interval, which APScheduler reports as "maximum number of running
instances reached" and silently skips.

`fn` MUST NOT touch the SQLAlchemy session: Flask-SQLAlchemy's scoped
session is bound to the calling thread/greenlet, so workers here would
either get a different session or none at all. Pass plain data in, get
plain data out, do the DB writes on the caller.
"""

import logging
from typing import Callable, Iterable, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar('T')
R = TypeVar('R')


def _gevent_pool_map(fn, items, width) -> Optional[list]:
    """Run `fn` over `items` on a gevent pool, or return None if gevent
    isn't the active concurrency model (tests, `flask run`)."""
    try:
        import gevent.monkey
        from gevent.pool import Pool
    except ImportError:
        return None
    if not gevent.monkey.is_module_patched('socket'):
        # gevent is installed but we're not running on it. A gevent pool
        # here would block the whole process on the first real socket.
        return None
    return list(Pool(width).imap(fn, items))


def map_concurrent(fn: Callable[[T], R], items: Iterable[T], *,
                   concurrency: int = 8,
                   label: str = 'task') -> List[Optional[R]]:
    """Apply `fn` to every item with at most `concurrency` in flight.

    Results come back in input order. An item whose call raises is logged
    and yields `None` rather than aborting the batch — every caller here
    is a best-effort poll where one dead box must not cost the tick.
    """
    items = list(items)
    if not items:
        return []

    def guarded(item):
        try:
            return fn(item)
        except Exception:
            logger.exception("%s failed for %r", label, item)
            return None

    width = min(max(1, concurrency), len(items))
    if width == 1:
        return [guarded(item) for item in items]

    results = _gevent_pool_map(guarded, items, width)
    if results is None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=width) as pool:
            results = list(pool.map(guarded, items))
    return results
