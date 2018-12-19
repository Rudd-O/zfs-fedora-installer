#!/usr/bin/env python

import contextlib
import sys
import os
import argparse
import subprocess
import stat
import time
from os.path import join as j
import shutil
import glob
import platform
import tempfile
import logging
import re
import signal
import shlex
import multiprocessing
import pipes
import errno

from installfedoraonzfs.cmd import check_call, format_cmdline, check_output
from installfedoraonzfs.cmd import Popen, mount, bindmount, umount, ismount
from installfedoraonzfs.cmd import get_associated_lodev, get_output_exitcode
from installfedoraonzfs.cmd import check_call_no_output
from installfedoraonzfs.pm import ChrootPackageManager, SystemPackageManager
from installfedoraonzfs.vm import boot_image_in_qemu, BootDriver, test_qemu
import installfedoraonzfs.retry as retrymod
from installfedoraonzfs.breakingbefore import BreakingBefore, break_stages


BASE_PACKAGES = ("basesystem rootfiles bash nano binutils rsync NetworkManager "
                 "rpm vim-minimal e2fsprogs passwd pam net-tools cryptsetup "
                 "kbd-misc kbd policycoreutils selinux-policy-targeted "
                 "libseccomp util-linux sed pciutils").split()
BASIC_FORMAT = '%(levelname)8s:%(name)14s:%(funcName)20s@%(lineno)4d\t%(message)s'
qemu_timeout = 180


def get_parser():
    parser = argparse.ArgumentParser(
        description="Install a minimal Fedora system inside a ZFS pool within a disk image or device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "voldev", metavar="VOLDEV", type=str, nargs=1,
        help="path to volume (device to use or regular file to create)"
    )
    parser.add_argument(
        "--vol-size", dest="volsize", metavar="VOLSIZE", type=int,
        action="store", default=11000, help="volume size in MiB (default 11000)"
    )
    parser.add_argument(
        "--separate-boot", dest="bootdev", metavar="BOOTDEV", type=str,
        action="store", default=None, help="place /boot in a separate volume"
    )
    parser.add_argument(
        "--boot-size", dest="bootsize", metavar="BOOTSIZE", type=int,
        action="store", default=512, help="boot partition size in MiB, or boot volume size in MiB, when --separate-boot is specified (default 256)"
    )
    parser.add_argument(
        "--pool-name", dest="poolname", metavar="POOLNAME", type=str,
        action="store", default="tank", help="pool name (default tank)"
    )
    parser.add_argument(
        "--host-name", dest="hostname", metavar="HOSTNAME", type=str,
        action="store", default="localhost.localdomain", help="host name (default localhost.localdomain)"
    )
    parser.add_argument(
        "--root-password", dest="rootpassword", metavar="ROOTPASSWORD", type=str,
        action="store", default="password", help="root password (default password)"
    )
    parser.add_argument(
        "--swap-size", dest="swapsize", metavar="SWAPSIZE", type=int,
        action="store", default=1024, help="swap volume size in MiB (default 1024)"
    )
    parser.add_argument(
        "--releasever", dest="releasever", metavar="VER", type=int,
        action="store", default=None, help="Fedora release version (default the same as the computer you are installing on)"
    )
    parser.add_argument(
        "--use-prebuilt-rpms", dest="prebuiltrpms", metavar="DIR", type=str,
        action="store", default=None, help="also install pre-built ZFS, GRUB and other RPMs in this directory, except for debuginfo packages within the directory (default: build ZFS and GRUB RPMs, within the chroot)"
    )
    parser.add_argument(
        "--luks-password", dest="lukspassword", metavar="LUKSPASSWORD", type=str,
        action="store", default=None, help="LUKS password to encrypt the ZFS volume with (default no encryption); unprintable glyphs whose ASCII value lies below 32 (the space character) will be rejected"
    )
    parser.add_argument(
        "--luks-options", dest="luksoptions", metavar="LUKSOPTIONS", type=str,
        action="store", default=None, help="space-separated list of options to pass to cryptsetup luksFormat (default no options)"
    )
    parser.add_argument(
        "--no-cleanup", dest="nocleanup",
        action="store_true", default=False, help="if an error occurs, do not clean up working volumes"
    )
    parser.add_argument(
        "--interactive-qemu", dest="interactive_qemu",
        action="store_true", default=False, help="QEMU will run interactively, with the console of your Linux system connected to your terminal; the normal timeout of %s seconds will not apply, and Ctrl+C will interrupt the emulation; this is useful to manually debug problems installing the bootloader; in this mode you are responsible for typing the password to any LUKS devices you have requested to be created" % qemu_timeout
    )
    parser.add_argument(
        "--yum-cachedir", dest="yum_cachedir",
        action="store", default=None, help="directory to use for a yum cache that persists across executions"
    )
    parser.add_argument(
        "--force-kvm", dest="force_kvm",
        action="store_true", default=None, help="force KVM use for the boot sector installation (default autodetect)"
    )
    parser.add_argument(
        "--chown", dest="chown",
        action="store", default=None, help="change the owner of the image files upon creation to this user"
    )
    parser.add_argument(
        "--chgrp", dest="chgrp",
        action="store", default=None, help="change the group of the image files upon creation to this group"
    )
    parser.add_argument(
        "--use-branch", dest="branch",
        action="store", default="master", help="when building ZFS from source, check out this branch instead of master"
    )
    parser.add_argument(
        "--break-before", dest="break_before",
        choices=break_stages,
        action="store", default=None,
        help="break before the specified stage (see below); useful to examine "
             "the file systems and files at a predetermined build stage; it is "
             "also useful to combine it with --no-cleanup to prevent the file "
             "systems and mounts from being undone, leaving you with a system "
             "ready to inspect"
    )
    parser.add_argument(
        "--workdir", dest="workdir",
        action="store", default='/var/lib/zfs-fedora-installer',
        help="use this directory as a working (scratch) space for the mount points of the created pool"
    )
    parser.epilog = "Stages for the --break_before argument:\n%s" % (
        "".join("\n* %s:%s%s" % (k, " "*(max(len(x) for x in break_stages)-len(k)+1), v) for k,v in break_stages.items()),
    )
    return parser

def get_deploy_parser():
    parser = argparse.ArgumentParser(
        description="Install ZFS on a running system"
    )
    parser.add_argument(
        "--use-prebuilt-rpms", dest="prebuiltrpms", metavar="DIR", type=str,
        action="store", default=None, help="also install pre-built ZFS, GRUB and other RPMs in this directory, except for debuginfo packages within the directory (default: build ZFS and GRUB RPMs, within the system)"
    )
    parser.add_argument(
        "--no-cleanup", dest="nocleanup",
        action="store_true", default=False, help="if an error occurs, do not clean up temporary mounts and files"
    )
    parser.add_argument(
        "--use-branch", dest="branch",
        action="store", default="master", help="when building ZFS from source, check out this branch instead of master"
    )
    return parser


def filetype(dev):
    '''returns 'file' or 'blockdev' or 'doesntexist' for dev'''
    try:
        s = os.stat(dev)
    except OSError, e:
        if e.errno == 2: return 'doesntexist'
        raise
    if stat.S_ISBLK(s.st_mode): return 'blockdev'
    if stat.S_ISREG(s.st_mode): return 'file'
    assert 0, 'specified path %r is not a block device or a file'


def losetup(path):
    dev = check_output(
        ["losetup", "-P", "--find", "--show", path]
    )[:-1]
    return dev

def import_pool(poolname, rootmountpoint):
    return check_call(["zpool", "import", "-f", "-R", rootmountpoint, poolname])

def list_pools():
    d = check_output(["zpool", "list", "-H", "-o", "name"], logall=True)
    return [x for x in d.splitlines() if x]

# We try the import of the pool 3 times, with a 5-second timeout in between tries.
import_pool_retryable = retrymod.retry(2, timeout=5, retryable_exception=subprocess.CalledProcessError)(import_pool)

def partition_boot(bootdev, bootsize, rootvol):
    '''Partitions device into four partitions.

    1. a 2MB biosboot partition
    2. an EFI partition, sized bootsize/2
    3. a /boot partition, sized bootsize/2
    4. if rootvol evals to True: a root volume partition

    Caller is responsible for waiting until the devices appear.
    '''
    cmd = ["gdisk", bootdev]
    pr = Popen(cmd, stdin=subprocess.PIPE)
    if rootvol:
        pr.communicate(
    '''o
y

n
1

+2M
21686148-6449-6E6F-744E-656564454649


n
2

+%sM
C12A7328-F81F-11D2-BA4B-00A0C93EC93B


n
3

+%sM



n
4





p
w
y
'''%(bootsize / 2, bootsize / 2)
        )
    else:
        pr.communicate(
        '''o
y

n
1

+2M
21686148-6449-6E6F-744E-656564454649


n
2

+%sM
C12A7328-F81F-11D2-BA4B-00A0C93EC93B


n
3



p
w
y
''' % (bootsize / 2)
        )
    retcode = pr.wait()
    if retcode != 0: raise subprocess.CalledProcessError(retcode, cmd)

@contextlib.contextmanager
def blockdev_context(voldev, bootdev, undoer, volsize, bootsize, chown, chgrp, create):
    '''Takes a volume device path, and possible a boot device path,
    and yields a properly partitioned set of volumes which can
    then be used to format and create pools on.

    Work to be undone is executed by the undoer passed by the caller.

    TODO: use context manager undo strategy instead.
    '''
    voltype = filetype(voldev)

    if voltype == 'doesntexist':  # FIXME use truncate directly with python.  no need to dick around.
        create_file(voldev, volsize * 1024 * 1024, owner=chown, group=chgrp)
        voltype = 'file'

    if voltype == 'file':
        if get_associated_lodev(voldev):
            new_voldev = get_associated_lodev(voldev)
        else:
            new_voldev = losetup(voldev)
        assert new_voldev is not None, (new_voldev, voldev)
        undoer.to_un_losetup.append(new_voldev)
        voldev = new_voldev
        voltype = 'blockdev'

    if bootdev:
        boottype = filetype(bootdev)

        if boottype == 'doesntexist':
            create_file(bootdev, bootsize * 1024 * 1024, owner=chown, group=chgrp)
            boottype = 'file'

        if boottype == 'file':
            if get_associated_lodev(bootdev):
                new_bootdev = get_associated_lodev(bootdev)
            else:
                new_bootdev = losetup(bootdev)
            assert new_bootdev is not None, (new_bootdev, bootdev)
            undoer.to_un_losetup.append(new_bootdev)
            bootdev = new_bootdev
            boottype = 'blockdev'

    def get_rootpart(rdev):
        parts = [
            rdev + "p4"
        ] if rdev.startswith("/dev/loop") else [
            rdev + "-part4",
            rdev + "4",
        ]
        for rootpart in parts:
            if os.path.exists(rootpart):
                return rootpart

    def get_efipart_bootpart(bdev):
        parts = [
            (bdev + "p2", bdev + "p3"),
        ] if bdev.startswith("/dev/loop") else [
            (bdev + "-part2", bdev + "-part3"),
            (bdev + "2", bdev + "3"),
        ]
        for efipart, bootpart in parts:
            if os.path.exists(efipart) and os.path.exists(bootpart):
                return efipart, bootpart
        return None, None

    efipart, bootpart = get_efipart_bootpart(bootdev or voldev)
    if None in (bootpart, efipart):
        if not create:
            raise Exception("Wanted to partition boot device %s but create=False" % (bootdev or voldev))
        partition_boot(bootdev or voldev, bootsize, not bootdev)

    efipart, bootpart = get_efipart_bootpart(bootdev or voldev)
    if None in (efipart, bootpart):
        assert 0, "partitions 2 or 3 in device %r failed to be created"%(bootdev or voldev)

    rootpart = voldev if bootdev else get_rootpart(voldev)
    assert rootpart or voldev, "partition 4 in device %r failed to be created"%voldev

#   # This is debugging code that has been shunted off.
#
#     if voldev.startswith("/dev/loop"):
#         o, r = get_output_exitcode(["losetup", "-l", voldev])
#         logging.debug("losetup voldev %s: %s", voldev, o)
#         assert r == 0, r
# 
#     if bootdev:
#         o, r = get_output_exitcode(["losetup", "-l", bootdev])
#         logging.debug("losetup bootdev %s: %s", bootdev, o)
#         assert r == 0, r
# 
#     for name, part in (
#         ("EFI", efipart),
#         ("boot", bootpart),
#         ("root", rootpart),
#     ):
#         o, r = get_output_exitcode(["ls", "-la", part])
#         logging.debug("ls %s partition %s: %s", name, part, o)
#         assert r == 0, r
#         o, r = get_output_exitcode(["blkid", "-c", "/dev/null", part])
#         logging.debug("blkid %s partition %s: %s", name, part, o)
#         if "root" == name:
#             assert r == 0 or o == "", (r, o)
#         else:
#             assert r == 0, r

    yield rootpart, bootpart, efipart

def setup_boot_filesystems(bootpart, efipart, label_postfix, create):
    '''Sets up boot and EFI file systems.

    This function is a noop if file systems already exist.
    '''
    try: output = check_output(["blkid", "-c", "/dev/null", bootpart])
    except subprocess.CalledProcessError: output = ""
    if 'TYPE="ext4"' not in output:
        if not create:
            raise Exception("Wanted to create boot file system on %s but create=False" % bootpart)
        check_call(["mkfs.ext4", "-L", "boot_" + label_postfix, bootpart])
    bootpartuuid = check_output(["blkid", "-c", "/dev/null", bootpart, "-o", "value", "-s", "UUID"]).strip()

    try: output = check_output(["blkid", "-c", "/dev/null", efipart])
    except subprocess.CalledProcessError: output = ""
    if 'TYPE="vfat"' not in output:
        if not create:
            raise Exception("Wanted to create EFI file system on %s but create=False" % efipart)
        check_call(["mkfs.vfat", "-F", "32", "-n", "efi_" + label_postfix, efipart])
    efipartuuid = check_output(["blkid", "-c", "/dev/null", efipart, "-o", "value", "-s", "UUID"]).strip()

    return bootpartuuid, efipartuuid

@contextlib.contextmanager
def filesystem_context(poolname, rootpart, bootpart, efipart, undoer, workdir,
                       swapsize, lukspassword, luksoptions, create):

    bootpartuuid, efipartuuid = setup_boot_filesystems(bootpart, efipart, poolname, create)

    if lukspassword:
        needsdoing = False
        try:
            rootuuid = check_output(["blkid", "-c", "/dev/null", rootpart, "-o", "value", "-s", "UUID"]).strip()
            if not rootuuid:
                raise IndexError("no UUID for %s" % rootpart)
            luksuuid = "luks-" + rootuuid
        except IndexError:
            needsdoing = True
        except subprocess.CalledProcessError, e:
            if e.returncode != 2: raise
            needsdoing = True
        if needsdoing:
            if not create:
                raise Exception("Wanted to create LUKS volume on %s but create=False" % rootpart)
            luksopts = shlex.split(luksoptions) if luksoptions else []
            cmd = ["cryptsetup", "-y", "-v", "luksFormat"] + luksopts + [rootpart, '-']
            proc = Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(lukspassword)
            retcode = proc.wait()
            if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
            rootuuid = check_output(["blkid", "-c", "/dev/null", rootpart, "-o", "value", "-s", "UUID"]).strip()
            if not rootuuid:
                raise IndexError("still no UUID for %s" % rootpart)
            luksuuid = "luks-" + rootuuid
        if not os.path.exists(j("/dev","mapper",luksuuid)):
            cmd = ["cryptsetup", "-y", "-v", "luksOpen", rootpart, luksuuid]
            proc = Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(lukspassword)
            retcode = proc.wait()
            if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
        undoer.to_luks_close.append(luksuuid)
        rootpart = j("/dev","mapper",luksuuid)
    else:
        rootuuid = None
        luksuuid = None

    rootmountpoint = j(workdir, poolname)
    if poolname not in list_pools():
        func = import_pool if create else import_pool_retryable
        try:
            func(poolname, rootmountpoint)
        except subprocess.CalledProcessError, e:
            if not create:
                check_call(['blkid', '-c', '/dev/null'])
                raise Exception("Wanted to create ZFS pool %s on %s but create=False" % (poolname, rootpart))
            check_call(["zpool", "create", "-m", "none",
                                "-o", "ashift=12",
                                "-O", "compression=on",
                                "-O", "atime=off",
                                "-O", "com.sun:auto-snapshot=false",
                                "-R", rootmountpoint,
                                poolname, rootpart])
            check_call(["zfs", "set", "xattr=sa", poolname])
    undoer.to_export.append(poolname)

    try:
        check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT")],
                            stdout=file(os.devnull,"w"))
    except subprocess.CalledProcessError, e:
        if not create:
            raise Exception("Wanted to create ZFS file system ROOT on %s but create=False" % poolname)
        check_call(["zfs", "create", j(poolname, "ROOT")])

    try:
        check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT", "os")],
                            stdout=file(os.devnull,"w"))
        if not os.path.ismount(rootmountpoint):
            check_call(["zfs", "mount", j(poolname, "ROOT", "os")])
    except subprocess.CalledProcessError, e:
        if not create:
            raise Exception("Wanted to create ZFS file system ROOT/os on %s but create=False" % poolname)
        check_call(["zfs", "create", "-o", "mountpoint=/", j(poolname, "ROOT", "os")])
        check_call(["touch", j(rootmountpoint, ".autorelabel")])
    undoer.to_unmount.append(rootmountpoint)

    try:
        check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "swap")],
                            stdout=file(os.devnull,"w"))
    except subprocess.CalledProcessError, e:
        if not create:
            raise Exception("Wanted to create ZFS file system swap on %s but create=False" % poolname)
        check_call(["zfs", "create", "-V", "%dM"%swapsize, "-b", "4K", j(poolname, "swap")])
        check_call(["zfs", "set", "compression=gzip-9", j(poolname, "swap")])
        check_call(["zfs", "set", "com.sun:auto-snapshot=false", j(poolname, "swap")])
    swappart = os.path.join("/dev/zvol", poolname, "swap")

    for _ in range(5):
        if not os.path.exists(swappart):
            time.sleep(5)
    if not os.path.exists(swappart):
        raise ZFSMalfunction("ZFS does not appear to create the device nodes for zvols.  If you installed ZFS from source, pay attention that the --with-udevdir= configure parameter is correct.")

    try: output = check_output(["blkid", "-c", "/dev/null", swappart])
    except subprocess.CalledProcessError: output = ""
    if 'TYPE="swap"' not in output:
        if not create:
            raise Exception("Wanted to create swap volume on %s/swap but create=False" % poolname)
        check_call(["mkswap", '-f', swappart])

    p = lambda withinchroot: j(rootmountpoint, withinchroot.lstrip(os.path.sep))
    q = lambda outsidechroot: outsidechroot[len(rootmountpoint):]

    # mount virtual file systems, creating their mount points as necessary
    for m in "boot sys proc".split():
        if not os.path.isdir(p(m)): os.mkdir(p(m))

    if not os.path.ismount(p("boot")):
        mount(bootpart, p("boot"))
    undoer.to_unmount.append(p("boot"))

    for m in "boot/efi".split():
        if not os.path.isdir(p(m)): os.mkdir(p(m))

    if not os.path.ismount(p("boot/efi")):
        mount(efipart, p("boot/efi"))
    undoer.to_unmount.append(p("boot/efi"))

    if not os.path.ismount(p("sys")):
        mount("sysfs", p("sys"), "-t", "sysfs")
    undoer.to_unmount.append(p("sys"))

    selinuxfs= p(j("sys", "fs", "selinux"))
    if os.path.isdir(selinuxfs):
        if not os.path.ismount(selinuxfs):
            mount("selinuxfs", selinuxfs, "-t", "selinuxfs")
        undoer.to_unmount.append(selinuxfs)

    if not os.path.ismount(p("proc")):
        mount("proc", p("proc"), "-t", "proc")
    undoer.to_unmount.append(p("proc"))

    # create needed directories to succeed in chrooting as per #22
    for m in "etc var var/lib var/lib/dbus var/log var/log/audit".split():
        if not os.path.isdir(p(m)):
            os.mkdir(p(m))
            if m == "var/log/audit":
                os.chmod(p(m), 0700)

    def in_chroot(lst):
        return ["chroot", rootmountpoint] + lst

    yield rootmountpoint, p, q, in_chroot, rootuuid, luksuuid, bootpartuuid, efipartuuid

def get_file_size(filename):
    "Get the file size by seeking at end"
    fd= os.open(filename, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)

def create_file(filename, sizebytes, owner=None, group=None):
    f = file(filename, "wb")
    f.seek(sizebytes-1)
    f.write("\0")
    f.close()
    if owner:
        check_call(["chown", owner, "--", filename])
    if group:
        check_call(["chgrp", group, "--", filename])

def delete_contents(directory):
    if not os.path.exists(directory):
        return
    ps = [ j(directory, p) for p in os.listdir(directory) ]
    if ps:
        check_call(["rm", "-rf"] + ps)



class ZFSMalfunction(Exception): pass
class ZFSBuildFailure(Exception): pass
class ImpossiblePassphrase(Exception): pass


class Undoer:

    def __init__(self):
        self.actions = []

        class Tracker:

            def __init__(self, typ):
                self.typ = typ

            def append(me, o):
                assert o is not None
                self.actions.append([me.typ, o])

            def remove(me, o):
                for n, (typ, origo) in reversed(list(enumerate(self.actions[:]))):
                    if typ == me.typ and o == origo:
                        self.actions.pop(n)
                        break

        self.to_un_losetup = Tracker("un_losetup")
        self.to_luks_close = Tracker("luks_close")
        self.to_export = Tracker("export")
        self.to_rmdir = Tracker("rmdir")
        self.to_unmount = Tracker("unmount")
        self.to_rmrf = Tracker("rmrf")

    def undo(self):
        for n, (typ, o) in reversed(list(enumerate(self.actions[:]))):
            if typ == "unmount":
                umount(o)
            if typ == "rmrf":
                shutil.rmtree(o)
            if typ == "rmdir":
                os.rmdir(o)
            if typ == "export":
                check_call(["sync"])
                check_call(["zpool", "export", o])
            if typ == "luks_close":
                check_call(["sync"])
                check_call(["cryptsetup", "luksClose", o])
            if typ == "un_losetup":
                check_call(["sync"])
                check_call(["losetup", "-d", o])
                time.sleep(1)
            self.actions.pop(n)


def install_fedora(voldev, volsize, bootdev=None, bootsize=256,
                   poolname='tank', hostname='localhost.localdomain',
                   rootpassword='password', swapsize=1024,
                   releasever=None, lukspassword=None,
                   do_cleanup=True,
                   interactive_qemu=False,
                   luksoptions=None,
                   prebuilt_rpms_path=None,
                   yum_cachedir_path=None,
                   force_kvm=None,
                   chown=None,
                   chgrp=None,
                   break_before=None,
                   branch="master",
                   workdir='/var/lib/zfs-fedora-installer',
    ):

    if lukspassword and not BootDriver.is_typeable(lukspassword):
        raise ImpossiblePassphrase("LUKS passphrase %r cannot be typed during boot" % lukspassword)

    if rootpassword and not BootDriver.is_typeable(rootpassword):
        raise ImpossiblePassphrase("root password %r cannot be typed during boot" % rootpassword)

    original_voldev = voldev
    original_bootdev = bootdev

    undoer = Undoer()
    to_rmdir = undoer.to_rmdir
    to_unmount = undoer.to_unmount
    to_rmrf = undoer.to_rmrf

    def cleanup():
        undoer.undo()

    if not releasever:
        releasever = ChrootPackageManager.get_my_releasever()

    try:
        # check for stage stop
        if break_before == "beginning":
            raise BreakingBefore(break_before)

        with blockdev_context(
            voldev, bootdev, undoer, volsize, bootsize, chown, chgrp, create=True
        ) as (
            rootpart, bootpart, efipart
        ):
            with filesystem_context(
                poolname, rootpart, bootpart, efipart, undoer, workdir,
                swapsize, lukspassword, luksoptions, create=True
            ) as (
                rootmountpoint, p, _, in_chroot, rootuuid, luksuuid, bootpartuuid, efipartuuid
            ):
                # Save the partitions' UUIDS for cross checking later.
                first_bootpartuuid = bootpartuuid
                first_rootuuid = rootuuid

                # sync device files
                check_call(["rsync", "-ax", "--numeric-ids",
#                           "--exclude=mapper",
                            "--exclude=zvol",
#                           "--exclude=disk",
                            "--exclude=sd*",
#                           "--exclude=zd*",
                            "--delete", "--delete-excluded",
                            "/dev/", p("dev/")])

                # sync RPM GPG keys
                for m in "etc/pki etc/pki/rpm-gpg".split():
                    if not os.path.isdir(p(m)): os.mkdir(p(m))
                check_call(
                    ["rsync", "-ax", "--numeric-ids"] + \
                    glob.glob("/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora*") + \
                    [p("etc/pki/rpm-gpg/")]
                )

                # make up a nice locale.conf file. neutral. international
                localeconf = \
'''LANG="en_US.UTF-8"
'''
                file(p(j("etc", "locale.conf")),"w").write(localeconf)

                # make up a nice vconsole.conf file. neutral. international
                vconsoleconf = \
'''KEYMAP="us"
'''
                file(p(j("etc", "vconsole.conf")), "w").write(vconsoleconf)

                # make up a nice fstab file
                fstab = \
'''%s/ROOT/os / zfs defaults,x-systemd-device-timeout=0 0 0
UUID=%s /boot ext4 noatime 0 1
UUID=%s /boot/efi vfat noatime 0 1
/dev/zvol/%s/swap swap swap discard 0 0
'''%(poolname, bootpartuuid, efipartuuid, poolname)
                file(p(j("etc", "fstab")),"w").write(fstab)

                # create a number of important files
                if not os.path.exists(p(j("etc", "mtab"))):
                    os.symlink("../proc/self/mounts", p(j("etc", "mtab")))
                resolvconf = p(j("etc", "resolv.conf"))
                if not os.path.isfile(resolvconf) and not os.path.islink(resolvconf):
                    file(resolvconf,"w").write(file(j("/etc", "resolv.conf")).read())
                if not os.path.exists(p(j("etc", "hostname"))):
                    file(p(j("etc", "hostname")),"w").write(hostname)
                if not os.path.exists(p(j("etc", "hostid"))):
                    randomness = file("/dev/urandom").read(4)
                    file(p(j("etc", "hostid")),"w").write(randomness)
                if not os.path.exists(p(j("etc", "locale.conf"))):
                    file(p(j("etc", "locale.conf")),"w").write("LANG=en_US.UTF-8\n")
                hostid = file(p(j("etc", "hostid"))).read().encode("hex")
                hostid = "%s%s%s%s"%(hostid[6:8],hostid[4:6],hostid[2:4],hostid[0:2])

                if luksuuid:
                    crypttab = \
'''%s UUID=%s none discard
'''%(luksuuid,rootuuid)
                    file(p(j("etc", "crypttab")),"w").write(crypttab)
                    os.chmod(p(j("etc", "crypttab")), 0600)

                pkgmgr = ChrootPackageManager(rootmountpoint, releasever, yum_cachedir_path)

                # install base packages
                packages = list(BASE_PACKAGES)
                if releasever >= 21:
                    packages.append("dnf")
                else:
                    packages.append("yum")
                # install initial boot packages
                packages = packages + "grub2 grub2-tools grubby efibootmgr".split()
                if releasever >= 27:
                    packages = packages + "shim-x64 grub2-efi-x64 grub2-efi-x64-modules".split()
                else:
                    packages = packages + "shim grub2-efi grub2-efi-modules".split()
                pkgmgr.ensure_packages_installed(packages, method='out_of_chroot')

                # omit zfs modules when dracutting
                if not os.path.exists(p("usr/bin/dracut.real")):
                    check_call(in_chroot(["mv", "/usr/bin/dracut", "/usr/bin/dracut.real"]))
                    file(p("usr/bin/dracut"), "w").write("""#!/bin/bash

echo This is a fake dracut.
""")
                    os.chmod(p("usr/bin/dracut"), 0755)

                if luksuuid:
                    luksstuff = " rd.luks.uuid=%s rd.luks.allow-discards"%(rootuuid,)
                else:
                    luksstuff = ""

                # write grub config
                grubconfig = """GRUB_TIMEOUT=0
GRUB_HIDDEN_TIMEOUT=3
GRUB_HIDDEN_TIMEOUT_QUIET=true
GRUB_DISTRIBUTOR="$(sed 's, release .*$,,g' /etc/system-release)"
GRUB_DEFAULT=saved
GRUB_CMDLINE_LINUX="rd.md=0 rd.lvm=0 rd.dm=0 $([ -x /usr/sbin/rhcrashkernel-param ] && /usr/sbin/rhcrashkernel-param || :) quiet systemd.show_status=true%s"
GRUB_DISABLE_RECOVERY="true"
GRUB_GFXPAYLOAD_LINUX="keep"
GRUB_TERMINAL_OUTPUT="vga_text"
GRUB_DISABLE_LINUX_UUID=true
GRUB_PRELOAD_MODULES='part_msdos ext2'
"""%(luksstuff,)
                file(p(j("etc","default","grub")),"w").write(grubconfig)

                # write kernel command line
                if not os.path.isdir(p(j("etc","kernel"))):
                    os.mkdir(p(j("etc","kernel")))
                grubconfig = """root=ZFS=%s/ROOT/os rd.md=0 rd.lvm=0 rd.dm=0 quiet systemd.show_status=true%s
"""%(poolname,luksstuff,)
                file(p(j("etc","kernel","cmdline")),"w").write(grubconfig)

                # install kernel packages
                packages = "kernel kernel-devel".split()
                pkgmgr.ensure_packages_installed(packages, method='out_of_chroot')

                # set password
                pwfile = file(p(j("etc", "shadow"))).readlines()
                pwnotset = bool([ l for l in pwfile if l.startswith("root:*:") ])
                if pwnotset:
                    cmd = in_chroot(["passwd", "--stdin", "root"])
                    pw = Popen(cmd, stdin=subprocess.PIPE)
                    pw.communicate(rootpassword + "\n")
                    retcode = pw.wait()
                    if retcode != 0: raise subprocess.CalledProcessError(retcode, cmd)

                deploy_zfs_in_machine(p=p,
                                    in_chroot=in_chroot,
                                    pkgmgr=pkgmgr,
                                    prebuilt_rpms_path=prebuilt_rpms_path,
                                    branch=branch,
                                    break_before=break_before,
                                    to_unmount=to_unmount,
                                    to_rmdir=to_rmdir,)

                # release disk space now that installation is done
                for pkgm in ('dnf', 'yum'):
                    for directory in ("cache", "lib"):
                        delete_contents(p(j("var", directory, pkgm)))

                # check for stage stop
                if break_before == "reload_chroot":
                    raise BreakingBefore(break_before)

        # The following reload in a different context scope is a workaround
        # for blkid failing without the reload happening first.

        check_call(['sync'])

        cleanup()

        with blockdev_context(
            voldev, bootdev, undoer, volsize, bootsize, chown, chgrp, create=False
        ) as (
            rootpart, bootpart, efipart
        ):
            with filesystem_context(
                poolname, rootpart, bootpart, efipart, undoer, workdir,
                swapsize, lukspassword, luksoptions, create=False
            ) as (
                _, p, q, in_chroot, rootuuid, _, bootpartuuid, efipartuuid
            ):
                # Check that our UUIDs haven't changed from under us.
                if bootpartuuid != first_bootpartuuid:
                    raise Exception("The boot partition UUID changed from %s to %s between remounts!" % (first_bootpartuuid, bootpartuuid))
                if rootuuid != first_rootuuid:
                    raise Exception("The root device UUID changed from %s to %s between remounts!" % (first_rootuuid, rootuuid))

                if os.path.exists(p("usr/bin/dracut.real")):
                    check_call(in_chroot(["mv", "/usr/bin/dracut.real", "/usr/bin/dracut"]))
                def get_kernel_initrd_kver():
                    try:
                        kernel = glob.glob(p(j("boot", "loader", "*", "linux")))[0]
                        kver = os.path.basename(os.path.dirname(kernel))
                        initrd = p(j("boot", "loader", kver, "initrd"))
                        hostonly = p(j("boot", "loader", kver, "initrd-hostonly"))
                        return kernel, initrd, hostonly, kver
                    except IndexError:
                        kernel = glob.glob(p(j("boot", "vmlinuz-*")))[0]
                        kver = os.path.basename(kernel)[len("vmlinuz-"):]
                        initrd = p(j("boot", "initramfs-%s.img"%kver))
                        hostonly = p(j("boot", "initramfs-hostonly-%s.img"%kver))
                        return kernel, initrd, hostonly, kver
                    except Exception:
                        check_call(in_chroot(["ls", "-lRa", "/boot"]))
                        raise
                kernel, initrd, hostonly_initrd, kver = get_kernel_initrd_kver()
                if os.path.isfile(initrd):
                    mayhapszfsko = check_output(["lsinitrd", initrd])
                else:
                    mayhapszfsko = ""
                # At this point, we regenerate the initrds.
                if "zfs.ko" not in mayhapszfsko:
                    check_call(in_chroot(["dracut", "-Nf", q(initrd), kver]))
                    for l in check_output(["lsinitrd", initrd]).splitlines(False):
                        logging.debug("initramfs: %s", l)

                # Kill the resolv.conf file written only to install packages.
                if os.path.isfile(p(j("etc", "resolv.conf"))):
                    os.unlink(p(j("etc", "resolv.conf")))

                # Remove host device files
                shutil.rmtree(p("dev"))
                # sync /dev but only itself and /dev/zfs
                check_call(["rsync", "-ptlgoD", "--numeric-ids", "/dev/", p("dev/")])
                check_call(["rsync", "-ptlgoD", "--numeric-ids", "/dev/zfs", p("dev/zfs")])

                # Restore SELinux contexts.
                if os.path.isfile(p(".autorelabel")):
                    check_call(in_chroot([
                        "/usr/sbin/genhomedircon"
                    ]))
                    check_call(in_chroot([
                        "/usr/sbin/restorecon", "-v", "-R", "/",
                        "-e", "/sys", "-e", "/proc", "-e", "/tmp", "-e", "/run"
                    ]))
                    os.unlink(p(".autorelabel"))

                # Snapshot the system as it is, now that it is fully done.
                try:
                    check_call(["zfs", "list", "-t", "snapshot",
                                "-H", "-o", "name", j(poolname, "ROOT", "os@initial")],
                               stdout=file(os.devnull,"w"))
                except subprocess.CalledProcessError, e:
                    check_call(["sync"])
                    check_call(["zfs", "snapshot", j(poolname, "ROOT", "os@initial")])

                # check for stage stop
                if break_before == "prepare_bootloader_install":
                    raise BreakingBefore(break_before)

                # create bootloader installer
                bootloadertext = \
'''#!/bin/bash -xe
error() {
    retval=$?
    echo There was an unrecoverable error finishing setup >&2
    exit $retval
}
trap error ERR
export PATH=/sbin:/usr/sbin:/bin:/usr/bin
mount /boot
mount /boot/efi
mount --bind /dev/stderr /dev/log
mount -t tmpfs tmpfs /tmp
ln -sf /proc/self/mounts /etc/mtab
mount

rm -f /boot/grub2/grubenv /boot/efi/EFI/fedora/grubenv
ln -s /boot/efi/EFI/fedora/grubenv /boot/grub2/grubenv
echo "# GRUB Environment Block" > /boot/efi/EFI/fedora/grubenv
for x in `seq 999`
do
    echo -n "#" >> /boot/efi/EFI/fedora/grubenv
done
grub2-install /dev/sda
grub2-mkconfig -o /boot/grub2/grub.cfg
cat /boot/grub2/grub.cfg > /boot/efi/EFI/fedora/grub.cfg
sed -i 's/linux16 /linuxefi /' /boot/efi/EFI/fedora/grub.cfg
sed -i 's/initrd16 /initrdefi /' /boot/efi/EFI/fedora/grub.cfg

zfs inherit com.sun:auto-snapshot "%s"
test -f %s || {
    dracut -Hf %s %s
    lsinitrd %s
    restorecon -v %s
}
sync
umount /tmp || true
umount /boot/efi || true
umount /boot || true
rm -f /installbootloader
# Very superstitious,
# writing's on the ROM.
sync
# When you believe in things,
# that you don't understand
# then you suffer.
sync
# Superstition ain't the way.
echo 1 > /proc/sys/kernel/sysrq
echo o > /proc/sysrq-trigger
sleep 5
echo b > /proc/sysrq-trigger
sleep 5
echo cannot power off VM.  Please kill qemu.
'''%(poolname, q(hostonly_initrd), q(hostonly_initrd), kver, q(hostonly_initrd), q(hostonly_initrd))
                bootloaderpath = p("installbootloader")
                bootloader = file(bootloaderpath,"w")
                bootloader.write(bootloadertext)
                bootloader.close()
                os.chmod(bootloaderpath, 0755)

                # copy necessary boot files to a temp dir
                try:
                    kerneltempdir = tempfile.mkdtemp(
                        prefix="install-fedora-on-zfs-bootbits-"
                    )
                    to_rmrf.append(kerneltempdir)
                    shutil.copy2(kernel, kerneltempdir)
                    shutil.copy2(initrd, kerneltempdir)
                except (KeyboardInterrupt, Exception):
                    shutil.rmtree(kerneltempdir)
                    raise

        to_rmrf.remove(kerneltempdir)
        cleanup()
        to_rmrf.append(kerneltempdir)

        # All the time
        # booting in and out of time
        # Hear the blowing of the fans on your computer

        biiq = lambda init, bb, whichinitrd: boot_image_in_qemu(
            hostname, init, poolname,
            original_voldev, original_bootdev,
            os.path.join(kerneltempdir, os.path.basename(kernel)),
            os.path.join(kerneltempdir, os.path.basename(whichinitrd)),
            force_kvm, interactive_qemu,
            lukspassword, rootpassword, rootuuid, luksuuid,
            break_before, qemu_timeout, bb
        )

        # Girl, we need some, girl, we need some retries
        # if we're gonna make it like a true bootloader
        # We need some retries
        # If we wanna make a good initrd

        # There's this thing about systemd on F24 randomly segfaulting.
        # We retry in those cases.

        @retrymod.retry(2)
        def biiq_bootloader():
            return biiq("init=/installbootloader", "boot_to_install_bootloader", initrd)

        @retrymod.retry(2)
        def biiq_test():
            biiq("systemd.unit=multi-user.target", "boot_to_test_hostonly", hostonly_initrd)

        # install bootloader and create hostonly initrd using qemu
        biiq_bootloader()

        with blockdev_context(
            voldev, bootdev, undoer, volsize, bootsize, chown, chgrp, create=False
        ) as (
            rootpart, bootpart, efipart
        ):
            with filesystem_context(
                poolname, rootpart, bootpart, efipart, undoer, workdir,
                swapsize, lukspassword, luksoptions, create=False
            ) as (
                _, _, _, _, _, _, _, _
            ):
                shutil.copy2(hostonly_initrd, kerneltempdir)

        to_rmrf.remove(kerneltempdir)
        cleanup()
        to_rmrf.append(kerneltempdir)

        # test hostonly initrd using qemu
        biiq_test()

    # tell the user we broke
    except BreakingBefore, e:
        print >> sys.stderr, "------------------------------------------------"
        print >> sys.stderr, "Breaking before %s" % break_stages[e.args[0]]
        if do_cleanup:
            print >> sys.stderr, "Cleaning up now"
            cleanup()
        raise

    # end operating with the devices
    except BaseException, e:
        logging.exception("Unexpected error")
        if do_cleanup:
            print >> sys.stderr, "Cleaning up now"
            cleanup()
        raise

    # Truly delete all files left behind.
    cleanup()


def test_cmd(cmdname, expected_ret):
    try: subprocess.check_call([cmdname],
                               stdin=file(os.devnull, "r"),
                               stdout=file(os.devnull, "w"),
                               stderr=file(os.devnull, "w"))
    except subprocess.CalledProcessError, e:
        if e.returncode == expected_ret: return True
        return False
    except OSError, e:
        if e.errno == 2: return False
        raise
    return True

def test_mkfs_ext4():
    return test_cmd("mkfs.ext4", 1)

def test_mkfs_vfat():
    return test_cmd("mkfs.vfat", 1)

def test_zfs():
    return test_cmd("zfs", 2) and os.path.exists("/dev/zfs")

def test_rsync():
    return test_cmd("rsync", 1)

def test_gdisk():
    return test_cmd("gdisk", 5)

def test_cryptsetup():
    return test_cmd("cryptsetup", 1)

def test_yum():
    pkgmgrs = {"yum":True, "dnf": True}
    for pkgmgr in pkgmgrs:
        try: subprocess.check_call([pkgmgr], stdout=file(os.devnull, "w"), stderr=file(os.devnull, "w"))
        except subprocess.CalledProcessError, e:
            if e.returncode != 1:
                pkgmgrs[pkgmgr] = False
        except OSError, e:
            if e.errno == 2:
                pkgmgrs[pkgmgr] = False
                continue
            raise
    return any(pkgmgrs.values())

def install_fedora_on_zfs():
    logging.basicConfig(level=logging.DEBUG, format=BASIC_FORMAT)
    args = get_parser().parse_args()
    if not test_rsync():
        print >> sys.stderr, "error: rsync is not available. Please use your package manager to install rsync."
        return 5
    if not test_zfs():
        print >> sys.stderr, "error: ZFS is not installed properly. Please install https://github.com/Rudd-O/zfs and then modprobe zfs.  If installing from source, pay attention to the --with-udevdir= configure parameter and don't forget to run ldconfig after the install."
        return 5
    if not test_mkfs_ext4():
        print >> sys.stderr, "error: mkfs.ext4 is not installed properly. Please install e2fsprogs."
        return 5
    if not test_mkfs_vfat():
        print >> sys.stderr, "error: mkfs.vfat is not installed properly. Please install dosfstools."
        return 5
    if not test_cryptsetup():
        print >> sys.stderr, "error: cryptsetup is not installed properly. Please install cryptsetup."
        return 5
    if not test_gdisk():
        print >> sys.stderr, "error: gdisk is not installed properly. Please install gdisk."
        return 5
    if not test_yum():
        print >> sys.stderr, "error: could not find either yum or DNF. Please use your package manager to install yum or DNF."
        return 5
    if not test_qemu():
        print >> sys.stderr, "error: QEMU is not installed properly. Please use your package manager to install QEMU (in Fedora, qemu-system-x86-core or qemu-kvm)."
        return 5
    try:
        install_fedora(
            args.voldev[0], args.volsize, args.bootdev, args.bootsize,
            args.poolname, args.hostname, args.rootpassword,
            args.swapsize, args.releasever, args.lukspassword,
            not args.nocleanup,
            args.interactive_qemu,
            args.luksoptions,
            args.prebuiltrpms,
            args.yum_cachedir,
            args.force_kvm,
            branch=args.branch,
            chown=args.chown,
            chgrp=args.chgrp,
            break_before=args.break_before,
            workdir=args.workdir,
        )
    except (ImpossiblePassphrase), e:
        print >> sys.stderr, "error:", e
        return os.EX_USAGE
    except (ZFSMalfunction, ZFSBuildFailure), e:
        print >> sys.stderr, "error:", e
        return 9
    except BreakingBefore:
        return 120
    return 0


def deploy_zfs_in_machine(p, in_chroot, pkgmgr, branch,
                          prebuilt_rpms_path, break_before, to_unmount, to_rmdir):
    arch = platform.machine()
    stringtoexclude = "debuginfo"

    # check for stage stop
    if break_before == "install_prebuilt_rpms":
        raise BreakingBefore(break_before)

    if prebuilt_rpms_path:
        target_rpms_path = p(j("tmp","zfs-fedora-installer-prebuilt-rpms"))
        if not os.path.isdir(target_rpms_path):
            os.mkdir(target_rpms_path)
        if ismount(target_rpms_path):
            if os.stat(prebuilt_rpms_path).st_ino != os.stat(target_rpms_path):
                umount(target_rpms_path)
                bindmount(os.path.abspath(prebuilt_rpms_path), target_rpms_path)
        else:
            bindmount(os.path.abspath(prebuilt_rpms_path), target_rpms_path)
        if os.path.isdir(target_rpms_path):
            to_rmdir.append(target_rpms_path)
        if ismount(target_rpms_path):
            to_unmount.append(target_rpms_path)
        prebuilt_rpms_to_install = glob.glob(j(prebuilt_rpms_path,"*%s.rpm"%(arch,))) + glob.glob(j(prebuilt_rpms_path,"*%s.rpm"%("noarch",)))
        prebuilt_rpms_to_install = set([
            os.path.basename(s)
            for s in prebuilt_rpms_to_install
            if stringtoexclude not in os.path.basename(s)
        ])
    else:
        target_rpms_path = None
        prebuilt_rpms_to_install = set()

    if prebuilt_rpms_to_install:
        logging.info(
            "Installing available prebuilt RPMs: %s",
            prebuilt_rpms_to_install
        )
        files_to_install = [
            j(target_rpms_path, s)
            for s in prebuilt_rpms_to_install
        ]
        pkgmgr.install_local_packages(files_to_install)

    if target_rpms_path:
        umount(target_rpms_path)
        to_unmount.remove(target_rpms_path)
        check_call(["rmdir", target_rpms_path])
        to_rmdir.remove(target_rpms_path)

    # check for stage stop
    if break_before == "install_grub_zfs_fixer":
        raise BreakingBefore(break_before)

    for project, patterns in (
        (
            "grub-zfs-fixer",
            (
                "RPMS/%s/*.%s.rpm" % ("noarch", "noarch"),
                "RPMS/*.%s.rpm" % ("noarch",),
            ),
        ),
    ):
        grubzfsfixerpath = j(os.path.dirname(__file__), os.path.pardir, os.path.pardir, "grub-zfs-fixer")
        class FixerNotInstalledYet(Exception): pass
        try:
            logging.info("Checking if %s has the GRUB ZFS fixer installed", project)
            try:
                fixerlines = file(j(grubzfsfixerpath, "grub-zfs-fixer.spec")).readlines()
                fixerversion = [ x.split()[1] for x in fixerlines if x.startswith("Version:") ][0]
                fixerrelease = [ x.split()[1] for x in fixerlines if x.startswith("Release:") ][0]
                check_output(in_chroot([
                    "rpm",
                    "-q",
                    "grub-zfs-fixer-%s-%s" % (fixerversion, fixerrelease)
                ]))
            except subprocess.CalledProcessError:
                raise FixerNotInstalledYet()
        except FixerNotInstalledYet:
            logging.info("%s does not have the GRUB ZFS fixer, building", project)
            project_dir = p(j("usr","src",project))
            def getrpms(pats, directory):
                therpms = [
                    rpmfile
                    for pat in pats
                    for rpmfile in glob.glob(j(directory, pat))
                    if stringtoexclude not in os.path.basename(rpmfile)
                ]
                return therpms
            files_to_install = getrpms(patterns, project_dir)
            if not files_to_install:
                if not os.path.isdir(project_dir):
                    os.mkdir(project_dir)

                pkgmgr.ensure_packages_installed(
                    [
                        "rpm-build", "tar", "gzip",
                    ],
                )
                logging.info("Tarring %s tarball", project)
                check_call(['tar', 'cvzf', j(project_dir, "%s.tar.gz" % project), project],
                            cwd=j(grubzfsfixerpath, os.path.pardir))
                logging.info("Building project: %s", project)
                project_dir_in_chroot = project_dir[len(p(""))-1:]
                check_call(in_chroot(["rpmbuild", "--define", "_topdir %s"%(project_dir_in_chroot,), "-ta", j(project_dir_in_chroot,"%s.tar.gz" % project)]))
                files_to_install = getrpms(patterns, project_dir)

            logging.info("Installing built RPMs: %s", files_to_install)
            pkgmgr.install_local_packages(files_to_install)

    # Check we have a patched grub2-mkconfig.
    mkconfig_file = p(j("usr", "sbin", "grub2-mkconfig"))
    mkconfig_text = file(mkconfig_file).read()
    if "This program was patched by fix-grub-mkconfig" not in mkconfig_text:
        raise ZFSBuildFailure("expected to find patched %s but could not find it.  Perhaps the grub-zfs-fixer RPM was never installed?" % mkconfig_file)

    for project, patterns, keystonepkgs, mindeps in (
        (
            "zfs",
            (
                "zfs-dkms-*.noarch.rpm",
                "libnvpair*.%s.rpm" % arch,
                "libuutil*.%s.rpm" % arch,
                "libzfs?-[0123456789]*.%s.rpm" % arch,
                "libzfs?-devel-[0123456789]*.%s.rpm" % arch,
                "libzpool*.%s.rpm" % arch,
                "zfs-[0123456789]*.%s.rpm" % arch,
                "zfs-dracut-*.%s.rpm" % arch,
            ),
            ('zfs', 'zfs-dkms', 'zfs-dracut'),
            [
                "zlib-devel", "libuuid-devel", "bc", "libblkid-devel",
                "libattr-devel", "lsscsi", "mdadm", "parted",
                "libudev-devel", "libtool", "openssl-devel",
                "make", "automake", "libtirpc-devel",
            ],
        ),
    ):
        # check for stage stop
        if break_before == "deploy_%s" % project:
            raise BreakingBefore(break_before)

        try:
            logging.info("Checking if keystone packages %s are installed", ", ".join(keystonepkgs))
            check_call(in_chroot(["rpm", "-q"] + list(keystonepkgs)),
                                stdout=file(os.devnull,"w"), stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            logging.info("Package %s-dkms is not installed, building", project)
            project_dir = p(j("usr","src",project))
            def getrpms(pats, directory):
                therpms = [
                    rpmfile
                    for pat in pats
                    for rpmfile in glob.glob(j(directory, pat))
                    if stringtoexclude not in os.path.basename(rpmfile)
                ]
                return therpms
            files_to_install = getrpms(patterns, project_dir)
            if not files_to_install:
                if not os.path.isdir(project_dir):
                    repo = "https://github.com/Rudd-O/%s" % project
                    logging.info("Cloning git repository: %s", repo)
                    cmd = ["git", "clone", repo, project_dir]
                    check_call(cmd)
                    cmd = ["git", "checkout", branch]
                    check_call(cmd, cwd= project_dir)
                    cmd = ["git", "--no-pager", "show"]
                    check_call(cmd, cwd= project_dir)

                pkgmgr.ensure_packages_installed(mindeps)

                logging.info("Building project: %s", project)
                cores = multiprocessing.cpu_count()
                cmd = in_chroot(["bash", "-c",
                    (
                        "cd /usr/src/%s && "
                        "./autogen.sh && "
                        "./configure --with-config=user && "
                        "make -j%s rpm-utils && "
                        "make -j%s rpm-dkms" % (project, cores, cores)
                    )
                ])
                check_call(cmd)
                files_to_install = getrpms(patterns, project_dir)

            logging.info("Installing built RPMs: %s", files_to_install)
            pkgmgr.install_local_packages(files_to_install)

    # Check we have a ZFS.ko for at least one kernel.
    modules_dir = p(j("usr", "lib", "modules", "*", "*", "zfs.ko*"))
    modules_files = glob.glob(modules_dir)
    if not modules_files:
        raise ZFSBuildFailure("expected to find but could not find module zfs.ko in %s.  Perhaps the ZFS source you used is too old to work with the kernel this program installed?" % modules_dir)


def deploy_zfs():
    args = get_deploy_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG, format=BASIC_FORMAT)
    if not test_yum():
        print >> sys.stderr, "error: could not find either yum or DNF. Please use your package manager to install yum or DNF."
        return 5
    p = lambda withinchroot: j("/", withinchroot.lstrip(os.path.sep))
    in_chroot = lambda x: x

    pkgmgr = SystemPackageManager()

    to_rmdir = []
    to_unmount = []

    def cleanup():
        for fs in reversed(to_unmount):
            umount(fs)
        for filename in to_rmdir:
            os.rmdir(filename)

    try:
        deploy_zfs_in_machine(p=p,
                              in_chroot=in_chroot,
                              pkgmgr=pkgmgr,
                              prebuilt_rpms_path=args.prebuiltrpms,
                              branch=args.branch,
                              break_before=None,
                              to_rmdir=to_rmdir,
                              to_unmount=to_unmount,)
    except BaseException:
        logging.exception("Unexpected error")
        if not args.nocleanup:
            logging.info("Cleaning up now")
            cleanup()
        raise

    cleanup()
