"""Package manager utilities."""

import contextlib
import errno
import logging
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
from typing import Any, Generator, Literal, Protocol, cast

from installfedoraonzfs import cmd as cmdmod
import installfedoraonzfs.retry as retrymod

_LOGGER = logging.getLogger(__name__)

DNF_DOWNLOAD_THEN_INSTALL: tuple[list[str], list[str]] = (
    ["--downloadonly"],
    [],
)


def _run_with_retries(cmd: list[str]) -> tuple[str, int]:
    r = retrymod.retry(2)
    return r(lambda: _check_call_detect_retryable_errors(cmd))()  # type: ignore


class ChrootBootstrapper(Protocol):
    """Protocol for a package manager that supports bootstrap of a chroot."""

    def bootstrap_packages(self) -> None:
        """Install a minimal set of packages for a shell."""
        ...

    def setup_kernel_bootloader(self) -> None:
        """Set up a kernel and a bootloader."""
        ...


class SupportsDownloadablePackageInstall(Protocol):
    """Protocol for a package manager that supports distro package installation."""


class PackageManager(Protocol):
    """Protocol for a package manager that supports universal package installation."""

    def ensure_packages_installed(self, package_specs: list[str]) -> None:
        """Install a list of packages from the distro.  Download them first."""
        ...

    def install_local_packages(self, package_files: list[Path]) -> None:
        """Install a list of local packages.  Download dependencies first."""
        ...


class ChrootPackageManager(PackageManager, Protocol):
    """Protocol for a package manager that can manage a chroot."""

    chroot: Path


class OSPackageManager(PackageManager, Protocol):
    """Protocol for a package manager that can manage an OS."""

    chroot: None


class DownloadFailed(retrymod.Retryable, subprocess.CalledProcessError):
    """Package download failed."""

    def __str__(self) -> str:
        """Stringify the exception."""
        return f"DNF download {self.cmd} failed with {self.returncode}: {self.output}"


class PluginSelinuxRetryable(retrymod.Retryable, subprocess.CalledProcessError):
    """Retryable SELinux plugin failure."""


def _check_call_detect_retryable_errors(cmd: list[str]) -> tuple[str, int]:
    out, ret = cmdmod.get_output_exitcode(cmd)
    if ret != 0:
        if "--downloadonly" in cmd:
            raise DownloadFailed(ret, cmd, output=out)
        if "error: Plugin selinux" in out:
            raise PluginSelinuxRetryable(ret, cmd, output=out)
        _LOGGER.error("This is not a retryable error, it should not be retried.")
        raise subprocess.CalledProcessError(ret, cmd, output=out)
    return out, ret


class LocalFedoraPackageManager:
    """Package manager that can install packages locally on Fedora systems."""

    chroot = None

    def __init__(self) -> None:
        """Initialize the package manager."""
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def install_local_packages(self, package_files: list[Path]) -> None:
        """Install a list of local packages on a Fedora system.  Download them first."""

        packages = [os.path.abspath(p) for p in package_files]
        for package in packages:
            if not os.path.isfile(package):
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), package
                )
        return self.ensure_packages_installed(packages)

    def ensure_packages_installed(self, package_names: list[str]) -> None:
        """Install a list of packages on a Fedora system.  Download them first."""

        for option in DNF_DOWNLOAD_THEN_INSTALL:
            self._logger.info(
                "Installing packages %s: %s", option, ", ".join(package_names)
            )
            cmd = (["dnf", "install"]) + ["-y"] + package_names
            _run_with_retries(cmd)


class LocalQubesOSPackageManager:
    """Package manager that can install packages locally on Qubes OS systems."""

    chroot = None

    def __init__(self) -> None:
        """Initialize the package manager."""
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def install_local_packages(self, package_files: list[Path]) -> None:
        """Install a list of local packages on a Qubes OS system.

        Installation is two-phase.  First, take all dependencies the package
        files need, and install these.  Then install the packages themselves.
        """
        packages = [os.path.abspath(p) for p in package_files]
        for package in packages:
            if not os.path.isfile(package):
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), package
                )

        deps: set[str] = set()
        cmd = ["rpm", "-q", "--requires"]
        for d in cmdmod.check_output(cmd + packages).splitlines():
            if d and not d.startswith("rpmlib("):
                deps.add(d)
        if deps:
            self._logger.info("First phase: installing dependencies: %s", deps)
            self.ensure_packages_installed(list(deps))

        self._logger.info(
            "Second phase: installing package files: %s", ", ".join(packages)
        )
        cmd = (["dnf", "install"]) + ["-y"] + packages
        _run_with_retries(cmd)

    def ensure_packages_installed(self, package_names: list[str]) -> None:
        """Install a list of packages on a Qubes OS system."""

        self._logger.info("Installing packages: %s", ", ".join(package_names))
        cmd = [
            "qubes-dom0-update",
            "--action=install",
            "--console",
            "-y",
        ] + package_names
        _run_with_retries(cmd)


class ChrootFedoraPackageManagerAndBootstrapper:
    """Package manager that can bootstrap and install packages on a Fedora chroot."""

    def __init__(self, releasever: str, chroot: Path, cachedir: Path | None):
        """Initialize the chroot package manager."""
        if chroot.absolute() == chroot.absolute().parent:
            assert 0, f"cannot use the root directory ({chroot}) as chroot"
        if cachedir and (cachedir.absolute() == cachedir.absolute().parent):
            assert 0, f"cannot use the root directory ({chroot}) as cache directory"

        self.chroot = chroot.absolute()
        self.releasever = releasever
        self._logger = logging.getLogger(f"{__name__}.{{self.__class__.__name__}}")
        self._cachedir = cachedir.absolute() if cachedir else None

    def bootstrap_packages(self) -> None:
        """Bootstrap the chroot."""

        def get_base_packages() -> list[str]:
            """Get packages to be installed from outside the chroot."""
            packages = (
                "filesystem basesystem setup rootfiles bash rpm passwd pam"
                " util-linux rpm dnf"
            ).split()
            return packages

        def get_in_chroot_packages() -> list[str]:
            """Get packages to be installed in the chroot phase."""
            pkgs = (
                "e2fsprogs nano binutils rsync coreutils"
                " vim-minimal net-tools"
                " cryptsetup kbd-misc kbd policycoreutils selinux-policy-targeted"
                " libseccomp sed pciutils kmod dracut"
                " grub2 grub2-tools grubby efibootmgr"
            ).split()
            e = pkgs.extend
            e("shim-x64 grub2-efi-x64 grub2-efi-x64-modules".split())
            e(["sssd-client"])
            e(["systemd-networkd"])
            return pkgs

        self._logger.info("Installing basic packages into chroot.")
        packages = get_base_packages()
        self._ensure_packages_installed(packages, method="out_of_chroot")
        self._logger.info("Installing more packages within chroot.")
        chroot_packages = get_in_chroot_packages()
        self._ensure_packages_installed(chroot_packages, method="out_of_chroot")

    def setup_kernel_bootloader(self) -> None:
        """Install the kernel and the bootloader into the chroot."""
        p = (
            "kernel kernel-headers kernel-modules "
            "kernel-devel dkms grub2 grub2-efi".split()
        )
        self._ensure_packages_installed(
            p,
            method="out_of_chroot",
            extra_args=["--setopt=install_weak_deps=true"],
        )

    def ensure_packages_installed(self, package_names: list[str]) -> None:
        """Install packages by name within the chroot."""
        self._ensure_packages_installed(package_names, method="out_of_chroot")

    def install_local_packages(self, package_files: list[Path]) -> None:
        """Install a list of local packages on a Fedora system.  Download them first."""

        packages = [os.path.abspath(p) for p in package_files]
        for package in packages:
            if not os.path.isfile(package):
                raise FileNotFoundError(
                    errno.ENOENT, os.strerror(errno.ENOENT), package
                )
        return self._ensure_packages_installed(
            [str(s) for s in packages], method="out_of_chroot"
        )

    @contextlib.contextmanager
    def _method(
        self, method: Literal["in_chroot"] | Literal["out_of_chroot"]
    ) -> Generator[Path, None, None]:
        pkgmgr = "dnf"
        guestver = self.releasever
        if method == "in_chroot":
            dirforconfig = self.chroot
            sourceconf = (
                (self.chroot / "/etc/dnf/dnf.conf")
                if os.path.isfile(self.chroot / "/etc/dnf/dnf.conf")
                else Path("/etc/dnf/dnf/conf")
            )
        else:
            dirforconfig = Path(os.getenv("TMPDIR") or "/tmp")  # noqa: S108
            sourceconf = Path("/etc/dnf/dnf.conf")

        parms = {
            "logfile": "/dev/null",
            "debuglevel": 2,
            "reposdir": "/nonexistent",
            "include": None,
            "max_parallel_downloads": 10,
            "keepcache": True,
            "install_weak_deps": False,
        }

        # /yumcache
        if not self._cachedir:
            n = self._make_temp_yum_config(sourceconf, dirforconfig, **parms)
            try:
                yield n.name
            finally:
                n.close()
                del n

        else:
            cmdmod.makedirs([self._cachedir])
            with cmdmod.lockfile(self._cachedir / "ifz-lockfile"):
                hostarch = platform.machine()
                guestarch = (
                    hostarch  # FIXME: at some point will we support other arches?
                )
                hostver = cmdmod.get_distro_release_info()["VERSION_ID"]
                # /yumcache/dnf/host-hostver-hostarch/chroot-guestver-guestarch
                # Must be this way because the data in the cachedir follows
                # specific formats that vary from hostver to hostver.
                cachedir = (
                    self._cachedir
                    / pkgmgr
                    / f"host-{hostver}-{hostarch}"
                    / f"chroot-{guestver}-{guestarch}"
                    / "cache"
                )
                cmdmod.makedirs([cachedir])
                # /yumcache/.../lock
                # /chroot/var/cache/dnf

            with cmdmod.lockfile(cachedir / "lock"):
                cachedir_in_chroot = cmdmod.makedirs(
                    [self.chroot / f"tmp-{pkgmgr}-cache"]
                )[0]
                parms["cachedir"] = str(cachedir_in_chroot)[len(str(self.chroot)) :]
                parms["keepcache"] = "true"
                while cmdmod.ismount(cachedir_in_chroot):
                    self._logger.debug("Preemptively unmounting %s", cachedir_in_chroot)
                    cmdmod.umount(cachedir_in_chroot)
                n = None
                cachemount = None
                try:
                    self._logger.debug(
                        "Mounting %s to %s", cachedir, cachedir_in_chroot
                    )
                    cachemount = cmdmod.bindmount(cachedir, cachedir_in_chroot)
                    n = self._make_temp_yum_config(sourceconf, dirforconfig, **parms)
                    self._logger.debug("Created custom dnf configuration %s", n.name)
                    yield n.name
                finally:
                    if n:
                        n.close()
                    del n
                    if cachemount:
                        cmdmod.umount(cachedir_in_chroot)
                    try:
                        with contextlib.suppress(FileNotFoundError):
                            os.rmdir(cachedir_in_chroot)
                    except Exception as e:
                        self._logger.debug(
                            "Ignoring inability to remove %s (%s)",
                            cachedir_in_chroot,
                            e,
                        )

    def _make_temp_yum_config(
        self, source: Path, directory: Path, **kwargs: Any
    ) -> Any:
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
        tempyumconfig = tempfile.NamedTemporaryFile(dir=directory)
        yumconfigtext = cmdmod.readtext(source)
        for optname, optval in list(kwargs.items()):
            if optval is None:
                yumconfigtext, repls = re.subn(
                    f"^ *{optname} *=.*$", "", yumconfigtext, flags=re.M
                )
            else:
                yumconfigtext, repls = re.subn(
                    f"^ *{optname} *=.*$",
                    f"{optname}={optval}",
                    yumconfigtext,
                    flags=re.M,
                )
                if not repls:
                    yumconfigtext, repls = re.subn(
                        "\\[main]", f"[main]\n{optname}={optval}", yumconfigtext
                    )
                    assert repls, (
                        "Could not substitute yum.conf main config section"
                        f" with the {optname} stanza.  Text: {yumconfigtext}"
                    )
        tempyumconfig.write(yumconfigtext.encode("utf-8"))
        tempyumconfig.write(fedora_repos_template.encode("utf-8"))
        tempyumconfig.flush()
        tempyumconfig.seek(0)
        return tempyumconfig

    def _ensure_packages_installed(
        self,
        packages: list[str],
        method: Literal["in_chroot"] | Literal["out_of_chroot"],
        extra_args: Any = None,
    ) -> None:
        more_args = extra_args if extra_args else []

        def in_chroot(lst: list[str]) -> list[str]:
            return ["chroot", str(self.chroot)] + lst

        with self._method(method) as config:
            try:
                cmdmod.check_call_silent(in_chroot(["rpm", "-q"] + packages))
                self._logger.info("All required packages are available")
                return
            except subprocess.CalledProcessError:
                pass

            for option in DNF_DOWNLOAD_THEN_INSTALL:
                self._logger.info(
                    "Installing packages %s %s (extra args: %s): %s",
                    option,
                    method,
                    extra_args,
                    ", ".join(packages),
                )
                cmd = (
                    (["dnf"] if method == "out_of_chroot" else in_chroot(["dnf"]))
                    + ["install", "-y"]
                    + more_args
                    + (
                        [
                            "-c",
                            str(config)
                            if method == "out_of_chroot"
                            else str(config)[len(str(self.chroot)) :],
                        ]
                    )
                    + option
                    + (
                        [
                            "--installroot=%s" % self.chroot,
                            "--releasever=%s" % self.releasever,
                        ]
                        if method == "out_of_chroot"
                        else [
                            "--releasever=%s" % self.releasever,
                        ]
                    )
                    + packages
                )
                _run_with_retries(cmd)


def os_package_manager_factory() -> OSPackageManager:
    """Create a package manager."""
    info = cmdmod.get_distro_release_info()
    distro = info.get("ID")
    if distro == "fedora":
        return LocalFedoraPackageManager()
    elif distro == "qubes":
        releasever = info.get("VERSION_ID")
        if releasever and releasever.zfill(5) < "4.2".zfill(5):
            raise cmdmod.UnsupportedDistributionVersion(info.get("NAME"), releasever)
        return LocalQubesOSPackageManager()
    raise cmdmod.UnsupportedDistribution(info.get("NAME"))


def _chroot_manager_factory(
    chroot: Path,
    cachedir: Path | None,
    releasever: str | None,
    distro: str | None,
    kind: Literal["bootstrapper"] | Literal["packagemanager"],
) -> ChrootBootstrapper | ChrootPackageManager:
    """Create a bootstrapper for a chroot."""
    info = cmdmod.get_distro_release_info()
    if distro is None:
        distro = info.get("ID")
    if releasever is None:
        if distro != info.get("ID"):
            assert (
                0
            ), "Cannot specify a different distro without specifying a releasever"
        releasever = info.get("VERSION_ID")
    assert releasever, f"Your releasever is invalid: {releasever}"

    if distro != "fedora":
        raise cmdmod.UnsupportedDistribution(info.get("NAME"))
    if releasever and releasever.zfill(5) < "37".zfill(5):
        raise cmdmod.UnsupportedDistributionVersion(info.get("NAME"), releasever)

    if kind == "bootstrapper":
        t: ChrootBootstrapper = ChrootFedoraPackageManagerAndBootstrapper(
            releasever, chroot, cachedir
        )
        return t

    elif kind == "packagemanager":
        u: ChrootPackageManager = ChrootFedoraPackageManagerAndBootstrapper(
            releasever, chroot, cachedir
        )
        return u

    assert 0, "not reached"


def chroot_package_manager_factory(
    chroot: Path,
    cachedir: Path | None,
    releasever: str | None,
    distro: str | None,
) -> ChrootPackageManager:
    """Create a bootstrapper for a chroot."""
    return cast(
        ChrootPackageManager,
        _chroot_manager_factory(chroot, cachedir, releasever, distro, "packagemanager"),
    )


def chroot_bootstrapper_factory(
    chroot: Path,
    cachedir: Path | None,
    releasever: str | None,
    distro: str | None,
) -> ChrootBootstrapper:
    """Create a bootstrapper for a chroot."""
    return cast(
        ChrootBootstrapper,
        _chroot_manager_factory(chroot, cachedir, releasever, distro, "bootstrapper"),
    )
