"""Tests for the connector locality check."""

import unittest
from unittest import mock

from cinder.volume import configuration
from oslo_config import cfg

from cinder_zfs_driver import driver as driver_mod

CONF = cfg.CONF


def make_driver(gethostname='storage1', getfqdn='storage1.example.com'):
    """Return a driver built with the given hostname answers mocked in."""
    with (
        mock.patch.object(
            driver_mod.socket, 'gethostname', return_value=gethostname
        ),
        mock.patch.object(driver_mod.socket, 'getfqdn', return_value=getfqdn),
    ):
        return driver_mod.ZFSVolumeDriver(
            configuration=configuration.Configuration(
                driver_mod.zfs_opts, config_group='zfs'
            ),
            execute=mock.Mock(),
        )


class TestLocalNames(unittest.TestCase):
    """local_names collects every name this node answers to."""

    def test_includes_short_and_qualified_forms(self):
        """Both gethostname and the FQDN land in the set."""
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
            names = driver_mod.local_names([])
        self.assertEqual({'storage1', 'storage1.example.com'}, names)

    def test_lowercases_and_adds_extras(self):
        """Names are lowercased and extra configured names are included."""
        with (
            mock.patch.object(
                driver_mod.socket, 'gethostname', return_value='Storage1'
            ),
            mock.patch.object(
                driver_mod.socket, 'getfqdn', return_value='Storage1'
            ),
        ):
            names = driver_mod.local_names(['Alias.Example.COM'])
        self.assertEqual({'storage1', 'alias.example.com'}, names)


class TestIsLocalConnector(unittest.TestCase):
    """_is_local_connector matches connector hosts against the name set."""

    def test_exact_hostname_is_local(self):
        """The node's own hostname is local."""
        self.assertTrue(
            make_driver()._is_local_connector({'host': 'storage1'})
        )

    def test_fqdn_connector_matches_short_hostname(self):
        """Nova has the FQDN while cinder knows only the short name."""
        drv = make_driver(gethostname='storage1', getfqdn='storage1')
        self.assertTrue(
            drv._is_local_connector({'host': 'storage1.example.com'})
        )

    def test_short_connector_matches_fqdn_hostname(self):
        """Nova configured short, cinder's gethostname fully qualified."""
        drv = make_driver(
            gethostname='storage1.example.com', getfqdn='storage1.example.com'
        )
        self.assertTrue(drv._is_local_connector({'host': 'storage1'}))

    def test_match_is_case_insensitive(self):
        """Host name comparison ignores case."""
        self.assertTrue(
            make_driver()._is_local_connector({'host': 'STORAGE1'})
        )

    def test_zfs_local_hosts_extends_the_set(self):
        """Names from zfs_local_hosts count as this node."""
        CONF.set_override(
            'zfs_local_hosts', ['other-name'], group='backend_defaults'
        )
        self.addCleanup(
            CONF.clear_override, 'zfs_local_hosts', group='backend_defaults'
        )
        self.assertTrue(
            make_driver()._is_local_connector({'host': 'other-name'})
        )

    def test_missing_or_empty_host_is_remote(self):
        """An absent or empty connector host counts as remote."""
        drv = make_driver()
        self.assertFalse(drv._is_local_connector(None))
        self.assertFalse(drv._is_local_connector({}))
        self.assertFalse(drv._is_local_connector({'host': ''}))

    def test_foreign_host_is_remote(self):
        """Another node's hostname is remote."""
        self.assertFalse(
            make_driver()._is_local_connector({'host': 'compute7.example.com'})
        )


if __name__ == '__main__':
    unittest.main()
