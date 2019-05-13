#!/bin/python3

import argparse
import shutil
import gi
import gzip
import librepo
import hawkey
import tempfile
import os
import subprocess
import sys

gi.require_version('Modulemd', '2.0')
from gi.repository import Modulemd
mmd = Modulemd


def _get_repoinfo(directory):
    with tempfile.TemporaryDirectory(prefix='elsplit_librepo_') as lrodir:
        h = librepo.Handle()
        h.setopt(librepo.LRO_URLS, ["file://%s" % directory])
        h.setopt(librepo.LRO_REPOTYPE, librepo.LR_YUMREPO)
        h.setopt(librepo.LRO_DESTDIR, lrodir)
        h.setopt(librepo.LRO_LOCAL, True)
        h.setopt(librepo.LRO_IGNOREMISSING, False)
        r = h.perform()
        return r.getinfo(librepo.LRR_YUM_REPO)


def _get_hawkey_sack(repo_info):
    hk_repo = hawkey.Repo("")
    hk_repo.filelists_fn = repo_info["filelists"]
    hk_repo.primary_fn = repo_info["primary"]
    hk_repo.repomd_fn = repo_info["repomd"]

    primary_sack = hawkey.Sack()
    primary_sack.load_repo(hk_repo, build_cache=False)
    
    return primary_sack


def _parse_repository_non_modular(repo_info, modpkgset):
    sack = _get_hawkey_sack(repo_info)
    pkgs = set()

    for pkg in hawkey.Query(sack):
        if pkg.location in modpkgset:
            continue
        pkgs.add(pkg.location)
    return pkgs


def _get_filename(nevra):
    n, ev, ra = nevra.rsplit('-', 2)
    ev = ev.split(':', 1)[1]
    return 'Packages/%s-%s-%s.rpm' % (n, ev, ra)


def _parse_repository_modular(repo_info):
    cts = {}
    idx = mmd.ModuleIndex()
    with gzip.GzipFile(filename=repo_info['modules'], mode='r') as gzf:
        mmdcts = gzf.read().decode('utf-8')
        res, failures = idx.update_from_string(mmdcts, True)
        if len(failures) != 0:
            raise Exception("YAML FAILURE: FAILURES: %s" % failures)
        if not res:
            raise Exception("YAML FAILURE: res != True")

    idx.upgrade_streams(2)
    for modname in idx.get_module_names():
        mod = idx.get_module(modname)
        for stream in mod.get_all_streams():
            cts[stream.get_nsvc()] = [
                _get_filename(pkg)
                for pkg
                in stream.get_rpm_artifacts()]
    return cts


def _get_modular_pkgset(mod):
    pkgs = set()

    for modcts in mod.values():
        for pkg in modcts:
            pkgs.add(pkg)

    return list(pkgs)


def validate_filenames(directory, repoinfo):
    isok = True
    for modname in repoinfo:
        for pkg in repoinfo[modname]:
            if not os.path.exists(os.path.join(directory, pkg)):
                isok = False
                print("Path %s from mod %s did not exist" % (pkg, modname))
    return isok


def parse_repository(directory):
    """
    Parse a specific directory, returning a dict with keys module NSVC's and
    values a list of package NVRs.
    The dict will also have a key "non_modular" for the non-modular packages.
    """
    directory = os.path.abspath(directory)
    repo_info = _get_repoinfo(directory)

    # Sometimes you get someone who blindly runs this against any
    # repository they find.  Let them know this is meant to work only
    # on regular repos.
    if 'modules' not in repo_info:
        print("This repository has no modules defined.")
        print("Grobisplitter only works on repos with modules.")
        sys.exit(0)

    mod = _parse_repository_modular(repo_info)
    modpkgset = _get_modular_pkgset(mod)
    non_modular = _parse_repository_non_modular(repo_info, modpkgset)

    mod['non_modular'] = non_modular

    return mod


def _perform_action(src, dst, action):
    if action == 'copy':
        try:
            shutil.copy(src, dst)
        except FileNotFoundError:
            # Missing files are acceptable: they're already checked before
            # this by validate_filenames.
            pass
    elif action == 'hardlink':
        os.link(src, dst)
    elif action == 'symlink':
        os.symlink(src, dst)


def perform_split(repos, args):
    for modname in repos:
        targetdir = os.path.join(args.target, modname)
        os.mkdir(targetdir)

        for pkg in repos[modname]:
            _, pkgfile = os.path.split(pkg)
            _perform_action(
                os.path.join(args.repository, pkg),
                os.path.join(targetdir, pkgfile),
                args.action)


def create_repos(target, repos):
    for modname in repos:
        subprocess.run([
            'createrepo_c', os.path.join(target, modname),
            '--no-database'])


def parse_args():
    parser = argparse.ArgumentParser(description='Split repositories up')
    parser.add_argument('repository', help='The repository to split')
    parser.add_argument('--action', help='Method to create split repos files',
                        choices=('hardlink', 'symlink', 'copy'),
                        default='hardlink')
    parser.add_argument('--target', help='Target directory for split repos')
    parser.add_argument('--skip-missing', help='Skip missing packages',
                        action='store_true', default=False)
    parser.add_argument('--create-repos', help='Create repository metadatas',
                        action='store_true', default=False)
    return parser.parse_args()


def check_args(args):
    if args.target:
        if os.path.exists(args.target):
            if not os.path.isdir(args.target):
                raise ValueError("Target must be a directory")
            elif len(os.listdir(args.target)) != 0:
                raise ValueError("Target must be empty")
        else:
            os.mkdir(args.target)


def main():
    args = parse_args()
    if args.target:
        args.target = os.path.abspath(args.target)
    check_args(args)
    repos = parse_repository(args.repository)
    if not args.skip_missing:
        if not validate_filenames(args.repository, repos):
            raise ValueError("Package files were missing!")
    if args.target:
        perform_split(repos, args)
        if args.create_repos:
            create_repos(args.target, repos)

if __name__ == '__main__':
    main()
