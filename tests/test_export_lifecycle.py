"""Tests for the export lifecycle and both connection kinds."""

import os
import tempfile
import types
import unittest
from unittest import mock

from cinder import exception
from cinder.volume import configuration
from oslo_config import cfg

from cinder_zfs_driver import ctl
from cinder_zfs_driver import driver as driver_mod

CONF = cfg.CONF

VOL_ID = '123e4567-e89b-42d3-a456-426614174000'
VOL_NAME = f'volume-{VOL_ID}'
IQN = f'iqn.2010-10.org.openstack:{VOL_NAME}'
LOCAL = {'host': 'storage1'}
REMOTE = {'host': 'compute7.example.com'}


def make_volume(**kwargs):
    """Return a volume stub with the given field overrides."""
    fields = {
        'name': VOL_NAME,
        'id': VOL_ID,
        'provider_location': None,
        'provider_auth': None,
    }
    fields.update(kwargs)
    return types.SimpleNamespace(**fields)


def make_exported_volume():
    """Return a volume stub carrying remote-export provider fields."""
    return make_volume(
        provider_location=f'192.0.2.10:3260,1 {IQN} 0',
        provider_auth='CHAP chapuser chapsecret123',
    )


class ExportTestCase(unittest.TestCase):
    """Shared fixture: a driver on storage1 with a temp ctl.conf."""

    def setUp(self):
        """Build the driver and point its config at a temp directory."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.driver = self._driver()
        for name, value in [
            ('target_ip_address', '192.0.2.10'),
            ('zfs_dataset', 'zroot/cinder'),
        ]:
            CONF.set_override(name, value, group='backend_defaults')
            self.addCleanup(
                CONF.clear_override, name, group='backend_defaults'
            )

    def _driver(self):
        """Return a driver whose node names are storage1 and its FQDN."""
        with (
            mock.patch.object(
                driver_mod.socket, 'gethostname', return_value='storage1'
            ),
            mock.patch.object(
                driver_mod.socket,
                'getfqdn',
                return_value='storage1.example.com',
            ),
        ):
            return driver_mod.ZFSVolumeDriver(
                configuration=configuration.Configuration(
                    driver_mod.zfs_opts, config_group='zfs'
                ),
                execute=mock.Mock(),
            )

    def _wire_ctl(self, driver=None):
        """Install a CtlTable over the temp ctl.conf with a mock reload."""
        driver = driver or self.driver
        reload = mock.Mock()
        driver._ctl_table = ctl.CtlTable(
            os.path.join(self.tmp.name, 'ctl.conf'), driver._portal(), reload
        )
        return reload

    def _conf_text(self):
        """Return the current content of the temp ctl.conf."""
        with open(os.path.join(self.tmp.name, 'ctl.conf')) as handle:
            return handle.read()

    def _set_chap(self, use=True, username='', password=''):
        """Override the CHAP options for this test."""
        for name, value in [
            ('use_chap_auth', use),
            ('chap_username', username),
            ('chap_password', password),
        ]:
            CONF.set_override(name, value, group='backend_defaults')
            self.addCleanup(
                CONF.clear_override, name, group='backend_defaults'
            )


class TestCreateExport(ExportTestCase):
    """create_export publishes remote volumes and skips local ones."""

    def test_remote_writes_stanza_and_returns_provider_fields(self):
        """A remote export lands in ctl.conf and in the provider fields."""
        self._set_chap(username='chapuser', password='chapsecret123')
        reload = self._wire_ctl()
        update = self.driver.create_export(None, make_volume(), REMOTE)
        self.assertEqual(
            f'192.0.2.10:3260,1 {IQN} 0', update['provider_location']
        )
        self.assertEqual(
            'CHAP chapuser chapsecret123', update['provider_auth']
        )
        text = self._conf_text()
        self.assertIn(f'target "{IQN}"', text)
        self.assertIn(f'/dev/zvol/zroot/cinder/{VOL_NAME}', text)
        self.assertIn('chap "chapuser" "chapsecret123"', text)
        reload.assert_called_once_with()

    def test_remote_generates_credentials_without_a_static_pair(self):
        """With no configured CHAP pair, a per-export pair is generated."""
        self._set_chap()
        self._wire_ctl()
        update = self.driver.create_export(None, make_volume(), REMOTE)
        method, username, password = update['provider_auth'].split()
        self.assertEqual('CHAP', method)
        self.assertGreaterEqual(len(username), 12)
        self.assertGreaterEqual(len(password), 12)

    def test_remote_without_chap(self):
        """With CHAP off, the target renders with no authentication."""
        self._set_chap(use=False)
        self._wire_ctl()
        update = self.driver.create_export(None, make_volume(), REMOTE)
        self.assertIsNone(update['provider_auth'])
        self.assertIn('auth-group no-authentication', self._conf_text())

    def test_local_is_a_noop_that_never_engages_ctl(self):
        """A local export must not even build the CTL table."""
        self.assertIsNone(
            self.driver.create_export(None, make_volume(), LOCAL)
        )
        self.assertIsNone(self.driver._ctl_table)


class TestEnsureExport(ExportTestCase):
    """ensure_export rebuilds exports after a service restart."""

    def test_rebuilds_export_from_provider_fields(self):
        """A restarted cinder-volume repopulates ctl.conf from the DB."""
        restarted = self._driver()
        reload = self._wire_ctl(restarted)
        restarted.ensure_export(None, make_exported_volume())
        text = self._conf_text()
        self.assertIn(f'target "{IQN}"', text)
        self.assertIn('chap "chapuser" "chapsecret123"', text)
        reload.assert_called_once_with()

    def test_without_provider_location_is_a_noop(self):
        """A volume with no export recorded needs nothing rebuilt."""
        self.driver.ensure_export(None, make_volume())
        self.assertIsNone(self.driver._ctl_table)


class TestRemoveExport(ExportTestCase):
    """remove_export withdraws remote exports."""

    def test_removes_stanza(self):
        """Removing an exported volume withdraws its target stanza."""
        self._set_chap(username='chapuser', password='chapsecret123')
        reload = self._wire_ctl()
        volume = make_volume()
        update = self.driver.create_export(None, volume, REMOTE)
        volume.provider_location = update['provider_location']
        volume.provider_auth = update['provider_auth']
        self.driver.remove_export(None, volume)
        self.assertNotIn(IQN, self._conf_text())
        self.assertEqual(2, reload.call_count)

    def test_never_exported_volume_is_a_noop(self):
        """A volume that was never exported does not engage CTL."""
        self.driver.remove_export(None, make_volume())
        self.assertIsNone(self.driver._ctl_table)


class TestInitializeConnection(ExportTestCase):
    """initialize_connection answers by connector locality."""

    def test_local_returns_the_exact_legacy_dict(self):
        """A local connector gets the zfs_local device-node dict."""
        expected = {
            'driver_volume_type': 'zfs_local',
            'data': {
                'device_path': f'/dev/zvol/zroot/cinder/{VOL_NAME}',
                'volume_id': VOL_ID,
            },
        }
        self.assertEqual(
            expected, self.driver.initialize_connection(make_volume(), LOCAL)
        )

    def test_remote_returns_iscsi_connection(self):
        """A remote connector gets the exported iSCSI target."""
        connection = self.driver.initialize_connection(
            make_exported_volume(), REMOTE
        )
        self.assertEqual('iscsi', connection['driver_volume_type'])
        self.assertEqual(
            {
                'target_discovered': False,
                'target_iqn': IQN,
                'target_portal': '192.0.2.10:3260',
                'target_lun': 0,
                'volume_id': VOL_ID,
                'discard': True,
                'auth_method': 'CHAP',
                'auth_username': 'chapuser',
                'auth_password': 'chapsecret123',
            },
            connection['data'],
        )

    def test_remote_without_chap_omits_auth_keys(self):
        """No provider_auth means no auth keys in the connection."""
        volume = make_exported_volume()
        volume.provider_auth = None
        data = self.driver.initialize_connection(volume, REMOTE)['data']
        self.assertNotIn('auth_method', data)

    def test_remote_without_export_raises(self):
        """No provider_location means create_export never ran."""
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.initialize_connection,
            make_volume(),
            REMOTE,
        )

    def test_remote_snapshot_connection_still_refuses(self):
        """Snapshot connections refuse remote connectors."""
        snapshot = types.SimpleNamespace(
            name='snapshot-1', id='snap-id', volume_name=VOL_NAME
        )
        self.assertRaises(
            exception.VolumeBackendAPIException,
            self.driver.initialize_connection_snapshot,
            snapshot,
            REMOTE,
        )


if __name__ == '__main__':
    unittest.main()
