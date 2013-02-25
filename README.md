Fedora on ZFS root installer
============================

This script will create a 4 GB image file containing a fresh, minimal Fedora 18 installation on a ZFS pool that can be booted on a QEMU / virt-manager virtual machine, or written to a block device (after which you can grow the last partition to make ZFS use the extra space).

If a device file is specified, then the device file will be used instead.

Usage:

    install-fedora-on-zfs <path to regular or device file> <host name>

Requirements
------------

These are the programs you need to execute this script

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