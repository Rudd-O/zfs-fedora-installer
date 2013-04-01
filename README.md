Fedora on ZFS root installer
============================

| Donate to support this free software |
|:------------------------------------:|
| <img width="164" height="164" title="" alt="" src="doc/bitcoin.png" /> |
| [1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V](bitcoin:1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V) |

This script will create an image file containing a fresh, minimal Fedora 18 installation on a ZFS pool that can be booted on a QEMU / virt-manager virtual machine, or written to a block device (after which you can grow the last partition to make ZFS use the extra space).  If a device file is specified, then the device file will be used instead.  The resulting image is bootable and portable.

Usage:

    ./install-fedora-on-zfs --help
    usage: install-fedora-on-zfs [-h] [--vol-size VOLSIZE]
                                [--separate-boot BOOTDEV] [--boot-size BOOTSIZE]
                                [--pool-name POOLNAME] [--host-name HOSTNAME]
                                [--root-password ROOTPASSWORD]
                                [--swap-size SWAPSIZE] [--releasever VER]
                                [--luks-password LUKSPASSWORD] [--no-cleanup]
                                VOLDEV

    Install a minimal Fedora system inside a ZFS pool within a disk image or
    device

    positional arguments:
    VOLDEV                path to volume (device to use or regular file to
                            create)

    optional arguments:
    -h, --help            show this help message and exit
    --vol-size VOLSIZE    volume size in MiB (default 7000)
    --separate-boot BOOTDEV
                            place /boot in a separate volume
    --boot-size BOOTSIZE  boot partition size in MiB, or boot volume size in
                            MiB, when --separate-boot is specified (default 256)
    --pool-name POOLNAME  pool name (default tank)
    --host-name HOSTNAME  host name (default localhost.localdomain)
    --root-password ROOTPASSWORD
                            root password (default password)
    --swap-size SWAPSIZE  swap volume size in MiB (default 1024)
    --releasever VER      Fedora release version (default 18)
    --luks-password LUKSPASSWORD
                            LUKS password to encrypt the ZFS volume with (default
                            no encryption)
    --no-cleanup          if an error occurs, do not clean up working volumes

After setup is done, you can use `dd` to transfer the image(s) to the appropriate media (perhaps an USB drive) for booting.

Details
-------

1. The specified device(s) / file(s) will be prepared for the ZFS installation.  If you specified a separate boot device, then it will be partitioned with a `/boot` partition, and the main device will be entirely used for a ZFS pool.  Otherwise, the main device will be partitioned between a `/boot` partition and a ZFS pool.
2. If you requested encryption, the main device is encrypted using LUKS, and the soon-to-be-done system is set up to use LUKS encryption on boot.  Be ware that you will be prompted for this password interactively at a later point.
3. Essential core packages (`yum`, `bash`, `basesystem`, `vim-minimal`, `nano`, `kernel`, `grub2`) will be installed on the system.
4. Within the freshly installed OS, my git repositories for ZFS will be cloned and built as RPMs.
5. The RPMs built will be installed in the OS root file system.
6. `grub2-mkconfig` will be patched so it works with ZFS on root.  Yum will be configured to ignore grub updates.
7. QEMU will be executed, booting the newly-created image with specific instructions to install the bootloader and perform other janitorial tasks.  At this point, if you requested LUKS encryption, you will be prompted for the LUKS password.
8. Everything the script did will be cleaned up, leaving the file / block device ready to be booted off a QEMU virtual machine, or whatever device you write the image to.

Requirements
------------

These are the programs you need to execute this script

* python
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
* cryptsetup

Transferring the images to media
--------------------------------

You can transfer the resulting disk images to larger media afterward.  The usual `dd if=/path/to/root/image of=/dev/path/to/disk/device` advice works fine.

You can also tell ZFS (and LUKS) to use the newly available space, if the target media is larger than the images.

If you used the default single volume mode:

1. Alter the partition table so the last partition ends on the last sector.
2. Reread the partition table (might require a reboot).
3. (If you used encryption) tell LUKS to resize the volume via `cryptsetup resize /dev/mapper/luks-<encrypted device UUID>`.
4. `zpool online -e <pool name> <path to partition or encrypted device>` to have ZFS recognize the full partition size.

If you used the boot-on-separate-device mode:

3. (If you used encryption) tell LUKS to resize the volume via `cryptsetup resize /dev/mapper/luks-<encrypted device UUID>`.
4. `zpool online -e <pool name> <path to whole disk or encrypted device>` to have ZFS recognize the full partition size.

Notes / known issues
--------------------

This script only works on Fedora hosts.

License
-------

GNU GPL v3.
