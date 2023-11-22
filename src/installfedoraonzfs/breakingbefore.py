#!/usr/bin/env python

import collections


class BreakingBefore(Exception):
    """Utility exception used to break at a particular stage."""

    pass


break_stages = collections.OrderedDict()
break_stages["beginning"] = "doing anything"
break_stages["deploy_zfs"] = "deploying ZFS"
break_stages["reload_chroot"] = "reloading the final chroot"
break_stages["bootloader_install"] = "installation of the bootloader"
break_stages[
    "boot_to_test_non_hostonly"
] = "booting with the generic initramfs to test it works"
break_stages[
    "boot_to_test_hostonly"
] = "booting with the host-only initramfs to test it works"


shell_stages = collections.OrderedDict()
shell_stages[
    "install_packages_in_chroot"
] = "installing system packages in chroot after bash is available"
shell_stages[
    "install_kernel"
] = "installing kernel, DKMS, and GRUB packages in the chroot"
shell_stages["install_prebuilt_rpms"] = "deploying ZFS to the chroot"
shell_stages["deploy_grub_zfs_fixer"] = "deploying ZFS to the chroot"
shell_stages["deploy_zfs"] = "deploying ZFS to the chroot"
shell_stages["reload_chroot"] = "preparing the chroot to boot"
