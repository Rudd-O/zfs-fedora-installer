Fedora on ZFS root installer
============================

| Donate to support this free software |
|:------------------------------------:|
| <img width="164" height="164" title="" alt="" src="doc/bitcoin.png" /> |
| [1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V](bitcoin:1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V) |

This script will create a 8 GB image file containing a fresh, minimal Fedora 18 installation on a ZFS pool that can be booted on a QEMU / virt-manager virtual machine, or written to a block device (after which you can grow the last partition to make ZFS use the extra space).

If a device file is specified, then the device file will be used instead.

Usage:

    install-fedora-on-zfs <path to regular or device file> <host name> [root password]

After setup is done, you can use `dd` to transfer the image to the appropriate media (perhaps an USB drive) for booting.

Details
-------

1. A file (8 GB in size) for the volume, will be created (unless a device file was specified).
2. The file (if not block device) will be made into a loopback device and partitioned with a MBR partition into a 256 MiB partition for `/boot`, and the rest for a ZFS pool, named after the first component of the host name you specified.
3. A pool will be created in the second partition (holding the root and the swap file systems), and an ext4 file system in the first.
4. Essential core packages (`yum`, `bash`, `basesystem`, `vim-minimal`, `nano`, `kernel`, `grub2`) will be installed on the system.
5. Within the freshly installed OS, my git repositories for ZFS will be cloned and built as RPMs.
6. The RPMs built will be installed in the OS root file system.
7. `grub2-mkconfig` will be patched so it works with ZFS on root.  Yum will be configured to ignore grub updates.
8. QEMU will be executed, booting the newly-created image with specific instructions to install the bootloader and perform other janitorial tasks.
9. Everything the script did will be cleaned up, leaving the file / block device ready to be booted off a QEMU virtual machine, or whatever device you write the image to.

Requirements
------------

These are the programs you need to execute this script

* python
* uuidgen
* qemu-kvm
* ZFS already installed
  * https://github.com/Rudd-O/spl
  * https://github.com/Rudd-O/zfs
* losetup
* mkfs.ext4
* grub2
* rsync
* yum
* dracut
* mkswap

Notes / known issues
--------------------

This script only works on Fedora hosts.

If you leave the root password empty, it will default to `password`.

License
-------

GNU GPL v3.
