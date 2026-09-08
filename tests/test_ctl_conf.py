"""Tests for ctl.conf rendering and the export table."""

import os
import tempfile
import unittest
from unittest import mock

from cinder_zfs_driver import ctl

VOL_A = 'volume-11111111-1111-1111-1111-111111111111'
VOL_B = 'volume-22222222-2222-2222-2222-222222222222'


def make_export(name=VOL_A, **kwargs):
    """Return an Export for the named volume with the given overrides."""
    defaults = {
        'volume_name': name,
        'iqn': f'iqn.2010-10.org.openstack:{name}',
        'device_path': f'/dev/zvol/zroot/cinder/{name}',
    }
    defaults.update(kwargs)
    return ctl.Export(**defaults)


class TestRender(unittest.TestCase):
    """render turns an export list into a complete ctl.conf."""

    def test_portal_group_listen(self):
        """The shared portal group carries the configured listen address."""
        text = ctl.render([], '192.0.2.5:3260')
        self.assertIn('portal-group "cinder" {', text)
        self.assertIn('    listen 192.0.2.5:3260', text)
        self.assertIn('    discovery-auth-group no-authentication', text)

    def test_target_stanza(self):
        """Each export becomes one target with its zvol at LUN 0."""
        text = ctl.render([make_export()], '127.0.0.1:3260')
        self.assertIn(f'target "iqn.2010-10.org.openstack:{VOL_A}" {{', text)
        self.assertIn('    portal-group "cinder"', text)
        self.assertIn(
            f'    lun 0 {{ path "/dev/zvol/zroot/cinder/{VOL_A}" }}',
            text,
        )

    def test_chap_line_only_with_credentials(self):
        """CHAP renders when credentials exist, no-authentication otherwise."""
        with_chap = ctl.render(
            [make_export(chap_username='user', chap_password='secret123')],
            '127.0.0.1:3260',
        )
        self.assertIn('    chap "user" "secret123"', with_chap)
        self.assertNotIn('auth-group no-authentication\n}', with_chap)
        without = ctl.render([make_export()], '127.0.0.1:3260')
        self.assertNotIn('chap "', without)
        self.assertIn('    auth-group no-authentication', without)

    def test_deterministic_order(self):
        """The same table renders the same bytes regardless of insert order."""
        a, b = make_export(VOL_A), make_export(VOL_B)
        self.assertEqual(
            ctl.render([a, b], '127.0.0.1:3260'),
            ctl.render([b, a], '127.0.0.1:3260'),
        )
        text = ctl.render([b, a], '127.0.0.1:3260')
        self.assertLess(text.index(VOL_A), text.index(VOL_B))

    def test_hostile_values_refused(self):
        """Anything that could become config syntax is refused outright."""
        with self.assertRaises(ValueError):
            make_export(name='volume-x"; } target "evil')
        with self.assertRaises(ValueError):
            make_export(chap_username='u', chap_password='se"cret')
        with self.assertRaises(ValueError):
            make_export(device_path='/etc/master.passwd')
        with self.assertRaises(ValueError):
            ctl.render([], 'listen "evil"')

    def test_chap_credentials_go_together(self):
        """A CHAP username without a password is refused."""
        with self.assertRaises(ValueError):
            make_export(chap_username='user', chap_password=None)


class TestCtlTable(unittest.TestCase):
    """CtlTable mirrors its exports to the file and reloads ctld."""

    def setUp(self):
        """Build a table over a temp ctl.conf with an observable reload."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'ctl.conf')
        self.reload = mock.Mock()
        self.table = ctl.CtlTable(self.path, '127.0.0.1:3260', self.reload)

    def _read(self):
        """Return the current content of the temp ctl.conf."""
        with open(self.path) as handle:
            return handle.read()

    def test_add_writes_and_reloads(self):
        """Adding an export writes the file and reloads ctld once."""
        self.table.add(make_export())
        self.assertIn(VOL_A, self._read())
        self.reload.assert_called_once_with()

    def test_add_same_export_again_is_a_noop(self):
        """Re-adding an identical export does not reload ctld."""
        self.table.add(make_export())
        self.table.add(make_export())
        self.reload.assert_called_once_with()

    def test_add_replaces_a_changed_export(self):
        """The same volume with new credentials is a real change."""
        self.table.add(make_export())
        self.table.add(make_export(chap_username='u', chap_password='p1234'))
        self.assertIn('chap "u" "p1234"', self._read())
        self.assertEqual(2, self.reload.call_count)

    def test_remove_deletes_stanza(self):
        """Removing one export leaves the others in place."""
        self.table.add(make_export(VOL_A))
        self.table.add(make_export(VOL_B))
        self.table.remove(VOL_A)
        text = self._read()
        self.assertNotIn(VOL_A, text)
        self.assertIn(VOL_B, text)

    def test_remove_absent_is_success(self):
        """Removing an unknown volume converges the file, then goes quiet."""
        self.table.remove(VOL_A)
        self.assertIn('portal-group "cinder"', self._read())
        self.table.remove(VOL_A)
        self.reload.assert_called_once_with()

    def test_remove_withdraws_a_stanza_from_a_previous_process(self):
        """A remove after a restart withdraws a stale stanza."""
        old = ctl.CtlTable(self.path, '127.0.0.1:3260', mock.Mock())
        old.add(make_export())
        self.table.remove(VOL_A)
        self.assertNotIn(VOL_A, self._read())
        self.reload.assert_called_once_with()

    def test_file_mode_is_group_readable_only(self):
        """The written file has mode 0640."""
        self.table.add(make_export(chap_username='u', chap_password='p1234'))
        self.assertEqual(0o640, os.stat(self.path).st_mode & 0o777)


if __name__ == '__main__':
    unittest.main()
