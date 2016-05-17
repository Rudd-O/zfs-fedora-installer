Qubes support for zfs-fedora-installer
======================================

Some dependencies for zfs-fedora-installer are not installable within Qubes OS.
The way the guest OSes set up their dependencies makes it impossible to run
the bootloader installation and testing part of the image creation.  Therefore,
it is necessary to create a special standalone VM for the purpose of running
zfs-fedora-installer within a Qubes OS VM.

These are the instructions to get it to work.  This will only work on a
Fedora-based standalone VM, which you are about to create for this purpose.

Step 0: install `grub2-xen` on your dom0
----------------------------------------

```
sudo qubes-dom0-update -y grub2-xen
```

Step 1: create a standalone VM
------------------------------

You must create a standalone VM at this point.  Name it whatever you will.

Step 2: enlarge the private storage of the standalone VM
--------------------------------------------------------

Use the Qubes OS preferences dialog, or the command line `qvm-prefs` command,
to enlarge the standalone VM's private storage to 20 GB or more.

Step 3: launch a terminal on the standalone VM
----------------------------------------------

As the VM boots, watch the terminal launch.

Step 4: clone the zfs-fedora-installer repository
-------------------------------------------------

Use your `git` command to clone it to the home directory:

```
git clone https://github.com/Rudd-O/zfs-fedora-installer
```

(This assumes that your standalone VM already has git.  If that is not the case,
you can always install it using `dnf` or `yum` first.)

Step 5: run the `vmpreparation` command
---------------------------------------

```
cd zfs-fedora-installer/qubes
sudo ./vmpreparation
cd ..
```

Step 6: set the standalone VM to boot using `pvgrub`
----------------------------------------------------

Now that the kernel has been installed in your newly-minted GRUB-booting
standalone VM, set it up in dom0 to boot using `pvgrub`:

```
qvm-prefs <name of standalone VM> -s kernel pvgrub2
```

Step 7: power the VM off
------------------------

Power the VM off, with `poweroff`, or from the Qubes Manager GUI.

Step 8: launch another terminal on the standalone VM
----------------------------------------------------

This boots the VM, now set up to boot using `pvgrub`.  Once you get to the
terminal, follow on with the next step.

Step 9: deploy ZFS on the VM
----------------------------

With the newly-minted kernel and kernel development packages installed
on your standalone VM, you can now deploy the ZFS utilities and the
kernel modules:

```
cd zfs-fedora-installer/qubes
sudo ./zfspreparation
cd ..
```

Step 10: create a workable ZFS image
------------------------------------

While the script `imagebuild` is crude, it should be adequate to at least
get you started.  The script uses `mock` (via `mockwrapper`) to create a chroot
within your home directory's `mockroot` directory that is equivalent to your
host, but with the whole setup necessary to run `zfs-fedora-installer`
without any complications.

To execute the script:

```
cd zfs-fedora-installer/qubes
mkdir -p path/to/mockroot
sudo ./imagebuild path/to/mockroot
cd ..
```

From what you can see in the script, the output image will end up created in
`path/to/mockroot/fedora-23-z86_64-custom/root/usr/src/diskimage/diskimage.img`
and, at that point, you can use the created image for your own purposes.

That should be all you need to do in order to get a bootable Fedora image
with ZFS on root and disk encryption, straight out of a Qubes OS VM.
