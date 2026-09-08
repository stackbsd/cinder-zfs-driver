# cinder-zfs-driver

A Cinder volume driver for FreeBSD that stores volumes as ZFS zvols.

## Status

### Features

- Volume create, delete, extend, clone and revert to snapshot
- Snapshots are ZFS snapshots, with deferred destroy while clones depend on them
- Glance image download to a volume and volume upload to an image
- Local attach to a nova-bhyve instance on the same host
- Remote iSCSI volume export served by CTL
- Sparse or fully reserved zvols on disk

### Roadmap

- Remote iSCSI attach / os-brick support
- Snapshot export over iSCSI
- Per-initiator ACLs, mutual CHAP and multipath portals
- Volume migration between two backends using `zfs send` / `zfs receive`
- Add support for NVMe over Fabrics remote attach

### Limitations

- No remote attach / os-brick support so nova and the cinder backend must run on the same host
- Remote snapshot connections are refused

## Host requirements

`zfs allow` delegation is needed for the dataset holding volumes.

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
