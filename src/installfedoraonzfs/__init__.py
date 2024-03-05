#!/usr/bin/env python

import argparse
import contextlib
import glob
import logging
import multiprocessing
import os
from os.path import join as j
from pathlib import Path
import platform
import shlex
import shutil
import signal  # noqa: F401
import subprocess
import tempfile
import time
from typing import Any, Callable, Generator, Sequence

from installfedoraonzfs import pm
from installfedoraonzfs.breakingbefore import BreakingBefore, break_stages, shell_stages
from installfedoraonzfs.cmd import (
    Popen,
    bindmount,
    check_call,
    check_call_silent,
    check_call_silent_stdout,
    check_output,
    create_file,
    delete_contents,
    filetype,
    get_associated_lodev,
    get_output_exitcode,
    ismount,
    losetup,
    mount,
    readlines,
    readtext,
    umount,
    writetext,
)
from installfedoraonzfs.git import Gitter, gitter_factory
from installfedoraonzfs.log import log_config
import installfedoraonzfs.retry as retrymod
from installfedoraonzfs.vm import BootDriver, boot_image_in_qemu, test_qemu

_LOGGER = logging.getLogger()


qemu_timeout = 360


def add_volume_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments common to volume mounting."""
    parser.add_argument(
        "voldev",
        metavar="VOLDEV",
        type=str,
        help="path to volume (device to use or regular file to create)",
    )
    parser.add_argument(
        "--separate-boot",
        dest="bootdev",
        metavar="BOOTDEV",
        type=str,
        action="store",
        default=None,
        help="place /boot in a separate volume",
    )
    parser.add_argument(
        "--pool-name",
        dest="poolname",
        metavar="POOLNAME",
        type=str,
        action="store",
        default="tank",
        help="pool name (default tank)",
    )


class UsesRepo(argparse.ArgumentParser):
    """Validates prebuilt RPMs."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore
        """Initialize the parser."""
        argparse.ArgumentParser.__init__(self, *args, **kwargs)
        add_common_arguments(self)
        self.add_repo_arguments()

    def parse_args(self, args=None, namespace=None) -> Any:  # type:ignore
        """Parse arguments."""
        args = argparse.ArgumentParser.parse_args(self)
        if args.prebuiltrpms and not os.path.isdir(args.prebuiltrpms):
            _LOGGER.error(
                "error: --prebuilt-rpms-path %r does not exist",
                args.prebuiltrpms,
            )
        return args

    def add_repo_arguments(self) -> None:
        """Add arguments pertaining to source and binary ZFS repo selection."""
        self.add_argument(
            "--use-prebuilt-rpms",
            dest="prebuiltrpms",
            metavar="DIR",
            type=str,
            action="store",
            default=None,
            help="also install pre-built ZFS, GRUB and other RPMs in this directory,"
            " except for debuginfo packages within the directory (default: build ZFS and"
            " GRUB RPMs, within the system)",
        )
        self.add_argument(
            "--zfs-repo",
            dest="zfs_repo",
            action="store",
            default="https://github.com/Rudd-O/zfs",
            help="when building ZFS from source, use this repository instead of master",
        )
        self.add_argument(
            "--use-branch",
            dest="branch",
            action="store",
            default="master",
            help="when building ZFS from source, check out this commit, tag or branch from"
            " the repository instead of master",
        )


def add_pm_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments for package manager."""
    parser.add_argument(
        "--yum-cachedir",
        dest="yum_cachedir",
        action="store",
        type=Path,
        default=None,
        help="directory to use for a yum cache that persists across executions",
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments common to all commands."""
    parser.add_argument(
        "--trace-file",
        dest="trace_file",
        action="store",
        default=None,
        help="file name for a detailed trace file of program activity (default no"
        " trace file)",
    )


def add_env_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments common to commands that use the pool."""
    parser.add_argument(
        "--workdir",
        dest="workdir",
        action="store",
        default=f"/run/user/{os.getuid()}/zfs-fedora-installer",
        help="use this directory as a working (scratch) space for the mount points of"
        " the created pool",
    )


def add_distro_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments pertaining to target distro selection."""
    parser.add_argument(
        "--releasever",
        dest="releasever",
        metavar="VER",
        type=str,
        action="store",
        default=None,
        help="Release version to install (default the same as the computer"
        " you are installing on)",
    )


def get_install_fedora_on_zfs_parser() -> UsesRepo:
    """Get a parser to configure install-fedora-on-zfs."""
    parser = UsesRepo(
        description="Install a minimal Fedora system inside a ZFS pool within"
        " a disk image or device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_volume_arguments(parser)
    parser.add_argument(
        "--vol-size",
        dest="volsize",
        metavar="VOLSIZE",
        type=str,
        action="store",
        default="11000",
        help="volume size in MiB (default 11000), or bytes if postfixed with a B",
    )
    parser.add_argument(
        "--boot-size",
        dest="bootsize",
        metavar="BOOTSIZE",
        type=int,
        action="store",
        default=1024,
        help="boot partition size in MiB, or boot volume size in MiB, when"
        " --separate-boot is specified (default 1024)",
    )
    parser.add_argument(
        "--host-name",
        dest="hostname",
        metavar="HOSTNAME",
        type=str,
        action="store",
        default="localhost.localdomain",
        help="host name (default localhost.localdomain)",
    )
    parser.add_argument(
        "--root-password",
        dest="rootpassword",
        metavar="ROOTPASSWORD",
        type=str,
        action="store",
        default="password",
        help="root password (default password)",
    )
    parser.add_argument(
        "--swap-size",
        dest="swapsize",
        metavar="SWAPSIZE",
        type=int,
        action="store",
        default=1024,
        help="swap volume size in MiB (default 1024)",
    )
    add_distro_arguments(parser)
    parser.add_argument(
        "--luks-password",
        dest="lukspassword",
        metavar="LUKSPASSWORD",
        type=str,
        action="store",
        default=None,
        help="LUKS password to encrypt the ZFS volume with (default no encryption);"
        " unprintable glyphs whose ASCII value lies below 32 (the space character)"
        " will be rejected",
    )
    parser.add_argument(
        "--luks-options",
        dest="luksoptions",
        metavar="LUKSOPTIONS",
        type=str,
        action="store",
        default=None,
        help="space-separated list of options to pass to cryptsetup luksFormat (default"
        " no options)",
    )
    parser.add_argument(
        "--interactive-qemu",
        dest="interactive_qemu",
        action="store_true",
        default=False,
        help="QEMU will run interactively, with the console of your Linux system"
        " connected to your terminal; the normal timeout of %s seconds will not"
        " apply, and Ctrl+C will interrupt the emulation; this is useful to"
        " manually debug problems installing the bootloader; in this mode you are"
        " responsible for typing the password to any LUKS devices you have requested"
        " to be created" % qemu_timeout,
    )
    add_pm_arguments(parser)
    parser.add_argument(
        "--force-kvm",
        dest="force_kvm",
        action="store_true",
        default=None,
        help="force KVM use for the boot sector installation (default autodetect)",
    )
    parser.add_argument(
        "--chown",
        dest="chown",
        action="store",
        default=None,
        help="change the owner of the image files upon creation to this user",
    )
    parser.add_argument(
        "--chgrp",
        dest="chgrp",
        action="store",
        default=None,
        help="change the group of the image files upon creation to this group",
    )
    parser.add_argument(
        "--break-before",
        dest="break_before",
        choices=break_stages,
        action="store",
        default=None,
        help="break before the specified stage (see below); useful to stop"
        " the process at a particular stage for debugging",
    )
    parser.add_argument(
        "--shell-before",
        dest="shell_before",
        choices=shell_stages,
        action="store",
        default=None,
        help="open a shell inside the chroot before running a specific stage"
        "; useful to debug issues within the chroot; process continues after"
        " exiting the shell",
    )
    parser.add_argument(
        "--short-circuit",
        dest="short_circuit",
        choices=break_stages,
        action="store",
        default=None,
        help="short-circuit to the specified stage (see below); useful to jump "
        "ahead and execute from a particular stage thereon; it can be "
        "combined with --break-before to stop at a later stage",
    )
    add_env_arguments(parser)
    parser.epilog = (
        "Stages for the --break-before and --short-circuit arguments:\n%s"
        % (
            "".join(
                "\n* %s:%s%s"
                % (k, " " * (max(len(x) for x in break_stages) - len(k) + 1), v)
                for k, v in list(break_stages.items())
            ),
        )
        + "\n\n"
        + "Stages for the --shell-before argument:\n%s"
        % (
            "".join(
                "\n* %s:%s%s"
                % (k, " " * (max(len(x) for x in shell_stages) - len(k) + 1), v)
                for k, v in list(shell_stages.items())
            ),
        )
    )
    return parser


def get_deploy_parser() -> UsesRepo:
    """Add arguments for deploy."""
    parser = UsesRepo(description="Install ZFS on a running system")
    parser.add_argument(
        "--no-update-sources",
        dest="update_sources",
        action="store_false",
        help="Update Git repositories if building from sources",
    )
    parser.add_argument(
        "--dispvm-template",
        dest="dispvm_template",
        default=None,
        help="Disposable qube template for checking out code (e.g. fedora-39-dvm)"
        " (only applicable to deployment of ZFS in a Qubes OS environment, defaults"
        " to whatever your system's default disposable qube template is, and must"
        " currently be a Fedora-based or dnf-managed disposable template); see"
        " https://www.qubes-os.org/doc/how-to-use-disposables/ for more information",
    )
    return parser


def get_bootstrap_chroot_parser() -> argparse.ArgumentParser:
    """Add arguments for chroot bootstrap."""
    parser = argparse.ArgumentParser(description="Bootstrap a chroot on a directory")
    parser.add_argument("chroot", type=Path, help="chroot directory to operate on")
    add_distro_arguments(parser)
    add_pm_arguments(parser)
    add_common_arguments(parser)
    return parser


def get_run_command_parser() -> argparse.ArgumentParser:
    """Add arguments for run command in chroot."""
    parser = argparse.ArgumentParser(
        description="Run a command in a Fedora system inside a ZFS pool within a disk"
        " image or device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_volume_arguments(parser)
    add_env_arguments(parser)
    add_common_arguments(parser)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def import_pool(poolname: str, rootmountpoint: Path) -> None:
    """Import a pool by name, and mount it to an alt mount point."""
    check_call(["zpool", "import"])
    check_call(["zpool", "import", "-f", "-R", str(rootmountpoint), poolname])


def list_pools() -> list[str]:
    """List pools active in the system."""
    d = check_output(["zpool", "list", "-H", "-o", "name"], logall=True)
    return [x for x in d.splitlines() if x]


# We try the import of the pool 3 times, with a 5-second timeout in between tries.
import_pool_retryable = retrymod.retry(
    2, timeout=5, retryable_exception=subprocess.CalledProcessError
)(import_pool)  # type: ignore


def partition_boot(bootdev: Path, bootsize: int, rootvol: bool) -> None:
    """Partitions device into four partitions.

    1. a 2MB biosboot partition
    2. an EFI partition, sized bootsize/2
    3. a /boot partition, sized bootsize/2
    4. if rootvol evals to True: a root volume partition

    Caller is responsible for waiting until the devices appear.
    """
    cmd = ["gdisk", str(bootdev)]
    pr = Popen(cmd, stdin=subprocess.PIPE)
    if rootvol:
        _LOGGER.info(
            "Creating 2M BIOS boot, %sM EFI system, %sM boot, rest root partition",
            int(bootsize / 2),
            int(bootsize / 2),
        )
        pr.communicate(
            f"""o
y

n
1

+2M
21686148-6449-6E6F-744E-656564454649


n
2

+{int(bootsize / 2)}M
C12A7328-F81F-11D2-BA4B-00A0C93EC93B


n
3

+{int(bootsize / 2)}M



n
4





p
w
y
"""
        )
    else:
        _LOGGER.info(
            "Creating 2M BIOS boot, %sM EFI system, the rest boot partition",
            int(bootsize / 2),
        )
        pr.communicate(
            f"""o
y

n
1

+2M
21686148-6449-6E6F-744E-656564454649


n
2

+{int(bootsize / 2)}M
C12A7328-F81F-11D2-BA4B-00A0C93EC93B


n
3



p
w
y
"""
        )
    retcode = pr.wait()
    if retcode != 0:
        raise subprocess.CalledProcessError(retcode, cmd)


@contextlib.contextmanager
def blockdev_context(
    voldev: Path,
    bootdev: None | Path,
    volsize: int,
    bootsize: int,
    chown: str | int | None,
    chgrp: str | int | None,
    create: bool,
) -> Generator[tuple[Path, Path, Path], None, None]:
    """Create a block device context.

    Takes a volume device path, and possible a boot device path,
    and yields a properly partitioned set of volumes which can
    then be used to format and create pools on.

    volsize is in bytes.  bootsize is in mebibytes.
    """
    undoer = Undoer()

    with undoer:
        _LOGGER.info("Entering blockdev context.  Create=%s.", create)

        def get_rootpart(rdev: Path) -> Path | None:
            parts = (
                [str(rdev) + "p4"]
                if str(rdev).startswith("/dev/loop")
                else [
                    str(rdev) + "-part4",
                    str(rdev) + "4",
                ]
            )
            for rootpart in parts:
                if os.path.exists(rootpart):
                    return Path(rootpart)
            return None

        def get_efipart_bootpart(bdev: Path) -> tuple[Path, Path] | tuple[None, None]:
            parts = (
                [
                    (str(bdev) + "p2", str(bdev) + "p3"),
                ]
                if str(bdev).startswith("/dev/loop")
                else [
                    (str(bdev) + "-part2", str(bdev) + "-part3"),
                    (str(bdev) + "2", str(bdev) + "3"),
                ]
            )
            for efipart, bootpart in parts:
                _LOGGER.info(
                    "About to check for the existence of %s and %s.", efipart, bootpart
                )
                if os.path.exists(efipart) and os.path.exists(bootpart):
                    _LOGGER.info("Both %s and %s exist.", efipart, bootpart)
                    return Path(efipart), Path(bootpart)
            return None, None

        voltype = filetype(voldev)

        if (
            voltype == "doesntexist"
        ):  # FIXME use truncate directly with python.  no need to dick around.
            if not create:
                raise Exception(
                    f"Wanted to create boot device {voldev} but create=False"
                )
            create_file(voldev, volsize, owner=chown, group=chgrp)
            voltype = "file"

        if voltype == "file":
            new_voldev = get_associated_lodev(voldev)
            if not new_voldev:
                new_voldev = losetup(voldev)

            assert new_voldev is not None, (new_voldev, voldev)
            undoer.to_un_losetup.append(new_voldev)
            voldev = new_voldev
            voltype = "blockdev"

        if bootdev:
            boottype = filetype(bootdev)

            if boottype == "doesntexist":
                if not create:
                    raise Exception(
                        f"Wanted to create boot device {bootdev} but create=False"
                    )
                create_file(bootdev, bootsize * 1024 * 1024, owner=chown, group=chgrp)
                boottype = "file"

            if boottype == "file":
                new_bootdev = get_associated_lodev(bootdev)
                if not new_bootdev:
                    new_bootdev = losetup(bootdev)

                assert new_bootdev is not None, (new_bootdev, bootdev)
                undoer.to_un_losetup.append(new_bootdev)
                bootdev = new_bootdev
                boottype = "blockdev"

        for i in reversed(range(2)):
            efipart, bootpart = get_efipart_bootpart(bootdev if bootdev else voldev)
            if None in (bootpart, efipart):
                if i > 0:
                    time.sleep(2)
                    continue
                if create:
                    partition_boot(bootdev or voldev, bootsize, not bootdev)
                else:
                    raise Exception(
                        f"Wanted to partition boot device {bootdev or voldev} but"
                        f" create=False ({(efipart, bootpart)})"
                    )
            break

        for i in reversed(range(2)):
            efipart, bootpart = get_efipart_bootpart(bootdev if bootdev else voldev)
            if None in (efipart, bootpart):
                if i > 0:
                    time.sleep(2)
                    continue
                raise Exception(
                    f"partitions 2 or 3 in device"
                    f" {bootdev if bootdev else voldev} failed to be created"
                )
            break

        rootpart = voldev if bootdev else get_rootpart(voldev)

        assert rootpart, "root partition in device %r failed to be created" % voldev
        assert bootpart, "boot partition in device %r failed to be created" % (
            bootdev or voldev
        )
        assert efipart, "EFI partition in device %r failed to be created" % (
            bootdev or voldev
        )

        _LOGGER.info("Blockdev context complete.")

        yield rootpart, bootpart, efipart


def setup_boot_filesystems(
    bootpart: Path,
    efipart: Path,
    label_postfix: str,
    create: bool,
) -> tuple[str, str]:
    """Set up boot and EFI file systems.

    This function is a noop if file systems already exist.
    """
    try:
        output = check_output(["blkid", "-c", "/dev/null", str(bootpart)])
    except subprocess.CalledProcessError:
        output = ""

    if 'TYPE="ext4"' not in output:
        if not create:
            raise Exception(
                f"Wanted to create boot file system on {bootpart}"
                f" but create=False (output: {output})"
            )
        # Conservative set of features so older distributions can be
        # tested when built in in newer distributions.
        ext4_opts = (
            "none,has_journal,ext_attr,resize_inode,dir_index,filetype"
            ",extent,64bit,flex_bg,sparse_super,large_file,huge_file,dir_nlink"
        )
        check_call(
            [
                "mkfs.ext4",
            ]
            + ["-O", ext4_opts]
            + ["-L", "boot_" + label_postfix, str(bootpart)]
        )
    bootpartuuid = check_output(
        ["blkid", "-c", "/dev/null", str(bootpart), "-o", "value", "-s", "UUID"]
    ).strip()

    try:
        output = check_output(["blkid", "-c", "/dev/null", str(efipart)])
    except subprocess.CalledProcessError:
        output = ""
    if 'TYPE="vfat"' not in output:
        if not create:
            raise Exception(
                f"Wanted to create EFI file system on {efipart} but create=False"
            )
        check_call(
            ["mkfs.vfat", "-F", "32", "-n", "efi_" + label_postfix[:7], str(efipart)]
        )
    efipartuuid = check_output(
        ["blkid", "-c", "/dev/null", str(efipart), "-o", "value", "-s", "UUID"]
    ).strip()

    return bootpartuuid, efipartuuid


@contextlib.contextmanager
def filesystem_context(
    poolname: str,
    rootpart: Path,
    bootpart: Path,
    efipart: Path,
    workdir: Path,
    swapsize: int,
    lukspassword: str | None,
    luksoptions: str | None,
    create: bool,
) -> Generator[
    tuple[
        Path,
        Callable[[str], str],
        Callable[[str], str],
        Callable[[list[str]], list[str]],
        str | None,
        str | None,
        str,
        str,
    ],
    None,
    None,
]:
    """Provide a filesystem context to the installer."""
    undoer = Undoer()
    _LOGGER.info("Entering filesystem context.  Create=%s.", create)
    bootpartuuid, efipartuuid = setup_boot_filesystems(
        bootpart, efipart, poolname, create
    )

    if lukspassword:
        _LOGGER.info("Setting up LUKS.")
        needsdoing = False
        try:
            rootuuid = check_output(
                ["blkid", "-c", "/dev/null", str(rootpart), "-o", "value", "-s", "UUID"]
            ).strip()
            if not rootuuid:
                raise IndexError("no UUID for %s" % rootpart)
            luksuuid = "luks-" + rootuuid
        except IndexError:
            needsdoing = True
        except subprocess.CalledProcessError as e:
            if e.returncode != 2:
                raise
            needsdoing = True
        if needsdoing:
            if not create:
                raise Exception(
                    f"Wanted to create LUKS volume on {rootpart} but create=False"
                )
            luksopts = shlex.split(luksoptions) if luksoptions else []
            cmd = (
                ["cryptsetup", "-y", "-v", "luksFormat"]
                + luksopts
                + [str(rootpart), "-"]
            )
            proc = Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(lukspassword)
            retcode = proc.wait()
            if retcode != 0:
                raise subprocess.CalledProcessError(retcode, cmd)
            rootuuid = check_output(
                ["blkid", "-c", "/dev/null", str(rootpart), "-o", "value", "-s", "UUID"]
            ).strip()
            if not rootuuid:
                raise IndexError("still no UUID for %s" % rootpart)
            luksuuid = "luks-" + rootuuid
        if not os.path.exists(j("/dev", "mapper", luksuuid)):
            cmd = ["cryptsetup", "-y", "-v", "luksOpen", str(rootpart), luksuuid]
            proc = Popen(cmd, stdin=subprocess.PIPE)
            proc.communicate(lukspassword)
            retcode = proc.wait()
            if retcode != 0:
                raise subprocess.CalledProcessError(retcode, cmd)
        undoer.to_luks_close.append(luksuuid)
        rootpart = Path(j("/dev", "mapper", luksuuid))
    else:
        rootuuid = None
        luksuuid = None

    rootmountpoint = Path(j(workdir, poolname))
    if poolname not in list_pools():
        func = import_pool if create else import_pool_retryable
        try:
            _LOGGER.info("Trying to import pool %s.", poolname)
            func(poolname, rootmountpoint)
        except subprocess.CalledProcessError as exc:
            if not create:
                raise Exception(
                    f"Wanted to create ZFS pool {poolname} on {rootpart}"
                    " but create=False"
                ) from exc
            _LOGGER.info("Creating pool %s.", poolname)
            check_call(
                [
                    "zpool",
                    "create",
                    "-m",
                    "none",
                    "-o",
                    "ashift=12",
                    "-O",
                    "compression=on",
                    "-O",
                    "atime=off",
                    "-O",
                    "com.sun:auto-snapshot=false",
                    "-R",
                    str(rootmountpoint),
                    poolname,
                    str(rootpart),
                ]
            )
            check_call(["zfs", "set", "xattr=sa", poolname])
    undoer.to_export.append(poolname)

    _LOGGER.info("Checking / creating datasets." if create else "Checking datasets")
    try:
        check_call_silent_stdout(
            ["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT")],
        )
    except subprocess.CalledProcessError as exc:
        if not create:
            raise Exception(
                f"Wanted to create ZFS file system ROOT on {poolname} but create=False"
            ) from exc
        check_call(["zfs", "create", j(poolname, "ROOT")])

    try:
        check_call_silent_stdout(
            ["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT", "os")],
        )
        if not os.path.ismount(rootmountpoint):
            check_call(["zfs", "mount", j(poolname, "ROOT", "os")])
    except subprocess.CalledProcessError as exc:
        if not create:
            raise Exception(
                f"Wanted to create ZFS file system ROOT/os on {poolname}"
                " but create=False"
            ) from exc
        check_call(["zfs", "create", "-o", "mountpoint=/", j(poolname, "ROOT", "os")])
    undoer.to_unmount.append(rootmountpoint)

    _LOGGER.info("Checking / creating swap zvol." if create else "Checking swap zvol.")
    try:
        check_call_silent_stdout(
            ["zfs", "list", "-H", "-o", "name", j(poolname, "swap")],
        )
    except subprocess.CalledProcessError as exc:
        if not create:
            raise Exception(
                f"Wanted to create ZFS file system swap on {poolname} but create=False"
            ) from exc
        check_call(
            ["zfs", "create", "-V", "%dM" % swapsize, "-b", "4K", j(poolname, "swap")]
        )
        check_call(["zfs", "set", "compression=gzip-9", j(poolname, "swap")])
        check_call(["zfs", "set", "com.sun:auto-snapshot=false", j(poolname, "swap")])
    swappart = os.path.join("/dev/zvol", poolname, "swap")

    for _ in range(5):
        if not os.path.exists(swappart):
            time.sleep(5)
    if not os.path.exists(swappart):
        raise ZFSMalfunction(
            "ZFS does not appear to create the device nodes for zvols.  If you"
            " installed ZFS from source recently, pay attention that the --with-udevdir="
            " configure parameter is correct, and ensure udev has reloaded."
        )

    _LOGGER.info("Checking / formatting swap." if create else "Checking swap.")
    try:
        output = check_output(["blkid", "-c", "/dev/null", swappart])
    except subprocess.CalledProcessError:
        output = ""
    if 'TYPE="swap"' not in output:
        if not create:
            raise Exception(
                f"Wanted to create swap volume on {poolname}/swap but create=False"
            )
        check_call(["mkswap", "-f", swappart])

    def p(withinchroot: str) -> str:
        return str(j(rootmountpoint, withinchroot.lstrip(os.path.sep)))

    def q(outsidechroot: str) -> str:
        return outsidechroot[len(str(rootmountpoint)) :]

    _LOGGER.info("Mounting virtual and physical file systems.")
    # mount virtual file systems, creating their mount points as necessary
    for m in "boot sys proc".split():
        if not os.path.isdir(p(m)):
            os.mkdir(p(m))

    if not os.path.ismount(p("boot")):
        mount(bootpart, Path(p("boot")))
    undoer.to_unmount.append(p("boot"))

    for m in "boot/efi".split():
        if not os.path.isdir(p(m)):
            os.mkdir(p(m))

    if not os.path.ismount(p("boot/efi")):
        mount(efipart, Path(p("boot/efi")))
    undoer.to_unmount.append(p("boot/efi"))

    for srcmount, bindmounted in [
        ("/proc", p("proc")),
        ("/sys", p("sys")),
        # ("/sys/fs/selinux", p("sys/fs/selinux")),
    ]:
        if not os.path.ismount(bindmounted) and os.path.ismount(srcmount):
            bindmount(Path(srcmount), Path(bindmounted))
        undoer.to_unmount.append(bindmounted)

    # create needed directories to succeed in chrooting as per #22
    for m in "etc var var/lib var/lib/dbus var/log var/log/audit".split():
        if not os.path.isdir(p(m)):
            os.mkdir(p(m))
            if m == "var/log/audit":
                os.chmod(p(m), 0o700)

    def in_chroot(lst: list[str]) -> list[str]:
        return ["chroot", str(rootmountpoint)] + lst

    _LOGGER.info("Filesystem context complete.")
    try:
        yield (
            rootmountpoint,
            p,
            q,
            in_chroot,
            rootuuid,
            luksuuid,
            bootpartuuid,
            efipartuuid,
        )
    finally:
        undoer.undo()


class ZFSMalfunction(Exception):
    """ZFS has malfunctioned."""


class ZFSBuildFailure(Exception):
    """ZFS build failure."""


class ImpossiblePassphrase(Exception):
    """Bad passphrase to use."""


class Undoer:
    """Helps stack undo actions in a LIFO manner."""

    def __init__(self) -> None:
        """Initialize an empty undoer."""
        self.actions: list[Any] = []

        class Tracker:
            def __init__(self, typ: str) -> None:
                self.typ = typ

            def append(me: "Tracker", o: Any) -> None:  # noqa:N805
                assert o is not None
                self.actions.append([me.typ, o])

            def remove(me: "Tracker", o: Any) -> None:  # noqa:N805
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

    def __enter__(self) -> None:
        """Enter the undoer context."""
        pass

    def __exit__(self, *unused_args: Any) -> None:
        """Exit the undoer context."""
        self.undo()

    def undo(self) -> None:
        """Execute the undo action list LIFO style."""
        logger = logging.getLogger("Undoer")
        logger.info("Rewinding stack of actions.")
        for n, (typ, o) in reversed(list(enumerate(self.actions[:]))):
            if typ == "unmount":
                umount(o)
            if typ == "rmrf":
                shutil.rmtree(o)
            if typ == "rmdir":
                os.rmdir(o)
            if typ == "export":
                # check_call(["sync"])
                check_call(["zpool", "export", o])
            if typ == "luks_close":
                # check_call(["sync"])
                check_call(["cryptsetup", "luksClose", str(o)])
            if typ == "un_losetup":
                # check_call(["sync"])
                cmd = ["losetup", "-d", str(o)]
                env = dict(os.environ)
                env["LANG"] = "C.UTF-8"
                env["LC_ALL"] = "C.UTF-8"
                output, exitcode = get_output_exitcode(cmd, env=env)
                if exitcode != 0:
                    if "No such device or address" in output:
                        logger.warning("Ignorable failure while detaching %s", o)
                    else:
                        raise subprocess.CalledProcessError(
                            exitcode, ["losetup", "-d", str(o)]
                        )
                time.sleep(1)
            self.actions.pop(n)
        logger.info("Rewind complete.")


def chroot_shell(
    in_chroot: Callable[[list[str]], list[str]],
    phase_to_stop_at: str | None,
    current_phase: str,
) -> None:
    """Drop user into a shell."""
    if phase_to_stop_at == current_phase:
        _LOGGER.info(
            "=== Dropping you into a shell before phase {current_phase}. ===",
        )
        _LOGGER.info(
            "=== Exit the shell to continue, or exit 1 to abort. ===",
        )
        subprocess.check_call(in_chroot(["/bin/bash"]))


def install_fedora(
    workdir: Path,
    voldev: Path,
    volsize: int,
    zfs_repo: str,
    bootdev: Path | None = None,
    bootsize: int = 256,
    poolname: str = "tank",
    hostname: str = "localhost.localdomain",
    rootpassword: str = "password",
    swapsize: int = 1024,
    releasever: str | None = None,
    lukspassword: str | None = None,
    interactive_qemu: bool = False,
    luksoptions: Any = None,  # FIXME
    prebuilt_rpms_path: Path | None = None,
    yum_cachedir: Path | None = None,
    force_kvm: bool | None = None,
    chown: str | int | None = None,
    chgrp: str | int | None = None,
    break_before: str | None = None,
    shell_before: str | None = None,
    short_circuit: str | None = None,
    branch: str = "master",
) -> None:
    """Install a bootable Fedora in an image or disk backed by ZFS."""
    if lukspassword and not BootDriver.is_typeable(lukspassword):
        raise ImpossiblePassphrase(
            "LUKS passphrase %r cannot be typed during boot" % lukspassword
        )

    if rootpassword and not BootDriver.is_typeable(rootpassword):
        raise ImpossiblePassphrase(
            "root password %r cannot be typed during boot" % rootpassword
        )

    original_voldev = voldev
    original_bootdev = bootdev

    def beginning() -> None:
        _LOGGER.info("Program has begun.")

        with blockdev_context(
            voldev, bootdev, volsize, bootsize, chown, chgrp, create=True
        ) as (rootpart, bootpart, efipart), filesystem_context(
            poolname,
            rootpart,
            bootpart,
            efipart,
            workdir,
            swapsize,
            lukspassword,
            luksoptions,
            create=True,
        ) as (
            rootmountpoint,
            p,
            _,
            in_chroot,
            rootuuid,
            luksuuid,
            bootpartuuid,
            efipartuuid,
        ):
            _LOGGER.info("Adding basic files.")
            # sync device files
            # FIXME: this could be racy when building multiple images
            # in parallel.
            # We really should only synchronize the absolute minimum of
            # device files (in/out/err/null, and the disks that belong
            # to this build MAYBE.)
            check_call(
                [
                    "rsync",
                    "-ax",
                    "--numeric-ids",
                    "--exclude=zvol",
                    "--exclude=sd*",
                    "--delete",
                    "--delete-excluded",
                    "/dev/",
                    p("dev/"),
                ]
            )

            # make up a nice locale.conf file. neutral. international
            localeconf = """LANG="en_US.UTF-8"
"""
            writetext(Path(p(j("etc", "locale.conf"))), localeconf)

            # make up a nice vconsole.conf file. neutral. international
            vconsoleconf = """KEYMAP="us"
"""
            writetext(Path(p(j("etc", "vconsole.conf"))), vconsoleconf)

            # make up a nice fstab file
            fstab = f"""{poolname}/ROOT/os / zfs defaults,x-systemd-device-timeout=0 0 0
UUID={bootpartuuid} /boot ext4 noatime 0 1
UUID={efipartuuid} /boot/efi vfat noatime 0 1
/dev/zvol/{poolname}/swap swap swap discard 0 0
"""
            writetext(Path(p(j("etc", "fstab"))), fstab)

            orig_resolv = p(j("etc", "resolv.conf.orig"))
            final_resolv = p(j("etc", "resolv.conf"))

            if os.path.lexists(final_resolv):
                if not os.path.lexists(orig_resolv):
                    _LOGGER.info("Backing up original resolv.conf")
                    os.rename(final_resolv, orig_resolv)

            if os.path.islink(final_resolv):
                os.unlink(final_resolv)
            _LOGGER.info("Writing temporary resolv.conf")
            writetext(Path(final_resolv), readtext(Path(j("/etc", "resolv.conf"))))

            if not os.path.exists(p(j("etc", "hostname"))):
                writetext(Path(p(j("etc", "hostname"))), hostname)
            if not os.path.exists(p(j("etc", "hostid"))):
                with open("/dev/urandom", "rb") as rnd:
                    randomness = rnd.read(4)
                    with open(p(j("etc", "hostid")), "wb") as hostidf:
                        hostidf.write(randomness)
            with open(p(j("etc", "hostid")), "rb") as hostidfile:
                hostid = hostidfile.read().hex()
            hostid = f"{hostid[6:8]}{hostid[4:6]}{hostid[2:4]}{hostid[0:2]}"
            _LOGGER.info("Host ID is %s", hostid)

            if luksuuid:
                crypttab = f"""{luksuuid} UUID={rootuuid} none discard
"""
                writetext(Path(p(j("etc", "crypttab"))), crypttab)
                os.chmod(p(j("etc", "crypttab")), 0o600)

            # install base packages
            pkgmgr = pm.chroot_bootstrapper_factory(
                rootmountpoint, yum_cachedir, releasever, None
            )
            pkgmgr.bootstrap_packages()

            # omit zfs modules when dracutting
            if not os.path.exists(p("usr/bin/dracut.real")):
                check_call(in_chroot(["mv", "/usr/bin/dracut", "/usr/bin/dracut.real"]))
                writetext(
                    p("usr/bin/dracut"),
                    """#!/bin/bash

echo This is a fake dracut.
""",
                )
                os.chmod(p("usr/bin/dracut"), 0o755)

            if luksuuid:
                luksstuff = f" rd.luks.uuid={rootuuid} rd.luks.allow-discards"
            else:
                luksstuff = ""

            # write grub config
            grubconfig = f"""GRUB_TIMEOUT=0
GRUB_HIDDEN_TIMEOUT=3
GRUB_HIDDEN_TIMEOUT_QUIET=true
GRUB_DISTRIBUTOR="$(sed 's, release .*$,,g' /etc/system-release)"
GRUB_DEFAULT=saved
GRUB_CMDLINE_LINUX="rd.md=0 rd.lvm=0 rd.dm=0 $([ -x /usr/sbin/rhcrashkernel-param ] && /usr/sbin/rhcrashkernel-param || :) quiet systemd.show_status=true{luksstuff}"
GRUB_DISABLE_RECOVERY="true"
GRUB_GFXPAYLOAD_LINUX="keep"
GRUB_TERMINAL_OUTPUT="vga_text"
GRUB_DISABLE_LINUX_UUID=true
GRUB_PRELOAD_MODULES='part_msdos ext2'
"""
            writetext(Path(p(j("etc", "default", "grub"))), grubconfig)

            # write kernel command line
            if not os.path.isdir(p(j("etc", "kernel"))):
                os.mkdir(p(j("etc", "kernel")))
            kernelcmd = (
                f"root=ZFS={poolname}/ROOT/os rd.md=0 rd.lvm=0 rd.dm=0 quiet"
                + f""" systemd.show_status=true{luksstuff}
"""
            )
            writetext(p(j("etc", "kernel", "cmdline")), kernelcmd)

            chroot_shell(in_chroot, shell_before, "install_kernel")

            # install kernel packages
            pkgmgr.setup_kernel_bootloader()

            # set password
            shadow = Path(p(j("etc", "shadow")))
            pwfile = readlines(shadow)
            pwnotset = bool(
                [
                    line
                    for line in pwfile
                    if line.startswith("root:*:") or line.startswith("root::")
                ]
            )
            if pwnotset:
                _LOGGER.info("Setting root password")
                cmd = ["mkpasswd", "--method=SHA-512", "--stdin"]
                pwproc = subprocess.run(
                    cmd, input=rootpassword, capture_output=True, text=True, check=True
                )
                pw = pwproc.stdout[:-1]
                for n, line in enumerate(pwfile):
                    if line.startswith("root:"):
                        fields = line.split(":")
                        fields[1] = pw
                        pwfile[n] = ":".join(fields)
                writetext(shadow, "".join(pwfile))
            else:
                _LOGGER.info(
                    "Not setting root password -- first line of /etc/shadow: %s",
                    pwfile[0].strip(),
                )

    def deploy_zfs() -> None:
        _LOGGER.info("Deploying ZFS and dependencies")

        gitter = gitter_factory()

        with blockdev_context(
            voldev, bootdev, volsize, bootsize, chown, chgrp, create=True
        ) as (rootpart, bootpart, efipart), filesystem_context(
            poolname,
            rootpart,
            bootpart,
            efipart,
            workdir,
            swapsize,
            lukspassword,
            luksoptions,
            create=True,
        ) as (
            rootmountpoint,
            p,
            _,
            in_chroot,
            _rootuuid,
            _luksuuid,
            _bootpartuuid,
            _efipartuuid,
        ):
            pkgmgr = pm.chroot_package_manager_factory(
                rootmountpoint, yum_cachedir, releasever, None
            )
            deploy_zfs_in_machine(
                p=p,
                in_chroot=in_chroot,
                pkgmgr=pkgmgr,
                gitter=gitter,
                prebuilt_rpms_path=prebuilt_rpms_path,
                zfs_repo=zfs_repo,
                branch=branch,
                break_before=break_before,
                shell_before=shell_before,
                install_current_kernel_devel=False,
            )

    def get_kernel_initrd_kver(p: Callable[[str], str]) -> tuple[Path, Path, Path, str]:
        try:
            _LOGGER.debug(
                "Fishing out kernel and initial RAM disk from %s",
                p(j("boot", "loader", "*", "linux")),
            )
            kernel = glob.glob(p(j("boot", "loader", "*", "linux")))[0]
            kver = os.path.basename(os.path.dirname(kernel))
            initrd = p(j("boot", "loader", kver, "initrd"))
            hostonly = p(j("boot", "loader", kver, "initrd-hostonly"))
            return Path(kernel), Path(initrd), Path(hostonly), kver
        except IndexError:
            _LOGGER.debug(
                "Fishing out kernel and initial RAM disk from %s",
                p(j("boot", "vmlinuz-*")),
            )
            kernel = glob.glob(p(j("boot", "vmlinuz-*")))[0]
            kver = os.path.basename(kernel)[len("vmlinuz-") :]
            initrd = p(j("boot", "initramfs-%s.img" % kver))
            hostonly = p(j("boot", "initramfs-hostonly-%s.img" % kver))
            return Path(kernel), Path(initrd), Path(hostonly), kver
        except Exception:
            check_call(["ls", "-lRa", p("boot")])
            raise

    def reload_chroot() -> None:  # FIXME should be named finalize_chroot
        # The following reload in a different context scope is a workaround
        # for blkid failing without the reload happening first.
        _LOGGER.info("Finalizing chroot for image")

        with blockdev_context(
            voldev, bootdev, volsize, bootsize, chown, chgrp, create=False
        ) as (rootpart, bootpart, efipart), filesystem_context(
            poolname,
            rootpart,
            bootpart,
            efipart,
            workdir,
            swapsize,
            lukspassword,
            luksoptions,
            create=False,
        ) as (_, p, q, in_chroot, rootuuid, _, bootpartuuid, _):
            chroot_shell(in_chroot, shell_before, "reload_chroot")

            # FIXME: package manager should do this.
            # but only if cachedir is off.
            if not yum_cachedir:
                # OS owns the cache directory.
                # Release disk space now that installation is done.
                for pkgm in ("dnf", "yum"):
                    for directory in ("cache", "lib"):
                        delete_contents(Path(p(j("var", directory, pkgm))))

            if os.path.exists(p("usr/bin/dracut.real")):
                check_call(in_chroot(["mv", "/usr/bin/dracut.real", "/usr/bin/dracut"]))
            kernel, initrd, hostonly_initrd, kver = get_kernel_initrd_kver(p)
            if os.path.isfile(initrd):
                mayhapszfsko = check_output(["lsinitrd", str(initrd)])
            else:
                mayhapszfsko = ""
            # At this point, we regenerate the initrd, if it does not have zfs.ko.
            if "zfs.ko" not in mayhapszfsko:
                check_call(in_chroot(["dracut", "-Nf", q(str(initrd)), kver]))
                after_recreation = check_output(["lsinitrd", str(initrd)])
                for line in after_recreation.splitlines(False):
                    if "zfs" in line:
                        _LOGGER.debug("initramfs: %s", line)
                if "zfs.ko" not in after_recreation:
                    assert 0, (
                        "ZFS kernel module was not found in the initramfs %s --"
                        " perhaps it failed to build." % initrd
                    )

            # Kill the resolv.conf file written only to install packages.
            orig_resolv = p(j("etc", "resolv.conf.orig"))
            final_resolv = p(j("etc", "resolv.conf"))
            if os.path.lexists(orig_resolv):
                _LOGGER.info("Restoring original resolv.conf")
                if os.path.lexists(final_resolv):
                    os.unlink(final_resolv)
                os.rename(orig_resolv, final_resolv)

            # Remove host device files
            shutil.rmtree(p("dev"))
            # sync /dev but only itself and /dev/zfs
            check_call(["rsync", "-ptlgoD", "--numeric-ids", "/dev/", p("dev/")])
            check_call(["rsync", "-ptlgoD", "--numeric-ids", "/dev/zfs", p("dev/zfs")])

            # Snapshot the system as it is, now that it is fully done.
            try:
                check_call_silent_stdout(
                    [
                        "zfs",
                        "list",
                        "-t",
                        "snapshot",
                        "-H",
                        "-o",
                        "name",
                        j(poolname, "ROOT", "os@initial"),
                    ]
                )
            except subprocess.CalledProcessError:
                # check_call(["sync"])
                check_call(["zfs", "snapshot", j(poolname, "ROOT", "os@initial")])

    def biiq(
        init: str, hostonly: bool, enforcing: bool, timeout_factor: float = 1.0
    ) -> None:
        def fish_kernel_initrd() -> (
            tuple[str | None, str | None, Path, Path, Path, Path]
        ):
            with blockdev_context(
                voldev, bootdev, volsize, bootsize, chown, chgrp, create=False
            ) as (rootpart, bootpart, efipart), filesystem_context(
                poolname,
                rootpart,
                bootpart,
                efipart,
                workdir,
                swapsize,
                lukspassword,
                luksoptions,
                create=False,
            ) as (_, p, _, _, rootuuid, luksuuid, _, _):
                kerneltempdir = tempfile.mkdtemp(
                    prefix="install-fedora-on-zfs-bootbits-"
                )
                try:
                    kernel, initrd, hostonly_initrd, _ = get_kernel_initrd_kver(p)
                    shutil.copy2(kernel, kerneltempdir)
                    shutil.copy2(initrd, kerneltempdir)
                    if os.path.isfile(hostonly_initrd):
                        shutil.copy2(hostonly_initrd, kerneltempdir)
                except (KeyboardInterrupt, Exception):
                    shutil.rmtree(kerneltempdir)
                    raise
            return (
                rootuuid,
                luksuuid,
                Path(kerneltempdir),
                kernel,
                initrd,
                hostonly_initrd,
            )

        undoer = Undoer()
        (
            rootuuid,
            luksuuid,
            kerneltempdir,
            kernel,
            initrd,
            hostonly_initrd,
        ) = fish_kernel_initrd()
        undoer.to_rmrf.append(kerneltempdir)
        with undoer:
            return boot_image_in_qemu(
                hostname,
                init,
                poolname,
                original_voldev,
                original_bootdev,
                Path(os.path.join(kerneltempdir, os.path.basename(kernel))),
                Path(
                    os.path.join(
                        kerneltempdir,
                        os.path.basename(initrd if not hostonly else hostonly_initrd),
                    )
                ),
                force_kvm,
                interactive_qemu,
                lukspassword,
                rootpassword,
                rootuuid,
                luksuuid,
                int(qemu_timeout * timeout_factor),
                enforcing,
            )

    def bootloader_install() -> None:
        _LOGGER.info("Installing bootloader.")
        with blockdev_context(
            voldev, bootdev, volsize, bootsize, chown, chgrp, create=False
        ) as (rootpart, bootpart, efipart), filesystem_context(
            poolname,
            rootpart,
            bootpart,
            efipart,
            workdir,
            swapsize,
            lukspassword,
            luksoptions,
            create=False,
        ) as (_, p, q, _, _, _, _, _):
            _, initrd, hostonly_initrd, kver = get_kernel_initrd_kver(p)
            # create bootloader installer
            bootloadertext = """#!/bin/bash -e
error() {{
    retval=$?
    echo There was an unrecoverable error finishing setup >&2
    exit $retval
}}
trap error ERR
export PATH=/sbin:/usr/sbin:/bin:/usr/bin
mount / -o remount,rw
mount /boot
mount /boot/efi
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /var/tmp
mount --bind /dev/stderr /dev/log

if ! test -f /.autorelabel ; then
    # We have already passed to the fixfiles stage,
    # so let's not redo the work.
    # Useful to save time when iterating on this stage
    # with short-circuiting.
    echo Setting up GRUB environment block
    rm -f /boot/grub2/grubenv /boot/efi/EFI/fedora/grubenv
    echo "# GRUB Environment Block" > /boot/grub2/grubenv
    for x in `seq 999`
    do
        echo -n "#" >> /boot/grub2/grubenv
    done
    chmod 644 /boot/grub2/grubenv

    echo Installing BIOS GRUB
    grub2-install --target=i386-pc /dev/sda
    grub2-mkconfig -o /boot/grub2/grub.cfg

    echo Adjusting ZFS cache file and settings
    rm -f /etc/zfs/zpool.cache
    zpool set cachefile=/etc/zfs/zpool.cache "{poolname}"
    ls -la /etc/zfs/zpool.cache
    zfs inherit com.sun:auto-snapshot "{poolname}"

    echo Generating initial RAM disks
    dracut -Nf {initrd} `uname -r`
    lsinitrd {initrd} | grep zfs
    dracut -Hf {hostonly_initrd} `uname -r`
    lsinitrd {hostonly_initrd} | grep zfs
fi

echo Setting up SELinux autorelabeling
fixfiles -F onboot

umount /var/tmp
umount /dev/log

echo Starting autorelabel boot
# systemd will now start and relabel, then reboot.
exec /sbin/init "$@"
""".format(
                **{
                    "poolname": poolname,
                    "kver": kver,
                    "hostonly_initrd": q(str(hostonly_initrd)),
                    "initrd": q(str(initrd)),
                }
            )
            bootloaderpath = Path(p("installbootloader"))
            writetext(Path(bootloaderpath), bootloadertext)
            os.chmod(bootloaderpath, 0o755)

        _LOGGER.info(
            "Entering sub-phase preparation of bootloader and SELinux relabeling in VM."
        )
        return biiq("init=/installbootloader", False, True, 2.0)

    def boot_to_test_x_hostonly(hostonly: bool) -> None:
        _LOGGER.info("Entering test of hostonly=%s initial RAM disk in VM.", hostonly)
        biiq("systemd.unit=multi-user.target", hostonly, True)

    def boot_to_test_non_hostonly() -> None:
        boot_to_test_x_hostonly(False)

    def boot_to_test_hostonly() -> None:
        boot_to_test_x_hostonly(True)

    try:
        # start main program
        for stage in [
            "beginning",
            "deploy_zfs",
            "reload_chroot",
            "bootloader_install",
            "boot_to_test_non_hostonly",
            "boot_to_test_hostonly",
        ]:
            if break_before == stage:
                raise BreakingBefore(stage)
            if short_circuit in (stage, None):
                locals()[stage]()
                short_circuit = None

    # tell the user we broke
    except BreakingBefore as e:
        _LOGGER.info("------------------------------------------------")
        _LOGGER.info("Breaking before %s", break_stages[e.args[0]])
        raise

    # end operating with the devices
    except BaseException:
        _LOGGER.exception("Unexpected error")
        raise


def _test_cmd(cmdname: str, expected_ret: int) -> bool:
    try:
        with open(os.devnull) as devnull_r, open(os.devnull, "w") as devnull_w:
            subprocess.check_call(
                shlex.split(cmdname),
                stdin=devnull_r,
                stdout=devnull_w,
                stderr=devnull_w,
            )
    except subprocess.CalledProcessError as e:
        if e.returncode == expected_ret:
            return True
        return False
    except FileNotFoundError:
        return False
    return True


def _test_mkfs_ext4() -> bool:
    return _test_cmd("mkfs.ext4", 1)


def _test_mkfs_vfat() -> bool:
    return _test_cmd("mkfs.vfat", 1)


def _test_zfs() -> bool:
    return _test_cmd("zfs", 2) and os.path.exists("/dev/zfs")


def _test_rsync() -> bool:
    return _test_cmd("rsync", 1)


def _test_gdisk() -> bool:
    return _test_cmd("gdisk", 5)


def _test_cryptsetup() -> bool:
    return _test_cmd("cryptsetup", 1)


def _test_mkpasswd() -> bool:
    return _test_cmd("mkpasswd --help", 0)


def _test_dnf() -> bool:
    try:
        check_call_silent_stdout(["dnf", "--help"])
    except subprocess.CalledProcessError as e:
        if e.returncode != 1:
            return False
    except OSError as e:
        if e.errno == 2:
            return False
        raise
    return True


def install_fedora_on_zfs() -> int:
    """Install Fedora on a ZFS root pool."""
    args = get_install_fedora_on_zfs_parser().parse_args()
    log_config(args.trace_file)
    if not _test_rsync():
        _LOGGER.error(
            "error: rsync is not available. Please use your package manager to install"
            " rsync."
        )
        return 5
    if not _test_zfs():
        _LOGGER.error(
            "error: ZFS is not installed properly. Please install ZFS with `deploy-zfs`"
            " and then modprobe zfs.  If installing from source, pay attention to the"
            " --with-udevdir= configure parameter and don't forget to run ldconfig"
            " after the install."
        )
        return 5
    if not _test_mkfs_ext4():
        _LOGGER.error(
            "error: mkfs.ext4 is not installed properly. Please install e2fsprogs."
        )
        return 5
    if not _test_mkfs_vfat():
        _LOGGER.error(
            "error: mkfs.vfat is not installed properly. Please install dosfstools."
        )
        return 5
    if not _test_cryptsetup():
        _LOGGER.error(
            "error: cryptsetup is not installed properly. Please install cryptsetup."
        )
        return 5
    if not _test_mkpasswd():
        _LOGGER.error(
            "error: mkpasswd is not installed properly. Please install mkpasswd."
        )
        return 5
    if not _test_gdisk():
        _LOGGER.error("error: gdisk is not installed properly. Please install gdisk.")
        return 5
    if not _test_dnf():
        _LOGGER.error("error: DNF is not installed properly.  Please install DNF.")
        return 5
    if not args.break_before and not test_qemu():
        _LOGGER.error(
            "error: QEMU is not installed properly. Please use your package manager"
            " to install QEMU (in Fedora, qemu-system-x86-core or qemu-kvm), or"
            " use --break-before=bootloader_install to create the image but not"
            " boot it in a VM (it is likely that the image will not be bootable"
            " since the bootloader will not be present)."
        )
        return 5

    if not args.volsize:
        _LOGGER.error("error: --vol-size must be a number.")
        return os.EX_USAGE
    try:
        if args.volsize[-1] == "B":
            volsize = int(args.volsize[:-1])
        else:
            volsize = int(args.volsize) * 1024 * 1024
    except Exception as exc:
        _LOGGER.error(
            "error: %s; --vol-size must be a valid number of megabytes,"
            " or bytes with a B postfix.",
            exc,
        )
        return os.EX_USAGE

    try:
        install_fedora(
            Path(args.workdir),
            Path(args.voldev),
            volsize,
            args.zfs_repo,
            Path(args.bootdev) if args.bootdev else None,
            args.bootsize,
            args.poolname,
            args.hostname,
            args.rootpassword,
            args.swapsize,
            args.releasever,
            args.lukspassword,
            args.interactive_qemu,
            args.luksoptions,
            args.prebuiltrpms,
            Path(args.yum_cachedir) if args.yum_cachedir else None,
            args.force_kvm,
            branch=args.branch,
            chown=args.chown,
            chgrp=args.chgrp,
            break_before=args.break_before,
            shell_before=args.shell_before,
            short_circuit=args.short_circuit,
        )
    except ImpossiblePassphrase as e:
        _LOGGER.error("error: %s", e)
        return os.EX_USAGE
    except (ZFSMalfunction, ZFSBuildFailure) as e:
        _LOGGER.error("error: %s", e)
        return 9
    except BreakingBefore:
        return 120
    return 0


def deploy_zfs_in_machine(
    p: Callable[[str], str],
    in_chroot: Callable[[list[str]], list[str]],
    pkgmgr: pm.PackageManager,
    gitter: Gitter,
    zfs_repo: str,
    branch: str,
    prebuilt_rpms_path: Path | None,
    break_before: str | None,
    shell_before: str | None,
    install_current_kernel_devel: bool,
    update_sources: bool = True,
) -> None:
    """Deploy ZFS in the local machine."""
    arch = platform.machine()
    stringtoexclude = "debuginfo"
    stringtoexclude2 = "debugsource"

    # check for shell
    chroot_shell(in_chroot, shell_before, "install_prebuilt_rpms")

    undoer = Undoer()

    with undoer:
        if prebuilt_rpms_path:
            target_rpms_path = Path(
                p(j("tmp", "zfs-fedora-installer-prebuilt-rpms"))
            )  # FIXME hardcoded! Use workdir instead.
            if not os.path.isdir(target_rpms_path):
                os.mkdir(target_rpms_path)
            if ismount(target_rpms_path):
                if (
                    os.stat(prebuilt_rpms_path).st_ino
                    != os.stat(target_rpms_path).st_ino
                ):
                    umount(target_rpms_path)
                    bindmount(
                        Path(os.path.abspath(prebuilt_rpms_path)), target_rpms_path
                    )
            else:
                bindmount(Path(os.path.abspath(prebuilt_rpms_path)), target_rpms_path)
            if os.path.isdir(target_rpms_path):
                undoer.to_rmdir.append(target_rpms_path)
            if ismount(target_rpms_path):
                undoer.to_unmount.append(target_rpms_path)
            prebuilt_rpms_to_install = {
                os.path.basename(s)
                for s in (
                    glob.glob(j(prebuilt_rpms_path, f"*{arch}.rpm"))
                    + glob.glob(j(prebuilt_rpms_path, "*noarch.rpm"))
                )
                if stringtoexclude not in os.path.basename(s)
                and stringtoexclude2 not in os.path.basename(s)
            }
        else:
            target_rpms_path = None
            prebuilt_rpms_to_install = set()

        if prebuilt_rpms_to_install and target_rpms_path:
            _LOGGER.info(
                "Installing available prebuilt RPMs: %s", prebuilt_rpms_to_install
            )
            files_to_install = [
                Path(j(target_rpms_path, s)) for s in prebuilt_rpms_to_install
            ]
            pkgmgr.install_local_packages(files_to_install)

        if target_rpms_path:
            umount(target_rpms_path)
            undoer.to_unmount.remove(target_rpms_path)
            os.rmdir(target_rpms_path)
            undoer.to_rmdir.remove(target_rpms_path)

        # kernel devel
        if install_current_kernel_devel:
            uname_r = check_output(in_chroot("uname -r".split())).strip()
            if "pvops.qubes" in uname_r:
                assert 0, (
                    "Installation on non-HVM Qubes AppVMs is unsupported due to the"
                    " unavailability of kernel-devel packages in-VM (kernel version"
                    f" {uname_r}).\n"
                    "If you want to boot an AppVM as HVM, follow the instructions here:"
                    " https://www.qubes-os.org/doc/managing-vm-kernel/#using-kernel-installed-in-the-vm"
                )
            pkgs = ["kernel-%s" % uname_r, "kernel-devel-%s" % uname_r]
            pkgmgr.ensure_packages_installed(pkgs)

        for project, patterns, keystonepkgs, mindeps, buildcmd in (
            (
                "grub-zfs-fixer",
                ("grub-zfs-fixer-*.noarch.rpm",),
                ("grub-zfs-fixer",),
                [
                    "make",
                    "rpm-build",
                ],
                "cd /usr/src/grub-zfs-fixer && make rpm",
            ),
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
                    "zfs-dracut-*.noarch.rpm",
                ),
                ("zfs", "zfs-dkms", "zfs-dracut"),
                [
                    "zlib-devel",
                    "libuuid-devel",
                    "bc",
                    "libblkid-devel",
                    "libattr-devel",
                    "lsscsi",
                    "mdadm",
                    "parted",
                    "libudev-devel",
                    "libtool",
                    "openssl-devel",
                    "make",
                    "automake",
                    "libtirpc-devel",
                    "libffi-devel",
                    "python3-devel",
                    "python3-cffi",
                    "libaio-devel",
                    "rpm-build",
                    "ncompress",
                    "python3-setuptools",
                ],
                (
                    "cd /usr/src/zfs && "
                    "./autogen.sh && "
                    "./configure --with-config=user && "
                    f"make -j{multiprocessing.cpu_count()} rpm-utils && "
                    f"make -j{multiprocessing.cpu_count()} rpm-dkms"
                ),
            ),
        ):
            # check for shell
            project_under = f"deploy_{project.replace('-', '_')}"
            chroot_shell(in_chroot, shell_before, project_under)

            try:
                _LOGGER.info(
                    "Checking if keystone packages %s are installed",
                    ", ".join(keystonepkgs),
                )
                check_call_silent(
                    in_chroot(["rpm", "-q"] + list(keystonepkgs)),
                )
            except subprocess.CalledProcessError:
                _LOGGER.info("Packages %s are not installed, building", keystonepkgs)
                project_dir = Path(p(j("usr", "src", project)))

                def getrpms(pats: Sequence[str], directory: Path) -> list[Path]:
                    therpms = [
                        Path(rpmfile)
                        for pat in pats
                        for rpmfile in glob.glob(j(directory, pat))
                        if stringtoexclude not in os.path.basename(rpmfile)
                    ]
                    return therpms

                files_to_install = getrpms(patterns, project_dir)
                if not files_to_install:
                    repo = (
                        zfs_repo
                        if project == "zfs"
                        else ("https://github.com/Rudd-O/%s" % project)
                    )
                    repo_branch = branch if project == "zfs" else "master"

                    gitter.checkout_repo_at(
                        repo, project_dir, repo_branch, update=update_sources
                    )

                    if mindeps:
                        pkgmgr.ensure_packages_installed(mindeps)

                    _LOGGER.info("Building project: %s", project)
                    cmd = in_chroot(["bash", "-c", buildcmd])
                    check_call(cmd)
                    files_to_install = getrpms(patterns, project_dir)

                _LOGGER.info("Installing built RPMs: %s", files_to_install)
                pkgmgr.install_local_packages(files_to_install)

        # Check we have a patched grub2-mkconfig.
        _LOGGER.info("Checking if grub2-mkconfig has been patched")
        mkconfig_file = Path(p(j("usr", "sbin", "grub2-mkconfig")))
        mkconfig_text = readtext(mkconfig_file)
        if "This program was patched by fix-grub-mkconfig" not in mkconfig_text:
            raise ZFSBuildFailure(
                f"expected to find patched {mkconfig_file} but could not find it."
                "  Perhaps the grub-zfs-fixer RPM was never installed?"
            )

        # Build ZFS for all kernels that have a kernel-devel.
        _LOGGER.info("Running DKMS install for all kernel-devel packages")
        check_call(
            in_chroot(
                [
                    "bash",
                    "-xc",
                    "ver= ;"
                    "for f in /var/lib/dkms/zfs/* ; do"
                    '  test -L "$f" && continue ;'
                    '  test -d "$f" || continue ;'
                    '  ver=$(basename "$f") ; '
                    "done ;"
                    "for kver in $(rpm -q kernel-devel"
                    " --queryformat='%{version}-%{release}.%{arch} ')"
                    ' ; do dkms install -m zfs -v "$ver" -k "$kver" || exit $? ; '
                    "done",
                ]
            )
        )

        # Check we have a ZFS.ko for at least one kernel.
        modules_dir = p(j("usr", "lib", "modules", "*", "*", "zfs.ko*"))
        modules_files = glob.glob(modules_dir)
        if not modules_files:
            raise ZFSBuildFailure(
                f"expected to find but could not find module zfs.ko in {modules_dir}."
                "  Perhaps the ZFS source you used is too old to work with the kernel"
                " this program installed?"
            )


def deploy_zfs() -> int:
    """Deploy ZFS locally."""
    args = get_deploy_parser().parse_args()
    log_config(args.trace_file)

    def p(withinchroot: str) -> str:
        return j("/", withinchroot.lstrip(os.path.sep))

    def in_chroot(x: list[str]) -> list[str]:
        return x

    package_manager = pm.os_package_manager_factory()
    try:
        gitter = gitter_factory(dispvm_template=args.dispvm_template)
    except ValueError as e:
        _LOGGER.error("error: %s.", e)
        return os.EX_USAGE

    try:
        deploy_zfs_in_machine(
            p=p,
            in_chroot=in_chroot,
            pkgmgr=package_manager,
            gitter=gitter,
            prebuilt_rpms_path=args.prebuiltrpms,
            zfs_repo=args.zfs_repo,
            branch=args.branch,
            break_before=None,
            shell_before=None,
            install_current_kernel_devel=True,
            update_sources=args.update_sources,
        )
    except BaseException:
        _LOGGER.exception("Unexpected error")
        raise

    return 0


def bootstrap_chroot() -> int:
    """Bootstrap a chroot."""
    args = get_bootstrap_chroot_parser().parse_args()
    log_config(args.trace_file)

    pkgmgr = pm.chroot_bootstrapper_factory(
        args.chroot, args.yum_cachedir, args.releasever, None
    )

    try:
        pkgmgr.bootstrap_packages()
    except BaseException:
        _LOGGER.exception("Unexpected error")
        raise

    return 0


def run_command_in_filesystem_context() -> int:
    """Run a command in the context of the created image."""
    args = get_run_command_parser().parse_args()
    log_config(args.trace_file)
    with blockdev_context(
        Path(args.voldev),
        Path(args.bootdev) if args.bootdev else None,
        0,
        0,
        None,
        None,
        create=False,
    ) as (rootpart, bootpart, efipart), filesystem_context(
        args.poolname,
        rootpart,
        bootpart,
        efipart,
        Path(args.workdir),
        0,
        "",
        "",
        create=False,
    ) as (_, _, _, in_chroot, _, _, _, _):
        return subprocess.call(in_chroot(args.args))
