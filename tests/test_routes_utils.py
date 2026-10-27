"""
Unit tests for app/routes/utils.py helpers that don't require app context.
"""

import unittest

from app.routes.utils import mask_ip_address


class MaskIPAddressTestCase(unittest.TestCase):
    def test_masks_single_ipv4(self):
        self.assertEqual(mask_ip_address('10.101.4.56'), '10.101.4.xxx')

    def test_masks_each_ip_in_comma_list(self):
        self.assertEqual(
            mask_ip_address('10.101.4.56, 10.101.4.57'),
            '10.101.4.xxx, 10.101.4.xxx',
        )

    def test_passes_through_sentinel_na(self):
        self.assertEqual(mask_ip_address('N/A'), 'N/A')

    def test_passes_through_sentinel_dash(self):
        self.assertEqual(mask_ip_address('-'), '-')

    def test_passes_through_sentinel_config_error(self):
        self.assertEqual(mask_ip_address('CONFIG ERROR'), 'CONFIG ERROR')

    def test_passes_through_sentinel_starting(self):
        self.assertEqual(mask_ip_address('Starting...'), 'Starting...')

    def test_passes_through_empty_string(self):
        self.assertEqual(mask_ip_address(''), '')

    def test_unparseable_part_is_left_alone(self):
        # A garbage value mixed with a valid IP: mask only the valid one.
        self.assertEqual(
            mask_ip_address('10.0.0.5, not-an-ip'),
            '10.0.0.xxx, not-an-ip',
        )

    def test_does_not_mask_non_ipv4_string(self):
        self.assertEqual(mask_ip_address('hostname.local'), 'hostname.local')


if __name__ == '__main__':
    unittest.main()
