#!/usr/bin/python -tt
#
# Copyright 2011 Intel, Inc.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.  Any Red Hat
# trademarks that are incorporated in the source code or documentation are not
# subject to the GNU General Public License and may only be used or replicated
# with the express permission of Red Hat, Inc.
#

import os
import subprocess
import shutil
import re
import tempfile

from mic import configmgr, pluginmgr, chroot, msger
from mic.utils import misc, fs_related, errors
from mic.utils.partitionedfs import PartitionedMount

import mic.imager.raw as raw

from mic.pluginbase import ImagerPlugin
class RawPlugin(ImagerPlugin):
    name = 'raw'

    @classmethod
    def do_create(self, subcmd, opts, *args):
        """${cmd_name}: create raw image

        ${cmd_usage}
        ${cmd_option_list}
        """

        if not args:
            raise errors.Usage("More arguments needed")

        if len(args) == 1:
            ksconf = args[0]
        else:
            raise errors.Usage("Extra arguments given")

        cfgmgr = configmgr.getConfigMgr()
        creatoropts = cfgmgr.create
        cfgmgr.setProperty("ksconf", ksconf)

        # try to find the pkgmgr
        pkgmgr = None
        plgmgr = pluginmgr.PluginMgr()
        for (key, pcls) in plgmgr.get_plugins('backend').iteritems():
            if key == creatoropts['pkgmgr']:
                pkgmgr = pcls
                break

        if not pkgmgr:
            raise errors.CreatorError("Can't find backend %s" % pkgmgr)

        creator = raw.RawImageCreator(creatoropts, pkgmgr)
        try:
            creator.check_depend_tools()
            creator.mount(None, creatoropts["cachedir"])
            creator.install()
            creator.configure(creatoropts["repomd"])
            creator.unmount()
            creator.package(creatoropts["outdir"])
            outimage = creator.outimage
            creator.print_outimage_info()
            outimage = creator.outimage

        except errors.CreatorError, e:
            raise errors.CreatorError("failed to create image : %s" % e)

        finally:
            creator.cleanup()

        msger.info("Finished.")
        return 0

    @classmethod
    def do_chroot(cls, target):
        img = target
        imgsize = misc.get_file_size(img) * 1024L * 1024L
        partedcmd = fs_related.find_binary_path("parted")
        disk = fs_related.SparseLoopbackDisk(img, imgsize)
        imgmnt = misc.mkdtemp()
        imgloop = PartitionedMount({'/dev/sdb':disk}, imgmnt, skipformat = True)
        img_fstype = "ext3"

        # Check the partitions from raw disk.
        root_mounted = False
        partition_mounts = 0
        for line in subprocess.Popen([partedcmd,"-s",img,"unit","B","print"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\
                                             .communicate()[0]\
                                             .strip()\
                                             .splitlines():

            line = line.strip()

            # Lines that start with number are the partitions,
            # because parted can be translated we can't refer to any text lines.
            if not line or not line[0].isdigit():
                continue

            # Some vars have extra , as list seperator.
            line = line.replace(",","")

            # Example of parted output lines that are handled:
            # Number  Start        End          Size         Type     File system     Flags
            #  1      512B         3400000511B  3400000000B  primary
            #  2      3400531968B  3656384511B  255852544B   primary  linux-swap(v1)
            #  3      3656384512B  3720347647B  63963136B    primary  fat16           boot, lba

            partition_info = re.split("\s+",line)

            size = partition_info[3].split("B")[0]

            if len(partition_info) < 6 or partition_info[5] in ["boot"]:
                # No filesystem can be found from partition line. Assuming
                # btrfs, because that is the only MeeGo fs that parted does
                # not recognize properly.
                # TODO: Can we make better assumption?
                fstype = "btrfs"
            elif partition_info[5] in ["ext2","ext3","ext4","btrfs"]:
                fstype = partition_info[5]
            elif partition_info[5] in ["fat16","fat32"]:
                fstype = "vfat"
            elif "swap" in partition_info[5]:
                fstype = "swap"
            else:
                raise errors.CreatorError("Could not recognize partition fs type '%s'." % partition_info[5])

            if not root_mounted and fstype in ["ext2","ext3","ext4","btrfs"]:
                # TODO: Check that this is actually the valid root partition from /etc/fstab
                mountpoint = "/"
                root_mounted = True
            elif fstype == "swap":
                mountpoint = "swap"
            else:
                # TODO: Assing better mount points for the rest of the partitions.
                partition_mounts += 1
                mountpoint = "/media/partition_%d" % partition_mounts

            if "boot" in partition_info:
                boot = True
            else:
                boot = False

            msger.verbose("Size: %s Bytes, fstype: %s, mountpoint: %s, boot: %s" % (size, fstype, mountpoint, boot))
            # TODO: add_partition should take bytes as size parameter.
            imgloop.add_partition((int)(size)/1024/1024, "/dev/sdb", mountpoint, fstype = fstype, boot = boot)

        try:
            imgloop.mount()

        except errors.MountError, e:
            imgloop.cleanup()
            raise errors.CreatorError("Failed to loopback mount '%s' : %s" % (img, e))

        try:
            chroot.chroot(imgmnt, None,  "/bin/env HOME=/root /bin/bash")
        except:
            raise errors.CreatorError("Failed to chroot to %s." %img)
        finally:
            chroot.cleanup_after_chroot("img", imgloop, None, imgmnt)

    @classmethod
    def do_unpack(cls, srcimg):
        srcimgsize = (misc.get_file_size(srcimg)) * 1024L * 1024L
        srcmnt = misc.mkdtemp("srcmnt")
        disk = fs_related.SparseLoopbackDisk(srcimg, srcimgsize)
        srcloop = PartitionedMount({'/dev/sdb':disk}, srcmnt, skipformat = True)

        srcloop.add_partition(srcimgsize/1024/1024, "/dev/sdb", "/", "ext3", boot=False)
        try:
            srcloop.mount()

        except errors.MountError, e:
            srcloop.cleanup()
            raise errors.CreatorError("Failed to loopback mount '%s' : %s" %
                               (srcimg, e))

        image = os.path.join(tempfile.mkdtemp(dir = "/var/tmp", prefix = "tmp"), "target.img")
        args = ['dd', "if=%s" % srcloop.partitions[0]['device'], "of=%s" % image]

        msger.info("`dd` image ...")
        rc = subprocess.call(args)
        srcloop.cleanup()
        shutil.rmtree(srcmnt, ignore_errors = True)

        if rc != 0:
            raise errors.CreatorError("Failed to dd")
        else:
            return image
