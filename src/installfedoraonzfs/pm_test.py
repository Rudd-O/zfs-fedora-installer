#!/usr/bin/env python

import unittest

import installfedoraonzfs.pm as pm
import installfedoraonzfs.retry as retrymod


class CmdInteractionTest(unittest.TestCase):

    def test_rpmdb_corruption_is_retryable(self):
        f = pm.check_call_retry_rpmdberror
        self.assertRaises(
            retrymod.Retryable,
            lambda: f(["bash", "-c", "echo Rpmdb checksum is invalid: blah; false"]),
        )
