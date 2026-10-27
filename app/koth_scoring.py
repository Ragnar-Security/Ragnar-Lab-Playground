"""Pure scoring math for KOTH. No DB or IO — easy to unit test.

Algorithm mirrors koth/app.js:calculate_score():
- For each box owned by a team:
    delta = box_ownership_score
    delta += port_open_score for each open service
    if exp_scoring: delta *= 1 + iteration/exp_val
- Sum deltas per team.
"""

from typing import Dict, Iterable


def compute_team_deltas(
    boxes: Iterable[dict],
    *,
    iteration: int,
    box_ownership_score: float,
    port_open_score: float,
    exp_scoring: bool,
    exp_val: int,
) -> Dict[int, float]:
    deltas: Dict[int, float] = {}
    factor = (1 + iteration / exp_val) if exp_scoring else 1.0
    for box in boxes:
        owner = box.get('owner_team_id')
        if owner is None:
            continue
        delta = box_ownership_score
        for svc in (box.get('services') or []):
            if svc.get('status') == 'open':
                delta += port_open_score
        delta *= factor
        deltas[owner] = round(deltas.get(owner, 0.0) + delta, 2)
    return deltas
