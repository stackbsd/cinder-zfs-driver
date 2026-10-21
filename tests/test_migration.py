"""Tests for volume migration between backends."""

import subprocess
import types
import unittest
from unittest import mock

from cinder import exception
from oslo_concurrency import processutils
from oslo_config import cfg

from cinder_zfs_driver import migration
from cinder_zfs_driver import receive as receive_mod
from tests.test_locality import make_driver

CONF = cfg.CONF

VOL_ID = '123e4567-e89b-42d3-a456-426614174000'
VOL_NAME = f'volume-{VOL_ID}'
SOURCE = f'zroot/cinder/{VOL_NAME}'
SNAPSHOT = f'{SOURCE}@cinder-migrate'
LOCAL_DEST = f'zroot/other/{VOL_NAME}'
REMOTE_DEST = f'tank/cinder/{VOL_NAME}'


class FakeZfs:
    """A zfs command fake tracking volumes and snapshots by name."""

    def __init__(self):
        """Start with no datasets."""
        self.volumes = set()
        self.snapshots = set()
        self.calls = []

    def __call__(self, *argv):
        """Record the call and answer like the zfs command would."""
        self.calls.append(argv)
        verb, target = argv[1], argv[-1]
        if verb == 'list' and 'clones' in argv:
            return '-\n', ''
        if verb == 'list' and 'snapshot' in argv:
            names = sorted(
                s for s in self.snapshots if s.startswith(target + '@')
            )
            return ''.join(name + '\n' for name in names), ''
        if verb == 'list':
            if target not in self.volumes | self.snapshots:
                raise processutils.ProcessExecutionError(
                    stderr=f"cannot open '{target}': dataset does not exist"
                )
            return target + '\n', ''
        if verb == 'snapshot':
            self.snapshots.add(target)
        if verb == 'destroy':
            self.volumes.discard(target)
            self.snapshots.discard(target)
            self.snapshots -= {
                s for s in self.snapshots if s.startswith(target + '@')
            }
        return '', ''


def make_volume(status='available'):
    """Return a volume stub with the given status."""
    return types.SimpleNamespace(
        name=VOL_NAME,
        id=VOL_ID,
        status=status,
        provider_location=None,
        provider_auth=None,
    )


def make_host(hostname, dataset, sparse=True, driver='ZFSVolumeDriver'):
    """Return the destination dict cinder hands to migrate_volume."""
    return {
        'host': f'{hostname}@zfs2#ZFS',
        'capabilities': {
            'location_info': f'{driver}:{hostname}:{dataset}',
            'thin_provisioning_support': sparse,
        },
    }


class MigrateTestCase(unittest.TestCase):
    """migrate_volume sends the zvol to the other backend."""

    def setUp(self):
        """Build a driver on storage1 over a fake zfs and transport."""
        CONF.set_override(
            'zfs_dataset', 'zroot/cinder', group='backend_defaults'
        )
        self.addCleanup(
            CONF.clear_override, 'zfs_dataset', group='backend_defaults'
        )
        self.driver = make_driver()
        self.zfs = FakeZfs()
        self.zfs.volumes.add(SOURCE)
        self.driver._execute = self.zfs
        self.send = mock.patch.object(
            migration, 'send', side_effect=self.fake_send
        ).start()
        self.addCleanup(mock.patch.stopall)

    def fake_send(self, snapshot, receiver):
        """Land a local receive in the fake with the carried snapshot."""
        if receiver[0] == 'zfs':
            self.zfs.volumes.add(receiver[-1])
            self.zfs.snapshots.add(receiver[-1] + '@cinder-migrate')

    def migrate(self, host, volume=None):
        """Run migrate_volume towards host."""
        return self.driver.migrate_volume(None, volume or make_volume(), host)

    def test_refuses_other_driver(self):
        """A non-ZFS destination falls back to the generic copy."""
        host = make_host('storage1', 'zroot/other', driver='LVMVolumeDriver')
        self.assertEqual((False, None), self.migrate(host))
        self.send.assert_not_called()

    def test_refuses_without_location_info(self):
        """A destination without location_info is not ours."""
        host = {'host': 'storage1@lvm#lvm', 'capabilities': {}}
        self.assertEqual((False, None), self.migrate(host))

    def test_refuses_attached_volume(self):
        """An in-use volume is left to the generic path."""
        host = make_host('storage1', 'zroot/other')
        result = self.migrate(host, make_volume(status='in-use'))
        self.assertEqual((False, None), result)
        self.send.assert_not_called()

    def test_refuses_same_dataset(self):
        """A destination on the source's own dataset is refused."""
        host = make_host('storage1', 'zroot/cinder')
        self.assertEqual((False, None), self.migrate(host))
        self.send.assert_not_called()
        self.assertIn(SOURCE, self.zfs.volumes)

    def test_local_move_receives_into_other_dataset(self):
        """Same-host moves pipe into a local zfs receive."""
        result = self.migrate(make_host('storage1', 'zroot/other'))
        self.send.assert_called_once_with(
            SNAPSHOT, ['zfs', 'receive', LOCAL_DEST]
        )
        self.assertEqual(
            (True, {'provider_location': None, 'provider_auth': None}),
            result,
        )
        self.assertEqual({LOCAL_DEST}, self.zfs.volumes)
        self.assertEqual(set(), self.zfs.snapshots)

    def test_local_move_applies_destination_reservation(self):
        """A thick destination gets refreservation=auto after the receive."""
        self.migrate(make_host('storage1', 'zroot/other', sparse=False))
        self.assertIn(
            ('zfs', 'set', 'refreservation=auto', LOCAL_DEST), self.zfs.calls
        )

    def test_local_move_keeps_sparse_destination_sparse(self):
        """A thin destination gets refreservation=none."""
        self.migrate(make_host('storage1', 'zroot/other'))
        self.assertIn(
            ('zfs', 'set', 'refreservation=none', LOCAL_DEST), self.zfs.calls
        )

    def test_remote_move_sends_through_the_helper(self):
        """Another host is reached over ssh with its backend group."""
        result = self.migrate(make_host('storage2', 'tank/cinder'))
        self.send.assert_called_once_with(
            SNAPSHOT,
            [
                'ssh',
                '-o',
                'BatchMode=yes',
                'storage2',
                'receive',
                'zfs2',
                VOL_NAME,
            ],
        )
        self.assertTrue(result[0])
        self.assertEqual(set(), self.zfs.volumes)
        self.assertEqual(set(), self.zfs.snapshots)
        self.assertNotIn(
            ('zfs', 'set', 'refreservation=none', REMOTE_DEST), self.zfs.calls
        )

    def test_remote_move_uses_configured_ssh_config(self):
        """A configured ssh config file is passed to ssh with -F."""
        CONF.set_override(
            'migration_ssh_config',
            '/usr/local/etc/cinder/ssh_config',
            group='backend_defaults',
        )
        self.addCleanup(
            CONF.clear_override,
            'migration_ssh_config',
            group='backend_defaults',
        )
        self.migrate(make_host('storage2', 'tank/cinder'))
        receiver = self.send.call_args[0][1]
        self.assertEqual(
            [
                'ssh',
                '-o',
                'BatchMode=yes',
                '-F',
                '/usr/local/etc/cinder/ssh_config',
            ],
            receiver[:5],
        )
        self.assertEqual(
            ['storage2', 'receive', 'zfs2', VOL_NAME], receiver[5:]
        )

    def test_fqdn_destination_is_local(self):
        """The destination's FQDN still names this host."""
        self.migrate(make_host('storage1.example.com', 'zroot/other'))
        self.send.assert_called_once_with(
            SNAPSHOT, ['zfs', 'receive', LOCAL_DEST]
        )

    def test_failed_send_keeps_source(self):
        """A failed transfer drops the snapshot and keeps the zvol."""
        self.send.side_effect = exception.VolumeDriverException(
            message='broken pipe'
        )
        self.assertRaises(
            exception.VolumeDriverException,
            self.migrate,
            make_host('storage2', 'tank/cinder'),
        )
        self.assertEqual({SOURCE}, self.zfs.volumes)
        self.assertEqual(set(), self.zfs.snapshots)

    def test_stale_snapshot_is_replaced(self):
        """A snapshot left by an earlier attempt is taken again."""
        self.zfs.snapshots.add(SNAPSHOT)
        self.migrate(make_host('storage2', 'tank/cinder'))
        self.assertIn(('zfs', 'destroy', SNAPSHOT), self.zfs.calls)
        self.assertLess(
            self.zfs.calls.index(('zfs', 'destroy', SNAPSHOT)),
            self.zfs.calls.index(('zfs', 'snapshot', SNAPSHOT)),
        )

    def test_withdraws_export_before_deleting(self):
        """A stale export is removed from ctld on the way out."""
        self.driver.remove_export = mock.Mock()
        volume = make_volume()
        volume.provider_location = '192.0.2.10:3260,1 iqn 0'
        self.migrate(make_host('storage2', 'tank/cinder'), volume)
        self.driver.remove_export.assert_called_once_with(None, volume)


class ReceiveTestCase(unittest.TestCase):
    """The helper lands a stream under the named backend's dataset."""

    def setUp(self):
        """Enable the zfs2 backend and fake both zfs entry points."""
        CONF.set_override('enabled_backends', ['zfs2'])
        self.addCleanup(CONF.clear_override, 'enabled_backends')
        CONF.set_override(
            'zfs_dataset', 'tank/cinder', group='backend_defaults'
        )
        self.addCleanup(
            CONF.clear_override, 'zfs_dataset', group='backend_defaults'
        )
        self.zfs = FakeZfs()
        self.run = mock.patch.object(
            subprocess, 'run', side_effect=self.fake_receive
        ).start()
        mock.patch.object(
            receive_mod.processutils, 'execute', self.zfs
        ).start()
        self.addCleanup(mock.patch.stopall)

    def fake_receive(self, argv, **kwargs):
        """Create the received zvol with the sender's snapshot."""
        dataset = argv[-1]
        self.zfs.volumes.add(dataset)
        self.zfs.snapshots.add(dataset + '@cinder-migrate')

    def test_receives_and_drops_sender_snapshot(self):
        """The stream lands in the backend dataset without snapshots."""
        self.assertEqual(0, receive_mod.serve(f'receive zfs2 {VOL_NAME}'))
        self.assertEqual(
            ['zfs', 'receive', REMOTE_DEST], self.run.call_args[0][0]
        )
        self.assertEqual({REMOTE_DEST}, self.zfs.volumes)
        self.assertEqual(set(), self.zfs.snapshots)
        self.assertIn(
            ('zfs', 'set', 'refreservation=none', REMOTE_DEST),
            self.zfs.calls,
        )

    def test_thick_backend_reserves_space(self):
        """A backend without sparse volumes sets refreservation=auto."""
        CONF.set_override(
            'zfs_sparse_volumes', False, group='backend_defaults'
        )
        self.addCleanup(
            CONF.clear_override, 'zfs_sparse_volumes', group='backend_defaults'
        )
        self.assertEqual(0, receive_mod.serve(f'receive zfs2 {VOL_NAME}'))
        self.assertIn(
            ('zfs', 'set', 'refreservation=auto', REMOTE_DEST),
            self.zfs.calls,
        )

    def test_failed_finish_destroys_received_volume(self):
        """A reservation that cannot be set leaves nothing behind."""
        real = self.zfs.__call__

        def failing_set(*argv):
            """Fail the zfs set step only."""
            if argv[1] == 'set':
                raise processutils.ProcessExecutionError(stderr='no space')
            return real(*argv)

        mock.patch.object(
            receive_mod.processutils, 'execute', failing_set
        ).start()
        self.assertEqual(1, receive_mod.serve(f'receive zfs2 {VOL_NAME}'))
        self.assertEqual(set(), self.zfs.volumes)

    def test_refuses_unknown_backend(self):
        """A group outside enabled_backends is refused before receiving."""
        self.assertEqual(1, receive_mod.serve(f'receive lvm {VOL_NAME}'))
        self.run.assert_not_called()

    def test_refuses_other_verbs_and_arity(self):
        """Only receive with two names is served."""
        self.assertEqual(1, receive_mod.serve(''))
        self.assertEqual(1, receive_mod.serve(f'discard zfs2 {VOL_NAME}'))
        self.assertEqual(1, receive_mod.serve('receive zfs2'))
        self.run.assert_not_called()

    def test_refuses_unsafe_names(self):
        """Names that could escape the dataset are refused."""
        for name in ('../x', 'a@b', 'a/b', '-x', 'zfs2 extra'):
            self.assertEqual(1, receive_mod.serve(f'receive zfs2 {name}'))
        self.run.assert_not_called()

    def test_failed_receive_reports_error(self):
        """A zfs receive failure exits non-zero."""
        self.run.side_effect = subprocess.CalledProcessError(
            1, ['zfs', 'receive', REMOTE_DEST]
        )
        self.assertEqual(1, receive_mod.serve(f'receive zfs2 {VOL_NAME}'))


class SendTestCase(unittest.TestCase):
    """send pipes zfs send into the receiver and reports failures."""

    def test_remote_builds_ssh_command(self):
        """The receiver runs under ssh in batch mode."""
        self.assertEqual(
            [
                'ssh',
                '-o',
                'BatchMode=yes',
                'storage2',
                'receive',
                'zfs2',
                VOL_NAME,
            ],
            migration.remote('storage2', 'receive', 'zfs2', VOL_NAME),
        )

    def test_receiver_failure_raises_with_stderr(self):
        """A non-zero receiver surfaces its stderr."""
        sender = mock.MagicMock(returncode=0)
        sender.__enter__.return_value = sender
        sender.stderr.read.return_value = b''
        receiver = mock.Mock(returncode=1)
        receiver.communicate.return_value = (None, b'dataset exists\n')
        with mock.patch.object(
            subprocess, 'Popen', side_effect=[sender, receiver]
        ):
            with self.assertRaises(exception.VolumeDriverException) as ctx:
                migration.send(SNAPSHOT, ['zfs', 'receive', LOCAL_DEST])
        self.assertIn('dataset exists', str(ctx.exception))
        sender.stdout.close.assert_called_once_with()
