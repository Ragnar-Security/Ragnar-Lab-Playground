import unittest
from app.koth_scoring import compute_team_deltas


class ScoringTestCase(unittest.TestCase):
    def test_unowned_box_scores_zero(self):
        boxes = [{'owner_team_id': None, 'services': []}]
        out = compute_team_deltas(boxes, iteration=1, box_ownership_score=1,
                                  port_open_score=3, exp_scoring=False, exp_val=60)
        self.assertEqual(out, {})

    def test_owned_box_scores_ownership(self):
        boxes = [{'owner_team_id': 7, 'services': []}]
        out = compute_team_deltas(boxes, iteration=1, box_ownership_score=1,
                                  port_open_score=3, exp_scoring=False, exp_val=60)
        self.assertEqual(out, {7: 1.0})

    def test_open_service_adds_port_score(self):
        boxes = [{'owner_team_id': 7, 'services': [
            {'name': 'SSH', 'port': 22, 'status': 'open'},
            {'name': 'HTTP', 'port': 80, 'status': 'closed'},
        ]}]
        out = compute_team_deltas(boxes, iteration=1, box_ownership_score=1,
                                  port_open_score=3, exp_scoring=False, exp_val=60)
        self.assertEqual(out, {7: 4.0})  # 1 + 3

    def test_exp_growth_increases_with_iteration(self):
        boxes = [{'owner_team_id': 7, 'services': []}]
        a = compute_team_deltas(boxes, iteration=1, box_ownership_score=1,
                                port_open_score=3, exp_scoring=True, exp_val=60)
        b = compute_team_deltas(boxes, iteration=120, box_ownership_score=1,
                                port_open_score=3, exp_scoring=True, exp_val=60)
        self.assertGreater(b[7], a[7])

    def test_aggregates_across_boxes(self):
        boxes = [
            {'owner_team_id': 7, 'services': []},
            {'owner_team_id': 7, 'services': []},
            {'owner_team_id': 8, 'services': []},
        ]
        out = compute_team_deltas(boxes, iteration=1, box_ownership_score=1,
                                  port_open_score=3, exp_scoring=False, exp_val=60)
        self.assertEqual(out, {7: 2.0, 8: 1.0})
