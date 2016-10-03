#!/usr/bin/env python

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
import pty
import tempfile
import logging
import uuid
import re
import shlex
import collections
import threading
import fnmatch
import multiprocessing
import pipes
import fcntl
import errno


break_stages = collections.OrderedDict()
break_stages["beginning"] = "doing anything"
break_stages["install_prebuilt_rpms"] = "installing prebuilt RPMs specified on the command line"
break_stages["install_grub_zfs_fixer"] = "installing GRUB2 fixer RPM"
break_stages["deploy_spl"] = "deploying SPL"
break_stages["deploy_zfs"] = "deploying ZFS"
break_stages["reload_chroot"] = "reloading the final chroot"
break_stages["install_bootloader"] = "installing the bootloader"
break_stages["boot_bootloader"] = "booting the installation of the bootloader"

qemu_timeout = 120
qemu_full_emulation_factor = 10
BASIC_FORMAT = '%(levelname)8s:%(name)s:%(funcName)20s@%(lineno)4d\t%(message)s'


def get_parser():
    parser = argparse.ArgumentParser(
        description="Install a minimal Fedora system inside a ZFS pool within a disk image or device"
    )
    parser.add_argument(
        "voldev", metavar="VOLDEV", type=str, nargs=1,
        help="path to volume (device to use or regular file to create)"
    )
    parser.add_argument(
        "--vol-size", dest="volsize", metavar="VOLSIZE", type=int,
        action="store", default=7000, help="volume size in MiB (default 7000)"
    )
    parser.add_argument(
        "--separate-boot", dest="bootdev", metavar="BOOTDEV", type=str,
        action="store", default=None, help="place /boot in a separate volume"
    )
    parser.add_argument(
        "--boot-size", dest="bootsize", metavar="BOOTSIZE", type=int,
        action="store", default=256, help="boot partition size in MiB, or boot volume size in MiB, when --separate-boot is specified (default 256)"
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
        action="store", default=None, help="also install pre-built SPL, ZFS, GRUB and other RPMs in this directory, except for debuginfo packages within the directory (default: build SPL, ZFS and GRUB RPMs, within the chroot)"
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
        action="store_true", default=False, help="QEMU will run interactively in curses mode and it won't be stopped after %s seconds; useful to manually debug problems installing the bootloader" % qemu_timeout
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
        "--break-before", dest="break_before",
        choices=break_stages,
        action="store", default=None,
        help="break before the specified stage: %s; useful to examine "
             "the file systems at a predetermined build stage" % (
            ", ".join("'%s' (%s)" % s for s in break_stages.items()),
        )
    )
    parser.add_argument(
        "--workdir", dest="workdir",
        action="store", default='/var/lib/zfs-fedora-installer',
        help="use this directory as a working (scratch) space for the mount points of the created pool"
    )
    return parser

def get_deploy_parser():
    parser = argparse.ArgumentParser(
        description="Install ZFS on a running system"
    )
    parser.add_argument(
        "--use-prebuilt-rpms", dest="prebuiltrpms", metavar="DIR", type=str,
        action="store", default=None, help="also install pre-built SPL, ZFS, GRUB and other RPMs in this directory, except for debuginfo packages within the directory (default: build SPL, ZFS and GRUB RPMs, within the system)"
    )
    parser.add_argument(
        "--no-cleanup", dest="nocleanup",
        action="store_true", default=False, help="if an error occurs, do not clean up temporary mounts and files"
    )
    return parser


def format_cmdline(lst):
    return " ".join(pipes.quote(x) for x in lst)

def check_call(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logging.debug("Check calling %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.check_call(*args,**kwargs)

def check_output(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logging.debug("Check outputting %s in cwd %r", format_cmdline(cmd), cwd)
    output = subprocess.check_output(*args,**kwargs)
    if output:
        firstline=output.splitlines()[0].strip()
        logging.debug("First line of output from command: %s", firstline)
    else:
        logging.debug("No output from command")
    return output

def Popen(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logging.debug("Popening %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.Popen(*args,**kwargs)

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


def get_associated_lodev(path):
    output = ":".join(check_output(
        ["losetup", "-j",path]
    ).rstrip().split(":")[:-2])
    if output: return output
    return None

def losetup(path):
    check_call(
        ["losetup", "-P", "--find", "--show", path]
    )
    return get_associated_lodev(path)

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

def mpdecode(encoded_mountpoint):
    chars = []
    pos = 0
    while pos < len(encoded_mountpoint):
        c = encoded_mountpoint[pos]
        if c == "\\":
          try:
            if encoded_mountpoint[pos+1] == "\\":
                chars.append("\\")
                pos = pos + 1
            elif (
                encoded_mountpoint[pos+1] in "0123456789" and
                encoded_mountpoint[pos+2] in "0123456789" and
                encoded_mountpoint[pos+3] in "0123456789"
                ):
                chunk = encoded_mountpoint[pos+1] + encoded_mountpoint[pos+2] + encoded_mountpoint[pos+3]
                chars.append(chr(int(chunk, 8)))
                pos = pos + 3
            else:
                raise ValueError("Unparsable mount point %r at pos %s" % (encoded_mountpoint, pos))
          except IndexError, e:
              raise ValueError("Unparsable mount point %r at pos %s: %s" % (encoded_mountpoint, pos, e))
        else:
            chars.append(c)
        pos = pos + 1
    return "".join(chars)

def check_for_open_files(prefix):
  """Check that there are open files or mounted file systems within the prefix.

  Returns a  dictionary where the keys are the files, and the values are lists
  that contain tuples (pid, command line) representing the processes that are
  keeping those files open, or tuples ("<mount>", description) representing
  the file systems mounted there."""
  results = dict()
  files = glob.glob("/proc/*/fd/*") + glob.glob("/proc/*/cwd")
  for f in files:
    try:
      d = os.readlink(f)
    except Exception:
      continue
    if d.startswith(prefix + os.path.sep) or d == prefix:
      pid = f.split(os.path.sep)[2]
      if pid == "self": continue
      c = os.path.join("/", *(f.split(os.path.sep)[1:3] + ["cmdline"]))
      try:
        cmd = format_cmdline(file(c).read().split("\0"))
      except Exception:
        continue
      if len(cmd) > 60:
        cmd = cmd[:57] + "..."
      if d not in results:
        results[d] = []
      results[d].append((pid, cmd))
  for l in file("/proc/self/mounts").readlines():
      fields = l[:-1].split(" ")
      dev = mpdecode(fields[0])
      mp = mpdecode(fields[1])
      if mp.startswith(prefix + os.path.sep):
        if mp not in results:
            results[mp] = []
        results[mp].append(("<mount>", dev))
  return results

def umount(mountpoint, tries=5):
    if not os.path.ismount(mountpoint):
        return
    try:
        check_call(["umount", mountpoint])
    except subprocess.CalledProcessError:
        if tries < 1:
            raise
        openfiles = check_for_open_files(mountpoint)
        if openfiles:
            logging.info("There are open files in %r:", mountpoint)
            for of, procs in openfiles.items():
                logging.info("%r:", of)
                for pid, cmd in procs:
                    logging.info("  %7s  %s", pid, cmd)
        logging.info("Syncing and sleeping 1 second")
        check_call(['sync'])
        time.sleep(1)
        umount(mountpoint, tries - 1)

def make_temp_yum_config(source, directory, **kwargs):
    tempyumconfig = tempfile.NamedTemporaryFile(dir=directory)
    yumconfigtext = file(source).read()
    for optname, optval in kwargs.items():
        if optval is None:
            yumconfigtext, repls = re.subn("^ *%s *=.*$" % (optname,), "", yumconfigtext, flags=re.M)
        else:
            if optname == "cachedir":
                optval = optval + "/$basearch/$releasever"
            yumconfigtext, repls = re.subn("^ *%s *=.*$" % (optname,), "%s=%s" % (optname, optval), yumconfigtext, flags=re.M)
            if not repls:
                yumconfigtext, repls = re.subn("\\[main]", "[main]\n%s=%s" % (optname, optval), yumconfigtext)
                assert repls, "Could not substitute yum.conf main config section with the %s stanza.  Text: %s" % (optname, yumconfigtext)
    tempyumconfig.write(yumconfigtext)
    tempyumconfig.flush()
    tempyumconfig.seek(0)
    return tempyumconfig

fedora_repos_template = """
[fedora]
name=Fedora $releasever - $basearch
failovermethod=priority
#baseurl=http://download.fedoraproject.org/pub/fedora/linux/releases/$releasever/Everything/$basearch/os/
metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-$releasever&arch=$basearch
enabled=1
metadata_expire=7d
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$releasever-$basearch
skip_if_unavailable=False

[updates]
name=Fedora $releasever - $basearch - Updates
failovermethod=priority
#baseurl=http://download.fedoraproject.org/pub/fedora/linux/updates/$releasever/$basearch/
metalink=https://mirrors.fedoraproject.org/metalink?repo=updates-released-f$releasever&arch=$basearch
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$releasever-$basearch
skip_if_unavailable=False
"""

class ChrootPackageManager(object):

    chroot = None
    cachedir = None
    cachedir_lockfile = None

    cachemount = None
    pkgmgr_config_outsidechroot = None
    pkgmgr_config_insidechroot = None
    strategy_outsidechroot = None
    strategy_insidechroot = None

    def __init__(self, chroot, releasever, cachedir=None):
        self.releasever = releasever
        self.chroot = chroot
        self.cachedir = None if cachedir is None else os.path.abspath(cachedir)

    def __enter__(self):
        if self.pkgmgr_config_outsidechroot:
            return

        if os.path.exists("/etc/dnf/dnf.conf"):
            sourceconf = "/etc/dnf/dnf.conf"
            self.strategy_outsidechroot = "dnf"
        else:
            sourceconf = "/etc/yum.conf"
            self.strategy_outsidechroot = "yum"

        # prepare yum cache directory
        if self.cachedir:
            for subdir in ["ephemeral", "permanent"]:
                subdir = os.path.join(self.cachedir, subdir)
                if not os.path.isdir(subdir):
                    os.makedirs(subdir)
            cachemount = j(self.chroot, "var", "cache", self.strategy_outsidechroot)
            if not os.path.isdir(cachemount):
                os.makedirs(cachemount)
            if not os.path.ismount(cachemount):
                check_call(["mount", "--bind", self.cachedir, cachemount])
            self.cachemount = cachemount
            if not os.path.isdir(j(self.chroot, "var", "lib", self.strategy_outsidechroot)):
                os.makedirs(j(self.chroot, "var", "lib", self.strategy_outsidechroot))
            parms = dict(
                source=sourceconf,
                directory=os.getenv("TMPDIR") or "/tmp",
                cachedir=j("/var", "cache", self.strategy_outsidechroot, "ephemeral"),
                persistdir=j("/var", "lib", self.strategy_outsidechroot),
                logfile="/dev/null",
                debuglevel=2,
                reposdir="/nonexistent",
                include=None,
            )
            self.cachedir_lockfile = open(
                os.path.join(self.cachedir, "lockfile"),
                'wb'
            )
            fcntl.flock(self.cachedir_lockfile.fileno(), fcntl.LOCK_EX)
        else:
            parms = dict(
                source=sourceconf,
                directory=os.getenv("TMPDIR") or "/tmp",
                reposdir="/nonexistent",
                include=None,
            )

        self.pkgmgr_config_outsidechroot = make_temp_yum_config(**parms)
        # write fedora repos configuration
        self.pkgmgr_config_outsidechroot.seek(0, 2)
        self.pkgmgr_config_outsidechroot.write(fedora_repos_template)
        self.pkgmgr_config_outsidechroot.flush()
        self.pkgmgr_config_outsidechroot.seek(0)

        if os.path.isfile(j(self.chroot, "etc", "dnf", "dnf.conf")):
            parms["source"] = j(self.chroot, "etc", "dnf", "dnf.conf")
            self.strategy_insidechroot = "dnf"

        elif os.path.isfile(j(self.chroot, "etc", "yum.conf")):
            parms["source"] = j(self.chroot, "etc", "yum.conf")
            self.strategy_insidechroot = "yum"

        if self.strategy_insidechroot:
            parms["directory"] = j(self.chroot, "tmp")
            self.pkgmgr_config_insidechroot = make_temp_yum_config(**parms)
            # write fedora repos configuration
            self.pkgmgr_config_insidechroot.seek(0, 2)
            self.pkgmgr_config_insidechroot.write(fedora_repos_template)
            self.pkgmgr_config_insidechroot.flush()
            self.pkgmgr_config_insidechroot.seek(0)

    def __exit__(self, *ignored, **kwignored):
        if self.cachedir_lockfile:
            self.cachedir_lockfile.close()
            self.cachedir_lockfile = None
        if self.cachemount:
            umount(self.cachemount)
            self.cachemount = None
        if self.pkgmgr_config_insidechroot:
            self.pkgmgr_config_insidechroot.close()
            self.pkgmgr_config_insidechroot = None
        if self.pkgmgr_config_outsidechroot:
            self.pkgmgr_config_outsidechroot.close()
            self.pkgmgr_config_outsidechroot = None
        self.strategy_insidechroot = None
        self.strategy_outsidechroot = None

    def _save_downloaded_packages(self, strategy):
        # DNF developers are criminals.
        # https://github.com/rpm-software-management/dnf/pull/286
        # https://bugzilla.redhat.com/show_bug.cgi?id=1046244
        # Who the fuck does this shit?
        if not self.cachedir:
            return
        if strategy != "dnf":
            return
        check_call([
            "rsync", "-axHAS",
            self.cachedir + os.path.sep + "ephemeral" + os.path.sep,
            self.cachedir + os.path.sep + "permanent" + os.path.sep,
        ])

    def _restore_downloaded_packages(self, strategy):
        if not self.cachedir:
            return
        if strategy != "dnf":
            return
        check_call([
            "rsync", "-axHAS",
            self.cachedir + os.path.sep + "permanent" + os.path.sep,
            self.cachedir + os.path.sep + "ephemeral" + os.path.sep,
        ])

    def ensure_packages_installed(self, packages, method="in_chroot"):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        with self:
            # method can be out_of_chroot or in_chroot
            if method == 'in_chroot':
                if not self.strategy_insidechroot:
                    raise Exception("Cannot use in_chroot method without a working yum or DNF inside the chroot")
                strategy = self.strategy_insidechroot
            elif method == 'out_of_chroot':
                if not self.strategy_outsidechroot:
                    raise Exception("Cannot use out_of_chroot method without a working yum or DNF installed on your system")
                strategy = self.strategy_outsidechroot
            else:
                assert 0, "unknown method %r" % method

            try:
                logging.info("Checking packages are available: %s", packages)
                check_call(in_chroot(["rpm", "-q"] + packages),
                                    stdout=file(os.devnull, "w"), stderr=subprocess.STDOUT)
                logging.info("All required packages are available")
            except subprocess.CalledProcessError:
                logging.info("Installing packages %s: %s", method, packages)
                if method == 'in_chroot':
                    yumconfig = self.pkgmgr_config_insidechroot.name[len(self.chroot):]
                    cmd = in_chroot([strategy, 'install', '-y'])
                elif method == 'out_of_chroot':
                    yumconfig = self.pkgmgr_config_outsidechroot.name
                    cmd = [strategy,
                           'install',
                           '--disableplugin=*qubes*',
                           '-y',
                           '--installroot=%s' % self.chroot,
                           '--releasever=%d' % self.releasever]
                cmd = cmd + ['-c', yumconfig]
                if strategy != "dnf":
                    cmd = cmd + ['--']
                cmd = cmd + packages
                return self._run_pkgmgr_install(cmd, strategy)

    def install_local_packages(self, packages):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        with self:
            # always happens in chroot
            # packages must be a list of paths to RPMs valid within the chroot
            if not self.strategy_insidechroot:
                raise Exception("Cannot install local packages without a working yum or DNF inside the chroot")

            packages = [ os.path.abspath(p) for p in packages ]
            for package in packages:
                if not os.path.isfile(package):
                    raise Exception("package file %r does not exist" % package)
                if not package.startswith(self.chroot + os.path.sep):
                    raise Exception("package file %r is not within the chroot" % package)
            logging.info("Installing packages: %s", packages)
            if self.strategy_insidechroot == "yum":
                cmd = in_chroot(["yum", 'localinstall', '-y'])
            elif self.strategy_insidechroot == "dnf":
                cmd = in_chroot(["dnf", 'install', '-y'])
            else:
                assert 0, "unknown strategy %r" % self.strategy_insidechroot
            yumconfig = self.pkgmgr_config_insidechroot.name[len(self.chroot):]
            cmd = cmd + ['-c', yumconfig]
            if self.strategy_insidechroot != "dnf":
                cmd = cmd + ['--']
            cmd = cmd + [ p[len(self.chroot):] for p in packages ]
            return self._run_pkgmgr_install(cmd, self.strategy_insidechroot)

    def _run_pkgmgr_install(self, cmd, strategy):
        installidx = None
        for x in ("install", "localinstall"):
            if x in cmd:
                installidx = cmd.index(x)
                break
        if installidx is not None:
            self._restore_downloaded_packages(self.strategy_insidechroot)
            precmd = cmd[:installidx] + ["--downloadonly"] + cmd[installidx:]
            check_call(precmd)
            self._save_downloaded_packages(self.strategy_insidechroot)
        check_call(cmd)


class SystemPackageManager(object):

    def __init__(self):
        if os.path.exists("/etc/dnf/dnf.conf"):
            self.strategy = "dnf"
        else:
            self.strategy = "yum"

    def ensure_packages_installed(self, packages, method="in_chroot"):
        logging.info("Checking packages are available: %s", packages)
        try:
            check_call(["rpm", "-q"] + packages,
                       stdout=file(os.devnull, "w"), stderr=subprocess.STDOUT)
            logging.info("All required packages are available")
        except subprocess.CalledProcessError:
            logging.info("Installing packages %s: %s", method, packages)
            cmd = [self.strategy, 'install', '-y']
            if self.strategy != "dnf":
                cmd = cmd + ['--']
            cmd = cmd + packages
            check_call(cmd)

    def install_local_packages(self, packages):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        packages = [ os.path.abspath(p) for p in packages ]
        for package in packages:
            if not os.path.isfile(package):
                raise Exception("package file %r does not exist" % package)
        logging.info("Installing packages: %s", packages)
        if self.strategy == "yum":
            cmd = ['yum', 'localinstall', '-y', '--']
        elif self.strategy == "dnf":
            cmd = ['dnf', 'install', '-y']
        else:
            assert 0, "unknown strategy %r" % self.strategy
        cmd = cmd + packages
        check_call(cmd)


class BootDriver(threading.Thread):

    @staticmethod
    def can_handle_passphrase(passphrase):
        for p in passphrase:
            if ord(p) < 32:
                return False
        return True

    def __init__(self, password, pty):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.password = password # fixme validate password
        self.pty = pty
        self.output = []
        self.error = None

    def run(self):
        logging.info("Boot driver started")
        pwendprompt = "".join(['!', ' ', '\x1b', '[', '0', 'm'])
        if self.password:
            logging.info("Expecting password prompt")
        lastline = []
        while True:
            try:
                try:
                    c = self.pty.read(1)
                except IOError, e:
                    if e.errno == errno.EIO:
                        c = ""
                    else:
                        raise
                if c == "":
                    logging.info("QEMU slave PTY gone")
                    break
                self.output.append(c)
                sys.stdout.write(c)
                if c == "\n":
                    lastline = []
                else:
                    lastline.append(c)
                s = "".join(lastline)
                if self.password and "Please enter passphrase for disk" in s and pwendprompt in s:
                    # Zero out the last line to prevent future spurious matches.
                    lastline = []
                    self.write_password()
            except Exception, e:
                self.error = e
        logging.info("Boot driver gone")

    def get_output(self):
        return "".join(self.output)

    def join(self):
        threading.Thread.join(self)
        if self.error:
            raise self.error

    def write_password(self):
        pw = []
        time.sleep(0.25)
        logging.info("Writing password to VM now")
        for char in self.password:
            self.pty.write(char)
            self.pty.flush()
        self.pty.write("\n")
        self.pty.flush()


class BootloaderWedged(Exception): pass
class MachineNeverShutoff(Exception): pass
class ZFSMalfunction(Exception): pass
class ZFSBuildFailure(Exception): pass
class ImpossiblePassphrase(Exception): pass


class BreakingBefore(Exception): pass

class Undoer:

    def __init__(self):
        self.actions = []

        class Tracker:

            def __init__(self, typ):
                self.typ = typ

            def append(me, o):
                self.actions.append([me.typ, o])

            def remove(me, o):
                for n, (typ, origo) in reversed(list(enumerate(self.actions[:]))):
                    if typ == me.typ and o == origo:
                        self.actions.pop(n)
                        break

        self.to_close = Tracker("close")
        self.to_un_losetup = Tracker("un_losetup")
        self.to_luks_close = Tracker("luks_close")
        self.to_export = Tracker("export")
        self.to_rmdir = Tracker("rmdir")
        self.to_unmount = Tracker("unmount")

    def undo(self):
        for typ, o in reversed(self.actions[:]):
            if typ == "close":
                o.close()
            if typ == "unmount":
                umount(o)
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
                   workdir='/var/lib/zfs-fedora-installer',
    ):

    if lukspassword and not BootDriver.can_handle_passphrase(lukspassword):
        raise ImpossiblePassphrase("passphrase %r cannot be handled by the boot driver" % lukspassword)

    original_voldev = voldev
    original_bootdev = bootdev

    undoer = Undoer()
    to_close = undoer.to_close
    to_un_losetup = undoer.to_un_losetup
    to_luks_close = undoer.to_luks_close
    to_export = undoer.to_export
    to_rmdir = undoer.to_rmdir
    to_unmount = undoer.to_unmount

    def cleanup():
        undoer.undo()

    if not releasever:
        releasever = int(check_output(["rpm", "-q", "fedora-release", "--queryformat=%{version}"]))

    try:
        # check for stage stop
        if break_before == "beginning":
            raise BreakingBefore(break_before)

        voltype = filetype(voldev)
        if bootdev: boottype = filetype(bootdev)

        if voltype == 'doesntexist':  # FIXME use truncate directly with python.  no need to dick around.
            create_file(voldev, volsize * 1024 * 1024, owner=chown, group=chgrp)
            voltype = 'file'

        if bootdev and boottype == 'doesntexist':
            create_file(bootdev, bootsize * 1024 * 1024, owner=chown, group=chgrp)
            boottype = 'file'

        if voltype == 'file':
            if get_associated_lodev(voldev):
                voldev = get_associated_lodev(voldev)
            else:
                voldev = losetup(voldev)
            to_un_losetup.append(voldev)
            voltype = 'blockdev'

        if bootdev and boottype == 'file':
            if get_associated_lodev(bootdev):
                bootdev = get_associated_lodev(bootdev)
            else:
                bootdev = losetup(bootdev)
            to_un_losetup.append(bootdev)
            boottype = 'blockdev'

        if bootdev:
            bootpart = bootdev + "-part1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = bootdev + "p1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = bootdev + "1"
            if not os.path.exists(bootpart):
                bootpart = None
            rootpart = voldev

            if not bootpart:
                cmd = ["fdisk", bootdev]
                pr = Popen(cmd, stdin=subprocess.PIPE)
                pr.communicate(
    '''n
    p
    1



    p
    w
    '''
                )
                retcode = pr.wait()
                if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
                time.sleep(2)

            bootpart = bootdev + "-part1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = bootdev + "p1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = bootdev + "1"
            if not os.path.exists(bootpart):
                assert 0, "partition 1 in device %r failed to be created"%bootdev

        else:
            bootpart = voldev + "-part1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = voldev + "p1"
            if not os.path.exists(bootpart):
                bootpart = bootpart = voldev + "1"
            if not os.path.exists(bootpart):
                bootpart = None
            rootpart = voldev + "-part2"
            if not os.path.exists(rootpart):
                rootpart = rootpart = voldev + "p2"
            if not os.path.exists(rootpart):
                rootpart = rootpart = voldev + "2"
            if not os.path.exists(rootpart):
                rootpart = None

            assert (not bootpart and not rootpart) or (bootpart and rootpart), "weird shit bootpart %s rootpart %s\nYou might want to nuke the partition table on the device/file you specified first." %(bootpart, rootpart)

            bootstartsector = 2048
            bootendsector = bootstartsector + ( bootsize * 1024 * 1024 / 512 ) - 1
            rootstartsector = bootendsector + 1
            rootendsector = ( get_file_size(voldev) / 512 ) - 1 - ( 16 * 1024 * 1024 / 512 )
            if not rootpart and not bootpart:
                cmd = ["fdisk", voldev]
                pr = Popen(cmd, stdin=subprocess.PIPE)
                pr.communicate(
    '''n
p
1
%d
%d

n
p
2
%d
%d
p
w
'''%(bootstartsector, bootendsector, rootstartsector, rootendsector)
                )
                retcode = pr.wait()
                if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
                time.sleep(2)

            bootpart = voldev + "-part1"
            if not os.path.exists(bootpart):
                bootpart = voldev + "p1"
            if not os.path.exists(bootpart):
                bootpart = voldev + "1"
            if not os.path.exists(bootpart):
                assert 0, "partition 1 in device %r failed to be created"%voldev
            rootpart = voldev + "-part2"
            if not os.path.exists(rootpart):
                rootpart = rootpart = voldev + "p2"
            if not os.path.exists(rootpart):
                rootpart = rootpart = voldev + "2"
            if not os.path.exists(rootpart):
                assert 0, "partition 2 in device %r failed to be created"%voldev

        try: output = check_output(["blkid", "-c", "/dev/null", bootpart])
        except subprocess.CalledProcessError: output = ""
        if 'TYPE="ext4"' not in output:
            check_call(["mkfs.ext4", "-L", poolname + "_boot", bootpart])
        bootpartuuid = check_output(["blkid", "-c", "/dev/null", bootpart, "-o", "value", "-s", "UUID"]).strip()

        if lukspassword:
            needsdoing = False
            try:
                output = check_output(["blkid", "-c", "/dev/null", rootpart])
                rootuuid = re.findall(' UUID="(.*?)"', output)[0]
                luksuuid = "luks-" + rootuuid
            except IndexError:
                needsdoing = True
            except subprocess.CalledProcessError, e:
                if e.returncode != 2: raise
                needsdoing = True
            if needsdoing:
                luksopts = shlex.split(luksoptions) if luksoptions else []
                cmd = ["cryptsetup", "-y", "-v", "luksFormat"] + luksopts + [rootpart, '-']
                proc = Popen(cmd, stdin=subprocess.PIPE)
                proc.communicate(lukspassword)
                retcode = proc.wait()
                if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
                output = check_output(["blkid", "-c", "/dev/null", rootpart])
                rootuuid = re.findall(' UUID="(.*?)"', output)[0]
                luksuuid = "luks-" + rootuuid
            if not os.path.exists(j("/dev","mapper",luksuuid)):
                cmd = ["cryptsetup", "-y", "-v", "luksOpen", rootpart, luksuuid]
                proc = Popen(cmd, stdin=subprocess.PIPE)
                proc.communicate(lukspassword)
                retcode = proc.wait()
                if retcode != 0: raise subprocess.CalledProcessError(retcode,cmd)
            to_luks_close.append(luksuuid)
            rootpart = j("/dev","mapper",luksuuid)

        rootmountpoint = j(workdir, poolname)
        try:
            check_call(["zfs", "list", "-H", "-o", "name", poolname],
                                stdout=file(os.devnull,"w"))
        except subprocess.CalledProcessError, e:
            try:
                check_call(["zpool", "import", "-f",
                                    "-R", rootmountpoint,
                                    poolname])
            except subprocess.CalledProcessError, e:
                check_call(["zpool", "create", "-m", "none",
                                    "-o", "ashift=12",
                                    "-O", "compression=on",
                                    "-O", "atime=off",
                                    "-O", "com.sun:auto-snapshot=false",
                                    "-R", rootmountpoint,
                                    poolname, rootpart])
        to_export.append(poolname)

        try:
            check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT")],
                                stdout=file(os.devnull,"w"))
        except subprocess.CalledProcessError, e:
            check_call(["zfs", "create", j(poolname, "ROOT")])

        try:
            check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "ROOT", "os")],
                                stdout=file(os.devnull,"w"))
        except subprocess.CalledProcessError, e:
            check_call(["zfs", "create", "-o", "mountpoint=/", j(poolname, "ROOT", "os")])
        to_unmount.append(rootmountpoint)

        try:
            check_call(["zfs", "list", "-H", "-o", "name", j(poolname, "swap")],
                                stdout=file(os.devnull,"w"))
        except subprocess.CalledProcessError, e:
            check_call(["zfs", "create", "-V", "%dM"%swapsize, "-b", "4K", j(poolname, "swap")])
            check_call(["zfs", "set", "compression=gzip-9", j(poolname, "swap")])
            check_call(["zfs", "set", "com.sun:auto-snapshot=false", j(poolname, "swap")])
        swappart = os.path.join("/dev/zvol", poolname, "swap")

        try: output = check_output(["blkid", "-c", "/dev/null", swappart])
        except subprocess.CalledProcessError: output = ""
        if 'TYPE="swap"' not in output:
            try:
                check_call(["mkswap", '-f', swappart])
            except subprocess.CalledProcessError, e:
                raise ZFSMalfunction("ZFS does not appear to create the device nodes for zvols.  If you installed ZFS from source, pay attention that the --with-udevdir= configure parameter is correct.")

        p = lambda withinchroot: j(rootmountpoint, withinchroot.lstrip(os.path.sep))

        # mount virtual file systems, creating their mount points as necessary
        for m in "boot sys proc etc".split():
            if not os.path.isdir(p(m)): os.mkdir(p(m))

        if not os.path.ismount(p("boot")):
            check_call(["mount", bootpart, p("boot")])
        to_unmount.append(p("boot"))

        if not os.path.ismount(p("sys")):
            check_call(["mount", "-t", "sysfs", "sysfs", p("sys")])
        to_unmount.append(p("sys"))

        if not os.path.ismount(p(j("sys", "fs", "selinux"))):
            check_call(["mount", "-t", "selinuxfs", "selinuxfs", p(j("sys", "fs", "selinux"))])
        to_unmount.append(p(j("sys", "fs", "selinux")))

        if not os.path.ismount(p("proc")):
            check_call(["mount", "-t", "proc", "proc", p("proc")])
        to_unmount.append(p("proc"))

        # sync device files
        check_call(["rsync", "-ax", "--numeric-ids",
#                            "--exclude=mapper",
                            "--exclude=zvol",
#                            "--exclude=disk",
                            "--exclude=sd*",
#                            "--exclude=zd*",
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
/dev/zvol/%s/swap swap swap discard 0 0
'''%(poolname, bootpartuuid, poolname)
        file(p(j("etc", "fstab")),"w").write(fstab)

        # create a number of important files
        if not os.path.exists(p(j("etc", "mtab"))):
            os.symlink("../proc/self/mounts", p(j("etc", "mtab")))
        if not os.path.isfile(p(j("etc", "resolv.conf"))):
            file(p(j("etc", "resolv.conf")),"w").write(file(j("/etc", "resolv.conf")).read())
        if not os.path.exists(p(j("etc", "hostname"))):
            file(p(j("etc", "hostname")),"w").write(hostname)
        if not os.path.exists(p(j("etc", "hostid"))):
            randomness = file("/dev/urandom").read(4)
            file(p(j("etc", "hostid")),"w").write(randomness)
        if not os.path.exists(p(j("etc", "locale.conf"))):
            file(p(j("etc", "locale.conf")),"w").write("LANG=en_US.UTF-8\n")
        hostid = file(p(j("etc", "hostid"))).read().encode("hex")
        hostid = "%s%s%s%s"%(hostid[6:8],hostid[4:6],hostid[2:4],hostid[0:2])

        if lukspassword:
            crypttab = \
'''%s UUID=%s none discard
'''%(luksuuid,rootuuid)
            file(p(j("etc", "crypttab")),"w").write(crypttab)
            os.chmod(p(j("etc", "crypttab")), 0600)

        def in_chroot(lst):
            return ["chroot", rootmountpoint] + lst

        pkgmgr = ChrootPackageManager(rootmountpoint, releasever, yum_cachedir_path)

        # install base packages
        packages = "basesystem rootfiles bash nano binutils rsync NetworkManager rpm vim-minimal e2fsprogs passwd pam net-tools cryptsetup kbd-misc kbd policycoreutils selinux-policy-targeted".split()
        if releasever >= 21:
            packages.append("dnf")
        else:
            packages.append("yum")
        # install initial boot packages
        packages = packages + "grub2 grub2-tools grubby".split()
        pkgmgr.ensure_packages_installed(packages, method='out_of_chroot')

        # omit zfs modules when dracutting
        if not os.path.exists(p("usr/bin/dracut.real")):
            check_call(in_chroot(["mv", "/usr/bin/dracut", "/usr/bin/dracut.real"]))
            file(p("usr/bin/dracut"), "w").write("""#!/bin/bash

echo NOT Executing fake dracut that omits ZFS dracut modules >&2
exit 0
exec /usr/bin/dracut.real -o "zfs zfsexpandknowledge zfssystemd" "$@"
""")
            os.chmod(p("usr/bin/dracut"), 0755)

        if lukspassword:
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
            if retcode != 0: raise subprocess.CalledProcessError(retcode, [cmd])

        deploy_zfs_in_machine(p=p,
                              in_chroot=in_chroot,
                              pkgmgr=pkgmgr,
                              prebuilt_rpms_path=prebuilt_rpms_path,
                              break_before=break_before,
                              to_unmount=to_unmount,
                              to_rmdir=to_rmdir,)

        # check for stage stop
        if break_before == "reload_chroot":
            raise BreakingBefore(break_before)

        # release disk space now that installation is done
        for pkgm in ('dnf', 'yum'):
            for directory in ("cache", "lib"):
                delete_contents(p(j("var", directory, pkgm)))

        check_call(['sync'])
        # workaround for blkid failing without the following block happening first
        for fs in ["boot", j("sys", "fs", "selinux"), "sys", "proc"]:
            fs = p(fs)
            umount(fs)
            to_unmount.remove(fs)

        umount(rootmountpoint)
        to_unmount.remove(rootmountpoint)

        check_call(["zpool", "export", poolname])
        to_export.remove(poolname)

        check_call(["zpool", "import", "-f",
                    "-R", rootmountpoint,
                    poolname])
        to_export.append(poolname)
        to_unmount.append(rootmountpoint)

        check_call(["mount", bootpart, p("boot")])
        to_unmount.append(p("boot"))

        check_call(["mount", "-t", "sysfs", "sysfs", p("sys")])
        to_unmount.append(p("sys"))

        check_call(["mount", "-t", "proc", "proc", p("proc")])
        to_unmount.append(p("proc"))

        if os.path.exists(p("usr/bin/dracut.real")):
            check_call(in_chroot(["mv", "/usr/bin/dracut.real", "/usr/bin/dracut"]))
        def get_kernel_initrd():
            if os.path.isdir(p(j("boot", "loader"))):
                kernel = glob.glob(p(j("boot", "*", "*", "linux")))[0]
                try:
                    initrd = glob.glob(p(j("boot", "*", "*", "initrd")))[0]
                except IndexError:
                    initrd = None
            else:
                kernel = glob.glob(p(j("boot", "vmlinuz-*")))[0]
                try:
                    initrd = glob.glob(p(j("boot", "initramfs-*")))[0]
                except IndexError:
                    initrd = None
            return kernel, initrd
        kernel, initrd = get_kernel_initrd()
        if initrd:
            mayhapszfsko = check_output(["lsinitrd", initrd])
        else:
            mayhapszfsko = ""
        # At this point, we regenerate the initrds.
        if "zfs.ko" not in mayhapszfsko:
            check_call(in_chroot(["dracut", "--no-hostonly", "-fv", "--regenerate-all"]))

        # Kill the resolv.conf file written only to install packages.
        if os.path.isfile(p(j("etc", "resolv.conf"))):
            os.unlink(p(j("etc", "resolv.conf")))

        # check for stage stop
        if break_before == "install_bootloader":
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
    ln -sf /proc/self/mounts /etc/mtab
    grub2-install /dev/sda
    grub2-mkconfig -o /boot/grub2/grub.cfg
    zfs inherit com.sun:auto-snapshot "%s"
    umount /boot
    rm -f /installbootloader
    sync
    sync
    echo 1 > /proc/sys/kernel/sysrq
    echo o > /proc/sysrq-trigger
    sleep 5
    echo b > /proc/sysrq-trigger
    sleep 5
    echo cannot power off VM.  Please kill qemu.
    '''%(poolname,)
        bootloaderpath = p("installbootloader")
        bootloader = file(bootloaderpath,"w")
        bootloader.write(bootloadertext)
        bootloader.close()
        os.chmod(bootloaderpath, 0755)

        # copy necessary boot files to a temp dir
        try:
            if os.path.ismount("/dev/shm"):
                kerneltempdir = tempfile.mkdtemp(dir="/dev/shm")
            else:
                kerneltempdir = tempfile.mkdtemp()
            kernel, initrd = get_kernel_initrd()
            shutil.copy2(kernel, kerneltempdir)
            shutil.copy2(initrd, kerneltempdir)
        except (KeyboardInterrupt, Exception):
            shutil.rmtree(kerneltempdir)
            raise

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
            logging.info("Cleaning up now")
            cleanup()
        raise

    cleanup()

    # install bootloader using qemu
    vmuuid = str(uuid.uuid1())
    emucmd, emuopts = detect_qemu(force_kvm)
    if '-enable-kvm' in emuopts:
        proper_timeout = qemu_timeout
    else:
        proper_timeout = qemu_timeout * qemu_full_emulation_factor
        logging.warning("No hardware (KVM) emulation available.  The next step is going to take a while.")
    dracut_cmdline = ("rd.info rd.debug rd.udev.debug systemd.show_status=1 "
                      "systemd.log_target=console systemd.log_level=debug")
    if interactive_qemu:
        screenmode = [
            "-nographic"
            "-monitor","none",
            "-chardev","stdio,id=char0",
        ]
    else:
        screenmode = [
            "-nographic",
            "-monitor","none",
            "-chardev","stdio,id=char0",
            "-chardev","file,id=char1,path=/dev/stderr",
            "-serial","chardev:char0",
            "-mon","char1,mode=control,default",
        ]
    if lukspassword:
        luks_cmdline = "rd.luks.uuid=%s rd.luks.key=/key "%(rootuuid,)
    else:
        luks_cmdline = ""
    cmdline = '%s %s console=ttyS0 root=ZFS=%s/ROOT/os ro init=/installbootloader' % (
        dracut_cmdline,
        luks_cmdline,
        poolname
    )
    cmd = [
        emucmd,
        ] + screenmode + [
        "-name", hostname,
        "-M", "pc-1.2",
        "-no-reboot",
        '-m', '256',
        '-uuid', vmuuid,
        "-kernel", os.path.join(kerneltempdir, os.path.basename(kernel)),
        '-initrd', os.path.join(kerneltempdir, os.path.basename(initrd)),
        '-append', cmdline,
    ]
    cmd = cmd + emuopts
    if original_bootdev:
        cmd.extend([
            '-drive', 'file=%s,if=none,id=drive-ide0-0-0,format=raw'%original_bootdev,
            '-device', 'ide-hd,bus=ide.0,unit=0,drive=drive-ide0-0-0,id=ide0-0-0,bootindex=1',
        ])
    cmd.extend([
        '-drive', 'file=%s,if=none,id=drive-ide0-0-1,format=raw' % original_voldev,
        '-device', 'ide-hd,bus=ide.0,unit=1,drive=drive-ide0-0-1,id=ide0-0-1,bootindex=2',
    ])

    # check for stage stop
    if break_before == "boot_bootloader":
        logging.info(
            "qemu process that would execute now: %s" % " ".join([
                pipes.quote(s) for s in cmd
            ])
        )
        logging.info("After using this command, remember to manually clean up files in %s" % kerneltempdir)
        raise BreakingBefore(break_before)

    def babysit(popenobject, timeout):
        for _ in xrange(timeout):
            if popenobject.returncode is not None:
                return
            time.sleep(1)
        logging.error("QEMU babysitter is killing stubborn qemu process after %s seconds", timeout)
        popenobject.kill()

    if interactive_qemu:
        stdin, stdout, stderr = (None, None, None)
        driver = None
        vmiomaster = None
    else:
        vmiomaster, vmioslave = pty.openpty()
        vmiomaster, vmioslave = os.fdopen(vmiomaster, "a+b"), os.fdopen(vmioslave, "rw+b")
        stdin, stdout, stderr = (vmioslave, vmioslave, vmioslave)
        logging.info("Creating a new BootDriver thread to input the passphrase if needed")
        driver = BootDriver(lukspassword if lukspassword else "", vmiomaster)

    machine_powered_off_okay = True if interactive_qemu else False
    try:
        qemu_process = Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, close_fds=True)
        vmioslave.close() # After handing it off.
        if not interactive_qemu:
            babysitter = threading.Thread(target=babysit, args=(qemu_process, proper_timeout))
            babysitter.setDaemon(True)
            babysitter.start()
        if driver:
            driver.start()
        retcode = qemu_process.wait()
        if vmiomaster:
            vmiomaster.close()
        if retcode == 0:
            pass
        elif not interactive_qemu and retcode == -9:
            raise BootloaderWedged("The bootloader appears wedged.  Check the QEMU boot log for errors or unexpected behavior.")
        else:
            raise subprocess.CalledProcessError(retcode,cmd)
    finally:
        shutil.rmtree(kerneltempdir)
        if driver:
            logging.info("Waiting to join the BootDriver thread")
            driver.join()
            if "reboot: Power down" in driver.get_output():
                machine_powered_off_okay = True
    if not machine_powered_off_okay:
        raise MachineNeverShutoff("The bootable image never shut off.  Check the QEMU boot log for errors or unexpected behavior.")

def test_cmd(cmdname, expected_ret):
    try: subprocess.check_call([cmdname], stdout=file(os.devnull, "w"), stderr=file(os.devnull, "w"))
    except subprocess.CalledProcessError, e:
        if e.returncode == expected_ret: return True
        return False
    except OSError, e:
        if e.errno == 2: return False
        raise
    return True

def test_mkfs_ext4():
    return test_cmd("mkfs.ext4", 1)

def test_zfs():
    return test_cmd("zfs", 2)

def test_flock():
    return test_cmd("flock", 64)

def test_rsync():
    return test_cmd("rsync", 1)

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

def cpuinfo(): return file("/proc/cpuinfo").read()

def detect_qemu(force_kvm=None):
    emucmd = "qemu-system-x86_64"
    emuopts = []
    if force_kvm is False:
        pass
    elif force_kvm is True:
       emucmd = "qemu-kvm"
       emuopts = ['-enable-kvm']
    else:
        if ("vmx" in cpuinfo() or "svm" in cpuinfo()):
           emucmd = "qemu-kvm"
           emuopts = ['-enable-kvm']
    return emucmd, emuopts

def test_qemu():
    try: subprocess.check_call([detect_qemu()[0], "--help"], stdout=file(os.devnull, "w"), stderr=file(os.devnull, "w"))
    except subprocess.CalledProcessError, e:
        if e.returncode == 0: return True
        raise
    except OSError, e:
        if e.errno == 2: return False
        raise
    return True

def install_fedora_on_zfs():
    logging.basicConfig(level=logging.DEBUG, format=BASIC_FORMAT)
    args = get_parser().parse_args()
    if not test_rsync():
        print >> sys.stderr, "error: rsync is not available. Please use your package manager to install rsync."
        return 5
    if not test_zfs():
        print >> sys.stderr, "error: ZFS is not installed properly. Please install https://github.com/Rudd-O/spl and then https://github.com/Rudd-O/zfs.  If installing from source, pay attention to the --with-udevdir= configure parameter and don't forget to run ldconfig after the install."
        return 5
    if not test_mkfs_ext4():
        print >> sys.stderr, "error: mkfs.ext4 is not installed properly. Please install e2fsprogs."
        return 5
    if not test_cryptsetup():
        print >> sys.stderr, "error: cryptsetup is not installed properly. Please install cryptsetup."
        return 5
    if not test_yum():
        print >> sys.stderr, "error: could not find either yum or DNF. Please use your package manager to install yum or DNF."
        return 5
    if args.yum_cachedir and not test_flock():
        print >> sys.stderr, "error: flock is not installed properly, but it is necessary to safely use --yum-cachedir. Please use your package manager to install flock (specifically, util-linux)."
        return 5
    if not test_qemu():
        print >> sys.stderr, "error: qemu-system-x86_64 is not installed properly. Please use your package manager to install QEMU (specifically, qemu-system-x86)."
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


def deploy_zfs_in_machine(p, in_chroot, pkgmgr,
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
        if os.path.ismount(target_rpms_path):
            if os.stat(prebuilt_rpms_path).st_ino != os.stat(target_rpms_path):
                umount(target_rpms_path)
                check_call(["mount", "--bind", os.path.abspath(prebuilt_rpms_path), target_rpms_path])
        else:
            check_call(["mount", "--bind", os.path.abspath(prebuilt_rpms_path), target_rpms_path])
        if os.path.isdir(target_rpms_path):
            to_rmdir.append(target_rpms_path)
        if os.path.ismount(target_rpms_path):
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
            "spl",
            (
                "spl-*.%s.rpm" % arch,
                "spl-dkms-*.noarch.rpm",
            ),
            ('spl', 'spl-dkms'),
            [
                "make", "autoconf", "automake", "gcc", "libtool", "git",
                "rpm-build", "dkms",
            ],
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
                "zfs-dracut-*.%s.rpm" % arch,
            ),
            ('zfs', 'zfs-dkms', 'zfs-dracut'),
            [
                "zlib-devel", "libuuid-devel", "bc", "libblkid-devel",
                "libattr-devel", "lsscsi", "mdadm", "parted",
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
    modules_dir = p(j("usr", "lib", "modules", "*", "*", "zfs.ko"))
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
                              break_before=None,
                              to_rmdir=to_rmdir,
                              to_unmount=to_unmount,)
    except BaseException, e:
        logging.exception("Unexpected error")
        if not args.nocleanup:
            logging.info("Cleaning up now")
            cleanup()
        raise

    cleanup()
