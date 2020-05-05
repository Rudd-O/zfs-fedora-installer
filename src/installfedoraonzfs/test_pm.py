#!/usr/bin/env python

import contextlib
import mock
import os
import shutil
import subprocess  #pylint: disable=unused-import
import tempfile
import unittest

from installfedoraonzfs import pm, cmd


def mock_check_call_no_output(actions):
    cmds = []
    def fun(cmd, *unused_args, **unused_kwargs):
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


class RetryTest(unittest.TestCase):

    def test_retries_work(self):
        counts = []
        def f(*args):
            counts.append(1)
            raise pm.PluginSelinuxRetryable(-1, ['a'], 'selinux retryable')
        with mock.patch.object(pm, 'run_and_repair', f):
            try:
                pm.run_and_repair_with_retries(1, 2, 3, 4, 5)
            except pm.PluginSelinuxRetryable:
                pass
            assert len(counts) == 3
            


class CmdInteractionTest(unittest.TestCase):

    def test_rpmdb_corruption_is_retryable(self):
        f = pm.check_call_detect_rpmdberror
        self.assertRaises(
            pm.RpmdbCorruptionError,
            lambda: f(["bash", "-c", "echo You probably have corrupted RPMDB, running 'rpm --rebuilddb' might fix the issue.; false"]),
        )
        self.assertRaises(
            pm.RpmdbCorruptionError,
            lambda: f(["bash", "-c", "echo Rpmdb checksum is invalid: blah; false"]),
        )
        self.assertRaises(
            pm.RpmdbCorruptionError,
            lambda: f(["bash", "-c", "echo Thread died in Berkeley DB library; false"]),
        )


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

    def testRpmdbCorruptionIsRetried(self):
        expected = [
            ["chroot", WILDCARD, "dnf", "install", "-y", "--downloadonly",
              "-c", WILDCARD, "/basesystem.rpm"],
            ["chroot", WILDCARD, "dnf", "install", "-y",
              "-c", WILDCARD, "/basesystem.rpm"],
            ["chroot", WILDCARD, "bash", "-c", "rm -f /var/lib/rpm/__db*"],
            ["chroot", WILDCARD, "rpm", "--rebuilddb"],
            ["chroot", WILDCARD, "dnf", "install", "-y",
              "-c", WILDCARD, "/basesystem.rpm"],
        ]
        behaviors = [
            ('', 0),
            pm.RpmdbCorruptionError(1, [], ''),
            None,
            ('', 0),
            ('', 0),
        ]
        results, fun = mock_check_call_no_output(behaviors)
        with tmpdir() as tmpd:
            with mock.patch.object(cmd, 'check_call', fun):
                with mock.patch.object(cmd, 'get_output_exitcode', fun):
                    bpkgmgrconf = "/etc/dnf/dnf.conf"
                    pkgmgrconf = tmpd + bpkgmgrconf
                    os.makedirs(os.path.dirname(pkgmgrconf))
                    shutil.copyfile(bpkgmgrconf, pkgmgrconf)
                    with mock.patch.object(os.path, 'exists', lambda _: True):
                        with mock.patch.object(os.path, 'isfile', lambda _: True):
                            c = pm.ChrootPackageManager(
                                tmpd,
                                "27",
                                None
                            )
                            c.install_local_packages([tmpd + "/basesystem.rpm"])
        assert len(expected) == len(results), (str(expected) + "\n" + str(results))
        for exp, res in zip(expected, results):
            self.assertListEqual(exp, res)

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


#testcases = []
#for release in (23, 25, 27):
    #exec '''testcases += [
        #(
            #{0},
            #"in_chroot",
            #"/etc/dnf/dnf.conf",
            #["basesystem"],
            #[
                #subprocess.CalledProcessError(1, [], ''),
                #('', 0),
                #('', 0),
            #],
            #[
                #["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                #["chroot", WILDCARD, "dnf", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "--downloadonly", "basesystem"],
                #["chroot", WILDCARD, "dnf", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "basesystem"],
            #]
        #),
        #(
            #{0},
            #"out_of_chroot",
            #"/etc/dnf/dnf.conf",
            #["basesystem"],
            #[
                #subprocess.CalledProcessError(1, [], ''),
                #('', 0),
                #('', 0),
            #],
            #[
                #["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                #["dnf", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "--downloadonly",
                  #WILDCARD, "--releasever={0}", "basesystem"],
                #["dnf", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD,
                  #WILDCARD, "--releasever={0}", "basesystem"],
            #]
        #),
        #(
            #{0},
            #"in_chroot",
            #"/etc/yum.conf",
            #["basesystem"],
            #[
                #subprocess.CalledProcessError(1, [], ''),
                #('', 0),
                #('', 0),
            #],
            #[
                #["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                #["chroot", WILDCARD, "yum", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "--downloadonly", "--", "basesystem"],
                #["chroot", WILDCARD, "yum", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "--", "basesystem"],
            #]
        #),
        #(
            #{0},
            #"out_of_chroot",
            #"/etc/yum.conf",
            #["basesystem"],
            #[
                #subprocess.CalledProcessError(1, [], ''),
                #('', 0),
                #('', 0),
            #],
            #[
                #["chroot", WILDCARD, "rpm", "-q", "basesystem"],
                #["yum", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD, "--downloadonly",
                  #WILDCARD, "--releasever={0}", "--", "basesystem"],
                #["yum", "install", "-y", "--disableplugin=*qubes*",
                  #"-c", WILDCARD,
                  #WILDCARD, "--releasever={0}", "--", "basesystem"],
            #]
        #),
    #]
#'''.format(release)

#for release, method, pkgmgrconf, packages, behaviors, expected in testcases:
    #pkgmgr = os.path.basename(pkgmgrconf).replace(".", "_")
    #def fun(self,
            #release=release,
            #method=method,
            #pkgmgrconf=pkgmgrconf,
            #packages=packages,
            #behaviors=behaviors,
            #expected=expected):
        #return self._do_a_tst(release, method, pkgmgrconf, packages, behaviors, expected)
    #name = "test_%s_%s_%s_%s" % (release, method, pkgmgr, "_".join(packages))
    #fun.__name__ = name
    #setattr(TestEnsurePackagesInstalled, name, fun)
#del fun
