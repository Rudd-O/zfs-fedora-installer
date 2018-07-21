#!/usr/bin/env python

import argparse
import contextlib
import fcntl
import os
from os.path import join as j
import pipes
import re
import tempfile
import logging
import sys
import subprocess

from installfedoraonzfs.cmd import check_output, bindmount, umount, ismount, makedirs, lockfile
from installfedoraonzfs import cmd as cmdmod
import installfedoraonzfs.retry as retrymod


class TemporaryRpmdbCorruptionError(retrymod.Retryable, subprocess.CalledProcessError): pass


class DownloadFailed(retrymod.Retryable, subprocess.CalledProcessError): pass


def check_call_retry_rpmdberror(cmd):
    out, ret = cmdmod.get_output_exitcode(cmd)
    if ret == 1 and "Rpmdb checksum is invalid" in out:
        raise TemporaryRpmdbCorruptionError(ret, cmd)
    elif ret != 0 and "--downloadonly" in cmd:
        raise DownloadFailed(ret, cmd)
    elif ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)
    return out, ret


options_retries = ((["--downloadonly"], 5), ([], 1))


logger = logging.getLogger("PM")


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
    tempyumconfig.write(fedora_repos_template)
    tempyumconfig.flush()
    tempyumconfig.seek(0)
    return tempyumconfig


@contextlib.contextmanager
def dummylock():
    yield


class ChrootPackageManager(object):

    chroot = None
    cachedir = None

    cachemounts = None
    pkgmgr_config = None

    def __init__(self, chroot, releasever, cachedir=None):
        self.releasever = releasever
        self.myreleasever = self.get_my_releasever()
        self.chroot = chroot
        self.cachemounts = []
        self.cachedir = None if cachedir is None else os.path.abspath(cachedir)

    @staticmethod
    def get_my_releasever():
        return int(check_output(["rpm", "-q", "fedora-release", "--queryformat=%{version}"]))

    def grab_pm(self, method):
        if self.cachemounts or self.pkgmgr_config:
            assert 0, "programming error, invalid state, cannot enter without exiting first"

        if method == "in_chroot":
            dirforconfig = self.chroot
            if os.path.isfile(j(self.chroot, "etc", "dnf", "dnf.conf")):
                sourceconf = j(self.chroot, "etc", "dnf", "dnf.conf")
                pkgmgr = "dnf"
            elif os.path.isfile(j(self.chroot, "etc", "yum.conf")):
                sourceconf = j(self.chroot, "etc", "yum.conf")
                pkgmgr = "yum"
            else:
                raise Exception("Cannot use in_chroot method without a working yum or DNF inside the chroot")
            ver = self.releasever
        elif method == "out_of_chroot":
            dirforconfig = os.getenv("TMPDIR") or "/tmp"
            if os.path.exists("/etc/dnf/dnf.conf"):
                sourceconf = "/etc/dnf/dnf.conf"
                pkgmgr = "dnf"
            elif os.path.exists("/etc/yum.conf"):
                sourceconf = "/etc/yum.conf"
                pkgmgr = "yum"
            else:
                raise Exception("Cannot use out_of_chroot method without a working yum or DNF installed on your system")
            ver = self.myreleasever
        else:
            assert 0, "method unknown: %r" % method

        parms = dict(
            source=sourceconf,
            directory=dirforconfig,
            logfile="/dev/null",
            debuglevel=2,
            reposdir="/nonexistent",
            include=None,
            keepcache=1 if pkgmgr == "yum" else True,
        )

        # /yumcache
        if self.cachedir:
            makedirs([self.cachedir])
            with lockfile(j(self.cachedir, "ifz-lockfile")):
                # /yumcache/(dnf|yum)/(ver)/(lib|cache)
                persistdir = j(self.cachedir, pkgmgr, str(ver), "lib")
                cachedir   = j(self.cachedir, pkgmgr, str(ver), "cache")
                makedirs([persistdir, cachedir])
                # /yumcache/(dnf|yum)/(ver)/lock
                lock = lockfile(j(self.cachedir, pkgmgr, str(ver), "lock"))
                # /chroot/var/(lib|cache)/(dnf|yum)
                persistin, cachein = makedirs([
                    j(self.chroot, "tmp-%s-%s" % (pkgmgr, x))
                    for x in ["lib", "cache"]
                ])
                maybemounted = [persistin, cachein]
                while maybemounted:
                    while ismount(maybemounted[-1]):
                        logger.debug("Preemptively unmounting %s", maybemounted[-1])
                        umount(maybemounted[-1])
                    maybemounted.pop()
                for x, y in ([persistdir, persistin], [cachedir, cachein]):
                    logger.debug("Mounting %s to %s", x, y)
                    self.cachemounts.append(bindmount(x, y))
            # /var/(lib|cache)/(dnf|yum)
            parms["persistdir"] = persistin[len(self.chroot):]
            parms["cachedir"] = cachein[len(self.chroot):]
        else:
            lock = dummylock()

        self.pkgmgr_config = make_temp_yum_config(**parms)
        return pkgmgr, self.pkgmgr_config, lock

    def ungrab_pm(self, *ignored, **kwignored):
        if self.cachedir:
            with lockfile(j(self.cachedir, "ifz-lockfile")):
                while self.cachemounts:
                    while ismount(self.cachemounts[-1]):
                        logger.debug("Unmounting %s", self.cachemounts[-1])
                        umount(self.cachemounts[-1])
                    os.rmdir(self.cachemounts[-1])
                    self.cachemounts.pop()
        if self.pkgmgr_config:
            self.pkgmgr_config.close()
            self.pkgmgr_config = None

    def ensure_packages_installed(self, packages, method="in_chroot"):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        pkgmgr, config, lock = self.grab_pm(method)
        try:
            with lock:
                try:
                    cmdmod.check_call_no_output(in_chroot(["rpm", "-q"] + packages))
                    logger.info("All required packages are available")
                    return
                except subprocess.CalledProcessError:
                    pass
                for option, retries in options_retries:
                    logger.info("Installing packages %s %s: %s", option, method, ", ".join(packages))
                    cmd = (
                        ([pkgmgr] if method == "out_of_chroot" else in_chroot([pkgmgr]))
                        + ["install", "-qy", "--disableplugin=*qubes*"]
                        + (["-c", config.name if method == "out_of_chroot" else config.name[len(self.chroot):]])
                        + option
                        + (['--installroot=%s' % self.chroot,
                            '--releasever=%d' % self.releasever] if method == "out_of_chroot" else [])
                        + (['--'] if pkgmgr == "yum" else [])
                        + packages
                    )
                    out, ret = retrymod.retry(retries)(check_call_retry_rpmdberror)(cmd)
                return out, ret
        finally:
            self.ungrab_pm()

    def install_local_packages(self, packages):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        packages = [ os.path.abspath(p) for p in packages ]
        for package in packages:
            if not os.path.isfile(package):
                raise Exception("package file %r does not exist" % package)
            if not package.startswith(self.chroot + os.path.sep):
                raise Exception("package file %r is not within the chroot" % package)

        pkgmgr, config, lock = self.grab_pm("in_chroot")
        try:
            with lock:
                # always happens in chroot
                # packages must be a list of paths to RPMs valid within the chroot
                for option, retries in options_retries:
                    logger.info("Installing packages %s: %s", option, ", ".join(packages))
                    cmd = in_chroot(
                        [pkgmgr]
                        + (['localinstall'] if pkgmgr == "yum" else ['install'])
                        + ['-qy']
                        + option
                        + ['-c', config.name[len(self.chroot):]]
                        + (['--'] if pkgmgr == "yum" else [])
                    ) + [ p[len(self.chroot):] for p in packages ]
                    out, ret = retrymod.retry(retries)(check_call_retry_rpmdberror)(cmd)
                return out, ret
        finally:
            self.ungrab_pm()


class SystemPackageManager(object):

    def __init__(self):
        if os.path.exists("/etc/dnf/dnf.conf"):
            self.strategy = "dnf"
        else:
            self.strategy = "yum"

    def ensure_packages_installed(self, packages, method="in_chroot"):
        logger.info("Checking packages are available: %s", packages)
        try:
            cmdmod.check_call_no_output(["rpm", "-q"] + packages)
            logger.info("All required packages are available")
            return
        except subprocess.CalledProcessError:
            pass
        for option, retries in options_retries:
            logger.info("Installing packages %s %s: %s", option, method, ", ".join(packages))
            cmd = (
                [self.strategy, 'install', '-qy']
                + option
                + (['--'] if self.strategy == "yum" else [])
                + packages
            )
            out, ret = retrymod.retry(retries)(check_call_retry_rpmdberror)(cmd)
        return out, ret

    def install_local_packages(self, packages):
        def in_chroot(lst):
            return ["chroot", self.chroot] + lst

        packages = [ os.path.abspath(p) for p in packages ]
        for package in packages:
            if not os.path.isfile(package):
                raise Exception("package file %r does not exist" % package)
        for option, retries in options_retries:
            logger.info("Installing packages %s: %s", option, ", ".join(packages))
            cmd = (
                [self.strategy]
                + (['localinstall'] if pkgmgr == "yum" else ['install'])
                + ['-qy']
                + (['--'] if pkgmgr == "yum" else [])
                + packages
            )
            out, ret = retrymod.retry(retries)(check_call_retry_rpmdberror)(cmd)
        return out, ret


def get_parser():
    parser = argparse.ArgumentParser(
        description="Install RPM packages in a chroot"
    )
    parser.add_argument(
        "--cachedir", dest="cachedir",
        action="store", default=None, help="directory to use for a yum cache that persists across executions"
    )
    parser.add_argument(
        "--method", dest="method",
        action="store", default="out_of_chroot", help="which method to use (in_chroot or out_of_chroot)"
    )
    parser.add_argument(
        "--releasever", dest="releasever", metavar="VER", type=int,
        action="store", default=None, help="Fedora release version (default the same as the computer you are installing on)"
    )
    parser.add_argument(
        "chroot", metavar="CHROOT",
        action="store", help="where to install the packages",
    )
    parser.add_argument(
        "packages", metavar="PACKAGES",
        action="store", help="which packages to install", nargs='+',
    )
    return parser


def deploypackagesinchroot():
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("shell").setLevel(logging.INFO)
    args = get_parser().parse_args()
    if args.method not in ["in_chroot", "out_of_chroot"]:
        print >> sys.stderr, "error: method must be one of in-chroot or out-of-chroot."
        return os.EX_USAGE
    releasever = args.releasever if args.releasever else ChrootPackageManager.get_my_releasever()
    pkgmgr = ChrootPackageManager(args.chroot, releasever, args.cachedir)
    pkgmgr.ensure_packages_installed(args.packages, args.method)
