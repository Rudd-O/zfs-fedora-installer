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
from installfedoraonzfs.cmd import Popen


logger = logging.getLogger("VM")
qemu_full_emulation_factor = 10


class BootloaderWedged(Exception): pass


class MachineNeverShutoff(Exception): pass


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
                       rootuuid,
                       break_before,
                       qemu_timeout):
    vmuuid = str(uuid.uuid1())
    emucmd, emuopts = detect_qemu(force_kvm)
    if '-enable-kvm' in emuopts:
        proper_timeout = qemu_timeout
    else:
        proper_timeout = qemu_timeout * qemu_full_emulation_factor
        logger.warning("No hardware (KVM) emulation available.  The next step is going to take a while.")
    dracut_cmdline = ("rd.info rd.shell systemd.show_status=1 "
                      "systemd.journald.forward_to_console=1 systemd.log_level=info")
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
        luks_cmdline = "rd.luks.uuid=%s "%(rootuuid,)
    else:
        luks_cmdline = ""
    cmdline = '%s %s console=ttyS0 root=ZFS=%s/ROOT/os ro %s' % (
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
        '-m', '256',
        '-uuid', vmuuid,
        "-kernel", kernelfile,
        '-initrd', initrdfile,
        '-append', cmdline,
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
    if break_before == "boot_bootloader":
        logger.info(
            "qemu process that would execute now: %s" % " ".join([
                pipes.quote(s) for s in cmd
            ])
        )
        raise BreakingBefore(break_before)

    def babysit(popenobject, timeout):
        for _ in xrange(timeout):
            if popenobject.returncode is not None:
                return
            time.sleep(1)
        logger.error("QEMU babysitter is killing stubborn qemu process after %s seconds", timeout)
        popenobject.kill()

    if interactive_qemu:
        stdin, stdout, stderr = (None, None, None)
        driver = None
        vmiomaster = None
    else:
        vmiomaster, vmioslave = pty.openpty()
        vmiomaster, vmioslave = os.fdopen(vmiomaster, "a+b"), os.fdopen(vmioslave, "rw+b")
        stdin, stdout, stderr = (vmioslave, vmioslave, vmioslave)
        logger.info("Creating a new BootDriver thread to input the passphrase if needed")
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
        if driver:
            logger.info("Waiting to join the BootDriver thread")
            driver.join()
            if "reboot: Power down" in driver.get_output():
                machine_powered_off_okay = True
    if not machine_powered_off_okay:
        raise MachineNeverShutoff("The bootable image never shut off.  Check the QEMU boot log for errors or unexpected behavior.")


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
        logger.info("Boot driver started")
        consolelogger = logging.getLogger("VM.console")
        pwendprompt = "".join(['!', ' ', '\x1b', '[', '0', 'm'])
        if self.password:
            logger.info("Expecting password prompt")
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
                    logger.info("QEMU slave PTY gone")
                    break
                self.output.append(c)
                if c == "\n":
                    consolelogger.debug("".join(lastline))
                    lastline = []
                elif c == "\r":
                    pass
                else:
                    lastline.append(c)
                s = "".join(lastline)
                if (self.password and
                    # Please enter passphrase for disk QEMU...
                    # Enter passphrase for /dev/...
                    "nter passphrase for" in s and
                    pwendprompt in s):
                    # Zero out the last line to prevent future spurious matches.
                    consolelogger.debug("".join(lastline))
                    lastline = []
                    self.write_password()
            except Exception, e:
                logger.error("Boot driver experienced an error (postponed): %s(%s)", e.__class__, e)
                self.error = e
                break
        if lastline:
            consolelogger.debug("".join(lastline))
        logger.info("Boot driver gone")

    def get_output(self):
        return "".join(self.output)

    def join(self):
        threading.Thread.join(self)
        if self.error:
            raise self.error

    def write_password(self):
        pw = []
        time.sleep(0.25)
        logger.info("Writing password to console now")
        for char in self.password:
            self.pty.write(char)
            self.pty.flush()
        self.pty.write("\n")
        self.pty.flush()
