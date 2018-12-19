'''
Created on Dec 19, 2018

@author: user
'''
import unittest

import contextlib
import installfedoraonzfs
import os
import shutil
import subprocess
import tempfile
import re


@contextlib.contextmanager
def preprootboot(directory=None):
    u = installfedoraonzfs.Undoer()
    d = tempfile.mkdtemp(dir=directory)
    root = os.path.join(d, "root")
    boot = os.path.join(d, "boot")
    try:
        yield d, root, boot, u
    finally:
        u.undo()
        shutil.rmtree(d)


def lodevs():
    devs = subprocess.check_output(["losetup", "-la"])
    return [x for x in devs.splitlines() if x]


class TestBlockdevContext(unittest.TestCase):

    @unittest.skipIf(os.getuid() != 0, "not root")
    def testSeparateBoot(self):
        firstlo = lodevs()
        with preprootboot() as (_, root, boot, undoer):
            with installfedoraonzfs.blockdev_context(root, boot, undoer, 32, 8, None, None) as (rootpart, bootpart, efipart):
                assert bootpart.endswith("p3"), (rootpart, bootpart, efipart)
                assert efipart.endswith("p2"), (rootpart, bootpart, efipart)
                assert re.match("^/dev/loop[0-9]+$", rootpart), (rootpart, bootpart, efipart)
        nowlo = lodevs()
        self.assertListEqual(firstlo, nowlo)

    @unittest.skipIf(os.getuid() != 0, "not root")
    def testSeparateBootTwice(self):
        firstlo = lodevs()
        with preprootboot() as (_, root, boot, undoer):
            with installfedoraonzfs.blockdev_context(root, boot, undoer, 32, 8, None, None) as (rootpart, bootpart, efipart):
                assert bootpart.endswith("p3"), (rootpart, bootpart, efipart)
                assert efipart.endswith("p2"), (rootpart, bootpart, efipart)
                assert re.match("^/dev/loop[0-9]+$", rootpart), (rootpart, bootpart, efipart)
            undoer.undo()
            nowlo = lodevs()
            self.assertListEqual(firstlo, nowlo)
            with installfedoraonzfs.blockdev_context(root, boot, undoer, 32, 8, None, None) as (rootpart2, bootpart2, efipart2):
                assert rootpart == rootpart2, (rootpart, rootpart2)
                assert bootpart == bootpart2, (bootpart, bootpart2)
                assert efipart == efipart2, (efipart, efipart2)
        nowlo = lodevs()
        self.assertListEqual(firstlo, nowlo)


class TestSetupFilesystems(unittest.TestCase):

    @unittest.skipIf(os.getuid() != 0, "not root")
    def testBasic(self):
        firstlo = lodevs()
        wdir = os.path.dirname(__file__)
        with preprootboot(wdir) as (workdir, root, boot, undoer):
            with installfedoraonzfs.blockdev_context(
                root, boot, undoer, 256, 128, None, None
            ) as (rootpart, bootpart, efipart):
                bootuuid, efiuuid = installfedoraonzfs.setup_boot_filesystems(
                    bootpart, efipart,
                    "postfix", True
                )
                assert bootuuid
                assert efiuuid
        nowlo = lodevs()
        self.assertListEqual(firstlo, nowlo)

    @unittest.skipIf(os.getuid() != 0, "not root")
    def testTwice(self):
        wdir = os.path.dirname(__file__)
        with preprootboot(wdir) as (workdir, root, boot, undoer):
            with installfedoraonzfs.blockdev_context(
                root, boot, undoer, 256, 128, None, None
            ) as (_, bootpart, efipart):
                bootuuid, efiuuid = installfedoraonzfs.setup_boot_filesystems(
                    bootpart, efipart, "postfix", True
                )
                assert bootuuid
                assert efiuuid
            undoer.undo()
            with installfedoraonzfs.blockdev_context(
                root, boot, undoer, 256, 128, None, None
            ) as (_, bootpart, efipart):
                bootuuid2, efiuuid2 = installfedoraonzfs.setup_boot_filesystems(
                    bootpart, efipart, "postfix", False
                )
                assert bootuuid == bootuuid2, (bootuuid, bootuuid2)
                assert efiuuid == efiuuid, (efiuuid, efiuuid2)


if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()