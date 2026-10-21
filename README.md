# cinder-zfs-driver

A Cinder volume driver for FreeBSD that stores volumes as ZFS zvols.

## Status

### Features

- Volume create, delete, extend, clone and revert to snapshot
- Snapshots are ZFS snapshots, with deferred destroy while clones depend on them
- Glance image download to a volume and volume upload to an image
- Local attach to a nova-bhyve instance on the same host
- Remote iSCSI volume export served by CTL, attached on FreeBSD hosts with [os-brick-freebsd](https://github.com/stackbsd/os-brick-freebsd)
- Sparse or fully reserved zvols on disk
- Volume migration between two ZFS backends, using `zfs send` over ssh when the destination is another host

### Roadmap

- Snapshot export over iSCSI
- Per-initiator ACLs, mutual CHAP and multipath portals
- Add support for NVMe over Fabrics remote attach

### Limitations

- Remote snapshot connections are refused

## Host requirements

`zfs allow` delegation is needed for the dataset holding volumes, including `send` and `receive` for migration.

To migrate volumes to a backend on another host, the cinder user needs an ssh key that the other host's cinder user trusts. On the receiving side, restrict that key to the receive helper:

```
command="cinder-zfs-receive --config-file /usr/local/etc/cinder/cinder.conf",restrict ssh-ed25519 AAAA... cinder@storage1
```

Remote export needs ctld running against the driver's config file:

```sh
sysrc ctld_flags="-f /var/db/cinder/ctl.conf" ctld_enable=YES
service ctld start
```

## Configuration

```ini
[zfs]
volume_driver = cinder_zfs_driver.driver.ZFSVolumeDriver
volume_backend_name = ZFS
zfs_dataset = zroot/cinder
zfs_sparse_volumes = true
use_chap_auth = true
# zfs_ctld_config = /var/db/cinder/ctl.conf
# zfs_local_hosts = alias1,alias2
```
