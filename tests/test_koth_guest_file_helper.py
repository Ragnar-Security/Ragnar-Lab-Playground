"""Tests for the `_read_guest_file` Proxmox guest-agent file helper.

The helper is the single chokepoint for reading the per-box claim file
during a KOTH scoring tick. We mock the proxmoxer client chain because
the real one needs a live cluster.

Note: Proxmox's REST API returns the file content already decoded as a
Python string in the `content` field (verified empirically against
Proxmox 8.x — the API does the base64 decode before handing back to
proxmoxer). The helper does NOT base64-decode again. An earlier version
did, which silently broke claim polling — these tests exist to keep that
regression from coming back.
"""

import unittest
from unittest.mock import MagicMock

from app import create_app
from app.routes.utils import _read_guest_file


class ReadGuestFileTests(unittest.TestCase):
    def setUp(self):
        # The helper logs via current_app at debug; spin up an app context
        # so the logger calls don't blow up in the rare paths that touch it.
        self.app = create_app(testing=True)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def _mock(self, return_value=None, side_effect=None):
        proxmox = MagicMock()
        chain = proxmox.nodes.return_value.qemu.return_value \
            .agent.return_value.get
        if side_effect is not None:
            chain.side_effect = side_effect
        else:
            chain.return_value = return_value
        return proxmox

    def test_returns_text_content_directly(self):
        # Regression: the Proxmox REST layer hands `content` back as a
        # decoded Python str. We must NOT base64-decode it; the previous
        # implementation did and silently produced garbage in production.
        proxmox = self._mock(return_value={
            'bytes-read': 12, 'content': 'hello-token\n',
        })
        result = _read_guest_file(proxmox, 'pve', 100, '/koth/claim')
        self.assertEqual(result, 'hello-token\n')

    def test_returns_none_on_proxmox_exception(self):
        proxmox = self._mock(side_effect=Exception('agent unreachable'))
        result = _read_guest_file(proxmox, 'pve', 100, '/koth/claim')
        self.assertIsNone(result)

    def test_returns_none_when_response_missing_content(self):
        for response in ({}, {'content': ''}, None):
            proxmox = self._mock(return_value=response)
            self.assertIsNone(
                _read_guest_file(proxmox, 'pve', 100, '/koth/claim'),
                f'expected None for response={response!r}')

    def test_returns_none_when_content_not_a_string(self):
        # Defensive: a malformed/unexpected response shape (e.g. content
        # as bytes from a future proxmoxer version) shouldn't crash the
        # whole tick.
        for bad in (b'binary-bytes', 12345, ['list'], {'nested': True}):
            proxmox = self._mock(return_value={'content': bad})
            self.assertIsNone(
                _read_guest_file(proxmox, 'pve', 100, '/koth/claim'),
                f'expected None for content={bad!r}')

    def test_caps_at_4kb_chars(self):
        # 10KB of plain text is truncated to 4096 chars at read time.
        big_text = 'a' * 10240
        proxmox = self._mock(return_value={'content': big_text})
        result = _read_guest_file(proxmox, 'pve', 100, '/koth/claim')
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 4096)

    def test_passes_file_param_to_proxmox(self):
        # Sanity: the path actually flows through to the proxmoxer call.
        proxmox = self._mock(return_value={'content': 'tok'})
        _read_guest_file(proxmox, 'pve2', 9423, '/koth/claim')
        chain = proxmox.nodes.return_value.qemu.return_value \
            .agent.return_value.get
        chain.assert_called_once_with(file='/koth/claim')

    def test_returns_none_on_empty_path(self):
        proxmox = MagicMock()
        result = _read_guest_file(proxmox, 'pve', 100, '')
        self.assertIsNone(result)
        # Helper must short-circuit before talking to proxmox.
        proxmox.nodes.assert_not_called()

    def test_returns_none_on_none_path(self):
        proxmox = MagicMock()
        result = _read_guest_file(proxmox, 'pve', 100, None)
        self.assertIsNone(result)
        proxmox.nodes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
