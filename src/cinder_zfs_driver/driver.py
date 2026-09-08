"""Cinder volume driver for ZFS on FreeBSD."""

import functools
import os
import socket

from cinder import exception, interface
from cinder.common import constants
from cinder.i18n import _
from cinder.image import image_utils
from cinder.volume import configuration, driver, volume_utils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from cinder_zfs_driver import ctl

LOG = logging.getLogger(__name__)

# keep this in sync with nova-bhyve
LOCAL_VOLUME_TYPE = 'zfs_local'
MAX_PROMOTIONS = 32
ZVOL_DEV_DIR = '/dev/zvol'

zfs_opts = [
    cfg.StrOpt(
        'zfs_dataset',
        default='cinder/volumes',
        help='Dataset under which volume zvols are created.',
    ),
    cfg.BoolOpt(
        'zfs_sparse_volumes',
        default=True,
        help='Create zvols with zfs create -s.',
    ),
    cfg.StrOpt(
        'zfs_ctld_config',
        default='/var/db/cinder/ctl.conf',
        help='Path of the ctld configuration file this driver writes.',
    ),
    cfg.ListOpt(
        'zfs_local_hosts',
        default=[],
        help='Additional connector host names treated as this node.',
    ),
]

CONF = cfg.CONF
CONF.register_opts(zfs_opts, group=configuration.SHARED_CONF_GROUP)


def describe(value):
    """Return a short identifying string for a driver call argument."""
    if isinstance(value, (list, tuple)):
        text = '[{}]'.format(', '.join(describe(i) for i in value))
    else:
        try:
            for attr in ('id', 'request_id'):
                ident = getattr(value, attr, None)
                if ident is not None:
                    return f'{type(value).__name__}<{ident}>'
            text = repr(value)
        except Exception:
            return type(value).__name__
    if len(text) > 120:
        return text[:120] + '...'
    return text


def local_names(extra):
    """Return every name this node answers to in lowercase."""
    names = {socket.gethostname(), socket.getfqdn()}
    names |= {name.split('.')[0] for name in set(names)}
    return {name.lower() for name in names if name} | {
        name.lower() for name in extra
    }


def log_call(method):
    """Wrap a driver method so each call is logged before the body runs."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        """Log the call, then run the wrapped method."""
        described = [describe(arg) for arg in args]
        described += [f'{key}={describe(val)}' for key, val in kwargs.items()]
        LOG.info('%s(%s)', method.__name__, ', '.join(described))
        return method(self, *args, **kwargs)

    return wrapper


@interface.volumedriver
class ZFSVolumeDriver(driver.VolumeDriver):
    """Cinder volume driver backed by ZFS zvols."""

    VERSION = '0.9.0'

    CI_WIKI_NAME = 'StackBSD_ZFS_CI'

    def __init__(self, *args, **kwargs):
        """Initialize the driver, register options and node names."""
        super().__init__(*args, **kwargs)
        self.configuration.append_config_values(zfs_opts)
        self.hostname = socket.gethostname()
        self.backend_name = (
            self.configuration.safe_get('volume_backend_name') or 'ZFS'
        )
        self.protocol = constants.ISCSI
        # computed once, getfqdn can block on DNS
        self._node_names = local_names(self.configuration.zfs_local_hosts)
        self._ctl_table = None

    @classmethod
    def get_driver_options(cls):
        """Return the options this driver consumes."""
        additional_opts = cls._get_oslo_driver_opts(
            'volume_backend_name',
            'reserved_percentage',
            'max_over_subscription_ratio',
            'volume_dd_blocksize',
            'target_prefix',
            'target_ip_address',
            'target_port',
            'use_chap_auth',
            'chap_username',
            'chap_password',
        )
        return zfs_opts + additional_opts

    def _dataset(self, volume):
        """Return the dataset name of a volume's zvol."""
        return f'{self.configuration.zfs_dataset}/{volume.name}'

    def _snapshot_dataset(self, snapshot):
        """Return the dataset name of a snapshot."""
        dataset = self.configuration.zfs_dataset
        return f'{dataset}/{snapshot.volume_name}@{snapshot.name}'

    def _exists(self, dataset):
        """Report whether a dataset or snapshot is present."""
        try:
            self._execute('zfs', 'list', '-H', '-o', 'name', dataset)
        except processutils.ProcessExecutionError as err:
            if 'does not exist' in (err.stderr or ''):
                return False
            raise
        return True

    def _dependent_clones(self, dataset):
        """Return the clones holding snapshots of a volume."""
        out, _err = self._execute(
            'zfs',
            'list',
            '-H',
            '-r',
            '-t',
            'snapshot',
            '-o',
            'clones',
            dataset,
        )
        clones = []
        for line in out.splitlines():
            value = line.strip()
            if value and value != '-':
                clones += value.split(',')
        return clones

    def _promote_dependent_clones(self, dataset):
        """Move snapshots shared with clones onto those clones."""
        for _attempt in range(MAX_PROMOTIONS):
            clones = self._dependent_clones(dataset)
            if not clones:
                return
            self._execute('zfs', 'promote', clones[0])
        raise exception.VolumeDriverException(
            message=_(
                '%(dataset)s still has dependent clones after '
                '%(count)d promotions'
            )
            % {'dataset': dataset, 'count': MAX_PROMOTIONS}
        )

    @log_call
    def do_setup(self, context):
        """Nothing to set up."""
        pass

    @log_call
    def check_for_setup_error(self):
        """Check the dataset and the prerequisites for remote export."""
        dataset = self.configuration.zfs_dataset
        try:
            self._execute('zfs', 'list', '-H', '-o', 'name', dataset)
        except processutils.ProcessExecutionError as err:
            raise exception.VolumeDriverException(
                message=_(
                    'Dataset %(dataset)s is not usable by this '
                    'service: %(error)s'
                )
                % {'dataset': dataset, 'error': err.stderr or err}
            ) from err
        if not os.path.exists(ctl.CTLD_PIDFILE):
            LOG.warning(
                'ctld is not running; remote export needs it started '
                'with -f %s',
                self.configuration.zfs_ctld_config,
            )
        config_dir = os.path.dirname(self.configuration.zfs_ctld_config)
        if not os.access(config_dir, os.W_OK):
            LOG.warning(
                '%s is not writable; remote export needs it', config_dir
            )

    def local_path(self, volume):
        """Return the device path of a volume's zvol."""
        return os.path.join(ZVOL_DEV_DIR, self._dataset(volume))

    def _provisioned_capacity_gb(self):
        """Return the total volsize of the zvols under the dataset."""
        out, _err = self._execute(
            'zfs',
            'list',
            '-Hp',
            '-r',
            '-t',
            'volume',
            '-o',
            'volsize',
            self.configuration.zfs_dataset,
        )
        return round(sum(int(line) for line in out.split()) / units.Gi, 2)

    def _update_volume_stats(self):
        """Refresh the stats dict from the dataset's space accounting."""
        dataset = self.configuration.zfs_dataset
        sparse = self.configuration.zfs_sparse_volumes
        out, _err = self._execute(
            'zfs', 'list', '-Hp', '-o', 'available,used', dataset
        )
        available, used = (int(value) for value in out.split())
        location_info = f'ZFSVolumeDriver:{self.hostname}:{dataset}'
        pool = {
            'pool_name': self.backend_name,
            'total_capacity_gb': round((available + used) / units.Gi, 2),
            'free_capacity_gb': round(available / units.Gi, 2),
            'provisioned_capacity_gb': self._provisioned_capacity_gb(),
            'reserved_percentage': self.configuration.reserved_percentage,
            'max_over_subscription_ratio': (
                self.configuration.max_over_subscription_ratio
            ),
            'thin_provisioning_support': sparse,
            'thick_provisioning_support': not sparse,
            'location_info': location_info,
            'QoS_support': False,
            'multiattach': False,
            'filter_function': self.get_filter_function(),
            'goodness_function': self.get_goodness_function(),
            'backend_state': 'up',
        }
        self._stats = {
            'volume_backend_name': self.backend_name,
            'vendor_name': 'StackBSD',
            'driver_version': self.VERSION,
            'storage_protocol': self.protocol,
            'pools': [pool],
        }
        LOG.debug(
            '%(free)s GB free of %(total)s GB on %(pool)s',
            {
                'free': pool['free_capacity_gb'],
                'total': pool['total_capacity_gb'],
                'pool': self.backend_name,
            },
        )

    @log_call
    def create_volume(self, volume):
        """Create the volume's zvol."""
        command = ['zfs', 'create']
        if self.configuration.zfs_sparse_volumes:
            command.append('-s')
        command += ['-V', f'{volume.size}G', self._dataset(volume)]
        self._execute(*command)

    @log_call
    def delete_volume(self, volume):
        """Destroy the volume's zvol after promoting dependent clones."""
        dataset = self._dataset(volume)
        if not self._exists(dataset):
            return
        self._promote_dependent_clones(dataset)
        self._execute('zfs', 'destroy', '-r', dataset)

    @log_call
    def create_cloned_volume(self, volume, src_vref):
        """Create the volume as a clone of a snapshot of the source."""
        snapshot = f'{self._dataset(src_vref)}@{volume.name}'
        self._execute('zfs', 'snapshot', snapshot)
        try:
            self._execute('zfs', 'clone', snapshot, self._dataset(volume))
        except processutils.ProcessExecutionError:
            self._execute('zfs', 'destroy', '-d', snapshot)
            raise
        if volume.size > src_vref.size:
            self.extend_volume(volume, volume.size)

    @log_call
    def create_volume_from_snapshot(self, volume, snapshot):
        """Create the volume as a clone of the snapshot."""
        self._execute(
            'zfs',
            'clone',
            self._snapshot_dataset(snapshot),
            self._dataset(volume),
        )
        if volume.size > snapshot.volume_size:
            self.extend_volume(volume, volume.size)

    @log_call
    def extend_volume(self, volume, new_size):
        """Grow the zvol to the new size."""
        self._execute(
            'zfs', 'set', f'volsize={new_size}G', self._dataset(volume)
        )

    @log_call
    def revert_to_snapshot(self, context, volume, snapshot):
        """Roll the zvol back to the snapshot."""
        self._execute('zfs', 'rollback', self._snapshot_dataset(snapshot))

    def snapshot_revert_use_temp_snapshot(self):
        """Report that revert needs no temporary snapshot."""
        return False

    @log_call
    def create_snapshot(self, snapshot):
        """Create the snapshot."""
        self._execute('zfs', 'snapshot', self._snapshot_dataset(snapshot))

    @log_call
    def delete_snapshot(self, snapshot):
        """Destroy the snapshot, deferring while clones depend on it."""
        dataset = self._snapshot_dataset(snapshot)
        if not self._exists(dataset):
            return
        self._execute('zfs', 'destroy', '-d', dataset)

    def _is_local_connector(self, connector):
        """Report whether the connector's host names this node."""
        host = ((connector or {}).get('host') or '').lower()
        if not host:
            return False
        return (
            host in self._node_names or host.split('.')[0] in self._node_names
        )

    def _ctl(self):
        """Return the CTL export table, building it on first use."""
        if self._ctl_table is None:
            from cinder.zfs import privsep as zfs_privsep

            self._ctl_table = ctl.CtlTable(
                self.configuration.zfs_ctld_config,
                self._portal(),
                zfs_privsep.reload_ctld,
            )
        return self._ctl_table

    def _portal(self):
        """Return the configured portal address as ip:port."""
        conf = self.configuration
        return f'{conf.target_ip_address}:{conf.target_port}'

    @staticmethod
    def _chap_from(provider_auth):
        """Split a 'CHAP <user> <secret>' provider_auth, or (None, None)."""
        if not provider_auth:
            return None, None
        method, username, password = provider_auth.split()
        if method != 'CHAP':
            raise exception.VolumeBackendAPIException(
                data=_('unsupported provider_auth method %s') % method
            )
        return username, password

    @log_call
    def ensure_export(self, context, volume):
        """Rebuild a remote export from the volume's provider fields."""
        if not volume.provider_location:
            return
        username, password = self._chap_from(volume.provider_auth)
        self._ctl().add(
            ctl.Export(
                volume_name=volume.name,
                iqn=volume.provider_location.split()[1],
                device_path=self.local_path(volume),
                chap_username=username,
                chap_password=password,
            )
        )

    @log_call
    def create_export(self, context, volume, connector):
        """Export the volume as an iSCSI target for a remote connector."""
        if self._is_local_connector(connector):
            return None
        if self.configuration.use_chap_auth:
            username = (
                self.configuration.chap_username
                or volume_utils.generate_username()
            )
            password = (
                self.configuration.chap_password
                or volume_utils.generate_password()
            )
            provider_auth = f'CHAP {username} {password}'
        else:
            username = password = provider_auth = None
        iqn = self.configuration.target_prefix + volume.name
        self._ctl().add(
            ctl.Export(
                volume_name=volume.name,
                iqn=iqn,
                device_path=self.local_path(volume),
                chap_username=username,
                chap_password=password,
            )
        )
        return {
            'provider_location': f'{self._portal()},1 {iqn} 0',
            'provider_auth': provider_auth,
        }

    @log_call
    def remove_export(self, context, volume):
        """Withdraw the volume's iSCSI export."""
        if not volume.provider_location:
            return
        self._ctl().remove(volume.name)

    @log_call
    def create_export_snapshot(self, context, snapshot, connector):
        """Nothing to export."""
        pass

    @log_call
    def remove_export_snapshot(self, context, snapshot):
        """Nothing to withdraw."""
        pass

    @log_call
    def initialize_connection(self, volume, connector, **kwargs):
        """Return the local device node or the exported iSCSI target."""
        if self._is_local_connector(connector):
            return {
                'driver_volume_type': LOCAL_VOLUME_TYPE,
                'data': {
                    'device_path': self.local_path(volume),
                    'volume_id': volume.id,
                },
            }
        if not volume.provider_location:
            raise exception.VolumeBackendAPIException(
                data=_('volume %s has no export to connect to') % volume.name
            )
        portal_field, iqn, lun = volume.provider_location.split()
        data = {
            'target_discovered': False,
            'target_iqn': iqn,
            'target_portal': portal_field.split(',')[0],
            'target_lun': int(lun),
            'volume_id': volume.id,
            'discard': True,
        }
        username, password = self._chap_from(volume.provider_auth)
        if username is not None:
            data['auth_method'] = 'CHAP'
            data['auth_username'] = username
            data['auth_password'] = password
        return {'driver_volume_type': 'iscsi', 'data': data}

    @log_call
    def terminate_connection(self, volume, connector, **kwargs):
        """Nothing per-connection to undo."""
        pass

    @log_call
    def initialize_connection_snapshot(self, snapshot, connector, **kwargs):
        """Return the local device node of the snapshot's zvol."""
        if not self._is_local_connector(connector):
            raise exception.VolumeBackendAPIException(
                data=_(
                    'snapshot %(name)s can only be attached on %(storage)s, '
                    'not from %(requester)s'
                )
                % {
                    'name': snapshot.name,
                    'storage': self.hostname,
                    'requester': (connector or {}).get('host'),
                }
            )
        return {
            'driver_volume_type': LOCAL_VOLUME_TYPE,
            'data': {
                'device_path': os.path.join(
                    ZVOL_DEV_DIR, self._snapshot_dataset(snapshot)
                ),
                'volume_id': snapshot.id,
            },
        }

    @log_call
    def terminate_connection_snapshot(self, snapshot, connector, **kwargs):
        """Nothing per-connection to undo."""
        pass

    @log_call
    def validate_connector(self, connector):
        """Accept any connector."""
        pass

    @log_call
    def clone_image(
        self, context, volume, image_location, image_meta, image_service
    ):
        """Decline to clone the image."""
        return None, False

    @log_call
    def copy_image_to_volume(
        self, context, volume, image_service, image_id, disable_sparse=False
    ):
        """Fetch the image raw onto the volume's device node."""
        image_utils.fetch_to_raw(
            context,
            image_service,
            image_id,
            self.local_path(volume),
            self.configuration.volume_dd_blocksize,
            size=volume.size,
            disable_sparse=disable_sparse,
        )

    @log_call
    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Upload the volume's device node as an image."""
        volume_utils.upload_volume(
            context, image_service, image_meta, self.local_path(volume), volume
        )

    @log_call
    def migrate_volume(self, context, volume, host):
        """Report the volume as migrated in place."""
        return True, None

    @log_call
    def update_migrated_volume(
        self, ctxt, volume, new_volume, original_volume_status
    ):
        """Report that the backend volume kept the new volume's identity."""
        return {
            '_name_id': new_volume.name_id,
            'provider_location': new_volume.provider_location,
        }

    @log_call
    def retype(self, context, volume, new_type, diff, host):
        """Report the retype as handled in place."""
        return True, None

    @log_call
    def accept_transfer(self, context, volume, new_user, new_project):
        """Nothing to change on ownership transfer."""
        pass

    @log_call
    def manage_existing(self, volume, existing_ref):
        """Accept the existing zvol as-is."""
        pass

    @log_call
    def manage_existing_get_size(self, volume, existing_ref):
        """Report the smallest size Cinder accepts."""
        return 1

    @log_call
    def get_manageable_volumes(
        self, cinder_volumes, marker, limit, offset, sort_keys, sort_dirs
    ):
        """Report no manageable volumes."""
        return []

    @log_call
    def unmanage(self, volume):
        """Leave the zvol in place."""
        pass

    @log_call
    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Accept the existing snapshot as-is."""
        pass

    @log_call
    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Report the smallest size Cinder accepts."""
        return 1

    @log_call
    def get_manageable_snapshots(
        self, cinder_snapshots, marker, limit, offset, sort_keys, sort_dirs
    ):
        """Report no manageable snapshots."""
        return []

    @log_call
    def unmanage_snapshot(self, snapshot):
        """Leave the snapshot in place."""
        pass

    @log_call
    def create_group(self, context, group):
        """Accept the group without backend state."""
        pass

    @log_call
    def delete_group(self, context, group, volumes):
        """Delete the group without backend state."""
        return None, None

    @log_call
    def update_group(
        self, context, group, add_volumes=None, remove_volumes=None
    ):
        """Accept membership changes without backend state."""
        return None, None, None

    @log_call
    def create_group_from_src(
        self,
        context,
        group,
        volumes,
        group_snapshot=None,
        snapshots=None,
        source_group=None,
        source_vols=None,
    ):
        """Create the group without backend state."""
        return None, None

    @log_call
    def create_group_snapshot(self, context, group_snapshot, snapshots):
        """Create the group snapshot without backend state."""
        return None, None

    @log_call
    def delete_group_snapshot(self, context, group_snapshot, snapshots):
        """Delete the group snapshot without backend state."""
        return None, None
