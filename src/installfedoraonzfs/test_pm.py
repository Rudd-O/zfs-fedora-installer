#!/usr/bin/env python

import contextlib
import mock
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from installfedoraonzfs import pm, cmd


def mock_check_call_no_output(actions):
    cmds = []
    def fun(cmd, *args, **kwargs):
        cmds.append(cmd)
        action = actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action
    return cmds, fun


@contextlib.contextmanager
def tmpdir():
    tmpd = tempfile.mkdtemp()
    try:
        yield tmpd
    finally:
        shutil.rmtree(tmpd)


WILDCARD = "*"


class TestEnsurePackagesInstalled(unittest.TestCase):

    maxDiff = None

    def assertListEqual(self, expected, results):
        expected = list(expected)
        results = list(results)
        if WILDCARD not in expected:
            return unittest.TestCase.assertListEqual(self, expected, results)
        for i, p in enumerate(expected):
            if p == WILDCARD:
                expected[i] = results[i]
        return unittest.TestCase.assertListEqual(self, expected, results)

    def _do_a_tst(self, release, method, pkgmgrconf, packages, behaviors, expected):
        results, fun = mock_check_call_no_output(behaviors)
        with tmpdir() as tmpd:
            with mock.patch.object(cmd, 'check_call_no_output', fun):
                with mock.patch.object(cmd, 'get_output_exitcode', fun):
                    os_path_exists = os.path.exists
                    def exists(f):
                        if method == "out_of_chroot":
                            if f == pkgmgrconf:
                                t = True
                            else:
                                t = False
                        else:
                            t = os_path_exists(f)
                        return t
                    with mock.patch.object(os.path, 'exists', exists):
                        if method == "in_chroot":
                            pkgmgrconf = tmpd + pkgmgrconf
                            os.makedirs(os.path.dirname(pkgmgrconf))
                            shutil.copyfile("/etc/dnf/dnf.conf", pkgmgrconf)
                        c = pm.ChrootPackageManager(
                            tmpd,
                            release,
                            None
                        )
                        c.ensure_packages_installed(packages, method=method)
        self.assertEqual(len(expected), len(results), results)
        for e, r in zip(expected, results):
            self.assertListEqual(e, r)


testcases = []
for release in (23, 25, 27):
    exec '''testcases += [
        (
            {0},
            "in_chroot",
            "/etc/dnf/dnf.conf",
            ["basesystem"],
            [
                subprocess.CalledProcessError(1, [], ''),
                ('', 0),
                ('', 0),
            ],
            [
                ["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                ["chroot", WILDCARD, "dnf", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "--downloadonly", "basesystem"],
                ["chroot", WILDCARD, "dnf", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "basesystem"],
            ]
        ),
        (
            {0},
            "out_of_chroot",
            "/etc/dnf/dnf.conf",
            ["basesystem"],
            [
                subprocess.CalledProcessError(1, [], ''),
                ('', 0),
                ('', 0),
            ],
            [
                ["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                ["dnf", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "--downloadonly",
                  WILDCARD, "--releasever={0}", "basesystem"],
                ["dnf", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD,
                  WILDCARD, "--releasever={0}", "basesystem"],
            ]
        ),
        (
            {0},
            "in_chroot",
            "/etc/yum.conf",
            ["basesystem"],
            [
                subprocess.CalledProcessError(1, [], ''),
                ('', 0),
                ('', 0),
            ],
            [
                ["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                ["chroot", WILDCARD, "yum", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "--downloadonly", "--", "basesystem"],
                ["chroot", WILDCARD, "yum", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "--", "basesystem"],
            ]
        ),
        (
            {0},
            "out_of_chroot",
            "/etc/yum.conf",
            ["basesystem"],
            [
                subprocess.CalledProcessError(1, [], ''),
                ('', 0),
                ('', 0),
            ],
            [
                ["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                ["yum", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD, "--downloadonly",
                  WILDCARD, "--releasever={0}", "--", "basesystem"],
                ["yum", "install", "-qy", "--disableplugin=*qubes*",
                  "-c", WILDCARD,
                  WILDCARD, "--releasever={0}", "--", "basesystem"],
            ]
        ),
    ]
'''.format(release)
for release, method, pkgmgrconf, packages, behaviors, expected in testcases:
    pkgmgr = os.path.basename(pkgmgrconf).replace(".", "_")
    def fun(self,
            release=release,
            method=method,
            pkgmgrconf=pkgmgrconf,
            packages=packages,
            behaviors=behaviors,
            expected=expected):
        return self._do_a_tst(release, method, pkgmgrconf, packages, behaviors, expected)
    name = "test_%s_%s_%s_%s" % (release, method, pkgmgr, "_".join(packages))
    fun.__name__ = name
    setattr(TestEnsurePackagesInstalled, name, fun)
del fun
