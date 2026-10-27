import unittest
from app.koth_layout import compute_layout


class LayoutTestCase(unittest.TestCase):
    def test_deterministic_for_same_inputs(self):
        boxes = [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}]
        a = compute_layout(boxes)
        b = compute_layout(boxes)
        self.assertEqual(a, b)

    def test_one_position_per_box(self):
        boxes = [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}]
        out = compute_layout(boxes)
        self.assertEqual(set(out.keys()), {'a', 'b', 'c'})
        for pos in out.values():
            self.assertIn('x', pos)
            self.assertIn('y', pos)
            self.assertIsInstance(pos['x'], (int, float))
            self.assertIsInstance(pos['y'], (int, float))

    def test_empty(self):
        self.assertEqual(compute_layout([]), {})
