#!/usr/bin/env python

import collections


class BreakingBefore(Exception):
    pass


break_stages = collections.OrderedDict()
break_stages["beginning"] = "doing anything"
break_stages[
    "install_prebuilt_rpms"
] = "installing prebuilt RPMs specified on the command line"
break_stages["install_grub_zfs_fixer"] = "installing GRUB2 fixer RPM"
break_stages["deploy_zfs"] = "deploying ZFS"
break_stages["reload_chroot"] = "reloading the final chroot"
break_stages["bootloader_install"] = "installation of the bootloader"
break_stages[
    "boot_to_test_non_hostonly"
] = "booting with the generic initramfs to test it works"
break_stages[
    "boot_to_test_hostonly"
] = "booting with the host-only initramfs to test it works"
