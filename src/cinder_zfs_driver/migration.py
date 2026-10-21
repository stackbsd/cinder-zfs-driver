"""Volume migration transport over zfs send."""

import subprocess

from cinder import exception
from cinder.i18n import _

SSH = ('ssh', '-o', 'BatchMode=yes')

SNAPSHOT = 'cinder-migrate'


def remote(host, *words, ssh_config=None):
    """Return the ssh command line running the receive helper on a host."""
    command = [*SSH]
    if ssh_config:
        command += ['-F', ssh_config]
    return [*command, host, *words]


def check_result(proc, err, what):
    """Raise if a transfer process exited non-zero."""
    if proc.returncode:
        raise exception.VolumeDriverException(
            message=_('%(what)s failed: %(err)s')
            % {'what': what, 'err': err.decode(errors='replace').strip()}
        )


def send(snapshot, receiver):
    """Pipe a zfs send of the snapshot into a receiving command."""
    with subprocess.Popen(
        ['zfs', 'send', '-c', snapshot],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as sender:
        proc = subprocess.Popen(
            receiver, stdin=sender.stdout, stderr=subprocess.PIPE
        )
        # lets zfs send see EPIPE if the receiver exits early
        sender.stdout.close()
        _out, recv_err = proc.communicate()
        send_err = sender.stderr.read()
    check_result(sender, send_err, f'sending {snapshot}')
    check_result(proc, recv_err, ' '.join(receiver))


def finish_receive(execute, dataset, sparse):
    """Drop the carried snapshot and apply the backend's reservation."""
    try:
        out, _err = execute(
            'zfs',
            'list',
            '-H',
            '-t',
            'snapshot',
            '-o',
            'name',
            '-d',
            '1',
            dataset,
        )
        for snapshot in out.split():
            execute('zfs', 'destroy', snapshot)
        reservation = 'none' if sparse else 'auto'
        execute('zfs', 'set', f'refreservation={reservation}', dataset)
    except Exception:
        execute('zfs', 'destroy', '-r', dataset)
        raise
