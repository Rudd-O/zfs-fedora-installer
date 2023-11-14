#!/usr/bin/env python

import errno
import fcntl
import glob
import logging
import os
import pipes
import select
import subprocess
import sys
import tempfile
import threading
import time


logger = logging.getLogger("cmd")


def readtext(fn):
    with open(fn) as f:
        return f.read()


def writetext(fn, text):
    with open(fn, "w") as f:
        return f.write(text)


def readlines(fn):
    with open(fn) as f:
        return f.readlines()


def format_cmdline(lst):
    return " ".join(pipes.quote(x) for x in lst)


def check_call(*args, **kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["close_fds"] = True
    kwargs["stdin"] = open(os.devnull)
    kwargs["universal_newlines"] = True
    cmd = args[0]
    logger.debug("Check calling %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.check_call(*args, **kwargs)


def check_call_silent_stdout(cmd):
    with open(os.devnull, "w") as devnull:
        return check_call(cmd, stdout=devnull)


def check_call_silent(cmd):
    with open(os.devnull, "w") as devnull:
        return check_call(cmd, stdout=devnull, stderr=devnull)


def check_output(*args, **kwargs):
    logall = kwargs.get("logall", False)
    if "logall" in kwargs:
        del kwargs["logall"]
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["universal_newlines"] = True
    kwargs["close_fds"] = True
    cmd = args[0]
    logger.debug("Check outputting %s in cwd %r", format_cmdline(cmd), cwd)
    output = subprocess.check_output(*args, **kwargs)
    if output:
        if logall:
            logger.debug("Output from command: %r", output)
        else:
            firstline = output.splitlines()[0].strip()
            logger.debug("First line of output from command: %s", firstline)
    else:
        logger.debug("No output from command")
    return output


def get_associated_lodev(path):
    output = ":".join(check_output(["losetup", "-j", path]).rstrip().split(":")[:-2])
    if output:
        return output
    return None


class Tee(threading.Thread):
    def __init__(self, *filesets):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.filesets = filesets
        self.err = None

    def run(self):
        pollables = dict((f[0], f[1:]) for f in self.filesets)
        for inf in list(pollables.keys()):
            flag = fcntl.fcntl(inf.fileno(), fcntl.F_GETFL)
            fcntl.fcntl(inf.fileno(), fcntl.F_SETFL, flag | os.O_NONBLOCK)
        while pollables:
            readables, _, _ = select.select(list(pollables.keys()), [], [])
            data = readables[0].read()
            try:
                if not data:
                    # Other side of file descriptor closed / EOF.
                    readables[0].close()
                    # We will not be polling it again
                    del pollables[readables[0]]
                    continue
                for w in pollables[readables[0]]:
                    w.write(data)
            except Exception as e:
                readables[0].close()
                del pollables[readables[0]]
                if not self.err:
                    self.err = e
                    break
        for f in self.filesets:
            for w in f[1:]:
                w.flush()

    def join(self):
        threading.Thread.join(self)
        if self.err:
            raise self.err


def get_output_exitcode(cmd, **kwargs):
    """Gets the output (stdout / stderr) of a command, and its exit code,
    while the stream is printed to standard output / standard error.

    stdout and stderr will be mixed in the returned output.
    """
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["universal_newlines"] = True
    stdin = kwargs.get("stdin")
    stdout = kwargs.get("stdout", sys.stdout)
    stderr = kwargs.get("stderr", sys.stderr)
    if stderr == subprocess.STDOUT:
        assert 0, "you cannot specify subprocess.STDOUT on this function"

    f = tempfile.TemporaryFile(mode="w+")
    try:
        logger.debug("Get output exitcode %s in cwd %r", format_cmdline(cmd), cwd)
        p = subprocess.Popen(
            cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
        )
        t = Tee((p.stdout, f, stdout), (p.stderr, f, stderr))
        t.start()
        t.join()
        retval = p.wait()
        f.seek(0)
        output = f.read()
    finally:
        f.close()
    return output, retval


def Popen(*args, **kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    kwargs["universal_newlines"] = True
    logger.debug("Popening %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.Popen(*args, **kwargs)


def mount(source, target, *opts):
    """Returns the mountpoint."""
    check_call(["mount"] + list(opts) + ["--", source, target])
    return target


def bindmount(source, target):
    """Returns the mountpoint."""
    return mount(source, target, "--bind")


def _lockf(f):
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def _unlockf(f):
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return f


def isbindmount(target):
    with open("/etc/mtab", "rb") as f:
        _lockf(f)
        try:
            mountpoints = [
                x.split()[1].decode("unicode-escape") for x in f.read().splitlines()
            ]
            return target in mountpoints
        finally:
            _unlockf(f)


def ismount(target):
    return os.path.ismount(target) or isbindmount(target)


def check_for_open_files(prefix: str) -> dict[str, list[tuple[str, str]]]:
    """Check that there are open files or mounted file systems within the prefix.

    Returns a  dictionary where the keys are the files, and the values are lists
    that contain tuples (pid, command line) representing the processes that are
    keeping those files open, or tuples ("<mount>", description) representing
    the file systems mounted there."""
    results: dict[str, list[tuple[str, str]]] = dict()
    files = glob.glob("/proc/*/fd/*") + glob.glob("/proc/*/cwd")
    for f in files:
        try:
            d = os.readlink(f)
        except Exception:
            continue
        if d.startswith(prefix + os.path.sep) or d == prefix:
            pid = f.split(os.path.sep)[2]
            if pid == "self":
                continue
            c = os.path.join("/", *(f.split(os.path.sep)[1:3] + ["cmdline"]))
            try:
                with open(c) as ff:
                    cmd = format_cmdline(ff.read().split("\0"))
            except Exception:
                continue
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            if d not in results:
                results[d] = []
            results[d].append((pid, cmd))
    with open("/proc/self/mounts", "rb") as mounts:
        for line in mounts.read().splitlines():
            fields = line.split()
            dev = fields[0].decode("unicode-escape")
            mp = fields[1].decode("unicode-escape")
            if mp.startswith(prefix + os.path.sep):
                if mp not in results:
                    results[mp] = []
                results[mp].append(("<mount>", dev))
    return results


def umount(mountpoint: str, tries=5):
    if not ismount(mountpoint):
        return
    try:
        check_call(["umount", mountpoint])
    except subprocess.CalledProcessError:
        if tries < 1:
            raise
        openfiles = check_for_open_files(mountpoint)
        if openfiles:
            logger.warn("There are open files in %r:", mountpoint)
            for of, procs in list(openfiles.items()):
                logger.warn("%r:", of)
                for pid, cmd in procs:
                    logger.warn("  %7s  %s", pid, cmd)
        logger.warn("Syncing and sleeping 1 second")
        check_call(["sync"])
        time.sleep(1)
        umount(mountpoint, tries - 1)
    return mountpoint


def makedirs(ds: list[str]) -> list[str]:
    for subdir in ds:
        while not os.path.isdir(subdir):
            os.makedirs(subdir, exist_ok=True)
    return ds


class lockfile(object):
    def __init__(self, path: str):
        self.path = path
        self.f = None

    def __enter__(self):
        logger.debug("Grabbing lock %s", self.path)
        self.f = open(self.path, "wb")
        _lockf(self.f)
        logger.debug("Grabbed lock %s", self.path)

    def __exit__(self, *unused_args):
        logger.debug("Releasing lock %s", self.path)
        _unlockf(self.f)
        self.f.close()
        self.f = None
        logger.debug("Released lock %s", self.path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    with lockfile("lock") as f:
        print(f)
