"""Privileged ctld reload."""

import os
import signal

import cinder.privsep
from oslo_log import log as logging

from cinder_zfs_driver.ctl import CTLD_PIDFILE

LOG = logging.getLogger(__name__)


@cinder.privsep.sys_admin_pctxt.entrypoint
def reload_ctld():
    """Resolve ctld's pid from its pidfile and send it SIGHUP."""
    try:
        with open(CTLD_PIDFILE) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError) as err:
        raise RuntimeError(
            f'ctld is not running (no usable {CTLD_PIDFILE}). Set '
            'ctld_enable=YES and ctld_flags="-f <zfs_ctld_config>" in '
            'rc.conf and start the service.'
        ) from err
    LOG.info('reloading ctld (pid %d)', pid)
    os.kill(pid, signal.SIGHUP)
