"""Receive helper for volumes migrated in over ssh."""

import os
import re
import subprocess
import sys

from cinder import exception
from cinder.common import config as cinder_config
from cinder.volume import configuration
from oslo_concurrency import processutils

from cinder_zfs_driver import migration
from cinder_zfs_driver.driver import zfs_opts

CONF = cinder_config.CONF

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def backend(group):
    """Return the configuration of an enabled backend group."""
    if group not in (CONF.enabled_backends or []):
        raise ValueError(f'{group} is not an enabled backend')
    return configuration.Configuration(zfs_opts, config_group=group)


def receive(group, name):
    """Receive a zvol stream from stdin into a backend's dataset."""
    conf = backend(group)
    dataset = f'{conf.zfs_dataset}/{name}'
    subprocess.run(['zfs', 'receive', dataset], stdin=sys.stdin, check=True)
    migration.finish_receive(
        processutils.execute, dataset, conf.zfs_sparse_volumes
    )


def serve(command):
    """Run the receive verb and return the exit status."""
    words = command.split()
    try:
        if len(words) != 3 or words[0] != 'receive':
            raise ValueError('expected: receive <backend> <volume>')
        if not all(NAME_RE.match(word) for word in words[1:]):
            raise ValueError(f'refusing {command!r}')
        receive(*words[1:])
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        processutils.ProcessExecutionError,
        exception.VolumeDriverException,
    ) as err:
        sys.stderr.write(f'cinder-zfs-receive: {err}\n')
        return 1
    return 0


def main():
    """Serve the receive verb named by SSH_ORIGINAL_COMMAND."""
    CONF(sys.argv[1:], project='cinder')
    return serve(os.environ.get('SSH_ORIGINAL_COMMAND', ''))


if __name__ == '__main__':
    sys.exit(main())
