"""Commands and utilities."""

import errno
import fcntl
import glob
import logging
import os
from pathlib import Path
import pipes
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any, BinaryIO, Literal, Sequence, TextIO, TypeVar, cast

logger = logging.getLogger("cmd")


def readtext(fn: Path) -> str:
    """Read a text file."""
    with open(fn) as f:
        return f.read()


def writetext(fn: Path, text: str) -> None:
    """Write text to a file.

    The write is not transactional.  Incomplete writes can appear after a crash
    """
    with open(fn, "w") as f:
        f.write(text)


def readlines(fn: Path) -> list[str]:
    """Read lines from a file.

    Lines returned do not get their newlines stripped.
    """
    with open(fn) as f:
        return f.readlines()


def format_cmdline(lst: Sequence[str]) -> str:
    """Format a command line for print()."""
    return " ".join(pipes.quote(x) for x in lst)


def check_call(cmd: list[str], *args: Any, **kwargs: Any) -> None:
    """subprocess.check_call with logging.

    Standard input will be closed and all I/O will proceed with text.

    Arguments:
      cmd: command and arguments to run
      cwd: current working directory
      *args: positional arguments for check_call
      **kwargs: keyword arguments for check_call
    """
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["close_fds"] = True
    kwargs["stdin"] = open(os.devnull)
    kwargs["universal_newlines"] = True
    logger.debug("Check calling %s in cwd %r", format_cmdline(cmd), cwd)
    subprocess.check_call(cmd, *args, **kwargs)


def check_call_silent_stdout(cmd: list[str]) -> None:
    """subprocess.check_call with no standard output."""
    with open(os.devnull, "w") as devnull:
        check_call(cmd, stdout=devnull)


def check_call_silent(cmd: list[str]) -> None:
    """subprocess.check_call with no standard output or error."""
    with open(os.devnull, "w") as devnull:
        check_call(cmd, stdout=devnull, stderr=devnull)


def check_output(cmd: list[str], *args: Any, **kwargs: Any) -> str:
    """Obtain the standard output of a command.

    Arguments:
      cmd: command and arguments to run
      cwd: current working directory
      *args: positional arguments for check_call
      **kwargs: keyword arguments for check_call
    """
    logall = kwargs.get("logall", False)
    if "logall" in kwargs:
        del kwargs["logall"]
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["universal_newlines"] = True
    kwargs["close_fds"] = True
    logger.debug("Check outputting %s in cwd %r", format_cmdline(cmd), cwd)
    output = cast(str, subprocess.check_output(cmd, *args, **kwargs))
    if output:
        if logall:
            logger.debug("Output from command: %r", output)
        else:
            firstline = output.splitlines()[0].strip()
            logger.debug("First line of output from command: %s", firstline)
    else:
        logger.debug("No output from command")
    return output


def get_associated_lodev(path: Path) -> Path | None:
    """Return loopback devices associated with path."""
    output = ":".join(
        check_output(["losetup", "-j", str(path)]).rstrip().split(":")[:-2]
    )
    if output:
        return Path(output)
    return None


def filetype(
    dev: Path
) -> Literal["file"] | Literal["blockdev"] | Literal["doesntexist"]:
    """Return 'file' or 'blockdev' or 'doesntexist' for dev."""
    try:
        s = os.stat(dev)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return "doesntexist"
        raise
    if stat.S_ISBLK(s.st_mode):
        return "blockdev"
    if stat.S_ISREG(s.st_mode):
        return "file"
    assert 0, "specified path %r is not a block device or a file"


def losetup(path: Path) -> Path:
    """Set up a local loop device for a file."""
    dev = check_output(["losetup", "-P", "--find", "--show", str(path)])[:-1]
    check_output(["blockdev", "--rereadpt", dev])
    return Path(dev)


class Tee(threading.Thread):
    """Tees output from filesets to filesets.

    Each fileset is a tuple with the first (read) file, and second/third write files.
    """

    def __init__(self, *filesets: tuple[TextIO, TextIO, TextIO]):
        """Initialize the tee."""
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.filesets = filesets
        self.err: BaseException | None = None

    def run(self) -> None:
        """Begin copying from readables to writables.

        The copying in the thread will continue until all readables
        have been closed.  Writables will not be closed by this
        algorithm.
        """
        pollables = {f[0]: f[1:] for f in self.filesets}
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
            except Exception as exc:
                readables[0].close()
                del pollables[readables[0]]
                if not self.err:
                    self.err = exc
                    break
        for f in self.filesets:
            for w in f[1:]:
                w.flush()

    def join(self, timeout: float | None = None) -> None:
        """Join the thread."""
        threading.Thread.join(self, timeout)
        if self.err:
            raise self.err


def get_output_exitcode(cmd: list[str], **kwargs: Any) -> tuple[str, int]:
    """Get the output (stdout / stderr) of a command and its exit code.

    The stdout/stderr stream will be printed to standard output / standard error.

    stdout and stderr will be mixed in the returned output.
    """
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["universal_newlines"] = True
    stdin = kwargs.get("stdin")
    if "stdin" in kwargs:
        del kwargs["stdin"]
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
        t = Tee(
            (cast(TextIO, p.stdout), f, stdout),
            (cast(TextIO, p.stderr), f, stderr),
        )
        t.start()
        t.join()
        retval = p.wait()
        f.seek(0)
        output = f.read()
    finally:
        f.close()
    return output, retval


def Popen(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen with logging."""
    cwd = kwargs.get("cwd", os.getcwd())
    kwargs["universal_newlines"] = True
    logger.debug("Popening %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.Popen(cmd, *args, **kwargs)


def mount(source: Path, target: Path, *opts: str) -> Path:
    """Mount a file system.

    Returns the mountpoint.
    """
    cmd = ["mount"]
    cmd.extend(opts)
    cmd.extend(["--", str(source), str(target)])
    check_call(cmd)
    return target


def bindmount(source: Path, target: Path) -> Path:
    """Bind mounts a path onto another path.

    Returns the mountpoint.
    """
    return mount(source, target, "--bind")


def get_file_size(filename: Path) -> int:
    """Get the file size by seeking at end."""
    fd = os.open(filename, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


Lockable = TypeVar("Lockable", bound="IO[Any]")


def _lockf(f: Lockable) -> Lockable:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f


def _unlockf(f: Lockable) -> Lockable:
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return f


def isbindmount(target: Path) -> bool:
    """Is path a bind mountpoint."""
    with open("/proc/self/mounts", "rb") as f:
        mountpoints = [
            x.split()[1].decode("unicode-escape") for x in f.read().splitlines()
        ]
        return str(target) in mountpoints


def ismount(target: Path) -> bool:
    """Is path a mountpoint."""
    return os.path.ismount(target) or isbindmount(target)


def check_for_open_files(prefix: Path) -> dict[str, list[tuple[str, str]]]:  # noqa: C901
    """Check that there are open files or mounted file systems within the prefix.

    Returns a  dictionary where the keys are the files, and the values are lists
    that contain tuples (pid, command line) representing the processes that are
    keeping those files open, or tuples ("<mount>", description) representing
    the file systems mounted there.
    """
    MAXWIDTH = 60
    results: dict[str, list[tuple[str, str]]] = {}
    files = glob.glob("/proc/*/fd/*") + glob.glob("/proc/*/cwd")
    for f in files:
        try:
            d = os.readlink(f)
        except Exception:
            continue
        if d.startswith(str(prefix) + os.path.sep) or d == str(prefix):
            pid = f.split(os.path.sep)[2]
            if pid == "self":
                continue
            c = os.path.join("/", *(f.split(os.path.sep)[1:3] + ["cmdline"]))
            try:
                with open(c) as ff:
                    cmd = format_cmdline(ff.read().split("\0"))
            except Exception:
                continue
            if len(cmd) > MAXWIDTH:
                cmd = cmd[:57] + "..."
            if d not in results:
                results[d] = []
            results[d].append((pid, cmd))
    with open("/proc/self/mounts", "rb") as mounts:
        for line in mounts.read().splitlines():
            fields = line.split()
            dev = fields[0].decode("unicode-escape")
            mp = fields[1].decode("unicode-escape")
            if mp.startswith(str(prefix) + os.path.sep):
                if mp not in results:
                    results[mp] = []
                results[mp].append(("<mount>", dev))
    return results


def umount(mountpoint: Path, tries: int = 5) -> None:
    """Unmount a file system, trying `tries` times."""
    while True:
        if not ismount(mountpoint):
            return None
        try:
            check_call(["umount", str(mountpoint)])
            break
        except subprocess.CalledProcessError:
            openfiles = check_for_open_files(mountpoint)
            if openfiles:
                logger.warning("There are open files in %r:", mountpoint)
                for of, procs in list(openfiles.items()):
                    logger.warning("%r:", of)
                    for pid, cmd in procs:
                        logger.warning("  %7s  %s", pid, cmd)
            if tries <= 0:
                raise
            logger.warning("Syncing and sleeping 1 second")
            check_call(["sync"])
            time.sleep(1)
            tries -= 1


def create_file(
    filename: Path,
    sizebytes: int,
    owner: str | int | None = None,
    group: str | int | None = None,
) -> None:
    """Create a file of a certain size."""
    with open(filename, "wb") as f:
        f.seek(sizebytes - 1)
        f.write(b"\0")
    if owner is not None:
        check_call(["chown", str(owner), "--", str(filename)])
    if group is not None:
        check_call(["chgrp", str(group), "--", str(filename)])


def delete_contents(directory: Path) -> None:
    """Remove a directory completely."""
    if not os.path.exists(directory):
        return
    ps = [str(os.path.join(directory, p)) for p in os.listdir(directory)]
    if ps:
        check_call(["rm", "-rf"] + ps)


def makedirs(ds: list[Path]) -> list[Path]:
    """Recursively create list of directories."""
    for subdir in ds:
        while not os.path.isdir(subdir):
            os.makedirs(subdir, exist_ok=True)
    return ds


class lockfile:
    """Create a lockfile to be used as a context manager."""

    def __init__(self, path: Path):
        """Initialize the lockfile object."""
        self.path = path
        self.f: BinaryIO | None = None

    def __enter__(self) -> None:
        """Grab the lock and permit execution of contexted code."""
        logger.debug("Grabbing lock %s", self.path)
        self.f = open(self.path, "wb")
        _lockf(self.f)
        logger.debug("Grabbed lock %s", self.path)

    def __exit__(self, *unused_args: Any) -> None:
        """Unlock and close lockfile."""
        logger.debug("Releasing lock %s", self.path)
        assert self.f
        _unlockf(self.f)
        self.f.close()
        self.f = None
        logger.debug("Released lock %s", self.path)


def cpuinfo() -> str:
    """Return the CPU info."""
    return open("/proc/cpuinfo").read()


def checkout_repo_at(repo: str, project_dir: Path, branch: str) -> None:
    """Checkout a git repository at a specific path."""
    qbranch = shlex.quote(branch)
    if os.path.isdir(project_dir):
        logger.info("Updating and checking out git repository: %s", repo)
        check_call("git fetch".split(), cwd=project_dir)
        check_call(
            [
                "bash",
                "-c",
                f"git reset --hard origin/{qbranch} || git reset --hard {qbranch}",
            ],
            cwd=project_dir,
        )
    else:
        logger.info("Cloning git repository: %s", repo)
        check_call(["git", "clone", repo, str(project_dir)])
        check_call(
            [
                "bash",
                "-c",
                f"git reset --hard origin/{qbranch} || git reset --hard {qbranch}",
            ],
            cwd=project_dir,
        )
    check_call(["git", "--no-pager", "show"], cwd=project_dir)
