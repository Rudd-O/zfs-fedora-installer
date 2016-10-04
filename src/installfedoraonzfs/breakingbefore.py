#!/usr/bin/env python

import collections


class BreakingBefore(Exception): pass


break_stages = collections.OrderedDict()
break_stages["beginning"] = "doing anything"
break_stages["install_prebuilt_rpms"] = "installing prebuilt RPMs specified on the command line"
break_stages["install_grub_zfs_fixer"] = "installing GRUB2 fixer RPM"
break_stages["deploy_spl"] = "deploying SPL"
break_stages["deploy_zfs"] = "deploying ZFS"
break_stages["reload_chroot"] = "reloading the final chroot"
break_stages["install_bootloader"] = "installing the bootloader"
break_stages["boot_bootloader"] = "booting the installation of the bootloader"
