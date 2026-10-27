"""TCP service probing and claim-beacon reading for the KOTH scoring tick.

Two jobs, one socket API:

* `probe_port` / `probe_many` answer "is this service up?" for scoring.
* `probe_beacon` answers "who owns this box?" by reading the token the
  owning team's listener serves.

The KOTH boxes sit on an isolated lab VLAN and cannot reach the portal,
so the portal has to reach them. The beacon is one extra TCP connect per
box per tick, issued in the same bounded fan-out as the service probes —
cheap, local, and crucially NOT a Proxmox API call, which is what the
claim-file transport costs for every box on every tick.

Timeouts are short and are whole-exchange budgets, not per-read: a slow
or hostile box must read as closed rather than hold up the tick.
"""

import logging
import socket
import time
from typing import Callable, List, Literal, Optional, Sequence, Tuple

from .concurrency import map_concurrent

logger = logging.getLogger(__name__)

Status = Literal['open', 'closed']

DEFAULT_TIMEOUT = 2.0
# The beacon is a same-LAN connect to a listener that writes on accept,
# so it gets a tighter budget than a service probe. This is also the
# per-tick cost of a box that is powered off behind a DROP firewall:
# there is no RST, so the probe burns the whole budget every tick.
BEACON_TIMEOUT = 1.5
# Fan-out width for a tick's network work. A 22-box game with ~2.4
# services each is 22 beacons + ~53 probes; at 32 in flight that is one
# round of beacons plus two of probes even when every box is unreachable,
# which has to stay well inside the 12s default tick interval or
# APScheduler starts dropping runs.
DEFAULT_CONCURRENCY = 32
# A claim token is ~43 chars of base64url. 256 bytes is room for a token
# plus a newline and some banner noise, and caps what a hostile root
# inside a box can make us buffer.
BEACON_MAX_BYTES = 256


def probe_port(host: Optional[str], port: int,
               timeout: float = DEFAULT_TIMEOUT) -> Status:
    if not host:
        return 'closed'
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return 'open'
    except (socket.timeout, ConnectionRefusedError, OSError):
        return 'closed'


def probe_beacon(host: Optional[str], port: int,
                 timeout: float = BEACON_TIMEOUT
                 ) -> Tuple[Status, Optional[str]]:
    """Connect once; return (service status, first line the box sent).

    The box is expected to answer a connect with its owner's submission
    token, e.g. `while :; do echo "$TOKEN" | nc -lN -p 31337; done`. We
    send nothing first, so a listener that writes on accept works without
    a protocol.

    `('open', None)` means the port answered but produced no usable line
    within the deadline — the box is up but unclaimed (or the student's
    listener is broken). That is deliberately distinct from
    `('closed', None)`, which means nothing is listening at all.

    `timeout` is the budget for the WHOLE exchange, connect included, not
    per `recv()`. The byte cap alone does not bound time: a box that
    trickles one byte every `timeout - epsilon` and never sends a newline
    would otherwise hold this greenlet for `BEACON_MAX_BYTES x timeout`.
    Students have root on these boxes, so that is an adversary we have by
    design — and 22 of them through a bounded pool would stall the
    scoring tick long enough for APScheduler (max_instances=1) to drop
    every tick behind it, freezing the scoreboard for the whole class.
    """
    if not host:
        return 'closed', None
    chunks: List[bytes] = []
    received = 0
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            while received < BEACON_MAX_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(BEACON_MAX_BYTES - received)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if b'\n' in chunk:
                    break
    except (socket.timeout, ConnectionRefusedError, OSError):
        return 'closed', None

    raw = b''.join(chunks)
    if not raw:
        return 'open', None
    line = raw.split(b'\n', 1)[0].strip()
    if not line:
        return 'open', None
    try:
        return 'open', line.decode('utf-8')
    except UnicodeDecodeError:
        # Some other service is on this port (TLS, binary banner). Up, but
        # not a claim.
        return 'open', None


def probe_many(targets: Sequence[Tuple[Optional[str], int]], *,
               prober: Optional[Callable[..., Status]] = None,
               timeout: float = DEFAULT_TIMEOUT,
               concurrency: int = DEFAULT_CONCURRENCY) -> List[Status]:
    """Probe every `(host, port)` concurrently; results in input order.

    `prober` defaults to `probe_port`. Callers pass their own module's
    reference so the probe stays substitutable from where it is used —
    otherwise a caller's fan-out would silently bind this module's
    function and behave differently from its single-target path.
    """
    fn = prober or probe_port
    results = map_concurrent(
        lambda t: fn(t[0], t[1], timeout=timeout),
        targets, concurrency=concurrency, label='port probe')
    return [r if r is not None else 'closed' for r in results]


def probe_beacons(targets: Sequence[Tuple[Optional[str], int]], *,
                  prober: Optional[Callable[..., Tuple[Status, Optional[str]]]] = None,
                  timeout: float = BEACON_TIMEOUT,
                  concurrency: int = DEFAULT_CONCURRENCY
                  ) -> List[Tuple[Status, Optional[str]]]:
    """Read every `(host, port)` beacon concurrently; results in input order."""
    fn = prober or probe_beacon
    results = map_concurrent(
        lambda t: fn(t[0], t[1], timeout=timeout),
        targets, concurrency=concurrency, label='beacon probe')
    return [r if r is not None else ('closed', None) for r in results]
