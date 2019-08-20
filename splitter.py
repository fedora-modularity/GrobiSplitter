#!/bin/python3

# Import libraries needed for application to work

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
import logging

# Look for a specific version of modulemd. The 1.x series does not
# have the tools we need.
try:
    gi.require_version('Modulemd', '2.0')
    from gi.repository import Modulemd as mmd
except ValueError:
    print("libmodulemd 2.0 is not installed..")
    sys.exit(1)

# We only want to load the module metadata once. It can be reused as often as required
_idx = None

def _get_latest_streams(mymod, stream):
    """
    Routine takes modulemd object and a stream name.
    Finds the lates stream from that and returns that as a stream
    object.
    """
    all_streams = mymod.search_streams(stream, 0)
    latest_streams = mymod.search_streams(stream,
                                          all_streams[0].props.version)

    return latest_streams


def _get_repoinfo(directory):
    """
    A function which goes into the given directory and sets up the
    needed data for the repository using librepo.
    Returns the LRR_YUM_REPO
    """
    with tempfile.TemporaryDirectory(prefix='elsplit_librepo_') as lrodir:
        h = librepo.Handle()
        h.setopt(librepo.LRO_URLS, ["file://%s" % directory])
        h.setopt(librepo.LRO_REPOTYPE, librepo.LR_YUMREPO)
        h.setopt(librepo.LRO_DESTDIR, lrodir)
        h.setopt(librepo.LRO_LOCAL, True)
        h.setopt(librepo.LRO_IGNOREMISSING, False)
        r = h.perform()
        return r.getinfo(librepo.LRR_YUM_REPO)


def _get_modulemd(directory=None, repo_info=None):
    """
    Retrieve the module metadata from this repository.
    :param directory: The path to the repository. Must contain repodata/repomd.xml and modules.yaml.
    :param repo_info: An already-acquired repo_info structure
    :return: A Modulemd.ModulemdIndex object containing the module metadata from this repository.
    """

    # Return the cached value
    global _idx
    if _idx:
        return _idx

    # If we don't have a cached value, we need either directory or repo_info
    assert directory or repo_info

    if directory:
        directory = os.path.abspath(directory)
        repo_info = _get_repoinfo(directory)

    if 'modules' not in repo_info:
        return None

    _idx = mmd.ModuleIndex.new()

    with gzip.GzipFile(filename=repo_info['modules'], mode='r') as gzf:
        mmdcts = gzf.read().decode('utf-8')
        res, failures = _idx.update_from_string(mmdcts, True)
        if len(failures) != 0:
            raise Exception("YAML FAILURE: FAILURES: %s" % failures)
        if not res:
            raise Exception("YAML FAILURE: res != True")

    # Ensure that every stream in the index is using v2
    _idx.upgrade_streams(mmd.ModuleStreamVersionEnum.TWO)

    return _idx


def _get_hawkey_sack(repo_info):
    """
    A function to pull in the repository sack from hawkey.
    Returns the sack.
    """
    hk_repo = hawkey.Repo("")
    hk_repo.filelists_fn = repo_info["filelists"]
    hk_repo.primary_fn = repo_info["primary"]
    hk_repo.repomd_fn = repo_info["repomd"]

    primary_sack = hawkey.Sack()
    primary_sack.load_repo(hk_repo, build_cache=False)

    return primary_sack


def _get_filelist(package_sack):
    """
    Determine the file locations of all packages in the sack. Use the
    package-name-epoch-version-release-arch as the key.
    Returns a dictionary.
    """
    pkg_list = {}
    for pkg in hawkey.Query(package_sack):
        nevr = "%s-%s:%s-%s.%s" % (pkg.name, pkg.epoch,
                                   pkg.version, pkg.release, pkg.arch)
        pkg_list[nevr] = pkg.location
    return pkg_list


def _parse_repository_non_modular(package_sack, repo_info, modpkgset):
    """
    Simple routine to go through a repo, and figure out which packages
    are not in any module. Add the file locations for those packages
    so we can link to them.
    Returns a set of file locations.
    """
    sack = package_sack
    pkgs = set()

    for pkg in hawkey.Query(sack):
        if pkg.location in modpkgset:
            continue
        pkgs.add(pkg.location)
    return pkgs


def _parse_repository_modular(repo_info, package_sack):
    """
    Returns a dictionary of packages indexed by the modules they are
    contained in.
    """
    cts = {}
    idx = _get_modulemd(repo_info=repo_info)

    pkgs_list = _get_filelist(package_sack)
    idx.upgrade_streams(2)
    for modname in idx.get_module_names():
        mod = idx.get_module(modname)
        for stream in mod.get_all_streams():
            templ = list()
            for pkg in stream.get_rpm_artifacts():
                if pkg in pkgs_list:
                    templ.append(pkgs_list[pkg])
                else:
                    continue
            cts[stream.get_NSVCA()] = templ

    return cts


def _get_modular_pkgset(mod):
    """
    Takes a module and goes through the moduleset to determine which
    packages are inside it.
    Returns a list of packages
    """
    pkgs = set()

    for modcts in mod.values():
        for pkg in modcts:
            pkgs.add(pkg)

    return list(pkgs)


def _perform_action(src, dst, action):
    """
    Performs either a copy, hardlink or symlink of the file src to the
    file destination.
    Returns None
    """
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


def validate_filenames(directory, repoinfo):
    """
    Take a directory and repository information. Test each file in
    repository to exist in said module. This stops us when dealing
    with broken repositories or missing modules.
    Returns True if no problems found. False otherwise.
    """
    isok = True
    for modname in repoinfo:
        for pkg in repoinfo[modname]:
            if not os.path.exists(os.path.join(directory, pkg)):
                isok = False
                print("Path %s from mod %s did not exist" % (pkg, modname))
    return isok


def _get_recursive_dependencies(all_deps, idx, stream, ignore_missing_deps):
    if stream.get_NSVCA() in all_deps:
        # We've already encountered this NSVCA, so don't go through it again
        logging.debug('Already included {}'.format(stream.get_NSVCA()))
        return

    # Store this NSVCA/NS pair
    local_deps = all_deps
    local_deps.add(stream.get_NSVCA())

    logging.debug("Recursive deps: {}".format(stream.get_NSVCA()))

    # Loop through the dependencies for this stream
    deps = stream.get_dependencies()

    # At least one of the dependency array entries must exist in the repo
    found_dep = False
    for dep in deps:
        # Within an array entry, all of the modules must be present in the
        # index
        found_all_modules = True
        for modname in dep.get_runtime_modules():
            # Ignore "platform" because it's special
            if modname == "platform":
                logging.debug('Skipping platform')
                continue
            logging.debug('Processing dependency on module {}'.format(modname))

            mod = idx.get_module(modname)
            if not mod:
                # This module wasn't present in the index.
                found_module = False
                continue

            # Within a module, at least one of the requested streams must be
            # present
            streamnames = dep.get_runtime_streams(modname)
            found_stream = False
            for streamname in streamnames:
                stream_list = _get_latest_streams(mod, streamname)
                for inner_stream in stream_list:
                    try:
                        _get_recursive_dependencies(
                            local_deps, idx, inner_stream, ignore_missing_deps)
                    except FileNotFoundError as e:
                        # Could not find all of this stream's dependencies in
                        # the repo
                        continue
                    found_stream = True

            # None of the streams were found for this module
            if not found_stream:
                found_all_modules = False

        # We've iterated through all of the modules; if it's still True, this
        # dependency is consistent in the index
        if found_all_modules:
            found_dep = True

    # We were unable to resolve the dependencies for any of the array entries.
    # raise FileNotFoundError
    if not found_dep and not ignore_missing_deps:
        raise FileNotFoundError(
            "Could not resolve dependencies for {}".format(
                stream.get_NSVCA()))

    all_deps.update(local_deps)


def get_default_modules(directory, ignore_missing_deps):
    """
    Work through the list of modules and come up with a default set of
    modules which would be the minimum to output.
    Returns a set of modules
    """

    all_deps = set()

    idx = _get_modulemd(directory)
    if not idx:
        return all_deps

    for modname, streamname in idx.get_default_streams().items():
        # Only the latest version of a stream is important, as that is the only one that DNF will consider in its
        # transaction logic. We still need to handle each context individually.
        mod = idx.get_module(modname)
        stream_set = _get_latest_streams(mod, streamname)
        for stream in stream_set:
            # Different contexts have different dependencies
            try:
                logging.debug("Processing {}".format(stream.get_NSVCA()))
                _get_recursive_dependencies(all_deps, idx, stream, ignore_missing_deps)
                logging.debug("----------")
            except FileNotFoundError as e:
                # Not all dependencies could be satisfied
                print(
                    "Not all dependencies for {} could be satisfied. {}. Skipping".format(
                        stream.get_NSVCA(), e))
                continue

    logging.debug('Default module streams: {}'.format(all_deps))

    return all_deps


def _pad_svca(svca, target_length):
    """
    If the split() doesn't return all values (e.g. arch is missing), pad it
    with `None`
    """
    length = len(svca)
    svca.extend([None] * (target_length - length))
    return svca


def _dump_modulemd(modname, yaml_file):
    idx = _get_modulemd()
    assert idx

    # Create a new index to hold the information about this particular
    # module and stream
    new_idx = mmd.ModuleIndex.new()

    # Add the module streams
    module_name, *svca = modname.split(':')
    stream_name, version, context, arch = _pad_svca(svca, 4)

    logging.debug("Dumping YAML for {}, {}, {}, {}, {}".format(
        module_name, stream_name, version, context, arch))

    mod = idx.get_module(module_name)
    streams = mod.search_streams(stream_name, int(version), context, arch)

    # This should usually be a single item, but we'll be future-compatible
    # and account for the possibility of having multiple streams here.
    for stream in streams:
        new_idx.add_module_stream(stream)

    # Add the module defaults
    defs = mod.get_defaults()
    if defs:
        new_idx.add_defaults(defs)

    # libmodulemd doesn't currently expose the get_translation()
    # function, but that will be added in 2.8.0
    try:
        # Add the translation object
        translation = mod.get_translation()
        if translation:
            new_idx.add_translation(translation)
    except AttributeError as e:
        # This version of libmodulemd does not yet support this function.
        # Just ignore it.
        pass

    # Write out the file
    try:
        with open(yaml_file, 'w') as output:
            output.write(new_idx.dump_to_string())
    except PermissionError as e:
        logging.error("Could not write YAML to file: {}".format(e))
        raise


def perform_split(repos, args, def_modules):
    for modname in repos:
        if args.only_defaults and modname not in def_modules:
            continue

        targetdir = os.path.join(args.target, modname)
        os.mkdir(targetdir)

        for pkg in repos[modname]:
            _, pkgfile = os.path.split(pkg)
            _perform_action(
                os.path.join(args.repository, pkg),
                os.path.join(targetdir, pkgfile),
                args.action)

        # Extract the modular metadata for this module
        if modname != 'non_modular':
            _dump_modulemd(modname, os.path.join(targetdir, 'modules.yaml'))


def create_repos(target, repos, def_modules, only_defaults):
    """
    Routine to create repositories. Input is target directory and a
    list of repositories.
    Returns None
    """
    for modname in repos:
        if only_defaults and modname not in def_modules:
            continue

        targetdir = os.path.join(target, modname)

        subprocess.run([
            'createrepo_c', targetdir,
            '--no-database'])
        if modname != 'non_modular':
            subprocess.run([
                'modifyrepo_c',
                '--mdtype=modules',
                os.path.join(targetdir, 'modules.yaml'),
                os.path.join(targetdir, 'repodata')
            ])


def parse_args():
    """
    A standard argument parser routine which pulls in values from the
    command line and returns a parsed argument dictionary.
    """
    parser = argparse.ArgumentParser(description='Split repositories up')
    parser.add_argument('repository', help='The repository to split')
    parser.add_argument('--debug', help='Enable debug logging',
                        action='store_true', default=False)
    parser.add_argument('--action', help='Method to create split repos files',
                        choices=('hardlink', 'symlink', 'copy'),
                        default='hardlink')
    parser.add_argument('--target', help='Target directory for split repos')
    parser.add_argument('--skip-missing', help='Skip missing packages',
                        action='store_true', default=False)
    parser.add_argument('--create-repos', help='Create repository metadatas',
                        action='store_true', default=False)
    parser.add_argument('--only-defaults', help='Only output default modules',
                        action='store_true', default=False)
    parser.add_argument('--ignore-missing-default-deps',
                        help='When using --only-defaults, do not skip '
                             'default streams whose dependencies cannot be '
                             'resolved within this repository',
                        action='store_true', default=False)
    return parser.parse_args()


def setup_target(args):
    """
    Checks that the target directory exists and is empty. If not it
    exits the program.  Returns nothing.
    """
    if args.target:
        args.target = os.path.abspath(args.target)
        if os.path.exists(args.target):
            if not os.path.isdir(args.target):
                raise ValueError("Target must be a directory")
            elif len(os.listdir(args.target)) != 0:
                raise ValueError("Target must be empty")
        else:
            os.mkdir(args.target)


def parse_repository(directory):
    """
    Parse a specific directory, returning a dict with keys module NSVC's and
    values a list of package NVRs.
    The dict will also have a key "non_modular" for the non-modular packages.
    """
    directory = os.path.abspath(directory)
    repo_info = _get_repoinfo(directory)

    # Get the package sack and get a filelist of all packages.
    package_sack = _get_hawkey_sack(repo_info)
    _get_filelist(package_sack)

    # If we have a repository with no modules we do not want our
    # script to error out but just remake the repository with
    # everything in a known sack (aka non_modular).

    if 'modules' in repo_info:
        mod = _parse_repository_modular(repo_info, package_sack)
        modpkgset = _get_modular_pkgset(mod)
    else:
        mod = dict()
        modpkgset = set()

    non_modular = _parse_repository_non_modular(package_sack, repo_info,
                                                modpkgset)
    mod['non_modular'] = non_modular

    # We should probably go through our default modules here and
    # remove them from our mod. This would cut down some code paths.

    return mod


def main():
    # Determine what the arguments are and
    args = parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    # Go through arguments and act on their values.
    setup_target(args)

    repos = parse_repository(args.repository)

    if args.only_defaults:
        def_modules = get_default_modules(args.repository, args.ignore_missing_default_deps)
    else:
        def_modules = set()

    def_modules.add('non_modular')

    if not args.skip_missing:
        if not validate_filenames(args.repository, repos):
            raise ValueError("Package files were missing!")
    if args.target:
        perform_split(repos, args, def_modules)
        if args.create_repos:
            create_repos(args.target, repos, def_modules, args.only_defaults)


if __name__ == '__main__':
    main()
