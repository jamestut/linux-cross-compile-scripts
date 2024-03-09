#!/usr/bin/env python3

'''
This script prepares a modern Enterprise Linux host on aarch64
for cross-compilation using clang. This will prepare the host
to target both aarch64 and x86_64 for C programs.
'''

import sys
import os
import subprocess
import platform
import shutil
import configparser
from contextlib import contextmanager
from pathlib import Path
from os import path

# global variables
HOMEDIR = Path.home()
PLATFORM_SUFFIX = None

def main():
    detect_machine_type()
    detect_rpm_distro()
    detect_dnf_plugins()
    install_native_dev_tools()
    set_lld()
    remove_libgcc_s()
    populate_platform_suffix()
    install_alt_platforms_rtlib()

def detect_machine_type():
    if not all([
        platform.system() == 'Linux',
        platform.machine() == 'aarch64'
    ]):
        printr("Only Linux on aarch64 is supported.")
        sys.exit(1)

def detect_rpm_distro():
    # we only do rpm-based distro (e.g. Enterprise Linux)
    pkg_mgr_exes = ('rpm', 'dnf', 'yum')
    if not all(shutil.which(exe) for exe in pkg_mgr_exes):
        printr(f"These programs needs to be present in the system: {', '.join(pkg_mgr_exes)}")
        sys.exit(1)

def detect_dnf_plugins():
    def dnf_plugins_installed():
        try:
            subprocess.run(['dnf', 'download', '--help'], check=True,
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False
    if not dnf_plugins_installed():
        printr("Installing DNF core plugins ...")
        subprocess.run(['dnf', '-y', 'install', 'dnf-plugins-core'], check=True)

def install_native_dev_tools():
    def check_and_install(exes, title, dnf_cmd_arg):
        tools_exists = all(shutil.which(exe) for exe in exes)
        if tools_exists:
            printr(f'{title} already installed.')
            return
        printr(f'Installing {title} ...')
        subprocess.run(['dnf', '-y'] + dnf_cmd_arg)

    # standard development tools
    check_and_install(
        ['gcc', 'make'],
        'Native Default Development Tools',
        ['groupinstall', 'Development Tools']
    )
    # native clang
    check_and_install(
        ['clang', 'lld'],
        'Native LLVM Development Tools',
        ['install', 'clang', 'lld', 'compiler-rt']
    )
    # other important tools
    check_and_install(['unzip', 'cpio'], 'archiving tools', ['install', 'unzip', 'cpio'])

def get_libgcc_path(*, rtlib='compiler-rt', platform=None):
    cmdline = ['clang']
    if rtlib:
        cmdline.append(f'--rtlib={rtlib}')
    if platform:
        cmdline.append(f'--target={platform}-{PLATFORM_SUFFIX}')
    cmdline.append('--print-libgcc-file-name')

    libgcc_path = subprocess.run(
        cmdline,
        stdout=subprocess.PIPE,
        check=True
    ).stdout.decode('utf8').strip()
    return path.abspath(libgcc_path)

def populate_platform_suffix():
    global PLATFORM_SUFFIX
    libgcc_path = get_libgcc_path()
    default_platform = path.split(path.split(libgcc_path)[0])[1]
    PLATFORM_SUFFIX = default_platform.split('-', maxsplit=1)[1]
    printr(f"Detected platform suffix: '{PLATFORM_SUFFIX}'")

def install_alt_platforms_rtlib():
    TARGET_PLATFORMS = ['x86_64']
    for platform in TARGET_PLATFORMS:
        platform_triplet = f'{platform}-{PLATFORM_SUFFIX}'
        # check if rtlib already installed
        libgcc_path = get_libgcc_path(platform=platform)
        if path.exists(libgcc_path):
            printr(f'compiler-rt for {platform} already installed.')
            continue

        printr(f'installing compiler-rt for {platform} ...')

        # create x86-64 repo
        yumdir = path.join(HOMEDIR, f'yum-{platform}')
        printr(f"Creating DNF repo for platform '{platform}' at '{yumdir}' ...")
        if path.exists(yumdir):
            shutil.rmtree(yumdir)
        yumcfgfile = path.join(yumdir, 'dnf.conf')
        yumrepodir = path.join(yumdir, 'yum.repos.d')
        yumrpmdir = path.join(yumdir, 'RPMs')

        os.makedirs(yumdir)
        os.makedirs(yumrpmdir)
        shutil.copytree('/etc/yum.repos.d', yumrepodir)
        shutil.copyfile('/etc/dnf/dnf.conf', yumcfgfile)

        # edit the new config
        dnfcfg = configparser.ConfigParser()
        dnfcfg.read(yumcfgfile)
        dnfcfg['main']['reposdir'] = yumrepodir
        with open(yumcfgfile, 'w') as f:
            dnfcfg.write(f)

        # edit the new repo
        for dirname, _, filenames in os.walk(yumrepodir):
            for filename in filenames:
                if not filename.endswith('.repo'):
                    continue
                editfn = path.join(yumrepodir, dirname, filename)
                with open(editfn, 'r') as f:
                    contents = f.read().replace('$basearch', platform)
                with open(editfn, 'w') as f:
                    f.write(contents)

        # download the compiler-rt
        printr(f"Setting up compiler-rt for platform '{platform}' ...")
        subprocess.run(['dnf', '-c', yumcfgfile, 'check-update'])
        subprocess.run(['dnf', '-c', yumcfgfile, 'download', f'compiler-rt.{platform}'],
            cwd=yumrpmdir, check=True)

        # extract the compiler-rt
        libgcc_rpm_file = None
        for fn in os.listdir(yumrpmdir):
            if fn.endswith('.rpm') and 'compiler-rt' in fn:
                libgcc_rpm_file = path.join(yumrpmdir, fn)
                break
        extractdir = path.join(yumrpmdir, 'extract')
        os.makedirs(extractdir)
        with popen_cm(['rpm2cpio', libgcc_rpm_file], stdout=subprocess.PIPE, check=True) as p1:
            with popen_cm(['cpio', '-idm'], stdin=p1.stdout, cwd=extractdir):
                pass

        # find the library directory
        libgcc_extract_dir = None
        for dirname, dirnames, _ in os.walk(extractdir):
            for idir in dirnames:
                if idir == platform_triplet:
                    libgcc_extract_dir = path.join(extractdir, dirname, idir)
                    break
            if libgcc_extract_dir is not None:
                break

        libgcc_extract_dir_rel = Path(libgcc_extract_dir).relative_to(extractdir)

        # copy!
        shutil.copytree(
            libgcc_extract_dir,
            path.join('/', libgcc_extract_dir_rel),
            symlinks=True
        )

def set_lld():
    '''
    Check if ld is lld
    '''
    def is_ld_lld():
        ld_str = subprocess.run(
            ['ld', '-version'],
            stdout=subprocess.PIPE,
            check=True
        ).stdout
        return ld_str.startswith(b'LLD') and b'compatible with GNU linkers' in ld_str

    if not is_ld_lld():
        printr("Setting up lld as default ld ...")
        subprocess.run(
            ['update-alternatives', '--set', 'ld', '/usr/bin/ld.lld'],
            check=True
        )
    else:
        printr("System ld is already lld.")

def remove_libgcc_s():
    '''
    Removes libgcc_s.so from gcc's runtime library. That file is a text file instructing
    ld to always use host's absolute path to /lib64/libgcc_s.so, thus causing problems
    when using --sysroot option as ld will pick up host's libgcc_s instead of sysroot's.
    '''
    libgcc_base_dir = path.split(get_libgcc_path(rtlib=None))[0]
    libgcc_s_file = path.join(libgcc_base_dir, 'libgcc_s.so')

    libgcc_s_is_not_elf = False
    try:
        with open(libgcc_s_file, 'rb') as f:
            libgcc_s_is_not_elf = f.read1(4) != b'\x7fELF'
    except FileNotFoundError:
        printr("libgcc_s.so is no more.")
        return

    if not libgcc_s_is_not_elf:
        printr('libgcc_s is ELF: ignoring.')
    else:
        printr('libgcc_s is not an ELF (likely LD instruction): moving ...')
        shutil.move(libgcc_s_file, f'{libgcc_s_file}.bak')

# utility functions
def printr(*args, **kwargs):
    kwargs['file'] = sys.stderr
    print(*args, **kwargs)

@contextmanager
def popen_cm(*args, check=False, **kwargs):
    p = subprocess.Popen(*args, **kwargs)
    yield p
    retcode = p.wait()
    if check:
        if retcode != 0:
            cmdstr = ' '.join(p.args)
            raise subprocess.CalledProcessError(retcode, cmdstr)

if __name__ == '__main__':
    sys.exit(main())
