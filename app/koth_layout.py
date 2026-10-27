"""Deterministic circular layout for KOTH network map nodes.

The frontend (Cytoscape) does prettier physics-based layout; this module
provides stable initial positions so the map doesn't reshuffle between page
loads, and so the backend can persist a default layout per box.
"""

from math import cos, pi, sin
from typing import Dict, Iterable


def compute_layout(boxes: Iterable[dict], radius: float = 300.0) -> Dict[str, Dict[str, float]]:
    """Place boxes evenly around a circle, sorted by name for determinism."""
    boxes = sorted(boxes, key=lambda b: b['name'])
    n = len(boxes)
    if n == 0:
        return {}
    out = {}
    for i, b in enumerate(boxes):
        angle = 2 * pi * i / n
        out[b['name']] = {
            'x': round(radius * cos(angle), 2),
            'y': round(radius * sin(angle), 2),
        }
    return out
