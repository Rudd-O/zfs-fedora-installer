"""Virtual machine tech used to set up a bootable system."""

import errno
import logging
import os
from pathlib import Path
import pty
import subprocess
import threading
import time
from typing import BinaryIO
import uuid

from installfedoraonzfs.cmd import (
    Popen,
    check_call_silent,
    cpuinfo,
    format_cmdline,
    get_associated_lodev,
)
from installfedoraonzfs.retry import Retryable

logger = logging.getLogger("VM")
qemu_full_emulation_factor = 5


class VMSetupException(Exception):
    """Base class for VM exceptions."""


class OOMed(VMSetupException):
    """Out of memory."""


class Panicked(VMSetupException):
    """Kernel panic."""


class SystemdSegfault(Retryable, VMSetupException):
    """Segfault in systemd."""


class MachineNeverShutoff(VMSetupException):
    """Machine timed out waiting to shut off."""


class QEMUDied(subprocess.CalledProcessError, VMSetupException):
    """QEMU died."""


class BadPW(VMSetupException):
    """Bad password."""


class Emergency(VMSetupException):
    """System dropped into emergency."""


QemuOpts = tuple[str, list[str]]


def detect_qemu(force_kvm: bool | None = None) -> QemuOpts:
    """Detect QEMU and what options to use."""
    emucmd = "qemu-system-x86_64"
    emuopts = []
    if force_kvm is False:
        pass
    elif force_kvm is True:
        emucmd = "qemu-kvm"
        emuopts = ["-enable-kvm"]
    elif "vmx" in cpuinfo() or "svm" in cpuinfo():
        emucmd = "qemu-kvm"
        emuopts = ["-enable-kvm"]
    return emucmd, emuopts


def test_qemu() -> bool:
    """Test for the presence of QEMU."""
    try:
        check_call_silent([detect_qemu()[0], "--help"])
    except subprocess.CalledProcessError as e:
        if e.returncode == 0:
            return True
        raise
    except OSError as e:
        if e.errno == errno.ENOENT:
            return False
        raise
    return True


def boot_image_in_qemu(
    hostname: str,
    initparm: str,
    poolname: str,
    voldev: Path,
    bootdev: Path | None,
    kernelfile: Path,
    initrdfile: Path,
    force_kvm: bool | None,
    interactive_qemu: bool,
    lukspassword: str | None,
    rootpassword: str,
    rootuuid: str | None,
    luksuuid: str | None,
    qemu_timeout: int,
    enforcing: bool,
) -> None:
    """Fully boot the Linux image inside QEMU."""
    if voldev:
        lodev = get_associated_lodev(voldev)
        assert not lodev, f"{voldev} still has a loopback device: {lodev}"
    if bootdev:
        lodev = get_associated_lodev(bootdev)
        assert not lodev, f"{bootdev} still has a loopback device: {lodev}"

    vmuuid = str(uuid.uuid1())
    emucmd, emuopts = detect_qemu(force_kvm)
    if "-enable-kvm" in emuopts:
        proper_timeout = qemu_timeout
    else:
        proper_timeout = qemu_timeout * qemu_full_emulation_factor
        logger.warning(
            "No hardware (KVM) emulation available.  The next step is going to take a while."
        )
    screenmode = [
        "-nographic",
        "-monitor",
        "none",
        "-chardev",
        "stdio,id=char0",
        "-serial",
        "chardev:char0",
    ] + (
        []
        if interactive_qemu
        else [
            "-chardev",
            "file,id=char1,path=/dev/stderr",
            "-mon",
            "char1,mode=control",
        ]
    )
    dracut_cmdline = (
        "rd.info rd.shell systemd.show_status=1 "
        "systemd.journald.forward_to_console=1 systemd.log_level=info "
        "systemd.log_target=console"
    )
    luks_cmdline = f"rd.luks.uuid={rootuuid} " if luksuuid else ""
    enforcingparm = "enforcing=1" if enforcing else "enforcing=0"
    cmdline = (
        f"{dracut_cmdline} {luks_cmdline} console=ttyS0"
        f" root=ZFS={poolname}/ROOT/os ro"
        f" {initparm} {enforcingparm} systemd.log_color=0"
    )
    cmd: list[str] = (
        [
            emucmd,
        ]
        + screenmode
        + ["-name", hostname, "-M", "pc", "-no-reboot", "-m", "1536"]
        + ["-uuid", vmuuid, "-kernel", str(kernelfile), "-initrd", str(initrdfile)]
        + ["-append", cmdline, "-net", "none"]
        + emuopts
        + (
            [
                "-drive",
                f"file={bootdev},if=none,id=drive-ide0-0-0,format=raw",
                "-device",
                "ide-hd,bus=ide.0,unit=0,drive=drive-ide0-0-0,id=ide0-0-0,bootindex=1",
            ]
            if bootdev
            else []
        )
        + [
            "-drive",
            f"file={voldev},if=none,id=drive-ide0-0-1,format=raw",
            "-device",
            "ide-hd,bus=ide.0,unit=1,drive=drive-ide0-0-1,id=ide0-0-1,bootindex=2",
        ]
    )

    logger.info("QEMU command: %s", format_cmdline(cmd))

    if interactive_qemu:
        vmiomaster, vmioslave = None, None
        stdin, stdout, stderr = None, None, None
        driver = None
    else:
        vmiomaster_i, vmioslave_i = pty.openpty()
        vmiomaster, vmioslave = (
            os.fdopen(vmiomaster_i, "a+b", buffering=0),
            os.fdopen(vmioslave_i, "a+b", buffering=0),
        )
        stdin, stdout, stderr = vmioslave, vmioslave, vmioslave
        logger.info(
            "Creating BootDriver to supervise boot and input passphrases if needed"
        )
        driver = BootDriver(
            "root", rootpassword, lukspassword if lukspassword else "", vmiomaster
        )

    try:
        qemu_process = Popen(
            cmd, stdin=stdin, stdout=stdout, stderr=stderr, close_fds=True
        )
        if vmioslave:
            vmioslave.close()
        if driver:
            driver.start()
            logger.info("Waiting for QEMU to finish or be killed.")
            while proper_timeout > 0:
                t = min([proper_timeout, 5])
                proper_timeout -= t
                try:
                    retcode = qemu_process.wait(t)
                except subprocess.TimeoutExpired:
                    if proper_timeout > 0 and driver.is_alive():
                        if proper_timeout % 60 == 0:
                            logger.info(
                                "Waiting for QEMU.  %s more seconds to go.",
                                proper_timeout,
                            )
                    else:  # Either ran out of time or driver is dead.
                        if driver.is_alive():
                            logger.error(
                                "QEMU did not exit within the timeout.  Killing it."
                            )
                        else:
                            logger.error(
                                "QEMU driver exited fortuitously."
                                "  Killing QEMU just in case."
                            )
                        qemu_process.kill()
                        retcode = qemu_process.wait()
        exception = None
        if driver:
            try:
                driver.join()
            except Exception as e:
                exception = e

        retcode = qemu_process.wait()
        if isinstance(exception, MachineNeverShutoff) and retcode != 0:
            raise QEMUDied(retcode, cmd)
        elif exception:
            raise exception
        elif retcode != 0:
            raise subprocess.CalledProcessError(retcode, cmd)
    finally:
        if vmioslave:
            vmioslave.close()
        if vmiomaster:
            vmiomaster.close()


class BootDriver(threading.Thread):
    """Boot driver that runs Linux inside a QEMU process."""

    @staticmethod
    def is_typeable(string: str) -> bool:
        """Can a string be typed to the console."""
        ASCII_SPACE = 32
        for p in string:
            if ord(p) < ASCII_SPACE:
                return False
        return True

    def __init__(
        self,
        login: str,
        password: str,
        luks_passphrase: str,
        pty: BinaryIO,
    ) -> None:
        """Initialize the boot driver."""
        threading.Thread.__init__(self)
        self.setDaemon(True)
        assert self.is_typeable(luks_passphrase), (
            "Cannot handle passphrase %r" % luks_passphrase
        )
        self.luks_passphrase = luks_passphrase
        assert self.is_typeable(luks_passphrase), (
            "Cannot handle passphrase %r" % luks_passphrase
        )
        assert self.is_typeable(login), "Cannot handle user name %r" % login
        assert self.is_typeable(password), "Cannot handle password %r" % password
        self.login = login
        self.password = password
        self.pty = pty
        self.output: list[bytes] = []
        self.error: Exception | None = None

    def run(self) -> None:
        """Thread of execution of the boot driver."""
        logger.info("Boot driver started")
        consolelogger = logging.getLogger("VM.console")
        if self.luks_passphrase:
            logger.info("Expecting LUKS passphrase prompt")
        lastline: list[bytes] = []

        unseen = "unseen"
        waiting_for_escape_sequence = "waiting_for_escape_sequence"
        pending_write = "pending_write"
        written = "written"

        login_prompt_about_to_appear = "login_prompt_about_to_appear"
        login_prompt_seen = "login_prompt_seen"
        login_written = "login_written"
        password_prompt_seen = "password_prompt_seen"
        password_written = "password_written"
        shell_prompt_seen = "shell_prompt_seen"
        poweroff_written = "poweroff_written"

        luks_passphrase_prompt_state = unseen
        login_prompt_state = unseen

        try:
            while True:
                try:
                    c = self.pty.read(1)
                except OSError as e:
                    if e.errno == errno.EIO:
                        c = b""
                    else:
                        raise
                if c == b"":
                    logger.info("QEMU slave PTY gone")
                    break
                self.output.append(c)
                if c == b"\n":
                    s = b"".join(lastline)
                    consolelogger.debug(s.decode("utf-8"))

                    if (
                        b"traps: systemd[1] general protection" in s
                        or b"memory corruption" in s
                        or b"Freezing execution." in s
                    ):
                        # systemd or udevd exploded.  Raise retryable SystemdSegfault.
                        self.error = SystemdSegfault(
                            "systemd appears to have segfaulted."
                        )
                    elif b" authentication failure." in s or b"Login incorrect" in s:
                        self.error = BadPW("authentication failed")
                    elif b" Not enough available memory to open a keyslot." in s:
                        # OOM.  Raise non-retryable OOMed.
                        self.error = OOMed("a process appears to have been OOMed.")
                    elif b" Killed" in s:
                        # OOM.  Raise non-retryable OOMed.
                        self.error = OOMed("a process appears to have been OOMed.")
                    elif b"end Kernel panic" in s:
                        # OOM.  Raise non-retryable kernel panic.
                        self.error = Panicked("kernel has panicked.")
                    elif b"Kernel panic - not syncing" in s:
                        # OOM.  Raise non-retryable kernel panic.
                        self.error = Panicked("kernel has panicked.")
                    elif b"root password for maintenance" in s:
                        # System did not boot.
                        self.error = Emergency("system entered emergency mode")

                    lastline = []
                elif c == b"\r":
                    pass
                else:
                    lastline.append(c)
                s = b"".join(lastline)

                if self.luks_passphrase:
                    if luks_passphrase_prompt_state == unseen:
                        if b"nter passphrase for" in s:
                            # Please enter passphrase for disk QEMU...
                            # Enter passphrase for /dev/...
                            # LUKS passphrase prompt appeared.  Enter it later.
                            logger.info("Passphrase prompt begun appearing.")
                            luks_passphrase_prompt_state = waiting_for_escape_sequence
                    if luks_passphrase_prompt_state == waiting_for_escape_sequence:
                        if b"[0m" in s or b")!" in s:
                            logger.info("Passphrase prompt done appearing.")
                            luks_passphrase_prompt_state = pending_write
                    if luks_passphrase_prompt_state == pending_write:
                        logger.info("Writing passphrase.")
                        self.write_luks_passphrase()
                        luks_passphrase_prompt_state = written

                if self.login and self.password:
                    if login_prompt_state == unseen:
                        if b"(ttyS0)" in s:
                            # Login prompt.
                            logger.info("Login prompt about to appear.  Hitting ENTER.")
                            self.hit_enter()
                            login_prompt_state = login_prompt_about_to_appear
                    if login_prompt_state == login_prompt_about_to_appear:
                        if b" login: " in s:
                            # Login prompt.
                            logger.info("Login prompt begun appearing.")
                            login_prompt_state = login_prompt_seen
                    if login_prompt_state == login_prompt_seen:
                        logger.info("Writing login.")
                        self.write_login()
                        login_prompt_state = login_written
                    if login_prompt_state == login_written:
                        if b"Password: " in s:
                            logger.info("Password prompt begun appearing.")
                            login_prompt_state = password_prompt_seen
                    if login_prompt_state == password_prompt_seen:
                        logger.info("Writing password.")
                        self.write_password()
                        login_prompt_state = password_written
                    if login_prompt_state == password_written:
                        if b" ~]# " in s:
                            logger.info("Shell prompt begun appearing.")
                            login_prompt_state = shell_prompt_seen
                    if login_prompt_state == shell_prompt_seen:
                        logger.info("Writing poweroff.")
                        self.write_poweroff()
                        login_prompt_state = poweroff_written

                if self.error:
                    logger.error(
                        "An error condition was encountered by the boot driver: %s",
                        self.error,
                    )
                    break

            logger.info("Boot driver ended")
            if not self.error:
                if (
                    b"reboot: Power down" not in self.get_output()
                    and b"reboot: Restarting system" not in self.get_output()
                ):
                    self.error = MachineNeverShutoff(
                        "The bootable image never shut off."
                    )

        except Exception as exc:
            self.error = exc

    def get_output(self) -> bytes:
        """Get the total sum of output from the Linux console."""
        return b"".join(self.output)

    def join(self, timeout: float | None = None) -> None:
        """Join the VM execuion thread."""
        threading.Thread.join(self, timeout)
        if self.error:
            raise self.error

    def _write_stuff(self, stuff: str) -> None:
        """Write text followed by a newline to the console."""
        time.sleep(0.25)
        for char in stuff:
            self.pty.write(char.encode("utf-8"))
            self.pty.flush()
        self.pty.write(b"\n")
        self.pty.flush()

    def write_luks_passphrase(self) -> None:
        """Write the LUKS passphrase to the console."""
        return self._write_stuff(self.luks_passphrase)

    def hit_enter(self) -> None:
        """Write the login username to the console."""
        return self._write_stuff("")

    def write_login(self) -> None:
        """Write the login username to the console."""
        return self._write_stuff(self.login)

    def write_password(self) -> None:
        """Write the login password to the console."""
        return self._write_stuff(self.password)

    def write_poweroff(self) -> None:
        """Write `poweroff` to the console."""
        return self._write_stuff("poweroff")
