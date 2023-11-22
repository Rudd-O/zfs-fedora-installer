Fedora on ZFS root installer
============================

| Donate to support this free software |
|:------------------------------------:|
| <img width="164" height="164" title="" alt="" src="doc/bitcoin.png" /> |
| [1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V](bitcoin:1NhK4UBCyBx4bDgxLVHtAK6EZjSxc8441V) |

This project contains two programs.

*Program number one* is `install-fedora-on-zfs`.  This script will create an image file containing a fresh, minimal Fedora 19+ installation on a ZFS pool.  This pool can:

* be booted on a QEMU / virt-manager virtual machine, or
* be written to a block device, after which you can grow the last partition to make ZFS use the extra space.

If you specify a path to a device instead of a path to an image file, then the device will be used instead.  The resulting image is obviously bootable and fully portable between computers, until the next time dracut regenerates the initial RAM disks, after which the initial RAM disks will be tailored to the specific hardware of the machine where it ran.  Make sure that the device is large enough to contain the whole install.

`install-fedora-on-zfs` requires a working ZFS install on the machine you are running it.  See below for instructions.

*Program number two* is `deploy-zfs`.  This script will deploy ZFS, ZFS-Dracut and `grub-zfs-fixer` via DKMS RPMs to a running Fedora system.  That system can then be converted to a full ZFS on root system if you so desire.

To keep the ZFS packages within the deployed system up-to-date, it's recommended that you use the excellent [ZFS updates](https://github.com/Rudd-O/ansible-samples/tree/master/zfsupdates) Ansible playbook, built specifically for this purpose.

See below for setup instructions.

Usage of `install-fedora-on-zfs`
--------------------------------

    ./install-fedora-on-zfs --help
    usage: install-fedora-on-zfs [-h] [--vol-size VOLSIZE]
                                [--separate-boot BOOTDEV] [--boot-size BOOTSIZE]
                                [--pool-name POOLNAME] [--host-name HOSTNAME]
                                [--root-password ROOTPASSWORD]
                                [--swap-size SWAPSIZE] [--releasever VER]
                                [--luks-password LUKSPASSWORD]
                                [--break-before STAGE]
                                [--shell-before STAGE]
                                [--use-prebuilt-rpms DIR] VOLDEV

    Install a minimal Fedora system inside a ZFS pool within a disk image or
    device

    positional arguments:
    VOLDEV                path to volume (device to use or regular file to
                            create)

    optional arguments:
    -h, --help            show this help message and exit
    --vol-size VOLSIZE    volume size in MiB (default 11000)
    --separate-boot BOOTDEV
                            place /boot in a separate volume
    --boot-size BOOTSIZE  boot partition size in MiB, or boot volume size in
                            MiB, when --separate-boot is specified (default 256)
    --pool-name POOLNAME  pool name (default tank)
    --host-name HOSTNAME  host name (default localhost.localdomain)
    --root-password ROOTPASSWORD
                            root password (default password)
    --swap-size SWAPSIZE  swap volume size in MiB (default 1024)
    --releasever VER      Fedora release version (default the same as the
                          computer you are installing on)
    --luks-password LUKSPASSWORD
                            LUKS password to encrypt the ZFS volume with (default
                            no encryption)
    --no-cleanup          if an error occurs, do not clean up working volumes
    --use-prebuilt-rpms DIR
                            use the pre-built (DKMS/tools) ZFS RPMs in
                            this directory (default: build ZFS RPMs
                            within the chroot)

After setup is done, you can use `dd` to transfer the image(s) to the appropriate media (perhaps an USB drive) for booting.  See below for examples and more information.

Usage of `deploy-zfs`
---------------------

    usage: deploy-zfs [-h] [--use-prebuilt-rpms DIR] [--no-cleanup]

    Install ZFS on a running system

    optional arguments:
      -h, --help            show this help message and exit
      --use-prebuilt-rpms DIR
                            also install pre-built ZFS, GRUB and other RPMs
                            in this directory, except for debuginfo packages
                            within the directory (default: build ZFS and GRUB
                            RPMs, within the system)
      --no-cleanup          if an error occurs, do not clean up temporary mounts
                            and files

Details about the `install-fedora-on-zfs` installation process
--------------------------------------------------------------

1. The specified device(s) / file(s) will be prepared for the ZFS installation.  If you specified a separate boot device, then it will be partitioned with a `/boot` partition, and the main device will be entirely used for a ZFS pool.  Otherwise, the main device will be partitioned between a `/boot` partition (which will use about 256 MB of the space on the device / file) and a ZFS pool (which will use the rest of the space available on the device / file, minus 16 MB at the end).
2. If you requested encryption, the device containing the ZFS pool is encrypted using LUKS, and the soon-to-be-done system is set up to use LUKS encryption on boot.  Be ware that you will be prompted for this password interactively at a later point.
3. Essential core packages (`yum`, `bash`, `basesystem`, `vim-minimal`, `nano`, `kernel`, `grub2`) will be installed on the system.
4. Within the freshly installed OS, my git repositories for ZFS will be cloned and built as RPMs.
5. The RPMs built will be installed in the OS root file system.
6. `grub2-mkconfig` will be patched so it works with ZFS on root.  Yum will be configured to ignore grub updates.
7. QEMU will be executed, booting the newly-created image with specific instructions to install the bootloader and perform other janitorial tasks.  At this point, if you requested LUKS encryption, you will be prompted for the LUKS password.
8. Everything the script did will be cleaned up, leaving the file / block device ready to be booted off a QEMU virtual machine, or whatever device you write the image to.

Requirements for `install-fedora-on-zfs`
----------------------------------------

These are the programs you need to execute `install-fedora-on-zfs`:

* a working ZFS install (see below)
* python
* qemu-kvm
* losetup
* mkfs.ext4
* grub2
* rsync
* yum
* dracut
* mkswap
* cryptsetup

Getting ZFS installed on your machine for `install-fedora-on-zfs` to use
------------------------------------------------------------------------

Before using this program in your computer, you need to have a functioning copy of ZFS in it.  Run the `deploy-zfs` program to get it.

After doing so, run:

    sudo udevadm control --reload-rules

Now you can verify that the `zfs` command works.  If it does, then you are ready to run the `install-fedora-on-zfs` program.

Transferring the `install-fedora-on-zfs` images to media
--------------------------------------------------------

You can transfer the resulting disk images to larger media afterward.  The usual `dd if=/path/to/root/image of=/dev/path/to/disk/device` advice works fine.  Here is an example showing how to write an image file that was just created to `/dev/sde`:

    dd if=/path/to/image/file of=/dev/sde

Of course, if you chose to have a separate boot image (`--separate-boot`), then you can write the boot image and the volume image `VOLDEV` to separate devices.

**Security warning**: disk images created this way should not be reused across hosts, because several unique identifiers (and the LUKS master key, in case of encrypted images) will then be shared across those hosts.  You should create distinct images for distinct systems instead.  Changes are in the pipeline to uniquify installs and strip them of identifying informatin, precisely to prevent this problem.

Taking advantage of increased disk space in the target media
------------------------------------------------------------

You can also tell ZFS (and LUKS) to use the newly available space, if the target media is larger than the images.  This is usually the case, because the destination device tends to be significantly larger than the partitions created when the installation process ran.

If you used the default single volume mode:

1. Alter the partition table so the last partition ends 16 MB before the last sector.
2. Reread the partition table (might require a reboot).
3. (If you used encryption) tell LUKS to resize the volume via `cryptsetup resize /dev/mapper/luks-<encrypted device UUID>`.
4. `zpool online -e <pool name> <path to partition or encrypted device>` to have ZFS recognize the full partition size.  In some circumstances you may have to `zpool set autoexpand=on <pool name>` to inform the pool that its underlying device has grown.

If you used the boot-on-separate-device mode:

3. (If you used encryption) tell LUKS to resize the volume via `cryptsetup resize /dev/mapper/luks-<encrypted device UUID>`.
4. `zpool online -e <pool name> <path to whole disk or encrypted device>` to have ZFS recognize the full partition size.  See above for instructions on how to use `zpool set autoexpand=on` if this does not work.

Of course, you can also extend the pool to other disk devices.  If you do so, make sure to regenerate the initial RAM disks with `dracut -fv` (which will regenerate the RAM disk for the currently booted kernel).

Notes / known issues
--------------------

This script only works on Fedora hosts.  I accept patches to make it work on CentOS.

License
-------

GNU GPL v3.
