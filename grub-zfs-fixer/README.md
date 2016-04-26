GRUB compatibility for booting off of ZFS root file systems
===========================================================

This directory contains a formula to create a RPM package that will ensure your computer's GRUB 2 will be able to boot off of a ZFS root file system.

Provided that:

1. This package is installed.
2. (Optionally,) your `/etc/fstab` file contains an entry for your root ZFS dataset.
3. You have run `grub2-mkconfig` at least once.

Then every execution of `grub2-mkconfig` will automatically add the necessary `root=pool/path/to/dataset` entry to the kernel command line of every entry in your GRUB configuration `grub.cfg`.

This program fixes the well-known `grub2-mkconfig` malfunction when the root file system is a ZFS dataset, which renders ZFS-on-root systems unbootable.  This program also makes it possible for multiple systems to coexist within the same ZFS pool, because the boot entries generated after installing this program will make it unnecessary to set the `bootfs` property of the pool.  Finally, this program makes it unnecessary to edit the file `/etc/default/grub` where you would normally have to specify by hand which root dataset to boot from.

Deployment instructions follow.

Preliminary caveats
-------------------

In this document, we will make a distinction between:

* The target system: the operating system tree where ZFS on root will be deployed.
* The management system: the operating system tree where you will build the GRUB fixer package.

These two systems can in fact be contained within the same computer (e.g. the target system may be a chroot mounted within the management system), but the distinction is more than just conceptual.

Ensure you have GRUB 2 in the target system
-------------------------------------------

Make sure that you have vanilla distribution GRUB installed on the target system.  Also make sure that the GRUB version you have installed there is the distributor's version, instead of a patched GRUB version that you may have had in the past.

Obtain these sources
--------------------

Using Git or the Github Web interface in your management system, obtain the sources to

    https://github.com/Rudd-O/zfs-fedora-installer

Once these sources are in a local directory, `tar cvzf` the `grub-zfs-fixer` subdirectory into a file named `grub-zfs-fixer.tar.gz`.

Build the RPM packages
----------------------

Once you have the `grub-zfs-fixer.tar.gz` file in your management system, run the command

    rpmbuild -ta grub-zfs-fixer.tar.gz

within the directory containing that file.

This command will generate an installable RPM in `~/rpmbuild/RPMS/noarch`.  This directory may vary depending on your distribution, but the output of the command will make it clear where it was created.

Install the RPM package in the target system
--------------------------------------------

Copy the installable RPM from the management system to the target system, then install it within the target system with the command:

    rpm -ivh grub-zfs-fixer*noarch.rpm

running it within the directory of the target system containing that file.  Naturally, if the target system is within a chroot (because e.g. you are preparing / building an image on top of a temporarily-mounted ZFS file system), you must install the RPM within the chroot.

When you install the `noarch` RPM, you will end up with the exact proper GRUB files that will let you boot the target system from its own ZFS on root.  The RPM will take care of modifying your GRUB scripts as GRUB gets upgraded, so you won't have to worry about GRUB problems anymore.

Optional: add an entry for your target system's root dataset to `/etc/fstab`
----------------------------------------------------------------

If you would like your target system's `/etc/fstab` to point to the root ZFS dataset, add the entry to the target system now.  Here is an example:

```
tank/ROOT/fedora       /     zfs       defaults     0 0
```

Re-run `grub2-mkconfig` on the target system
--------------------------------------------

Within the target system, re-run `/usr/sbin/grub2-mkconfig`.

This will regenerate the boot entries in `/boot/grub2/grub.cfg`, autodetecting the root dataset containing the target system, and ensuring that each entry has a valid `root=` parameter pointing to that dataset.  This parameter instructs the initial RAM disk to mount the correct dataset during boot.
