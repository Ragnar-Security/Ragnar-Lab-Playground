import unittest
from unittest.mock import patch

from app.routes.vm_creation import _cleanup_with_sweep


class ExactCleanupSweepTestCase(unittest.TestCase):
    def _run_sweep(self, inventory, **kwargs):
        captured = []

        def cleanup(payload):
            captured.extend(payload)
            return {item['vmid'] for item in payload}

        with patch('app.routes.vm_creation.find_vms_by_name_prefix',
                   return_value=inventory), \
             patch('app.routes.vm_creation.cleanup_vms',
                   side_effect=cleanup):
            _cleanup_with_sweep([], **kwargs)
        return captured

    def test_hyphenated_group_slug_neighbour_is_not_swept(self):
        inventory = [
            {'vmid': 501, 'node': 'pve', 'vm_type': 'qemu',
             'name': 'group-team-router'},
            {'vmid': 502, 'node': 'pve', 'vm_type': 'qemu',
             'name': 'group-team-a-router'},
        ]

        swept = self._run_sweep(
            inventory,
            templates=[{'name': 'router'}],
            owning_identifier='team',
            owner_group_id=1,
        )

        self.assertEqual([vm['vmid'] for vm in swept], [501])

    def test_admins_other_lab_is_not_swept_when_new_lab_fails(self):
        inventory = [
            {'vmid': 601, 'node': 'pve', 'vm_type': 'qemu',
             'name': 'student-admin-lab-b'},
            {'vmid': 602, 'node': 'pve', 'vm_type': 'qemu',
             'name': 'student-admin-lab-a'},
        ]

        swept = self._run_sweep(
            inventory,
            templates=[{'name': 'lab-b'}],
            owning_identifier='admin',
        )

        self.assertEqual([vm['vmid'] for vm in swept], [601])


if __name__ == '__main__':
    unittest.main()
