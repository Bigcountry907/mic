#!/usr/bin/python -tt
#
# Copyright (c) 2007 Red Hat  Inc.
# Copyright (c) 2010, 2011 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import tempfile
import glob
from string import Template

import rpmUtils
import yum

from mic import msger
from mic.kickstart import ksparser
from mic.utils import misc, rpmmisc
from mic.utils.grabber import TextProgress
from mic.utils.proxy import get_proxy_for
from mic.utils.errors import CreatorError
from mic.utils.safeurl import SafeURL


YUMCONF_TEMP = """[main]
installroot=$installroot
cachedir=/var/cache/yum
persistdir=/var/lib/yum
plugins=0
reposdir=
failovermethod=priority
http_caching=packages
sslverify=1
"""

class MyYumRepository(yum.yumRepo.YumRepository):
    def __init__(self, repoid, nocache):
        super(MyYumRepository, self).__init__(repoid)
        self.nocache = nocache

    def __del__(self):
        pass

    def dirSetup(self):
        super(MyYumRepository, self).dirSetup()
        # relocate package dir
        pkgdir = os.path.join(self.basecachedir, 'packages', self.id)
        self.setAttribute('_dir_setup_pkgdir', pkgdir)
        self._dirSetupMkdir_p(self.pkgdir)

    def _getFile(self, url=None,
                       relative=None,
                       local=None,
                       start=None,
                       end=None,
                       copy_local=None,
                       checkfunc=None,
                       text=None,
                       reget='simple',
                       cache=True,
                       size=None):

        m2c_connection = None
        if not self.sslverify:
            try:
                import M2Crypto
                m2c_connection = M2Crypto.SSL.Connection.clientPostConnectionCheck
                M2Crypto.SSL.Connection.clientPostConnectionCheck = None
            except ImportError, err:
                raise CreatorError("%s, please try to install python-m2crypto" % str(err))

        proxy = None
        if url:
            proxy = get_proxy_for(url)
        else:
            proxy = get_proxy_for(self.urls[0])

        if proxy:
            self.proxy = str(proxy)

        size = int(size) if size else None
        rvalue = super(MyYumRepository, self)._getFile(url,
                                                       relative,
                                                       local,
                                                       start,
                                                       end,
                                                       copy_local,
                                                       checkfunc,
                                                       text,
                                                       reget,
                                                       cache,
                                                       size)

        if m2c_connection and \
           not M2Crypto.SSL.Connection.clientPostConnectionCheck:
            M2Crypto.SSL.Connection.clientPostConnectionCheck = m2c_connection

        return rvalue

from mic.pluginbase import BackendPlugin
class Yum(BackendPlugin, yum.YumBase):
    name = 'yum'

    def __init__(self, target_arch, instroot, cachedir, strict_mode = False):
        yum.YumBase.__init__(self)

        self.cachedir = cachedir
        self.instroot  = instroot
        self.target_arch = target_arch
        self.strict_mode = strict_mode

        if self.target_arch:
            if not rpmUtils.arch.arches.has_key(self.target_arch):
                rpmUtils.arch.arches["armv7hl"] = "noarch"
                rpmUtils.arch.arches["armv7tnhl"] = "armv7nhl"
                rpmUtils.arch.arches["armv7tnhl"] = "armv7thl"
                rpmUtils.arch.arches["armv7thl"] = "armv7hl"
                rpmUtils.arch.arches["armv7nhl"] = "armv7hl"
            self.arch.setup_arch(self.target_arch)

        self.__pkgs_license = {}
        self.__pkgs_content = {}
        self.__pkgs_vcsinfo = {}
        self.check_pkgs = []

        self.install_debuginfo = False

    def doFileLogSetup(self, uid, logfile):
        # don't do the file log for the livecd as it can lead to open fds
        # being left and an inability to clean up after ourself
        pass

    def close(self):
        try:
            os.unlink(self.confpath)
            os.unlink(self.conf.installroot + "/yum.conf")
        except:
            pass

        if self.ts:
            self.ts.close()
        self._delRepos()
        self._delSacks()
        yum.YumBase.close(self)
        self.closeRpmDB()

    def __del__(self):
        pass

    def _writeConf(self, confpath, installroot):
        conf = Template(YUMCONF_TEMP).safe_substitute(installroot=installroot)

        f = file(confpath, "w+")
        f.write(conf)
        f.close()

        os.chmod(confpath, 0644)

    def _cleanupRpmdbLocks(self, installroot):
        # cleans up temporary files left by bdb so that differing
        # versions of rpm don't cause problems
        for f in glob.glob(installroot + "/var/lib/rpm/__db*"):
            os.unlink(f)

    def setup(self):
        # create yum.conf
        (fn, self.confpath) = tempfile.mkstemp(dir=self.cachedir,
                                               prefix='yum.conf-')
        os.close(fn)
        self._writeConf(self.confpath, self.instroot)
        self._cleanupRpmdbLocks(self.instroot)
        # do setup
        self.doConfigSetup(fn = self.confpath, root = self.instroot)
        self.conf.cache = 0
        self.doTsSetup()
        self.doRpmDBSetup()
        self.doRepoSetup()
        self.doSackSetup()

    def preInstall(self, pkg):
        # FIXME: handle pre-install package
        return None

    def checkPackage(self, pkg):
        self.check_pkgs.append(pkg)

    def selectPackage(self, pkg):
        """Select a given package.
        Can be specified with name.arch or name*
        """

        try:
            self.install(pattern = pkg)
            return None
        except yum.Errors.InstallError:
            return "No package(s) available to install"
        except yum.Errors.RepoError, e:
            raise CreatorError("Unable to download from repo : %s" % (e,))
        except yum.Errors.YumBaseError, e:
            raise CreatorError("Unable to install: %s" % (e,))

    def deselectPackage(self, pkg):
        """Deselect package.  Can be specified as name.arch or name*
        """

        sp = pkg.rsplit(".", 2)
        txmbrs = []
        if len(sp) == 2:
            txmbrs = self.tsInfo.matchNaevr(name=sp[0], arch=sp[1])

        if len(txmbrs) == 0:
            exact, match, unmatch = yum.packages.parsePackages(
                                            self.pkgSack.returnPackages(),
                                            [pkg],
                                            casematch=1)
            for p in exact + match:
                txmbrs.append(p)

        if len(txmbrs) > 0:
            for x in txmbrs:
                self.tsInfo.remove(x.pkgtup)
                # we also need to remove from the conditionals
                # dict so that things don't get pulled back in as a result
                # of them.  yes, this is ugly.  conditionals should die.
                for req, pkgs in self.tsInfo.conditionals.iteritems():
                    if x in pkgs:
                        pkgs.remove(x)
                        self.tsInfo.conditionals[req] = pkgs
        else:
            msger.warning("No such package %s to remove" %(pkg,))

    def selectGroup(self, grp, include = ksparser.GROUP_DEFAULT):
        try:
            yum.YumBase.selectGroup(self, grp)
            if include == ksparser.GROUP_REQUIRED:
                for p in grp.default_packages.keys():
                    self.deselectPackage(p)

            elif include == ksparser.GROUP_ALL:
                for p in grp.optional_packages.keys():
                    self.selectPackage(p)

            return None
        except (yum.Errors.InstallError, yum.Errors.GroupsError), e:
            return e
        except yum.Errors.RepoError, e:
            raise CreatorError("Unable to download from repo : %s" % (e,))
        except yum.Errors.YumBaseError, e:
            raise CreatorError("Unable to install: %s" % (e,))

    def addRepository(self, name, url = None, mirrorlist = None, proxy = None,
                      proxy_username = None, proxy_password = None,
                      inc = None, exc = None, ssl_verify=True, nocache=False,
                      cost = None, priority=None):
        # TODO: Handle priority attribute for repos
        def _varSubstitute(option):
            # takes a variable and substitutes like yum configs do
            option = option.replace("$basearch", rpmUtils.arch.getBaseArch())
            option = option.replace("$arch", rpmUtils.arch.getCanonArch())
            return option
        repo = MyYumRepository(name, nocache)

        # Set proxy
        repo.proxy = proxy
        repo.proxy_username = proxy_username
        repo.proxy_password = proxy_password

        if url:
            repo.baseurl.append(_varSubstitute(url.full))

        if mirrorlist:
            repo.mirrorlist = _varSubstitute(mirrorlist)

        conf = yum.config.RepoConf()
        for k, v in conf.iteritems():
            if v or not hasattr(repo, k):
                repo.setAttribute(k, v)

        repo.sslverify = ssl_verify

        repo.basecachedir = self.cachedir
        repo.base_persistdir = self.conf.persistdir
        repo.failovermethod = "priority"
        repo.metadata_expire = 0
        # Enable gpg check for verifying corrupt packages
        repo.gpgcheck = 1
        repo.enable()
        repo.setup(0)
        self.repos.add(repo)
        if cost:
            repo.cost = cost

        msger.verbose('repo: %s was added' % name)
        return repo

    def installLocal(self, pkg, po=None, updateonly=False):
        ts = rpmUtils.transaction.initReadOnlyTransaction()
        try:
            hdr = rpmUtils.miscutils.hdrFromPackage(ts, pkg)
        except rpmUtils.RpmUtilsError, e:
            raise yum.Errors.MiscError, \
                  'Could not open local rpm file: %s: %s' % (pkg, e)

        self.deselectPackage(hdr['name'])
        yum.YumBase.installLocal(self, pkg, po, updateonly)

    def installHasFile(self, file):
        provides_pkg = self.whatProvides(file, None, None)
        dlpkgs = map(
                    lambda x: x.po,
                    filter(
                        lambda txmbr: txmbr.ts_state in ("i", "u"),
                        self.tsInfo.getMembers()))

        for p in dlpkgs:
            for q in provides_pkg:
                if (p == q):
                    return True

        return False

    def runInstall(self, checksize = 0):
        os.environ["HOME"] = "/"
        os.environ["LD_PRELOAD"] = ""
        try:
            (res, resmsg) = self.buildTransaction()
        except yum.Errors.RepoError, e:
            raise CreatorError("Unable to download from repo : %s" %(e,))

        if res != 2:
            raise CreatorError("Failed to build transaction : %s" \
                               % str.join("\n", resmsg))

        dlpkgs = map(
                    lambda x: x.po,
                    filter(
                        lambda txmbr: txmbr.ts_state in ("i", "u"),
                        self.tsInfo.getMembers()))

        # record all pkg and the content
        for pkg in dlpkgs:
            pkg_long_name = misc.RPM_FMT % {
                                'name': pkg.name,
                                'arch': pkg.arch,
                                'version': pkg.version,
                                'release': pkg.release
                            }
            self.__pkgs_content[pkg_long_name] = pkg.files
            license = pkg.license
            if license in self.__pkgs_license.keys():
                self.__pkgs_license[license].append(pkg_long_name)
            else:
                self.__pkgs_license[license] = [pkg_long_name]

            if pkg.name in self.check_pkgs:
                self.check_pkgs.remove(pkg.name)

        if self.check_pkgs:
            raise CreatorError('Packages absent in image: %s' % ','.join(self.check_pkgs))

        total_count = len(dlpkgs)
        cached_count = 0
        download_total_size = sum(map(lambda x: int(x.packagesize), dlpkgs))

        msger.info("\nChecking packages cached ...")
        for po in dlpkgs:
            local = po.localPkg()
            repo = filter(lambda r: r.id == po.repoid, self.repos.listEnabled())[0]
            if repo.nocache and os.path.exists(local):
                os.unlink(local)
            if not os.path.exists(local):
                continue
            if not self.verifyPkg(local, po, False):
                msger.warning("Package %s is damaged: %s" \
                              % (os.path.basename(local), local))
            else:
                download_total_size -= int(po.packagesize)
                cached_count += 1

        cache_avail_size = misc.get_filesystem_avail(self.cachedir)
        if cache_avail_size < download_total_size:
            raise CreatorError("No enough space used for downloading.")

        # record the total size of installed pkgs
        pkgs_total_size = 0L
        for x in dlpkgs:
            if hasattr(x, 'installedsize'):
                pkgs_total_size += int(x.installedsize)
            else:
                pkgs_total_size += int(x.size)

        # check needed size before actually download and install
        if checksize and pkgs_total_size > checksize:
            raise CreatorError("No enough space used for installing, "
                               "please resize partition size in ks file")

        msger.info("Packages: %d Total, %d Cached, %d Missed" \
                   % (total_count, cached_count, total_count - cached_count))

        try:
            repos = self.repos.listEnabled()
            for repo in repos:
                repo.setCallback(TextProgress(total_count - cached_count))

            self.downloadPkgs(dlpkgs)
            # FIXME: sigcheck?

            self.initActionTs()
            self.populateTs(keepold=0)

            deps = self.ts.check()
            if len(deps) != 0:
                # This isn't fatal, Ubuntu has this issue but it is ok.
                msger.debug(deps)
                msger.warning("Dependency check failed!")

            rc = self.ts.order()
            if rc != 0:
                raise CreatorError("ordering packages for installation failed")

            # FIXME: callback should be refactored a little in yum
            cb = rpmmisc.RPMInstallCallback(self.ts)
            cb.tsInfo = self.tsInfo
            cb.filelog = False

            msger.warning('\nCaution, do NOT interrupt the installation, '
                          'else mic cannot finish the cleanup.')

            installlogfile = "%s/__catched_stderr.buf" % (self.instroot)
            msger.enable_logstderr(installlogfile)
            transactionResult = self.runTransaction(cb)
            if transactionResult.return_code != 0 and self.strict_mode:
                raise CreatorError("mic failes to install some packages")
            self._cleanupRpmdbLocks(self.conf.installroot)

        except rpmUtils.RpmUtilsError, e:
            raise CreatorError("mic does NOT support delta rpm: %s" % e)
        except yum.Errors.RepoError, e:
            raise CreatorError("Unable to download from repo : %s" % e)
        except yum.Errors.YumBaseError, e:
            raise CreatorError("Unable to install: %s" % e)
        finally:
            msger.disable_logstderr()

    def getVcsInfo(self):
        if self.__pkgs_vcsinfo:
            return self.__pkgs_vcsinfo
        if not self.ts:
            self.__initialize_transaction()
        mi = self.ts.dbMatch()
        for hdr in mi:
            lname = misc.RPM_FMT % {
                        'name': hdr['name'],
                        'arch': hdr['arch'],
                        'version': hdr['version'],
                        'release': hdr['release']
                    }
            try:
                self.__pkgs_vcsinfo[lname] = hdr['VCS']
            except ValueError, KeyError:
                self.__pkgs_vcsinfo[lname] = None

        return self.__pkgs_vcsinfo

    def getAllContent(self):
        return self.__pkgs_content

    def getPkgsLicense(self):
        return self.__pkgs_license

    def getFilelist(self, pkgname):
        if not pkgname:
            return None

        pkg = filter(lambda txmbr: txmbr.po.name == pkgname, self.tsInfo.getMembers())
        if not pkg:
            return None
        return pkg[0].po.filelist

    def package_url(self, pkgname):
        pkgs = self.pkgSack.searchNevra(name=pkgname)
        if pkgs:
            pkg = pkgs[0]

            repo = pkg.repo
            url = SafeURL(repo.baseurl[0]).join(pkg.remote_path)

            proxy = repo.proxy
            if not proxy:
                proxy = get_proxy_for(url)
            if proxy:
                proxies = {str(url.split(':')[0]): str(proxy)}
            else:
                proxies = None

            return (url, proxies)

        return (None, None)
