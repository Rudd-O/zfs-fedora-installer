#!/usr/bin/env python

import contextlib
import fcntl
import glob
import logging
import os
import pipes
import subprocess
import time


logger = logging.getLogger("shell")


def format_cmdline(lst):
    return " ".join(pipes.quote(x) for x in lst)


def check_call(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logger.debug("Check calling %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.check_call(*args,**kwargs)


def check_output(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logger.debug("Check outputting %s in cwd %r", format_cmdline(cmd), cwd)
    output = subprocess.check_output(*args,**kwargs)
    if output:
        firstline=output.splitlines()[0].strip()
        logger.debug("First line of output from command: %s", firstline)
    else:
        logger.debug("No output from command")
    return output


def Popen(*args,**kwargs):
    cwd = kwargs.get("cwd", os.getcwd())
    cmd = args[0]
    logger.debug("Popening %s in cwd %r", format_cmdline(cmd), cwd)
    return subprocess.Popen(*args,**kwargs)


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
    f = file("/etc/mtab")
    _lockf(f)
    try:
        mountpoints = [x.strip().split()[1].decode("string_escape") for x in f.readlines()]
        return target in mountpoints
    finally:
        _unlockf(f)
        f.close()


def ismount(target):
    return os.path.ismount(target) or isbindmount(target)


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
            for of, procs in openfiles.items():
                logger.warn("%r:", of)
                for pid, cmd in procs:
                    logger.warn("  %7s  %s", pid, cmd)
        logger.warn("Syncing and sleeping 1 second")
        check_call(['sync'])
        time.sleep(1)
        umount(mountpoint, tries - 1)
    return mountpoint


def makedirs(ds):
    for subdir in ds:
        while not os.path.isdir(subdir):
            try:
                os.makedirs(subdir)
            except OSError, e:
                if e.errno != errno.EEXIST:
                    raise
    return ds


@contextlib.contextmanager
def lockfile(path):
    lf = _lockf(open(path, 'wb'))
    lf.write(str(os.getpid())+"\n")
    lf.flush()
    yield lf
    lf.truncate()
    lf.flush()
    lf.close()
