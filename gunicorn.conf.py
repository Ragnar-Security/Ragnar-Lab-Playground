"""Gunicorn configuration file for production deployment."""

import logging
import os

# Server socket
bind = "0.0.0.0:5000"
backlog = 2048

# Worker processes.
#
# Stay at workers=1 on purpose: APScheduler runs the KOTH scoring tick
# in-process. A second worker would spawn a second scheduler racing the
# first one — and the in-memory event_bus only fans out to subscribers in
# the same process anyway, so multi-worker would also break SSE delivery.
#
# worker_class=gevent: each open SSE client (KOTH live scoreboard) used
# to cost one OS thread (~8 MB stack + 1 FD). With 22 teams × 2-3 tabs
# the gthread model exhausted the container's 1024-FD ulimit and crashed
# the worker with EMFILE. gevent gives us greenlets that cost ~bytes
# each and multiplex on a single OS thread via epoll, so a class-sized
# fan-out fits comfortably. monkey.patch_all() runs at the top of
# run_prod.py before any stdlib import.
workers = 1
worker_class = "gevent"
# worker_connections caps concurrent greenlets (HTTP + SSE + scheduler tick
# + Proxmox API calls). 2000 is generous; the real ceiling is the FD budget
# we set in docker-compose.prod.yml (65536) and the tiny per-greenlet cost.
worker_connections = 2000
timeout = 120
graceful_timeout = 10
keepalive = 5

# Worker recycling is OFF on purpose.
#
# This worker is not stateless: it owns the KOTH APScheduler and the
# in-memory SSE event bus. A recycle drops every open scoreboard stream
# and restarts the scoring loop mid-game. With max_requests=5000 and the
# old client behaviour (every tab re-fetched full state on every tick,
# ~5 req/s for a class) that fired roughly every 17 minutes of gameplay.
#
# The client now renders from the pushed event instead of polling, and
# the event bus is bounded and coalescing, so the growth this guarded
# against is gone. If memory growth reappears, fix the growth — recycling
# this process is not a safe mitigation for it. Watch /koth/api/_diag
# during a game.
max_requests = 0

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = "proxmox-vm-automation"


# --- worker lifecycle hooks --------------------------------------------------
def on_starting(server):
    """Called just before the master process is initialized."""
    pass


def post_worker_init(worker):
    """Called just after a worker has been initialized."""
    # Sequential worker ID for log correlation.
    os.environ['GUNICORN_WORKER_ID'] = str(worker.age)


def _shutdown_koth_scheduler(reason):
    """Stop the in-process APScheduler before atexit shuts down its
    ThreadPoolExecutor.

    Without this, a graceful worker restart triggers Python's atexit
    handlers (which close the executor) before the scheduler's main
    loop notices, and the loop keeps trying to submit ticks to the
    closed pool — spamming "cannot schedule new futures after shutdown"
    until the process actually exits.
    """
    try:
        from app import koth_scorer
        koth_scorer.stop_scheduler()
    except Exception:
        # Never let a hook crash the worker shutdown path.
        logging.getLogger(__name__).exception(
            "gunicorn %s hook: stop_scheduler failed", reason)


def worker_int(worker):
    """SIGINT / SIGQUIT — graceful shutdown."""
    _shutdown_koth_scheduler('worker_int')


def worker_abort(worker):
    """SIGABRT — also stop the scheduler so the half-dead process doesn't
    keep logging scheduler errors until the master kills it."""
    _shutdown_koth_scheduler('worker_abort')


def worker_exit(server, worker):
    """Final cleanup hook called by gunicorn after worker exit."""
    _shutdown_koth_scheduler('worker_exit')
