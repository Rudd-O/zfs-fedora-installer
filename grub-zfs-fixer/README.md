GRUB compatibility for booting off of ZFS root file systems
===========================================================

This directory contains a formula to create a RPM package that will ensure your computer's GRUB 2 will be able to boot off of a ZFS root file system.

In this document, we will make a distinction between:

* The target system: the operating system where ZFS on root will be deployed.
* The management system: the operating system where you will build the GRUB fixer package.

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
