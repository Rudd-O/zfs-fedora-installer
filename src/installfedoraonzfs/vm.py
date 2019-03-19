#!/usr/bin/env python

import errno
import logging
import os
import pipes
import pty
import uuid
import subprocess
import threading
import time

from installfedoraonzfs.breakingbefore import BreakingBefore
from installfedoraonzfs.cmd import Popen, get_associated_lodev
from installfedoraonzfs.retry import Retryable


logger = logging.getLogger("VM")
qemu_full_emulation_factor = 5


class BootloaderWedged(Exception): pass


class OOMed(Exception): pass


class Panicked(Exception): pass


class SystemdSegfault(Retryable, Exception): pass


class MachineNeverShutoff(Exception): pass


class Babysitter(threading.Thread):

    def __init__(self, popenobject, timeout):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.popenobject = popenobject
        self.timeout = timeout
        self._stopped_cond = threading.Condition()
        self._stopped_val = False

    def run(self):
        logger = logging.getLogger("VM.babysitter")
        popenobject = self.popenobject
        timeout = self.timeout
        stopped = False
        for x in xrange(timeout):
            if stopped:
                return
            if popenobject.returncode is not None:
                return
            if x and (x % 60 == 0):
                logger.info("%s minutes elapsed", x / 60)
            self._stopped_cond.acquire()
            self._stopped_cond.wait(1.0)
            stopped = self._stopped_val
            self._stopped_cond.release()
        logger.error("Killing lame duck emulator after %s seconds", timeout)
        popenobject.kill()

    def stop(self):
        self._stopped_cond.acquire()
        self._stopped_val = True
        self._stopped_cond.notify()
        self._stopped_cond.release()

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


def boot_image_in_qemu(hostname,
                       initparm,
                       poolname,
                       voldev,
                       bootdev,
                       kernelfile,
                       initrdfile,
                       force_kvm,
                       interactive_qemu,
                       lukspassword,
                       rootpassword,
                       rootuuid,
                       luksuuid,
                       qemu_timeout):

    if voldev:
        assert not get_associated_lodev(voldev), "%s still has a loopback device: %s" % (voldev, get_associated_lodev(voldev))
    if bootdev:
        assert not get_associated_lodev(bootdev), "%s still has a loopback device: %s" % (bootdev, get_associated_lodev(bootdev))

    vmuuid = str(uuid.uuid1())
    emucmd, emuopts = detect_qemu(force_kvm)
    if '-enable-kvm' in emuopts:
        proper_timeout = qemu_timeout
    else:
        proper_timeout = qemu_timeout * qemu_full_emulation_factor
        logger.warning("No hardware (KVM) emulation available.  The next step is going to take a while.")
    dracut_cmdline = ("rd.info rd.shell systemd.show_status=1 "
                      "systemd.journald.forward_to_console=1 systemd.log_level=info "
                      "systemd.log_target=console")
    screenmode = [
        "-nographic",
        "-monitor","none",
        "-chardev","stdio,id=char0",
        "-serial","chardev:char0",
    ]
    if not interactive_qemu:
        screenmode += [
            "-chardev","file,id=char1,path=/dev/stderr",
            "-mon","char1,mode=control",
        ]
    if luksuuid:
        luks_cmdline = "rd.luks.uuid=%s "%(rootuuid,)
    else:
        luks_cmdline = ""
    cmdline = '%s %s console=ttyS0 root=ZFS=%s/ROOT/os ro %s enforcing=0' % (
        dracut_cmdline,
        luks_cmdline,
        poolname,
        initparm,
    )
    cmd = [
        emucmd,
        ] + screenmode + [
        "-name", hostname,
        "-M", "pc-1.2",
        "-no-reboot",
        '-m', '512',
        '-uuid', vmuuid,
        "-kernel", kernelfile,
        '-initrd', initrdfile,
        '-append', cmdline,
        '-net', 'none',
    ]
    cmd = cmd + emuopts
    if bootdev:
        cmd.extend([
            '-drive', 'file=%s,if=none,id=drive-ide0-0-0,format=raw'%bootdev,
            '-device', 'ide-hd,bus=ide.0,unit=0,drive=drive-ide0-0-0,id=ide0-0-0,bootindex=1',
        ])
    cmd.extend([
        '-drive', 'file=%s,if=none,id=drive-ide0-0-1,format=raw' % voldev,
        '-device', 'ide-hd,bus=ide.0,unit=1,drive=drive-ide0-0-1,id=ide0-0-1,bootindex=2',
    ])

    # check for stage stop
    logger.info(
        "qemu process will would execute now: %s" % " ".join([
            pipes.quote(s) for s in cmd
        ])
    )

    babysitter = None
    if interactive_qemu:
        vmiomaster, vmioslave = None, None
        stdin, stdout, stderr = None, None, None
        driver = None
    else:
        vmiomaster, vmioslave = pty.openpty()
        vmiomaster, vmioslave = os.fdopen(vmiomaster, "a+b"), os.fdopen(vmioslave, "rw+b")
        stdin, stdout, stderr = vmioslave, vmioslave, vmioslave
        logger.info("Creating BootDriver to supervise boot and input passphrases if needed")
        driver = BootDriver("root", rootpassword, lukspassword if lukspassword else "", vmiomaster)

    try:
        qemu_process = Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, close_fds=True)
        if vmioslave:
            vmioslave.close()
        if driver:
            babysitter = Babysitter(qemu_process, proper_timeout)
            babysitter.start()
            driver.start()
            e = None
            try:
                logger.info("Waiting to join BootDriver")
                driver.join()
            except MachineNeverShutoff, e:
                # The driver got an EOF or something, before
                # it had a chance to read the normal shutdown
                # text from the VM.
                # If this was because qemu was killed by the
                # babysitter, we must decide about it later.
                pass
            except BaseException, e:
                # Something else went wrong, so we kill the
                # qemu process and raise the exception.
                logger.exception("boot_image_in_qemu experienced exception, killing QEMU process")
                qemu_process.kill()
                qemu_process.wait()
                raise
            retcode = qemu_process.wait()
            if retcode == -9:
                # If qemu got SIGKILL, that means the babysitter did it.
                # We assume the bootloader got wedged, thus killed.
                raise BootloaderWedged("The bootloader appears wedged.")
            elif retcode != 0:
                # Abnormal return code from qemu, we must raise it.
                raise subprocess.CalledProcessError(retcode, cmd)
            elif e:
                # qemu returned normally, but the machine does not.
                # appear to have shut off cleanly, so we raise it.
                raise e
        else:
            retcode = qemu_process.wait()
            if retcode != 0:
                raise subprocess.CalledProcessError(retcode, cmd)
    finally:
        if babysitter:
            babysitter.stop()
            babysitter.join()
        if vmioslave:
            vmioslave.close()
        if vmiomaster:
            vmiomaster.close()


class BootDriver(threading.Thread):

    @staticmethod
    def is_typeable(passphrase):
        for p in passphrase:
            if ord(p) < 32:
                return False
        return True

    def __init__(self, login, password, luks_passphrase, pty):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        assert self.is_typeable(luks_passphrase), "Cannot handle passphrase %r" % luks_passphrase
        self.luks_passphrase = luks_passphrase
        assert self.is_typeable(luks_passphrase), "Cannot handle passphrase %r" % luks_passphrase
        assert self.is_typeable(login), "Cannot handle user name %r" % login
        assert self.is_typeable(password), "Cannot handle password %r" % password
        self.login = login
        self.password = password
        self.pty = pty
        self.output = []
        self.error = None

    def run(self):
        logger.info("Boot driver started")
        consolelogger = logging.getLogger("VM.console")
        if self.luks_passphrase:
            logger.info("Expecting LUKS passphrase prompt")
        lastline = []

        unseen = "unseen"
        waiting_for_escape_sequence = "waiting_for_escape_sequence"
        pending_write = "pending_write"
        written = "written"

        login_prompt_seen = "login_prompt_seen"
        login_written = "login_written"
        password_prompt_seen = "password_prompt_seen"
        password_written = "password_written"
        shell_prompt_seen = "shell_prompt_seen"
        poweroff_written = "poweroff_written"

        luks_passphrase_prompt_state = unseen
        login_prompt_state = unseen

        segfaulted = False
        oom = False
        panicked = False

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
                    logger.info("QEMU slave PTY gone")
                    break
                self.output.append(c)
                if c == "\n":
                    consolelogger.debug("".join(lastline))
                    lastline = []
                    if segfaulted:
                        raise SystemdSegfault("systemd appears to have segfaulted.")
                    if oom:
                        raise OOMed("a process appears to have been OOMed.")
                    if panicked:
                        raise Panicked("kernel has panicked.")
                elif c == "\r":
                    pass
                else:
                    lastline.append(c)
                s = "".join(lastline)

                if self.luks_passphrase:
                    if luks_passphrase_prompt_state == unseen:
                        if "nter passphrase for" in s:
                            # Please enter passphrase for disk QEMU...
                            # Enter passphrase for /dev/...
                            # LUKS passphrase prompt appeared.  Enter it later.
                            logger.info("Passphrase prompt begun appearing.")
                            luks_passphrase_prompt_state = waiting_for_escape_sequence
                    if luks_passphrase_prompt_state == waiting_for_escape_sequence:
                        if "[0m" in s or ")!" in s:
                            logger.info("Passphrase prompt done appearing.")
                            luks_passphrase_prompt_state = pending_write
                    if luks_passphrase_prompt_state == pending_write:
                        logger.info("Writing passphrase.")
                        self.write_luks_passphrase()
                        luks_passphrase_prompt_state = written

                if self.login and self.password:
                    if login_prompt_state == unseen:
                        if " login: " in s:
                            # Please enter passphrase for disk QEMU...
                            # Enter passphrase for /dev/...
                            # LUKS passphrase prompt appeared.  Enter it later.
                            logger.info("Login prompt begun appearing.")
                            login_prompt_state = login_prompt_seen
                    if login_prompt_state == login_prompt_seen:
                        logger.info("Writing login.")
                        self.write_login()
                        login_prompt_state = login_written
                    if login_prompt_state == login_written:
                        if "Password: " in s:
                            logger.info("Password prompt begun appearing.")
                            login_prompt_state = password_prompt_seen
                    if login_prompt_state == password_prompt_seen:
                        logger.info("Writing password.")
                        self.write_password()
                        login_prompt_state = password_written
                    if login_prompt_state == password_written:
                        if " ~]# " in s:
                            logger.info("Shell prompt begun appearing.")
                            login_prompt_state = shell_prompt_seen
                    if login_prompt_state == shell_prompt_seen:
                        logger.info("Writing poweroff.")
                        self.write_poweroff()
                        login_prompt_state = poweroff_written

                if ("traps: systemd[1] general protection" in s or
                    "memory corruption" in s or
                    "Freezing execution." in s):
                    # systemd or udevd exploded.  Raise retryable SystemdSegfault later.
                    segfaulted = True
                if (" Killed" in s):
                    # OOM.  Raise non-retryable OOMed.
                    oom = True
                if ("end Kernel panic" in s):
                    # OOM.  Raise non-retryable kernel panic.
                    panicked = True
            except Exception, e:
                self.error = e
                break
        if lastline:
            consolelogger.debug("".join(lastline))
        logger.info("Boot driver ended")
        if not self.error:
            if "reboot: Power down" not in self.get_output():
                self.error = MachineNeverShutoff("The bootable image never shut off.")

    def get_output(self):
        return "".join(self.output)

    def join(self):
        threading.Thread.join(self)
        if self.error:
            raise self.error

    def _write_stuff(self, stuff):
        time.sleep(0.25)
        for char in stuff:
            self.pty.write(char)
            self.pty.flush()
        self.pty.write("\n")
        self.pty.flush()

    def write_luks_passphrase(self):
        return self._write_stuff(self.luks_passphrase)

    def write_login(self):
        return self._write_stuff(self.login)

    def write_password(self):
        return self._write_stuff(self.password)

    def write_poweroff(self):
        return self._write_stuff("poweroff")
