#!/usr/bin/env python3
"""Build fixed, nondeployable runner OCI artifacts on a disposable hosted worker.

No registry push, live credentials, site certificates or deployment operations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid

if __package__:
    from .build_runner_go_tools import bounded_readonly_command
    from . import prepare_accepted_builder as accepted
    from . import runner_image_cgroup as cgroup
    from . import runner_image_oci as oci
else:
    from build_runner_go_tools import bounded_readonly_command
    import prepare_accepted_builder as accepted
    import runner_image_cgroup as cgroup
    import runner_image_oci as oci

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/containers/runner-artifacts.json'
GIB = 1024**3
RESERVE = 2 * GIB
START_FREE = 10 * GIB
SENTINEL = b'UNCONFIGURED SITE CA\n'
OWNER = 'io.victron.runner-artifact-owner'
RUNC_PATH = 'usr/local/bin/runc'
LIMITS = {'Memory': 4 * GIB, 'MemorySwap': 4 * GIB,
          'CpuQuota': 200000, 'CpuPeriod': 100000}
require = oci.require
save = oci.save


# BEGIN REVIEWED SCAN POLICY
# Generated from retained full reports and per-CVE source review.
# Advisory hashes exclude only image/package-location fields; never rewrite raw scans.
SCAN_POLICY = {'schema': 1,
 'scope': 'linux/amd64 image payload only; not the host kernel or vendor advisory status',
 'source_evidence': {'cna_repository_commit': '509084ac5403afa3487fa193fdbeb47842a80370',
                     'public_rationale_document_sha256': 'fd9d1f9d92e5baa8e01b260534e097d74b3f43dff355b5be9fc8d950dd34f56d',
                     'official_header_provenance_sha256': '210990b813fe81a35a8f598ebb0fe77ef743327751cb8b15801e199c71449db2',
                     'ubuntu_deb_sha256': 'fec8e4a308c03cd98dace67370a8d5a8031c36741796065c9f6e3f9548ab0cd2',
                     'ubuntu_signing_fingerprint': 'F6ECB3762474EDA9D21B7022871920D1991BC93C',
                     'runc_source_commit': '29dd3dc2b13b4123162e5fe132504bb4b15569f1',
                     'runc_source_archive_sha256': '5386e1968f70cc1482b4b2a93dc1a88ea7fa5517fba49b9e0fafe614d5a4c97a'},
 'runc_packages': [['github.com/aperturerobotics/protobuf-go-lite', 'v0.14.0'],
                   ['github.com/checkpoint-restore/go-criu/v8', 'v8.3.0'],
                   ['github.com/cilium/ebpf', 'v0.22.0'],
                   ['github.com/containerd/console', 'v1.0.5'],
                   ['github.com/coreos/go-systemd/v22', 'v22.7.0'],
                   ['github.com/cpuguy83/go-md2man/v2', 'v2.0.7'],
                   ['github.com/cyphar/filepath-securejoin', 'v0.7.0'],
                   ['github.com/docker/go-units', 'v0.5.0'],
                   ['github.com/godbus/dbus/v5', 'v5.2.2'],
                   ['github.com/moby/sys/capability', 'v0.4.0'],
                   ['github.com/moby/sys/mountinfo', 'v0.7.2'],
                   ['github.com/moby/sys/user', 'v0.4.1'],
                   ['github.com/moby/sys/userns', 'v0.2.1'],
                   ['github.com/mrunalp/fileutils', 'v0.5.1'],
                   ['github.com/opencontainers/cgroups', 'v0.1.0'],
                   ['github.com/opencontainers/runc', None],
                   ['github.com/opencontainers/runtime-spec', 'v1.3.0'],
                   ['github.com/opencontainers/selinux', 'v1.13.1'],
                   ['github.com/russross/blackfriday/v2', 'v2.1.0'],
                   ['github.com/seccomp/libseccomp-golang', 'v0.12.0'],
                   ['github.com/sirupsen/logrus', 'v1.9.4'],
                   ['github.com/urfave/cli', 'v1.22.17'],
                   ['github.com/vishvananda/netlink', 'v1.3.1'],
                   ['github.com/vishvananda/netns', 'v0.0.5'],
                   ['golang.org/x/net', 'v0.62.0'],
                   ['golang.org/x/sys', 'v0.49.0'],
                   ['stdlib', 'v1.27.3']],
 'headers': {'package': 'linux-libc-dev',
             'architecture': 'amd64',
             'version': '6.8.0-147.147',
             'source_package': 'linux',
             'filelist_sha256': '32d9e172ef54b79528d0e5e02278c9e37e8d3c9e1a0ed87056a9f0dabdd72a5b',
             'md5sums_sha256': '1b9b3ccd6ca69cd3825c602f31e2fd6364e674ebdd8bdc1706677431fab63996',
             'inventory_sha256': 'ff5df311395966f35d8be4265422da233af4abcb5b89ce923f0257b300f3dbe6',
             'selected_header': {'type': 'regular',
                                 'bytes': 4588,
                                 'mode': 420,
                                 'sha256': 'f96c03a170825a42f0a417efe70d77f05dd4265d7855eb05ba8024a58cc938f8',
                                 'elf': False}},
 'runc': {'type': 'regular',
          'mode': 493,
          'sha256': '266ee9ba59f57fd4285d64f2542641bf70f61aacf88b68dc42ab92ff2767ef46',
          'elf': True},
 'gstreamer': {'source_manifest': {'schema': 1,
                     'source_package': 'gst-plugins-bad1.0',
                     'version': '1.24.2-1ubuntu4',
                     'downstream_version': '1.24.2-1ubuntu4+victron1',
                     'architectures': ['amd64', 'arm64'],
                     'source_date_epoch': 1792886400,
                     'library': 'libgstcodecparsers-1.0.so.0.2402.0',
                     'soname': 'libgstcodecparsers-1.0.so.0',
                     'sources': {'gst-plugins-bad1.0_1.24.2.orig.tar.xz': {'url': 'https://archive.ubuntu.com/ubuntu/pool/universe/g/gst-plugins-bad1.0/gst-plugins-bad1.0_1.24.2.orig.tar.xz',
                                                                           'sha256': '448e32787bc82b586c6cb2f81c9a8ef404fea4f77f25566fe06e597a3f59136b',
                                                                           'bytes': 7046860},
                                 'gst-plugins-bad1.0_1.24.2-1ubuntu4.debian.tar.xz': {'url': 'https://archive.ubuntu.com/ubuntu/pool/universe/g/gst-plugins-bad1.0/gst-plugins-bad1.0_1.24.2-1ubuntu4.debian.tar.xz',
                                                                                      'sha256': '05fefdaf55bd00dfd434caa10e837a4f01fb5ff6fdd54c9c8a0ddddcec15620b',
                                                                                      'bytes': 47348},
                                 'gst-plugins-bad1.0_1.24.2-1ubuntu4.dsc': {'url': 'https://archive.ubuntu.com/ubuntu/pool/universe/g/gst-plugins-bad1.0/gst-plugins-bad1.0_1.24.2-1ubuntu4.dsc',
                                                                            'sha256': '064b23e05d08dd65a8ea16e8325fe0fc21468d21e3fd0347ba5b069617034d02',
                                                                            'bytes': 5850}},
                     'binaries': {'libgstreamer-plugins-bad1.0-0_1.24.2-1ubuntu4_amd64.deb': {'url': 'https://archive.ubuntu.com/ubuntu/pool/universe/g/gst-plugins-bad1.0/libgstreamer-plugins-bad1.0-0_1.24.2-1ubuntu4_amd64.deb',
                                                                                              'bytes': 797390,
                                                                                              'sha256': '6e8da41c0cbb2070f3477cf02360c2ce2eb55e825ba09e12b4270b3254e04f45'},
                                  'gstreamer1.0-plugins-bad_1.24.2-1ubuntu4_amd64.deb': {'url': 'https://archive.ubuntu.com/ubuntu/pool/universe/g/gst-plugins-bad1.0/gstreamer1.0-plugins-bad_1.24.2-1ubuntu4_amd64.deb',
                                                                                         'bytes': 3081180,
                                                                                         'sha256': '56f47c2a6b390722416c962564da6ab5fc7315dcbaa653fe4457a8db6ed34ca6'},
                                  'libgstreamer-plugins-bad1.0-0_1.24.2-1ubuntu4_arm64.deb': {'url': 'https://ports.ubuntu.com/ubuntu-ports/pool/universe/g/gst-plugins-bad1.0/libgstreamer-plugins-bad1.0-0_1.24.2-1ubuntu4_arm64.deb',
                                                                                              'bytes': 770868,
                                                                                              'sha256': '917784636880a70102884e4842b4bf226feafb5b29417fdd71522dff4253c2ec'},
                                  'gstreamer1.0-plugins-bad_1.24.2-1ubuntu4_arm64.deb': {'url': 'https://ports.ubuntu.com/ubuntu-ports/pool/universe/g/gst-plugins-bad1.0/gstreamer1.0-plugins-bad_1.24.2-1ubuntu4_arm64.deb',
                                                                                         'bytes': 2821832,
                                                                                         'sha256': '4948ce79d4e173ec50605188784082f2161e7fd374a584e28db38d44e8cf46ff'}},
                     'patch': {'path': '.github/containers/gstreamer/0001-h265-parser-bounds.patch',
                               'sha256': '7192211e1e5733acde830dda9eb8820a40eb04a17bd14e57cd7bf40e0beba651',
                               'target': 'gst-libs/gst/codecparsers/gsth265parser.c',
                               'before_sha256': 'd0752d902ddf36da0d1dbd7f1eef757147384174928d4de421aae421f345368f',
                               'after_sha256': 'c95c1583d806f6438b024d329247cb16bcd276ce149dc171adf24c483899f08a',
                               'upstream_commits': ['5463f0e09768ca90aa8c58357c1f4c645db580db',
                                                    'bcaab3609805ea10fb3d9ac0c9d947b4c3563948'],
                               'upstream_patch_sha256': {'5463f0e09768ca90aa8c58357c1f4c645db580db': '0067dff9de76b5e68ddb5a008f141df1a879c1ed0ea31a3738f45cc75d1b9111',
                                                         'bcaab3609805ea10fb3d9ac0c9d947b4c3563948': 'b05c34612b45b35c0fb950832733033deb6a52356130aaeacf408ecdd665ca75'}},
                     'advisories': ['https://gstreamer.freedesktop.org/security/sa-2025-0001.html',
                                    'https://ubuntu.com/security/CVE-2025-3887'],
                     'scope': 'Only codecparsers library rebuilt; original two Ubuntu package payloads '
                              'retained except that library. Raw findings remain subject to independent '
                              'final-image acceptance.',
                     'original_library_sha256': {'amd64': 'f1d6e9fe6e0a648dae98ff11560bca9b1c414855ad2f212a2cc10821f264036f',
                                                 'arm64': '841ae4d85d2e85f6269deac782f937993d64ce22abaa80e0bcbe19b1a49f2927'}},
 'source_manifest_sha256': '361200a0384f0bab930307ef64ebe0ebdb7980820fe0ccfef0870459aa6ec75e',
 'builder_sha256': '83673709321b28a55f3abfc30df7be5101f152ef351749eff34513a053e264a5',
 'baselines': {'amd64': {'libgstreamer-plugins-bad1.0-0': {'payload_sha256': '7be90d716bb2ce333eab7b02eca28e5d3458ab15319ff57fd1707b342adc3883',
                                                           'entries': 65,
                                                           'symlinks': {'usr/lib/x86_64-linux-gnu/libgstadaptivedemux-1.0.so.0': 'libgstadaptivedemux-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstanalytics-1.0.so.0': 'libgstanalytics-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstbadaudio-1.0.so.0': 'libgstbadaudio-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstcodecparsers-1.0.so.0': 'libgstcodecparsers-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstcodecs-1.0.so.0': 'libgstcodecs-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstcuda-1.0.so.0': 'libgstcuda-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstdxva-1.0.so.0': 'libgstdxva-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstinsertbin-1.0.so.0': 'libgstinsertbin-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstisoff-1.0.so.0': 'libgstisoff-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstmpegts-1.0.so.0': 'libgstmpegts-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstmse-1.0.so.0': 'libgstmse-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstplay-1.0.so.0': 'libgstplay-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstplayer-1.0.so.0': 'libgstplayer-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstsctp-1.0.so.0': 'libgstsctp-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgsturidownloader-1.0.so.0': 'libgsturidownloader-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstva-1.0.so.0': 'libgstva-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstvulkan-1.0.so.0': 'libgstvulkan-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstwayland-1.0.so.0': 'libgstwayland-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstwebrtc-1.0.so.0': 'libgstwebrtc-1.0.so.0.2402.0',
                                                                        'usr/lib/x86_64-linux-gnu/libgstwebrtcnice-1.0.so.0': 'libgstwebrtcnice-1.0.so.0.2402.0'},
                                                           'preserved_control_sha256': '10ec7183685d9bdeeec1530080e3af46171abc7f5530658a24bbc1021e2caa95',
                                                           'original_elf_info': {'class': 64,
                                                                                 'endianness': 'little',
                                                                                 'type': 3,
                                                                                 'machine': 62,
                                                                                 'soname': 'libgstcodecparsers-1.0.so.0',
                                                                                 'needed': ['libc.so.6',
                                                                                            'libglib-2.0.so.0',
                                                                                            'libgstbase-1.0.so.0',
                                                                                            'libgstreamer-1.0.so.0',
                                                                                            'libm.so.6']}},
                         'gstreamer1.0-plugins-bad': {'payload_sha256': '011385d083895deeb7e6a282573290203422f9ada685f281b4a3b386d3aeb447',
                                                      'entries': 295,
                                                      'symlinks': {'usr/share/doc/gstreamer1.0-plugins-bad/changelog.Debian.gz': '../libgstreamer-plugins-bad1.0-0/changelog.Debian.gz'},
                                                      'preserved_control_sha256': '171f41ad58b6f0e78d53d44fda1841bb28d987e24081d1de1ad537673db3bba2'}},
               'arm64': {'libgstreamer-plugins-bad1.0-0': {'payload_sha256': 'c6b099ca929a2602f08816cec653adf0953ea6a6580fee432398962c48e06bba',
                                                           'entries': 65,
                                                           'symlinks': {'usr/lib/aarch64-linux-gnu/libgstadaptivedemux-1.0.so.0': 'libgstadaptivedemux-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstanalytics-1.0.so.0': 'libgstanalytics-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstbadaudio-1.0.so.0': 'libgstbadaudio-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstcodecparsers-1.0.so.0': 'libgstcodecparsers-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstcodecs-1.0.so.0': 'libgstcodecs-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstcuda-1.0.so.0': 'libgstcuda-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstdxva-1.0.so.0': 'libgstdxva-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstinsertbin-1.0.so.0': 'libgstinsertbin-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstisoff-1.0.so.0': 'libgstisoff-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstmpegts-1.0.so.0': 'libgstmpegts-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstmse-1.0.so.0': 'libgstmse-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstplay-1.0.so.0': 'libgstplay-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstplayer-1.0.so.0': 'libgstplayer-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstsctp-1.0.so.0': 'libgstsctp-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgsturidownloader-1.0.so.0': 'libgsturidownloader-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstva-1.0.so.0': 'libgstva-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstvulkan-1.0.so.0': 'libgstvulkan-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstwayland-1.0.so.0': 'libgstwayland-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstwebrtc-1.0.so.0': 'libgstwebrtc-1.0.so.0.2402.0',
                                                                        'usr/lib/aarch64-linux-gnu/libgstwebrtcnice-1.0.so.0': 'libgstwebrtcnice-1.0.so.0.2402.0'},
                                                           'preserved_control_sha256': 'd974914d6c3eef2b54d23e7c8c9f3628c753965589c5bb5628b1a7ebe2a70720'},
                         'gstreamer1.0-plugins-bad': {'payload_sha256': '4e412ccaa2558b57798e6b3401d215eb6fffd2c1eda7654dd26c0e8313ece05a',
                                                      'entries': 293,
                                                      'symlinks': {'usr/share/doc/gstreamer1.0-plugins-bad/changelog.Debian.gz': '../libgstreamer-plugins-bad1.0-0/changelog.Debian.gz'},
                                                      'preserved_control_sha256': '3c5014c8ed3516693dd5bfdda94eaca2364dea38df6a5636029bfdc4eeeb39b4'}}},
 'advisory': 'CVE-2025-3887',
 'advisory_sha256': '1900462e5beeef08968686315584b82bd8a761c85eacc52714543996a38e0fe0',
 'build_command': ['ninja',
                   '-j2',
                   '-C',
                   '/opt/victron-gstreamer-build/build',
                   'gst-libs/gst/codecparsers/libgstcodecparsers-1.0.so.0.2402.0']},
 'libbpf': {'source_manifest': {'schema': 1,
                                'source_package': 'libbpf',
                                'upstream_version': '1.3.0',
                                'ubuntu_source_version': '1.3.0-2build2',
                                'ubuntu_binary_version': '1:1.3.0-2build2',
                                'downstream_source_version': '1.3.0-2build2+victron1',
                                'downstream_binary_version': '1:1.3.0-2build2+victron1',
                                'architectures': ['amd64', 'arm64'],
                                'soname': 'libbpf.so.1',
                                'library': 'libbpf.so.1.3.0',
                                'source_date_epoch': 1792886400,
                                'sources': {'libbpf_1.3.0-2build2.dsc': {'url': 'https://archive.ubuntu.com/ubuntu/pool/main/libb/libbpf/libbpf_1.3.0-2build2.dsc',
                                                                         'sha256': '87023eb0efe8f19c9066433593b4218f623931d2b5bfb92d31796eace8bd2dbe',
                                                                         'bytes': 2024},
                                            'libbpf_1.3.0-2build2.debian.tar.xz': {'url': 'https://archive.ubuntu.com/ubuntu/pool/main/libb/libbpf/libbpf_1.3.0-2build2.debian.tar.xz',
                                                                                   'sha256': 'cf307f4a37e17847b5cf91b0ed5426c2213a4c0e30a9010a10d4854f23ebe156',
                                                                                   'bytes': 7224},
                                            'libbpf_1.3.0.orig.tar.gz': {'url': 'https://archive.ubuntu.com/ubuntu/pool/main/libb/libbpf/libbpf_1.3.0.orig.tar.gz',
                                                                         'sha256': '11db86acd627e468bc48b7258c1130aba41a12c4d364f78e184fd2f5a913d861',
                                                                         'bytes': 999282}},
                                'patch': {'path': '.github/containers/libbpf/0001-restrict-core-poisoning.patch',
                                          'sha256': '87751eaa64a8f7c410e7829da9c01ee18cb2b543f6cd19e387904b9f554b6365',
                                          'upstream_commit': '394ae398337c5f87e567f6cd63b937fc2b2f6ddc',
                                          'upstream_url': 'https://github.com/torvalds/linux/commit/394ae398337c5f87e567f6cd63b937fc2b2f6ddc',
                                          'upstream_hunks_sha256': 'a7355f88126164ba7f9b74a1f241a7d07390822fafdde47efb7b29bff74d651b',
                                          'target': 'src/relo_core.c',
                                          'before_sha256': '801264d318edfc8e1439c31a25ca955e15ca187f31ec02bc54ddbc45416bf28e',
                                          'after_sha256': 'ff512de8993cd6dc270a732a0d72e82a4d53bab2caf3e2ff92e3bc5c66b5eb0c'},
                                'abi': {'map_sha256': 'a6db7eb8328bf9c46814a670e28712590bf69a906e9324276bdb50c4b2a5aa3f',
                                        'debian_symbols_sha256': 'ab490f7bf94b224f4d5314c5d2600f5894a3b43df353d0d2981f5074b3e5f5b8'}},
            'source_manifest_sha256': '504a2a2d6d2ed407405e4e5f61abb189fdee6bcf44dec545fed6dcbb515a42d2',
            'unpatched_library_sha256': '0e9998e17e38d78e5ce953f89a5e6a7c8b457d3e1bfe71e3ec5a1cfb05bbd1c3',
            'build_command': ['dpkg-buildpackage',
                              '-us',
                              '-uc',
                              '-b',
                              '-rfakeroot',
                              '-j2',
                              '--source-option=--no-unapply-patches'],
            'fixed_files': {'0001-restrict-core-poisoning.patch': '87751eaa64a8f7c410e7829da9c01ee18cb2b543f6cd19e387904b9f554b6365',
                            'src-libbpf.map': 'a6db7eb8328bf9c46814a670e28712590bf69a906e9324276bdb50c4b2a5aa3f',
                            'debian-libbpf1.symbols': 'ab490f7bf94b224f4d5314c5d2600f5894a3b43df353d0d2981f5074b3e5f5b8'}},
 'identities': {'headers': {'class': 'os-pkgs',
                            'type': 'ubuntu',
                            'package_id': 'linux-libc-dev@6.8.0-147.147',
                            'package': 'linux-libc-dev',
                            'version': '6.8.0-147.147',
                            'purl': 'pkg:deb/ubuntu/linux-libc-dev@6.8.0-147.147?arch=amd64&distro=ubuntu-24.04',
                            'source_package': 'linux',
                            'source_version': '6.8.0',
                            'source_release': '147.147',
                            'architecture': 'amd64',
                            'package_path': None}},
 'advisories': {'CVE-2022-50240': ['headers',
                                   'b2ab0615d6a746a196315b8ce869b6d2d14cc4305a7d77bc03b1998ce215675f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-35948': ['headers',
                                   'deaa02520ecfffb4a799df06edd4a5c80bf572546cf9f8c4639d5f1a5456f533',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-38581': ['headers',
                                   'a32589ef41a29da5d378fe579cc1cbbb272bdb62968949b4cd640bee99921257',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-40954': ['headers',
                                   'f74bcf797cf8d07127f5a4c6dc4fafa557c4c9946ea16908f61510aad04ef4c4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-44964': ['headers',
                                   'b28da464e529f8840fcb45fb92b4830a0beb6cb34b9c3289e2786d5fb894cfd7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-46742': ['headers',
                                   '996de74679a89f6e17da33cf2fe20e644526ca243a5d052b83413fae728ca8a0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-46833': ['headers',
                                   '158b8c222185ef1501692c1b45541e29cc6557a0ba356832d4df03f2d3ebcf0a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-52560': ['headers',
                                   'f07900cc74ecec9bde67cffc10e6b269651ba1e1d42e41aa1b8507efa271ae3b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-56591': ['headers',
                                   'e15df3a3cb3e6218c634be65be241b265a5cc8ae0d6f168bfe5dc21a44a1b793',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-57857': ['headers',
                                   '2758201b992dc36ba576b927f7e19833c49888532120ce53dc8fb085119f48c1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-58098': ['headers',
                                   'bad868712db6798a85bdbf991718022d2fabc7621f5fc5e5f4ab9cfc282c5d67',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2024-58100': ['headers',
                                   '7ed2775ec2fbd939e93ced46f134d053747945719faaad7e231a67c3c4d6a5e8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-22043': ['headers',
                                   'c7c0248249b1bd21cc521ac4d37b757c1a5ed502ea4cbbbe3e77cd5a6038d6c1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-37743': ['headers',
                                   '7743a25470d61944c4bda15cf94c0be678254ec63ab3f12d7ca7da14de77f13c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-37776': ['headers',
                                   '98874f1fc8b80236441090d3afeae9d75a8b92746292bc2ba686164c86539fa3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-37906': ['headers',
                                   '9d4885c5e48b3819f59142725db6652f15c71732adfd61a80f08990ef11357e3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-38204': ['headers',
                                   'bdc4cc798c9db2a236c5a9ef20f8c9368ecd23640252d47d169acf9bc919346b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-38717': ['headers',
                                   '50cc0b64fecfd99edf8a5ecc50e21410be5c1be24aa9e62b1bec5a63d1b4afb8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-39933': ['headers',
                                   '4509d0737ee65fc0549c29af5398b95c44b55186c7a1eb93396571f94eecefce',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40025': ['headers',
                                   '759493f8b32f2801d609dd0200e7365c8aa8cc658fa6f7c4be7fefdea78d30c4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40064': ['headers',
                                   'a0acaccbbcc8259f9d0b8acf268ab6a7bae15ad94e0fdfd657cc0d1b4d0324e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40074': ['headers',
                                   '92135a7fa6ace351fcbdb406aefa562d6ff3593734cb85fe817796ed9a62469b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40075': ['headers',
                                   '142b9341e2da1cd38c4f6dc5ab5c3c4bdfab8afc01c10b501ff551a141007578',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40139': ['headers',
                                   '8867b629953fd481f9f12f858880545e208a16f4fb7ed1a5a7d8b78d05796f0f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40158': ['headers',
                                   '2ce96887594493425ec6c8f5d3157f40429be4264069b9e928b398183e6cc8fa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40168': ['headers',
                                   'f09eaf5f4b52f8fe869e174d5d7705b487e8f93b4c6c1da84a8c07753401754a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40170': ['headers',
                                   'd210e463c43c087da8f05c93b72398a41f32ea164cddaba1f06f58e6d0e128e7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40190': ['headers',
                                   '5ed5cc749b825b1e1df285b6030bc054f855df1364804a749b255aae068cbe0d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40274': ['headers',
                                   'cdb0ce31e4c9b478b182a6e82200e6ead4f5b4dfa0ab5df7d64eec526a18f32e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40344': ['headers',
                                   '5e55d63bed00c6a62b2e98b0d8200c33b560d29e7f11393c42628a31ac5eb42b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68251': ['headers',
                                   '7232f720b38cf6412c2dec57c72dc19cc988035bae2512bc9d3445c265d62219',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68304': ['headers',
                                   'eab70e49a4d7286181389698c70b64915770707d308c77820eeeccd42c3f5f4f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68360': ['headers',
                                   '34105ba8b9d1990a8e40ed2274ed3e40170f8a2e5a1d4cf4b701e7b19e1b49d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68729': ['headers',
                                   'f30ee115336990b819cc58448e3c28d140b3322b1ef2d8195da577df45176d67',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68730': ['headers',
                                   'f1a3b0e8d4a6d4fbfbcca33838d9699113fb86335d5aee17a3b283d041cde2a7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-68745': ['headers',
                                   '7465f9d1e60953ba8a98ff49712da97d7655c120a69b7434c01b2de8aa7b0bcd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71073': ['headers',
                                   'ed0a15a8c7373de1e58764f159111cb6ab03e0ecda741e7acb1c95df74941acd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71074': ['headers',
                                   'f83f58b7675895b9cb49bc121f8681144f65d679427dc5453b3310c87af13cd5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71202': ['headers',
                                   '81998cc0b1b8a763143b50c396b99f74c56e73d2aebc7b7229fe535ee66e6e44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71315': ['headers',
                                   '7457d1aa33fc649ff115107e6f0aebbe2d458878c9e6ea554afb6e3ed10fef1b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-100074': ['headers',
                                    '89184b6b8b11aed2fdb74e204282ebae176b2bb7af459c6428659b716a606d2c',
                                    'kernel_code_absent_from_header_payload'],
                'CVE-2026-23208': ['headers',
                                   '97b6fcc00e0c8c80440e1bf2b8c44859eb6d1e020d07c45d196a38c108369f44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23239': ['headers',
                                   'da3fc0e8fd0761eb18dcf0d8d91a0072137f8e7f491bf392d61ffd338def7755',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23240': ['headers',
                                   'a770b0182bf4753a1a729c6c60219a984cf5d495e8b05a981c0e25af1b39bb36',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23265': ['headers',
                                   '9e1a0666ebf653cf6752d19fa5a294dd6ad83cf7febc930f4cf33dd9d1036cbd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23377': ['headers',
                                   'bac171494db887e2a1d6531df4e3d63c5cacd67033c1aaf7aee3cc590da828bc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23385': ['headers',
                                   '1c41256884d8b1571492233954c0197f60079ff48e2c50e83b55010724177d85',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23393': ['headers',
                                   '8e0b6292fcdc1e02f4ecbd9f5e828a66987fc9e37516e27efbc18e027ce72985',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31410': ['headers',
                                   'b711dbc97c407ef4d10af6a76960720f8959a93d8dc4e267f2904d87f10a15bc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31536': ['headers',
                                   'a82f101f905975e45053c03a5df918978570598de68b8cf07148cf965ba83345',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31692': ['headers',
                                   '5fe5c4a43495800c26360cb900412c789e2513c823767b18aaae8e697f818a60',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43009': ['headers',
                                   'ada6b96a118561cb9e8b8e3063cecff1eefa99c484a5285e381bbeccb6cdbceb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43034': ['headers',
                                   '6920478d51f530d8ae652539e2aa5bd83302ccc6e9903fbf937c77152686ec2b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43042': ['headers',
                                   '3e84a61b4dfe63d41d1f17ae8a8a69e2ed17642bdb616d26db3ef90067d7a5fb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43048': ['headers',
                                   '1ced6cececf794b89439af1e993939d7a70b1622218c6d6f9dea5545a5b5579f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43213': ['headers',
                                   '4a986206ca00d5447dfef988db65c89f5e26a00737ebef41644a6f0a40d35a9b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43263': ['headers',
                                   'f05f49787d8b50673563079ef9d1e57f4b951fc7c9b6659bbdf7f4e26fe98272',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43379': ['headers',
                                   'fee167fb8ea6f40af249708bd208f93cb16f8c83a082819457569c27213095a9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43464': ['headers',
                                   'fd89bb44409405750700a88fd61bf695764f49ba8637e2f00456fa82d925d47b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-45940': ['headers',
                                   '919a499ee7ac0223a4d63bddf391f4d0e544e1385971bf2c4d664426d25d0f2a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46181': ['headers',
                                   'f2e6e3f31d2176dd4df4302c7c7c55f14860da8368fb1901552714df87e5868b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46330': ['headers',
                                   '042c3cd0693ddc43c81eae2a9ed8e9c42f1911ae67180a6f8a467acab76a4943',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-52946': ['headers',
                                   '351299a17887bb06efd87e403b3e1e0323630292385ec35ce9906977e44670ed',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-52991': ['headers',
                                   '1ab43add48dc3d3ae12b4239fdfb5ca2d500c57d56814db7ec64c08d17b88217',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53024': ['headers',
                                   'b0d3b904f9f82e1ad12b28c3c1fac60b8ca61989e56e84e2b17764b474c180df',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53178': ['headers',
                                   'af3d60e4245712d33b5ed0a3a1d2e374bb9cd62aabcc297b4dd0d16875ec2f4d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53250': ['headers',
                                   '503f8f7fa9bce2e4e77c49ed548abc23d606d34992d8f2e99957826830f5ad22',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53281': ['headers',
                                   'cf3e86b32acc4bc24693f086a990f0cc4048e7c362ac39c3ad66b54ad8024f88',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53377': ['headers',
                                   'e62331dbe94d17ec8da4589166ca052aea23f05e30e2a076341458382527a645',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53401': ['headers',
                                   '9c100e9f7db5f510b7cded3957414979003abff44154663c93758bf5b5bbe517',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63819': ['headers',
                                   '10b50d2255ec1812e9e8bc6a0fe346409be749a3a6533607a8d491673d36ae17',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63825': ['headers',
                                   'c78dd37e919e43a697502a1612525ed10403929f83523e85bdcca287a95105cc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63879': ['headers',
                                   '0a1a7be046b501f07af46542f2ecc05c042f4a05f6a5a841d7fc38e2695bf6ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64036': ['headers',
                                   '3be34083edf7670af3d8e1fa018ef4bbdf5b954419a692a3dfc39b7fd2286a7d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64052': ['headers',
                                   '2b7edee0cb7e0d63776e38c081bbad0aba4b204f2820b7604d23fe15def23b76',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64077': ['headers',
                                   'b54c0398fe6dfda326045fb0d09dbbbf59fe3c3236eb036ab967abd8c9ae7f24',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64078': ['headers',
                                   '26353320cf3af140098018bf9adedae362b65c34d0de5460c9ee6d5978cb595e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64523': ['headers',
                                   'efbf557a8cf10133871c5bdefca3e1e0ab8f7985159e7452bd353e5b682538e1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68081': ['headers',
                                   'f381e5fa138270c76661e6dcf9b2d7fe477f645b3fcfe1ff26fb93181c7e565a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68103': ['headers',
                                   '061b53ee8c456b2f70ad027be0268ae0fa6edddb57104ca0fcd72495b50f36ab',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68152': ['headers',
                                   '52c31c419066b85b34ba096fdd3a726c606da4dd3616a9295f9c5fd19d82810f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68198': ['headers',
                                   'dcc32f32cd32f6567ac1838ab22929b236e6317aba24a886639af0df83edb2dc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68245': ['headers',
                                   '842e56f4f1eb4677342283b3ac6bac967bc6c1d96fcbd7b527edcac9e660fa71',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68257': ['headers',
                                   '0e88bc610553f21ae5376a8559bb00f98cb3712e857a17526f2859018d11bdaa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68323': ['headers',
                                   '6334bfa346cd513b346331952c89abd039b239c8e8f0b91f89031a92dbaaede0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68401': ['headers',
                                   '8fe7b91967dc77ce9b5c4d4beab261152403d363c28989d415c012b6916e54e6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68404': ['headers',
                                   'fce88d52cab170d00b8dfed93fa5ea0af0ddcd40ddcac4cf8e54a372067d7148',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68409': ['headers',
                                   '0145f35b93498f6b2ccc13944d7431eca43238d3d2819c30cc28f8c845750dad',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68426': ['headers',
                                   '6f8af88a92c5c68d6170d9db198f0d1394c43b75f49c2418fb458d230dda939a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68447': ['headers',
                                   '4c3d0762a06cda64b11418405e1eca6559218b1098c6e6fdb14d54b2cdd07352',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68451': ['headers',
                                   '974013ae18ea9fd05f92e6ddb04ca4a3bf532b5b95b2e4247575c0a089639fe0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68470': ['headers',
                                   '5301b95f910f6f02f246eedfd5104fc61afd3f7154118f84ca7ea312c6f5e4b6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68471': ['headers',
                                   '547efdadb198c166134b3332c4291eedcaf83757a8b7b17a3e73164dbcbe3b3c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72111': ['headers',
                                   'b768b2839f4c658f21a78e369165603dc68fd9e07e118deeeb0995cca6409601',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72162': ['headers',
                                   '3b8740bc0496f582bc6a417ce87aa67365ac61413d457f037ac851b6b8ccec97',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72198': ['headers',
                                   'ef1b6113220c562adfe24decf629b40255e7e803d1733a33ddbb00db12374d1c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72288': ['headers',
                                   '50597dfdd4f79ebf136d82a3541ac7ff413114fd0e80220dfe7f37793eab3bb3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72375': ['headers',
                                   '35edaf92d3a2568113d7d4e6c90e3af1193af9fd68c29925a37b1e8c23f1d134',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72380': ['headers',
                                   '4310a189b209e2340e4f36fffeaee7f4e605f258d0dfe24b2d87b796a0565812',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72383': ['headers',
                                   '5e5987458590b6aa70d4e61840a01e230d8e93a7c30326f1397950f2e512dca1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72413': ['headers',
                                   '32ae97e5a4aeb3189022d8ee4a69aa7aaa1d2c12b69185cb5930e25e01a1b83f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72423': ['headers',
                                   'b52e73cdcf6a70e718cd88bb3baaf17a7775404d51e72487a9a96c1026ca272d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72472': ['headers',
                                   'b211371e6a7c2b6a280af73cab9cf6a7b7085c0cb7e711bd42cf3b463cf65cde',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72493': ['headers',
                                   '0a6123f855191d5efe8168a6b1fd4fc4e0ace0992ba30c52feff5cae12980456',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72494': ['headers',
                                   'a7d3b1c071a02f2eb9331307c4a70197a5606160294646bc33e53d06be7c4ff9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74257': ['headers',
                                   'a0a81b527cfc7639ff4284b2c95ff3a2937e74243d26c136d2bcbb8fb342282d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74268': ['headers',
                                   '728938a103430df83238bf331bd484bf064c3045ff98c323d71b84332f6dfd91',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74269': ['headers',
                                   'c72fc796f06c7ef062e470918dce6628737494ad01a62cf71f462e9be6baa6bd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74314': ['headers',
                                   '00be392e686604b726613b8155e10787a3092f52970c9baf228b5fa6207e2c2a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74317': ['headers',
                                   'e29e14b55717b2932483377d9489faa46f99be20d76ae22c40c079af5e99e850',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74334': ['headers',
                                   'c52de62df71af63b6841b875f7f8b7c54369ee94554b3837bdeb2fc1e9de71bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74344': ['headers',
                                   '417ebc66a58bcf93fe28012839d22e2b4355f26d5217d33b70b967b7e0f9a35a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74347': ['headers',
                                   '2325ad754db392a4e339ba81cc2e7802eba0ab81b82b6acfdd245adca84ad262',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74350': ['headers',
                                   '746e3234bdd839404d1e24e0c05415603797dc4841b05738efb150ad57afcf65',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74385': ['headers',
                                   '430a10d694847a68ada86a6b080bbafdb02a22e1a84a80db7f00b41732c38afc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74388': ['headers',
                                   '5a35879c80cd3aaf6ec71bf0c65501709913a06ab1ecdf27e37897808c65cba9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74446': ['headers',
                                   '79bd27bcf37fb9ce45eea6e15591a8ee44f76327594113b4170a87665d3ed05c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74454': ['headers',
                                   '1f25922af5d5275828b8cade440c3aceffc8e2cf47167406e6a8bb083b69fd88',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74465': ['headers',
                                   '94bcf1a1a76dcdbf3e5619fc2f0f4468beac8af514689d87a3bffc68f4aa42be',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74470': ['headers',
                                   'b83ad68816bcc8e7d55fa7ddd181c35af6195294811ac5951ae0b7f710c4ed7d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74506': ['headers',
                                   '45af81667c0c02ee601f8eebf6397f1489638ef18ad2217665ba56a4c2d2b675',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74510': ['headers',
                                   'bf4aeba6d1ec67a16a14cc34d032526daaed7f717bf32bd0c9f6e62ca9791b59',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74522': ['headers',
                                   '7238351ef6806c331b641b789b391af0758c55f2a6fef73b498d0e99ca873613',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74530': ['headers',
                                   '440cdbb611b6df383274228d30014bf6adbe3489b5ae6b05f50c6f2ad943b03b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74533': ['headers',
                                   '30e1b68c859ee3f5df567caaa5585017e20e8b46b1bf8bafc865c4c6145abb40',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74534': ['headers',
                                   '41ed1c385a799cb3a80fe354f644a21f8352cfe9358b5da29c270e2df915aab1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74535': ['headers',
                                   '2857fc41658117c86ab2fb81d4feb13d8324f5ff50455f9bc7d319eee3a0988e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74537': ['headers',
                                   'b3624d3f059ed8bbb8d560d63d37ae64f510453736fe97e36516ba286b6e1f78',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74565': ['headers',
                                   '040f75578b231214ae40182078aab2751fca2851180c3bb7fb1244e7cf080e00',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74578': ['headers',
                                   'f9d01e46ab590e06f6e7bf3e1f419ddc4bf4b613fba1e60dfd5eb15d8d1e596b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74677': ['headers',
                                   'f904bc42322432e08f10ffa09e9307599e5807facac2aeea8ad6be9a1216203d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74684': ['headers',
                                   '00ee1c7bac4a7c04190ca8b21008612bd582c80038c5190f248b70fe090e9726',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74700': ['headers',
                                   '2f3de41d40919f1e185293b7e93c903c57694a3bf7ac3f4484172a3e91b2b95e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74714': ['headers',
                                   '45a4bcefbbd26dc6c769b65a69253c4b90d58d23eb45fbd2f40bb2008e4f0b07',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74715': ['headers',
                                   '6a08e9a4a6db6af68f2274b1195773e9e8d84351fc403865f4c19c51defcad8d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74723': ['headers',
                                   '0e0e9254602d9e45c0c6636fd61ecea3c04cdf8c62453c11e9e69f758718031c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74724': ['headers',
                                   '54d782e52cba0f89ded6399c461faf0ae4045f032c3ff8a7140b05e62d636ca2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74744': ['headers',
                                   '73c49e96fc6a755bce89eed8354fc33b5bd35cd5786b062f8257ca92cb995870',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74752': ['headers',
                                   '7d14c48ae30be4468a0b7464774a902aacf2adeecb9871c25054830ba235a2cd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80551': ['headers',
                                   '1a16b2ffc4af3df6983934c7c43b31a386fa5e58222ddf3734bec2434c44238c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80554': ['headers',
                                   'ec2695090d432d47ae91c8351e9d4f68915ec4c455f697d0570bd651270ba436',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80631': ['headers',
                                   'bb80fcc3c4025ae2b2b974277cf090094409f50cc7fa583709e49297a4688c5d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80634': ['headers',
                                   '9366073595c8c4a86f9d2380b491f78654f740297e96110606630bf51f507a85',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80668': ['headers',
                                   '6600589223c19ffa3c30a1372dbd4d61292643d32060c669ad5859217258c2b1',
                                   'sanitized_uapi_header'],
                'CVE-2026-80671': ['headers',
                                   '5b5916cfdabd44a076eca143b2cc93b2852e9b0ca0de75d2ad887e7d5ccc1af0',
                                   'perf_payload_absent'],
                'CVE-2026-80681': ['headers',
                                   'ef56b876ecd9b0126f7ce4d2a68cd31113d354b122ee63402afd9f5daac4c3a4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80691': ['headers',
                                   '37539e86afc08568210bd3b1c76969e6f439f26a393c6de9f86fd65c82a62014',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80692': ['headers',
                                   'c20dfbcc688efba644b1a2d3cf7ff548ef1ad3382ee41bf2e87542d4d8374f2e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80693': ['headers',
                                   '5059553fddd76cc8807556f390e18ef97ac94eecf7512fcc9874b79e4f730171',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80700': ['headers',
                                   '2f7f8ba19429dc97b8ebc4d34edfe8f82deb541bb2be4226ad280ddff0cb3185',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80702': ['headers',
                                   'a142d22c4e3ffbab6c414efb752865a34031cff80cc57aa6dec3be5531872b44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80710': ['headers',
                                   '1598dd08f6118a5816466d9633d4ad1f3ff250f9e84b16ee720d383e0e5acb3f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80714': ['headers',
                                   'ddf858f24f982cf96bd8da17a068e801a6874dfaa76a3cf9a4df43c8acc8cf6c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80716': ['headers',
                                   '251896e8659ab2a45566e7a62d6b7f5936e1d3f462d26e2354340f6f447e2896',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80718': ['headers',
                                   '9dcdc993ca5f79a31d0f500f9d7d454b9faec28423ed57688d27e3505031a6ba',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80721': ['headers',
                                   '9556006a6bc9ce66c65965eabd0b8dcece1a9a100d82d4b1449881b179cb1e63',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80725': ['headers',
                                   '0d1a6e9f7de8b95083ad05bbb520b76bf5329e673b56bd62832d9169a017fae6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80837': ['headers',
                                   'eebcf1babce8b6b1ac3373b45950635c8f2538c31b4391e8d1070359cad4b77e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80841': ['headers',
                                   '148ad83cbe3aa4181a9671597d37c2f254cfd82c9cc6183f3b5cbf26bd046dac',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80852': ['headers',
                                   '08508c52279099e790bdcc979c291d0b6be7b2bcae0a1b7569722390372ca928',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80864': ['headers',
                                   '4081dac6c2c27989cfb6ab67be17b294b15feaf46f09b7cbfb6d723679264dc0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80891': ['headers',
                                   'b629e6dccf37cfcea2382c14e5d63af0703159dcab4d1f7d90540ffcf62072f5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80905': ['headers',
                                   '5be72ab97a6661477de9d2f1cb8dff77e7dd97f992289a12e5f8d36734b46cec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80937': ['headers',
                                   'b9eef4791b3322b0d7dc5b554fd2e5707f8d1f2350d8504fe84716d03270ab4f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80980': ['headers',
                                   '50f7302002ba291986656329b21600683a13172fb40e69c1e1cfb0aef0c1bd84',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80982': ['headers',
                                   'b013ab9cfd2522856676eafc1f2ca427a853e7ca2effc7d2889df97606c72303',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80991': ['headers',
                                   '5df72667505e45543c88fceb8c0ed7d21480ebab9b48f6deee2e0ccc755e16ec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89448': ['headers',
                                   'b7f23740a9e92a08c755ddb5890f5e8cd61e2f0e047b2757c856b3a8b24ef7cd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89481': ['headers',
                                   '6fb65d9eeff8ea08f2e782a9d1616b3030442ff643b7225dea2717d40d4f6c9f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89492': ['headers',
                                   '404f1d55066a553a5d35ab5f1046dd3b753702a73a804f7df02b7348e614c5dd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89520': ['headers',
                                   '2ac429f79c01f2b22563c00817ebf00d3fe509130d2c79d4e3e7140bb821b63d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89526': ['headers',
                                   '8ecf076421ac40f3c2f0b200fcea36093d590f2077158d018a0d0cd118419f9f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89528': ['headers',
                                   '36419024cdbd607d71d59f302877cf044c1c29d15ce872d1e459055e3861ec7a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89530': ['headers',
                                   'c46ca179828fab2601aaf0a0c1597c4f7438f86c751e9065feef9ac6e8497594',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89531': ['headers',
                                   'cea5ac80b24bad9d1dcbdf96e2ce40e762d07709cf362e0958fa9c0b35658c96',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89537': ['headers',
                                   'a2d9352b99ea703c4c59246a732cc5e3c488245c2d503f5f44f2012c1e84fc54',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89544': ['headers',
                                   '057367779465629de08e9969ba5a0e51ed7009058e3f3d9409c9ee3622f925f7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89550': ['headers',
                                   '4cac6d75a02764ba214260e543ac6e32d99d2cfec8008f9a9ce409709b85af4b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89564': ['headers',
                                   '7a20969f2863607be8289699e56508a73a58cd8027b50ea42002eedf4ce062fb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89565': ['headers',
                                   '4067eab46a49b9a50118dc99a7566aa2c47c6323047eec69cee888a295bab89c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89580': ['headers',
                                   '003111c77d0f762198e473ab381233a19dd6048b0da8c973f37e10b324c46adb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89622': ['headers',
                                   'b151cf87c703d443c4948146f21ffc292fb29a43efd90c8eaf536cec1fc956c6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89631': ['headers',
                                   'c8bd7cc048e5d6d0335f23ac1299f677e896306cea1e5fc44802ba635a3fdbd3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89633': ['headers',
                                   '94bc9a52f359b80606796c7ecf03a684d1094c1f77c1e2f4e8487db6b8ab0c4a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89637': ['headers',
                                   '26875240fa79ab62cbe328d971481a874b211e5c8be909617906ae20b085caed',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89638': ['headers',
                                   '213999563935b2716b9527e4aa8521999580365deaae9c6c5e8dc9d7e4e9d9ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89652': ['headers',
                                   '462771f5f52ef9ef4926e9c338107b81480f2bd65ed7b91722414d48b4aa7a96',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89659': ['headers',
                                   'e026898d9f67fd84ca014db75e72b1dfe655c9d7325ad1599a191d83d9953aa3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89675': ['headers',
                                   '1fd03d4a1216e5fecfe67124593d4ad9f04364f3644cfd8d01f8f9341670ea3c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89676': ['headers',
                                   'b06e57523064e6373884d2e4e116675983450945da514bbd3971233efcada61f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89680': ['headers',
                                   '6d43d3c519e0a3709bd7e3a2de238b0c6de16695d44cec684da500232eaa40d7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89685': ['headers',
                                   'e24f20a3782ef7e3ff68921ffa42bedf4df81bb6773faad0bdc17185cafc8503',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89707': ['headers',
                                   '8ad4ce203e659759ee74ce4828fa43b3d14984bef246a07318f030a9e810244a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89708': ['headers',
                                   '3e08adc9f9e2350f804f5d47f368e9eec678676badc06edc8c8321b1dda98702',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89788': ['headers',
                                   '953973e4bbef33c06fada15f8057bb85f9df86c7a42bf6bce845e23b8ec9cd63',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89803': ['headers',
                                   '7afa2375856b01dc5a165ca8e43b6856bdd9f3eea2c5c7daa64f4c33c3764a60',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89810': ['headers',
                                   'c45f2d9af7bd4dc1769dc5ac61d1fad0f056ec6d63288f3928470be9d71c0577',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89837': ['headers',
                                   'de1647992065c742dff75f1eae573b652d0e4c0ce5d524464bd6fd1bea71a725',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89913': ['headers',
                                   '5541d33bce2d38eb6053733454727b974deceeb68ff756b5fdf404ae01cd366a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89922': ['headers',
                                   '99e9594aaee85ee1272a7fd2a77658f6972f6aa4dfed8cce7a03b805b2346e0b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90003': ['headers',
                                   '395af9b366d58860f70148ec170c9fc3d1ca861105a4cc266d6771bee5a8e6dc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90037': ['headers',
                                   'c3ee608eb4d9daad760695d822ce94c02da696118bff0b5e9703f5b44df6bbad',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90048': ['headers',
                                   'bb7974d1ed73c2f23936d3269656aa1f812b6611118085a337a2c8cdf6876ba6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90057': ['headers',
                                   '6ea98b820e35a89eacec427ef5e692f9acc872aaea90e631f12a0b5476d607fe',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90092': ['headers',
                                   'bdfb1872b87bd641344ef67ac911da19ff0583fa1dec9e361d811ea3ab1ec7cb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90093': ['headers',
                                   '84dbc160e78f0da1107a67794d6bf42c95cdc9dc03336b12eedfc0c3a029be95',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90104': ['headers',
                                   'f2ecc302f1b2a905e07465d540cb11b94f3aab3adfa887d5c9ae12cb4bee6101',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90108': ['headers',
                                   '8cc7942f626539695e3d2f12f80f5a2f3ab5cf1c5140f7f276a53c7da47493cd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90111': ['headers',
                                   'c48e33d6ee5f56efb9bcfb9433b0c3c95e6d25384afa37d475f95392f0207397',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90148': ['headers',
                                   '831b30bae2f93b73782bc783cf28f5a56a3a6a88b381e009cb31eb690af00ae3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90149': ['headers',
                                   '6bf4b5e03947a7f833661158f6f6ab13655ffb33d07b0ed8209aa0ae0d4d8499',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90150': ['headers',
                                   'ed112e8987403c6ef6816c9bf7a214b93d927252f882807ac9bd59f838ba1c1e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90152': ['headers',
                                   '90894db443efbdbdec87f767463eca54a709379f1cee45032665e6746cd69331',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90155': ['headers',
                                   '0000817b0bb76b2a5a896704b2d9a28582df027844bb49f3df012b4b4bb2aec7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90161': ['headers',
                                   'c457b3700f22861a12d298cb7b51e3500c7e46bdede8c7e40ab7e713128ee0bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90162': ['headers',
                                   'bf2b49de891d23ad4ebbe8b292ba9c566f18f2017e31f1c26a9db68e1023036a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90167': ['headers',
                                   '388c6c9ff017c0920204bf77401fa4d900f961152bae08c75038516778888155',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90169': ['headers',
                                   '63d0dd4418e075cd9b5d93d328c0aa69ff92d6f18434cdc49a3c73dc9156ab18',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90199': ['headers',
                                   'a90b5bb392f8278e90d73a7fa4c1b393f1a504cbcf65134a5c45a3e33f843f53',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90200': ['headers',
                                   'c196b75257b0d65698a6568123cc61d6bdd9868d7170637e7bde8168e4bbc9e1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90204': ['headers',
                                   'd6f0ee6d6414ef33e4e6ebb5aad81cf5e79727e49ec884cf1b309483d513ed7a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90205': ['headers',
                                   '4c95e1d4eeb20d15b14259b869fbdb7616abe1e3927cc896fbd146de1f7a7a4b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90217': ['headers',
                                   '42cc3a66fd4fe4b7b5d50c9bc13c2dcb30df007f28c3de82e3d8a13111059ba2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90227': ['headers',
                                   'c1e1c65c6d322b36782d8bc10e89e899e311ab5748d4b860d662f7283b6dd997',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90237': ['headers',
                                   'e129323683a0b8049ea5fc341ebd097521a0da1304b44088a769842317e1e0d0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90256': ['headers',
                                   'f313a0e82e96d7979346590b92c022f0ac3cb6d14c798d805373173af7cf4bf2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90313': ['headers',
                                   '6e0e42f69168ab97ca55e252869de319eb44a4b8ee8d73ce9306624b7e207251',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90317': ['headers',
                                   '23b1c5bc0a624abbf70b2d6dd173922e4c2fd4ff9614832061e11cb385348364',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90320': ['headers',
                                   'be409761710d7d11b83dc07e27470ed535aeaf1343bd1fc2575d84a7a66ddaaa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90321': ['headers',
                                   '363358c672d793e8cee1fd78fdd1cfa89e32012465cdd0bd6b0d7ca44bef2b52',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90324': ['headers',
                                   '01f3a4c6284a7eddad98eb2963ba41d6bdbef6b5dcdd52d4309d887a7307d8a8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90343': ['headers',
                                   '616e02354e08974b743ced5e1c26c139c1f8823aed82e44b0c766921e6e6e1b0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90375': ['headers',
                                   '547e5fcbc849398d9ea3bf69ce2d45d71d1e9505a94b10447323872ba85e9e49',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90380': ['headers',
                                   'd82d9cf391bade6e53c39d993b6ee5e14501043db2d0a7440075a841775996e7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90392': ['headers',
                                   'e8733b0213f854cf789c84d0a3e5bd3a0ac7d77d9513f5526f6b545c460f93b2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90398': ['headers',
                                   'cfaed9e177ca5175e3181273947d7425ab76d8394cecb3d6266da2df7fdfaeae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90417': ['headers',
                                   '97e99a0cb8263388d21a8a80609654d50caa9253ed9f88da1a003d9c9e726db4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92499': ['headers',
                                   '6b93f9b20664469bc3f1cc139bd28801cec20b410fd7b6b0c04b2f39c76a37ff',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92507': ['headers',
                                   '7e782076901311c1b5d15769351ded29e16fb2b95c170cf0a4b2f53eed759063',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92509': ['headers',
                                   'f74d6e7611a1ce427bdec837671cc1fc72a00040ab55bc732e998aa754a50859',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92510': ['headers',
                                   'd2f0738cbb6508a1b354ebb8e1a9159fb8d28268d8f5b77c3203c389be686dee',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92511': ['headers',
                                   '090855391ef12cd6ca063a57d490969adb65bbe8a9f21ea837989234f63f6665',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92512': ['headers',
                                   '16e0b2884f5420b81bc0621773550737b8c5dba1a2993b41ee735a13eb84a9f4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93105': ['headers',
                                   '8ab9db3f21786295f359359577dd8e8c6feafe5cb589e443977eee6896289997',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93125': ['headers',
                                   'd51e490d32899ff59623817ba7f23427cef03bb667a219a386d51b1107b5c410',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93144': ['headers',
                                   '9804f0825b889f75aac565ec9318de40f8dd444dead40653cafe4f2873d689ef',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93148': ['headers',
                                   '0cb7045ec98640ff28e51922f577ad6546d482e5a2693a6ad237069b339284fa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93154': ['headers',
                                   '382102d940a2d34e77f1637bd436784e1a67ac0d36e48a03b0e10a2f9b628b6b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93175': ['headers',
                                   '06442f58870f9b8a44865e64c042716bc8744eb49ba088866c343700eea867e6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93176': ['headers',
                                   'cbdd00505d7b0a535f4971b68cf14c583d4e1305b3673f68d89f7f7f630e99d2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93192': ['headers',
                                   '4a1093b535c10f5c757401ced0d76578be77d25dfd2ef93010e85c2815f9dba2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93196': ['headers',
                                   '158716fb43ced19b1ac49ade2002247c10f0abb6c0964cc74cfe2def330397d0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93199': ['headers',
                                   '244a71acd2ae4fbb24d5b47c59b526e5c78533c576024b132b2938a33265d0ec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93210': ['headers',
                                   '8cd992268f2ee7f7fcee2e2e35d75feb57b5622afefe8aee732efa0c6c908f86',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93221': ['headers',
                                   '80eb991551663b842a462582e0773b2a35262f1173def9c7aa755373eeb2e048',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93282': ['headers',
                                   '1293af1c5517a8d7a36b389dcd7b796af948f32ad8588e68c64b94ee30181f3d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93787': ['headers',
                                   '05624587e1b2951889bd9f32e54c41a643e138a3909ebd2eacf4475bd167ef15',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93790': ['headers',
                                   'dba0f044ce1d4f12524acfca31c23e3482e13bad7d4154b8b2a08c92586833a2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93792': ['headers',
                                   '7d2c91ef1019b8af7fae988c06e0e9e798f5500d45510b6241a36abf083765c5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93796': ['headers',
                                   'cbadb07b7b175b9849f2550e95eb3cc0f1ea34a77e27c64266404a8d3eb3f1c9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93806': ['headers',
                                   'b152110c6bdbf74db47d7bdf0e9021d8925072d493a21614f13c1ab09d7b9f3c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93817': ['headers',
                                   '1517d093436e14727f0b20082b66190b7b4a1be96a30e148f813ad09412075ac',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93824': ['headers',
                                   '1f3883f39a1c749cedc174ea6c76ca2a082dffe1c74a7604a6ce721f59c2daee',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93828': ['headers',
                                   '289f9d4894c14fb1e3b3a1d34c078ace2a0a9b3c01ef5c6749fd36c5b4cbda65',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97415': ['headers',
                                   '42c8d3a3ea78b32156e152b867f2b3c8b0e40861b819c89c1c36d584f0ea2885',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97429': ['headers',
                                   'd4baeafd4fffd7aa0ebf8758b37fd18e96ce98b3725212ce02f0b52cdd1f6c69',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97442': ['headers',
                                   '4f7e9a38ae6c1590e9e254cb33d8694fcece7e4f26b8803c66b4115250cf5c40',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97493': ['headers',
                                   'ea1305daa95d319dcba371ce0c3a418499cc9633163dae044b02865049953892',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97497': ['headers',
                                   '5d7d6f583a4ea17f9549471967a20c9cf7d5f64432da716fb1f05fd2227b0fd7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97513': ['headers',
                                   '19857dcf3012285a4730b11fe7103bca56dd6317ff000885dda14b2f3d46a197',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97530': ['headers',
                                   'bf5d9a4607f32d4ebf0da627134df4e870f446ea44bfdbb445585fbf4cdb2bc9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97555': ['headers',
                                   '0048ac221e3686e6df1501d677a4b871c37921aed8a68bcb5689790876b954c0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97557': ['headers',
                                   '8a0563a3b4ffb90f23d271a68f9a56b26f6a60cb474deffeeab6b2018d646b2e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97562': ['headers',
                                   '4e5a23f14ad43bf69bbf0d41c1b405cd1dade8fc82e4ea55bff9bb6349472346',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97564': ['headers',
                                   '752ae06fb12c24595c0fdfc1cd01700b6ea3080186faf0b503a8a61f89425346',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97565': ['headers',
                                   '9636c64a43dc2207ac3c2d9b86e3eecfd4b8e8ee3db8efa036b671173213cac9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97581': ['headers',
                                   '2062e928bd8b343f5d6e50dac9310f8fcb8e000e82ab0cab9ad4ea36200419ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97603': ['headers',
                                   '0771a45f9f2b37014b463387ddc1cd6144f856cc549217e8ee301d964d949104',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97615': ['headers',
                                   'c404d37a55aab75e4581ef54b776ec8c12860cc469718056fe699f0bad9ed862',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97906': ['headers',
                                   'cf0bb99cf20b86698730d2029ad7bc9c3ba6cf3e55ef16f455becc4aca0b59c1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97960': ['headers',
                                   '283764030007328f825ed57e053fca8b8d10431ecfc56beeaa129d1fcebef05a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97988': ['headers',
                                   'e8f350de5a1772a06a08dbd93c8e732d3ebc8b40e0a14ba66e959f8668219a90',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98017': ['headers',
                                   '844001e6f634a89fe484666ccc3051587f6f77e4a73676664ce54192e5f574f1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98019': ['headers',
                                   '2d716ba08dd464b79f06462e42628844de07565f23d14ae2499d09e8e1df50d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98033': ['headers',
                                   'cbb2e15748d6f8504b7f3a7856217d931aedf7e6fe7774af68630be66144477b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98034': ['headers',
                                   '9dcf43c26dc8320e8b7ff3e38af5b7f1fbc0d4500486238ae1c78bae7f58e54a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98041': ['headers',
                                   '739e5ae925a638f999c6eed28b0208382c159a35a3d903527da584c993689de7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98056': ['headers',
                                   'e3ce66815844b491dc9f52644bf8ff31073a1417eb6c4d8ef3a9d7d1cc7cb0ad',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98084': ['headers',
                                   '56afe5431234a76d3cde0c33dd4d9c9b0506136723e04c170bc605e35696f9eb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98085': ['headers',
                                   '013f0a8fcb52b5cb531054a4ffa34fffbf416988580a4c225c3a6aa3fe52a3fc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98104': ['headers',
                                   '61ef278face87ee309b78fabcdec185b3294a92bc9f3aebd2cf40e1376ced2c5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98107': ['headers',
                                   '096d8241b08b722940bc66604b716511cd756fedd9b9b2f16fcdade201ce1ff7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98115': ['headers',
                                   'b4e7d32e44317fb0630c80e90fc1412c8458501614ef6b87b2e0001329607150',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98134': ['headers',
                                   '193da78ea6b2e6bda0800a630ebb14673318ba5b251ced9f04f196b17da67802',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98164': ['headers',
                                   'ce1e1c3f525e8e0e17840d7b38fbc0ba5fe4c7daa4ce72f36632eebd3edce73e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98167': ['headers',
                                   '06e8bc6fc17f834b916906b7853fd07cd434307fad5e64ce5b2867a3aaeb8239',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98171': ['headers',
                                   'af54bd5197f91e47c4c7ac4561e640b257ca7f96c2a173976ae7c106386cb91d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98222': ['headers',
                                   '18c8a9ce577e08b363023b281058db589662143a34aac97ded78d67a66a75673',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98229': ['headers',
                                   'f52ad4925e9011c84b2893fa415af89b6a0efef2cf8d58c24cdec6ad52d6e621',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98260': ['headers',
                                   '69e6576cb046b79718c1bc8635450b931060cc3052092cde8b4ebe312b2c7c1a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98283': ['headers',
                                   '6132a00950bcf3beea8d10bc7495c591281c536fbee79670197cf4ed49d0ecc3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98332': ['headers',
                                   '0e10ab12381289a4e117a2dff7350a254b10265983f4ddb6a9f6f566bcb0504e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98333': ['headers',
                                   '28f0c3c1c0ce631aad656d911846593fae307e7f490af8696b4a0e4e7d1e305e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98341': ['headers',
                                   '6fc67391505f18a947e9e72c5911d915286868469ac12f5a7f9293c4ae135349',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98359': ['headers',
                                   '644af73ba395afbaca17f7e553e7ca6b0dd3424d8f3f13a3510f01977e2183d4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98364': ['headers',
                                   '12c79f36d625fc575702b28470654acb755372714f8e34f6a716a62d987a30e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98365': ['headers',
                                   '360d182e7318d95fa198d8c02cb8a94405756d532d26fa765c20b147518ae449',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98420': ['headers',
                                   'c8c29070bd72a2cb96bb8f0f35e954c5a7ddf949acfd062a819f341bba4d3894',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98432': ['headers',
                                   '090d91ea723729333d821a70c1d16535418713bdaa288ba533ca818c12669d46',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98439': ['headers',
                                   '8a484e98964e5eeca8abb3c22d1fa2034a65161f83d13e8a38da654e094d9e75',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98441': ['headers',
                                   'ce023cc0240b0318ce5418f39ea6f84757ae1a1fe8c519d689433a88c15cc0b4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98442': ['headers',
                                   '649d32615f03e2e9de294a91a9c9cc7b4f133ee8f38b0aaefec3531bd1a37d87',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98454': ['headers',
                                   '0a5d6d025375dddc37bb6b506b6ceb259d32240dc28cc3a21de1d6e62da2503f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98456': ['headers',
                                   '60f7e172839e16f056c073b2f463ea42cc75251fc71704d37db6bf687320b474',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98457': ['headers',
                                   '6a34e877105377460c5ce3c1a700e1ce6232e3fb65c93193d91929ea3a9b2421',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98459': ['headers',
                                   'f76512666570aaada17b3051b1e1b3201bb2d0be36a88d0fa1504c36225600b7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98462': ['headers',
                                   'a5306e65ca8d712e3561dc7b7cebd1b9c2826d681e214cb2a9ead42c05aa814f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98464': ['headers',
                                   'fa73d9835f832dd4487fe8e163bcf7afb1dd316d4a6b19a45ddce425e95fd3e7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98479': ['headers',
                                   '9f1d377fcce430ed9f3f2b6fedbf0d0757c564fc002dd0864be4fe2793f0c23b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98487': ['headers',
                                   '36548ad3feb801744ffd201c83fcd7521a541477acf73ae4167e5b04d43a023f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98490': ['headers',
                                   '1ec3b03b348fdc0140cd0c7d4c1e6f5b53f8f6759be047933892f797720afdf4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98491': ['headers',
                                   'a5c0c435d6446c94326995f7c21e139f7e574e1b8db719f89187e0291b365d93',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98503': ['headers',
                                   '75c50b02d21feb12c2e07fd75272fb7917603c41c33baab076dae3c6c9157904',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98515': ['headers',
                                   '7d7a37cc871d499046b6068dd4cb20e3cc941c17573d8fd0be8497b7a2b0d080',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98524': ['headers',
                                   '826859ec24dcc1a478c12b476d87bf97bf33704c77565e8a49086c74fcd793fe',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98526': ['headers',
                                   '6bc54ced1acb6bc2964e1b3245e6f7a242e12abeda8d093efb864a2366df8591',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98532': ['headers',
                                   '5d84e8a25b6b1cd868a5f51596ad0f62a4896e25aacd20059ffb4f92634eda39',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98548': ['headers',
                                   '6a70602a5d2ce81e25d347623f3667b6bb7f7c2bde95d7c511504b3463443a4b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98561': ['headers',
                                   '8673fe4c2d865f49c3d9d09c673627f352f1aa8631eee66af9a6ec92e9eba30a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98567': ['headers',
                                   '62cfb1dd8056c3b83f629a10cc927fe4f7298203da4afe402f908f2b878a310b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98568': ['headers',
                                   'c5ddb7c13456ec132ecfae299ccfda7931b7ff0aa1882de6dd5472765d8bf9cc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98570': ['headers',
                                   'a6121396ecca955c0ac9547f3c4f2e17cd662506a5323b89afae280f05f52b5f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98572': ['headers',
                                   '30ca12e81a51f48e8e7810004bb2fd8572b5afd0909e2544c976062cc3df95ab',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98573': ['headers',
                                   '55c70c4ceb6105a9814a7d74b24f13b2f6b2be28dd8f713c6cd4b4aec9566c02',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98580': ['headers',
                                   '0fa998eceff5fe34a33925b93948d688e9bfed2ce158494c5986f6aa5758081b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98586': ['headers',
                                   '15b4d0152aa730d471607004a8a30bf6dcc5f62b83127cbfb2f18fbd3847ac8e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98588': ['headers',
                                   'bd651166e55122cccb2d589e2e12ee929cb633cd611cfc268a34a01bc0ab94de',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98589': ['headers',
                                   'c86d98443d47145c2fb3b0b082a9f2a92eabd62f0310f81b327f0d37aa57bcc7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98590': ['headers',
                                   '92ae27f7fad111697b6c079e8e33fafbbb6800c763b86cbeaa521cc9314c917e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98591': ['headers',
                                   '5a1c783c62580af439e3bb10bb3e94af40c02517d0d89a7caa54013806f1b25c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98599': ['headers',
                                   'c4f794007b48bc9783557f70f723a9eb131fb326e6788b4718d10dbd2a42554e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98606': ['headers',
                                   '1dfdce623ed53f03bf939f1a698de16c6148e7066a83e8dd12ad14eac213c156',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98621': ['headers',
                                   'fe327079e24013aba85e43c96fffb6e15e810dfeba505be8a68f0521d84a548c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98624': ['headers',
                                   'acf24cfe51dabbe740bc07f942d73db540da6739c15cfffc3e6ca91df79cb251',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98625': ['headers',
                                   '11ed5ec2fa9e317fdbd56c82253ec98ab61cacce6d8c74fd3b35493666051277',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98626': ['headers',
                                   '14dee6d541570b753d859aa1c6d02cddca0b08ab8e82dc1d2e3b8258d33850a5',
                                   'headers_with_separately_repaired_libbpf'],
                'CVE-2026-98632': ['headers',
                                   'fa917b358feec2b6fd7f8f262d18e7b8755aece8b2c1d259b90258dde09351dd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98634': ['headers',
                                   '656def0f4a20f60cd251f20dd39c050952c75ab3aa2c861f6d5917976b396205',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98642': ['headers',
                                   '5abc54e10fb1383d701cf81632c691477e86e6fa60647d7ef89a263b59e3f433',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98652': ['headers',
                                   '3765be99697e4fbd042242b17ffaffe027bf35a9215710711bf67b14beb3e1a6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98699': ['headers',
                                   'c299a45abda9160f938bd0f8eeff03aaeef740fe39e4a2aa2c097d82d011a59b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98701': ['headers',
                                   'bd7b2765427ab3bfcf19b0bc669fe36951117d66cb82dcdd1fb27354afad9b79',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98704': ['headers',
                                   'e3da3227488dac146ddddce4b61f1179981f44363ae690caa3762bfefb9096d9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98706': ['headers',
                                   'cd4ddee5f498fb319fa139111826dc56e2cc700f7331b842dae4f48172602b54',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98707': ['headers',
                                   '3a5000cc80260351ab8f9c44f085e4a53146e4682ebd515a0a2c48c6c2a78936',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98708': ['headers',
                                   'bc1a6ef1d2e3c43f1e8188173ebf0ac52fd6a1da7a94f94c16c81b504d203c70',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98709': ['headers',
                                   'e7418b271673a74d10bbf7c87d1d50df06fb1b13defe359a37e94e7ee4ea964f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98725': ['headers',
                                   '443b6f553bab526eddcbcb1417a1efd1680d53f03160c410d02969a7aef634cb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98733': ['headers',
                                   'e496b67968b1a4c9fe0451f03f722d36e3298216e3bb6cea6b6193395157d22c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98734': ['headers',
                                   '22336a5d82a8623ca3da84ab50419c2e5ef2533670321391cb6e3529d6b0a544',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98735': ['headers',
                                   '636b521b04d167700faed5016789491c276ac0775f729d005bb5139c62c29bc6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98737': ['headers',
                                   '33aebdad55f04994c94ae0446b5e55873e70db219ca5c9c26498e6d76e88952d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98738': ['headers',
                                   '3620b7ccd73748f72ac5c1fa2333cb438811be1eec777e538bec6870ae650ae0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98739': ['headers',
                                   '26139538b9ca52f7e79f77b139cebe2734704a804e5640e840419580db26723d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98741': ['headers',
                                   'f8d0580cb6acb5bb1572b21bf794bfff8758a5d87b42e6add418b50d63e43a68',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98742': ['headers',
                                   '493b95a89d54e5780541e0a2c5f697fbe244bf46ebe7daa75ff6eea7878a1f75',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98752': ['headers',
                                   'e3dbc2bca04e1864b7471e1387c4a14ca2a5adeaf0b1c05c634db4e5d4383c2d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98754': ['headers',
                                   'af534b7f70d88dceb51de3d6b6f91544f174fe3217e507997afeadd6e92f808c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98763': ['headers',
                                   'c7d0bf1db444fa811cb2bea0d0da0a21d80b849723cffa35f447045383640e33',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98792': ['headers',
                                   '8b0b1913f76bbe018cd1b6a691ad429a0e302cf1c6bb0e7bd8a0dd332c3d3caa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98794': ['headers',
                                   'a3a819112ae4b0f0de49f502ef0f2d40331797960b3cb02f9f15acfb28ef6900',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98797': ['headers',
                                   'b9177c5f1e889387f83d1af993a46ed1be8ca1d08b8f8c1efcf408156fc2986e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98799': ['headers',
                                   '6cd150ca90039a736d4e7f533684b9260078ce52ee8acd9370726123c6c57256',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98800': ['headers',
                                   '73589080e954f5f75ec0d82e15b335414a77f9ac04fd6dc1e37aa508af5667fc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98803': ['headers',
                                   '2afcdd222f1e974a0d00194089b64d61e2503ab916fc3ffd967c3d95b45ec8cb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98805': ['headers',
                                   '3687afbfc74dcb78ab2ceb57bc1be88a6f95fb3bc481a4063c062864b86daf2e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98813': ['headers',
                                   'e837fd6edcc5aca3745700a3544b6133aefd45376b0f1c759e86bb8cfe368b0f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98823': ['headers',
                                   '1d8b4918487faf53b6bd22af72c8f603e4aa9a2c3374af0b5a408eb1f7f222f8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98824': ['headers',
                                   '31d235107aaccc5f986d41352106cfdf0a4ceb621485430f922a219f60abe48c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98827': ['headers',
                                   '1c781717cb0afb7d22fd0cfb7bb432e1f52b97c663bc0e4994ae18e88ec1cc81',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98829': ['headers',
                                   'c78cceefce0db35bca6cbf91f77a77d665158f2e74e5adb783e2ba827b213630',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98836': ['headers',
                                   'b87a35bb509939e188875af074e1bb5ef13c315527de3dc0ba44c22fac2f8b99',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98837': ['headers',
                                   'b40b0cd9d50a2b9d50f30a0d966870feca75a65f63d8857f5e1d0bed79934ab2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98846': ['headers',
                                   'e9f0163c4994efe5a3fe1a8df1e7831025aa52d3bf8775d08c3d4d297eb87dd7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98862': ['headers',
                                   'c28c6f39797df079ee96c3105193b1339f24c019088c1709c214e727e6edeb9d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98863': ['headers',
                                   '7fc25dbc6bdf2e95ae485f2859426ddbfb4aeecf1c4ed64f5ff4c5aef72debda',
                                   'kernel_code_absent_from_header_payload']}}
# END REVIEWED SCAN POLICY


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def document(path, maximum=8 * 1024**2):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= maximum, 'Unsafe metadata')
        raw = stream.read(maximum + 1)
    require(len(raw) == info.st_size, 'Metadata changed')
    return oci.json_document(raw)


def validate_event(env, event):
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('RUNNER_ENVIRONMENT') == 'github-hosted',
            'Disposable GitHub-hosted worker required')
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and env.get('RUNNER_ARCH') == 'X64'
            and env.get('RUNNER_OS') == 'Linux', 'Manual native Linux dispatch required')
    repo = event.get('repository', {})
    require(repo.get('private') is False and repo.get('full_name') == env.get('GITHUB_REPOSITORY'),
            'Public repository identity differs')
    require('https://github.com/' + repo['full_name'] == oci.SOURCE_URL, 'Canonical toolkit repository required')
    require(repo.get('default_branch') and env.get('GITHUB_REF') == 'refs/heads/' + repo['default_branch'],
            'Default branch required')
    require(re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', '')), 'Full source identity required')
    for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(re.fullmatch(r'[1-9][0-9]*', env.get(key, '')), 'Invalid workflow identity')
    require(env.get('REVIEWED_COMMIT') == env['GITHUB_SHA'] and env['GITHUB_RUN_ATTEMPT'] == '1',
            'Exact reviewed first-attempt source required')
    return {'source_commit': env['GITHUB_SHA'], 'run_id': env['GITHUB_RUN_ID'],
            'run_attempt': env['GITHUB_RUN_ATTEMPT'], 'repository': repo['full_name']}


def clean_env(home):
    # Neither Actions auth nor host Docker/Git credentials enter build processes.
    return {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'HOME': str(home), 'DOCKER_CONFIG': str(home / 'docker'),
            'DOCKER_HOST': 'unix:///var/run/docker.sock', 'LANG': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false'}


def validate_manifest(data):
    require(data.get('schema') == 1 and data.get('platform') == 'linux/amd64', 'Unexpected build manifest')
    require(type(data.get('scan_acceptance_schema')) is int and data['scan_acceptance_schema'] == 1,
            'Explicit scan acceptance schema required')
    require(re.fullmatch(r'[0-9a-f]{40}', data.get('canonical_source', '')), 'Canonical source missing')
    require(data.get('sentinel') == {'path': 'deploy/runner-reserve/guard-ca.crt',
            'installed_path': '/opt/victron-ci-reserve/guard-ca.crt', 'text': SENTINEL.decode()},
            'Only invalid site sentinel allowed')
    require(set(data.get('contexts', {})) == {'public', 'private'}, 'Unexpected stages')
    for name, spec in data['contexts'].items():
        recipe = 'deploy/' + ('runner-reserve' if name == 'public' else 'arc-private') + '/Dockerfile'
        require(spec['recipe'] == recipe and recipe in spec['files'], 'Canonical recipe differs')
        for path, item in spec['files'].items():
            require(not Path(path).is_absolute() and '..' not in Path(path).parts and
                    not any(c in path for c in '*?[]\\') and item['mode'] in {'100644', '100755'} and
                    re.fullmatch(r'[0-9a-f]{64}', item['sha256']) and 0 < item['bytes'] < 2 * 1024**2,
                    'Unsafe context declaration')
    require(set(data['tools']) == {'buildkit', 'trivy'}, 'Unexpected tool inventory')
    lock = accepted.read_lock()
    require(data['tools']['buildkit'] == {
        'kind': 'accepted-artifact', 'lock_sha256': oci.digest_file(accepted.LOCK),
        'transport_sha256': accepted.transport_receipt()['lock_sha256'],
        'image': lock['architectures']['amd64']['config']}, 'Exact accepted builder required')
    require(re.fullmatch(r'ghcr.io/aquasecurity/trivy@sha256:[0-9a-f]{64}',
                        data['tools']['trivy']['image']), 'Immutable official scanner required')


def prepare_context(root, destination, spec, sentinel, env):
    """Read only committed blobs and copy the exact reviewed sparse inventory."""
    destination.mkdir(mode=0o700)
    for name, expected in spec['files'].items():
        tree = subprocess.check_output(['git', '-C', str(root), 'ls-tree', 'HEAD', '--', name], env=env, timeout=20).decode()
        require(tree.startswith(expected['mode'] + ' blob ') and tree.endswith('\t' + name + '\n'),
                'Context file mode or identity differs')
        raw = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:' + name], env=env, timeout=20)
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'Canonical context differs from reviewed source')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o755 if expected['mode'] == '100755' else 0o644)
    if sentinel:
        target = destination / 'deploy/runner-reserve/guard-ca.crt'
        target.write_bytes(SENTINEL)
        target.chmod(0o644)
    lines = (destination / (spec['recipe'] + '.dockerignore')).read_text().splitlines()
    actual = {line[1:] for line in lines if line.startswith('!')}
    require(actual == set(spec['files']) | ({'deploy/runner-reserve/guard-ca.crt'} if sentinel else set()),
            'Dockerignore differs from sparse inventory')
    require([line for line in lines if line and not line.startswith('#')][0] == '**',
            'Context must deny all undeclared inputs')


class Supervisor:
    """Finite owned commands with explicit disk observations; never prunes host state."""
    def __init__(self, env, paths, log, disk_log):
        self.env, self.paths, self.log, self.disk_log = env, paths, log, disk_log
        self.minimum = None
        self.last_disk_record = None
        self.last_disk_floor = None

    def check(self, floor=RESERVE, *, force_record=False):
        free = {str(path): shutil.disk_usage(path).free for path in self.paths}
        minimum = min(free.values())
        self.minimum = minimum if self.minimum is None else min(self.minimum, minimum)
        observed_at = time.monotonic()
        # Hashing invokes this guard for every block. Only the log is sampled;
        # every call still checks all filesystems and retains the observed minimum.
        if (force_record or self.last_disk_record is None or observed_at - self.last_disk_record >= 1
                or floor != self.last_disk_floor or minimum < floor):
            with self.disk_log.open('a') as out:
                out.write(json.dumps({'at': now(), 'free_bytes': free, 'required_bytes': floor,
                                      'minimum_free_bytes': self.minimum}) + '\n')
            self.last_disk_record, self.last_disk_floor = observed_at, floor
        require(minimum >= floor, 'Disk reserve exhausted')
        return minimum

    def run(self, command, seconds=60, capture=False, inspect=None):
        self.check(force_record=True)
        failed = False
        try:
            if capture:
                result = subprocess.run(command, env=self.env, capture_output=True, timeout=seconds, check=True)
                require(len(result.stdout) <= 2 * 1024**2, 'Metadata response too large')
                return result.stdout.decode().strip()
            with self.log.open('ab') as output:
                process = subprocess.Popen(command, env=self.env, stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                deadline = time.monotonic() + seconds
                try:
                    while process.poll() is None:
                        require(time.monotonic() < deadline, 'Owned command deadline exceeded')
                        self.check()
                        if inspect:
                            inspect()
                        time.sleep(5)
                    require(process.returncode == 0, 'Owned command failed: ' + str(process.returncode))
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                self.check(force_record=True)
            except BaseException:
                if not failed:
                    raise


def builder_identity(item, name, owner, image):
    require(item.get('Name') == '/buildx_buildkit_' + name + '0' and
            item.get('Config', {}).get('Image') == image and
            'RUNNER_ARTIFACT_OWNER=' + owner in item.get('Config', {}).get('Env', []) and
            re.fullmatch(r'[0-9a-f]{64}', item.get('Id', '')), 'Foreign builder identity')
    require(all(item.get('HostConfig', {}).get(k) == v for k, v in LIMITS.items()), 'Builder resource limits differ')
    require(item.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') == 'no', 'Builder restart policy differs')
    return item['Id']


def container_absent(result, name):
    expected = (('Error: No such object: ' + name).encode(),
                ('Error response from daemon: No such container: ' + name).encode())
    return result.returncode == 1 and result.stdout.strip() in {b'', b'[]'} and result.stderr.strip() in expected


def probe_builder(sup, name, owner, image):
    # This path deliberately bypasses ordinary admission: a full disk or OOM
    # must not prevent deletion of an independently identified owned process.
    result = subprocess.run(['docker', 'inspect', 'buildx_buildkit_' + name + '0'],
                            env=sup.env, capture_output=True, timeout=30)
    if container_absent(result, 'buildx_buildkit_' + name + '0'):
        return None
    require(result.returncode == 0, 'Unable to determine builder ownership')
    items = json.loads(result.stdout)
    require(len(items) == 1, 'Ambiguous builder')
    builder_identity(items[0], name, owner, image)
    return items[0]


def inspect_builder(sup, name, owner, image):
    item = probe_builder(sup, name, owner, image)
    require(item is not None, 'Owned builder is missing')
    require(not item['State'].get('OOMKilled'), 'Builder was OOM-killed')
    return item


def qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe, *,
                   architecture='amd64', cgroup_reader='cat'):
    require(architecture in {'amd64', 'arm64'}, 'Unsupported native cgroup proof architecture')
    require(cgroup_reader in {'cat', '/usr/bin/oci-cgroup-reader'}, 'Unreviewed cgroup reader')
    require(observed['HostConfig'].get('CgroupnsMode') == 'private', 'Private builder cgroup namespace required')
    pid = observed['State']['Pid']
    identity = cgroup.process_identity(pid)
    # PID 1 is the same process as Docker State.Pid, unlike the short-lived
    # `docker exec cat` process. BuildKit may move the daemon into /init while
    # both it and RUN workers still inherit the Docker-owned root's limits.
    reader_command = ['cat', '/proc/1/cgroup'] if cgroup_reader == 'cat' else [cgroup_reader]
    inside = sup.run(['docker', 'exec', observed['Id'], *reader_command], capture=True)
    after = cgroup.process_identity(pid)
    observe({'kind': 'binding', 'container_id': observed['Id'], 'host_before': identity,
             'host_after': after, 'private_cgroup': inside})
    require(after == identity, 'Builder process changed during namespace correlation')
    private_path = cgroup.group_path(inside)
    binding = {'container_id': observed['Id'], 'builder_process': identity,
               'private_process_path': private_path,
               'owned_root': cgroup.owned_root(observed['Id'], identity, private_path)}
    _, limits = cgroup.sample(pid, binding, observe=observe)
    bases = re.findall(r'^FROM (docker.io/library/ubuntu@sha256:[0-9a-f]{64})$', public_recipe, re.M)
    require(len(bases) == 1, 'Canonical preflight Ubuntu base missing')
    context = work / 'cgroup-preflight'
    context.mkdir()
    marker = 'runner-proof-' + uuid.uuid4().hex
    # The shell deliberately remains alive so a host-side read-only observer
    # can see the actual RUN process, not just the BuildKit daemon settings.
    (context / 'Dockerfile').write_text('FROM ' + bases[0] + '\nRUN ' +
        json.dumps(['/bin/sh', '-c', 'sleep 20; :', marker]) + '\n')
    seen = {}
    def check():
        item = inspect_builder(sup, name, owner, image)
        require(item['Id'] == observed['Id'], 'Builder identity changed')
        cgroup.sample(item['State']['Pid'], binding, observe=observe)
        for child in cgroup.run_processes(marker, binding):
            seen[(child['pid'], child['starttime_ticks'])] = child
    sup.run(['docker', 'buildx', 'build', '--builder', name, '--platform', 'linux/' + architecture,
             '--progress', 'plain', '--no-cache', str(context)], seconds=180, inspect=check)
    check()
    require(len(seen) == 1, 'Actual bounded RUN process was not observed')
    return {**binding, 'cgroup_version': 2, 'namespace': 'private',
            'effective_limits': limits, 'run_process': next(iter(seen.values())),
            'preflight_base': bases[0], 'run_inherits_builder_limits': True,
            'platform': 'linux/' + architecture, 'cgroup_reader': cgroup_reader}


def state_volume(name):
    return 'buildx_buildkit_' + name + '0_state'


def probe_volume(sup, name):
    result = subprocess.run(['docker', 'volume', 'inspect', name], env=sup.env,
                            capture_output=True, timeout=30)
    missing = {('Error response from daemon: get ' + name + ': no such volume').encode(),
               ('Error: No such volume: ' + name).encode()}
    if result.returncode == 1 and result.stdout.strip() in {b'', b'[]'} and result.stderr.strip() in missing:
        return None
    require(result.returncode == 0, 'Unable to determine owned volume state')
    items = json.loads(result.stdout)
    require(len(items) == 1 and items[0].get('Name') == name and items[0].get('Driver') == 'local' and
            items[0].get('Scope') == 'local' and not items[0].get('Options'), 'Unexpected state volume')
    return items[0]


def cleanup_step(observations, step, identity, operation):
    entry = {'step': step, 'identity': identity, 'started_at': now()}
    observations.append(entry)
    try:
        result = operation()
        entry['status'] = 'completed'
        return result
    except BaseException as error:
        entry.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        entry['finished_at'] = now()


def probe_instance(sup, name):
    path = Path(sup.env['DOCKER_CONFIG']) / 'buildx/instances' / name
    require(not path.is_symlink(), 'Unsafe owned builder metadata')
    if not path.exists():
        return None
    require(path.is_file(), 'Unsafe owned builder metadata')
    return {'name': name}


def cleanup_absence(observations, step, identity, probe, message):
    def verify():
        require(probe() is None, message)
    cleanup_step(observations, step, identity, verify)


def remove_builder(sup, name, owner, image, *, volume_absent_before=False, observations=None):
    observations = observations if observations is not None else []
    try:
        return remove_builder_resources(sup, name, owner, image, volume_absent_before, observations)
    except BaseException:
        for step, identity, operation in (
                ('readback-container', name, lambda: probe_builder(sup, name, owner, image)),
                ('readback-instance', name, lambda: probe_instance(sup, name)),
                ('readback-volume', state_volume(name), lambda: probe_volume(sup, state_volume(name)))):
            try:
                present = cleanup_step(observations, step, identity, operation) is not None
                observations[-1]['present'] = present
            except Exception:
                # Preserve the original cleanup failure; an inconclusive read is not absence.
                observations[-1]['status'] = 'unknown'
        raise


def remove_builder_resources(sup, name, owner, image, volume_absent_before, observations):
    item = cleanup_step(observations, 'probe-container', name, lambda: probe_builder(sup, name, owner, image))
    volume = state_volume(name)
    associated = False
    if item is not None:
        mounts = [mount for mount in item.get('Mounts', []) if mount.get('Destination') == '/var/lib/buildkit']
        require(len(mounts) == 1 and mounts[0].get('Type') == 'volume' and mounts[0].get('Name') == volume and
                mounts[0].get('RW') is True, 'Owned BuildKit state volume association differs')
        associated = True
        # Remove by immutable container ID, including an unhealthy owned instance.
        cleanup_step(observations, 'remove-container', item['Id'], lambda:
            subprocess.run(['docker', 'rm', '--force', item['Id']], env=sup.env, check=True, timeout=60))
    if probe_instance(sup, name) is not None:
        # This private DOCKER_CONFIG was exclusively created by this invocation.
        # Remove only that instance's metadata. An inactive Buildx instance may
        # retain its volume, so cache removal is independently verified below.
        cleanup_absence(observations, 'probe-instance-container', name,
                        lambda: probe_builder(sup, name, owner, image),
                        'Builder name reappeared during cleanup')
        cleanup_step(observations, 'remove-instance', name, lambda:
            subprocess.run(['docker', 'buildx', 'rm', '--force', name], env=sup.env, check=True, timeout=60))
    cleanup_absence(observations, 'verify-instance-absent', name,
                    lambda: probe_instance(sup, name), 'Owned instance removal not confirmed')
    cleanup_absence(observations, 'verify-container-absent', name,
                    lambda: probe_builder(sup, name, owner, image), 'Owned builder removal not confirmed')
    if cleanup_step(observations, 'probe-volume', volume, lambda: probe_volume(sup, volume)) is not None:
        require(associated or volume_absent_before, 'State volume ownership is not established')
        # Native snapshot deletion outlasted 60s; allow only this owned volume 180s.
        # No force: Docker refuses removal if another container references it.
        cleanup_step(observations, 'remove-volume', volume, lambda:
            subprocess.run(['docker', 'volume', 'rm', volume], env=sup.env, check=True, timeout=180))
    cleanup_absence(observations, 'verify-volume-absent', volume,
                    lambda: probe_volume(sup, volume), 'Owned state volume removal not confirmed')
    result = {key: item['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error')} if item else {'absent': True}
    return {**result, 'state_volume': volume, 'state_volume_removed': True}


def observe_builder_storage(sup, name, owner, image, record, phase):
    entry = {'phase': phase, 'started_at': now()}
    record.setdefault('builder_storage_observations', []).append(entry)
    try:
        item = inspect_builder(sup, name, owner, image)
        require(item['Id'] == record['builder_id'], 'Builder identity changed during storage diagnostic')
        entry['container_id'] = item['Id']
        raw = bounded_readonly_command(
            ['docker', 'exec', item['Id'], '/usr/bin/buildctl', 'du', '-v'], env=sup.env)
        entry.update(status='observed', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                     output=raw.decode(errors='replace'))
    except Exception as error:
        entry.update(status='unavailable', error_type=type(error).__name__)
    entry['finished_at'] = now()


def build_command(stage, data, identity, builder, context, output, layout=None, base=None, *, tools_layout=None):
    command = ['docker', 'buildx', 'build', '--builder', builder, '--platform', 'linux/amd64',
               '--progress', 'plain', '--provenance=mode=max', '--network=default',
               '--label', 'org.opencontainers.image.revision=' + identity['source_commit'],
               '--label', 'org.opencontainers.image.source=https://github.com/' + identity['repository'],
               '--label', 'io.victron.runner.stage=' + stage,
               '--output', 'type=oci,dest=' + str(output), '--tag', 'localhost/runner-' + stage + ':artifact',
               '--file', str(context / data['contexts'][stage]['recipe'])]
    if stage == 'public':
        require(tools_layout is not None, 'Verified builder layout required for accepted runc')
        spec = accepted.read_lock()['architectures']['amd64']
        command += ['--build-context', 'accepted-tools=oci-layout://' + str(tools_layout) + '@' + spec['manifest']]
    if stage == 'private':
        require(layout is not None and base is not None, 'Accepted public base required')
        command += ['--build-context', 'accepted-base=oci-layout://' + str(layout) + '@' + base['image_index'],
                    '--build-arg', 'BASE_IMAGE=accepted-base']
    return command + [str(context)]


def patch_preflight_command(data, builder, context, component):
    # Same canonical stage, platform, context and bounded builder as the full
    # image. Cache-only output cannot be mistaken for a qualified final image.
    require(component in {'libbpf', 'gstreamer'}, 'Unknown canonical patch preflight')
    return ['docker', 'buildx', 'build', '--builder', builder, '--platform', 'linux/amd64',
            '--progress', 'plain', '--network=default', '--target', 'patched-' + component,
            '--output', 'type=cacheonly', '--file', str(context / data['contexts']['public']['recipe']),
            str(context)]


def run_patch_preflights(sup, data, builder, context, record, check_build):
    started = time.monotonic()
    deadline = started + 900
    record['patch_preflight_budget_seconds'] = 900
    try:
        for component in ('libbpf', 'gstreamer'):
            key = component + '_preflight_'
            command = patch_preflight_command(data, builder, context, component)
            record[key + 'command'] = command
            record[key + 'started_at'] = now()
            remaining = deadline - time.monotonic()
            require(0 < remaining <= 900, 'Canonical patch preflight budget exhausted')
            record[key + 'timeout_seconds'] = remaining
            sup.run(command, seconds=remaining, inspect=check_build)
            check_build()
            require(time.monotonic() <= deadline, 'Canonical patch preflight budget exhausted')
            record[key + 'completed'] = True
            record[key + 'finished_at'] = now()
            record[key + 'claim'] = 'Canonical stage succeeded; the full build may reuse this builder cache.'
    finally:
        record['patch_preflight_elapsed_seconds'] = time.monotonic() - started


def runc_packages(data):
    """Require one complete final executable inventory, including its main module."""
    rows = [r for r in data.get('Results', []) if r.get('Target') == RUNC_PATH
            and r.get('Type') == 'gobinary' and r.get('Class') == 'lang-pkgs']
    if len(rows) != 1 or not rows[0].get('Packages'):
        return None
    values = [[p.get('Name'), p.get('Version')] for p in rows[0]['Packages']]
    if not all(isinstance(n, str) and (v is None or isinstance(v, str)) for n, v in values):
        return None
    return sorted(values, key=lambda pair: (pair[0], pair[1] or ''))


def scan_summary(data, proof):
    require(data.get('SchemaVersion') == 2 and isinstance(data.get('Results'), list) and data['Results'],
            'Incomplete image vulnerability report')
    require(data.get('ArtifactType') == 'container_image', 'Scanner did not analyze an image')
    image_id = data.get('Metadata', {}).get('ImageID')
    require(image_id in {proof['platform']['config'], proof['platform']['config'][7:]}, 'Scan image identity differs')
    results = data['Results']
    require(any(r.get('Class') == 'os-pkgs' and r.get('Packages') for r in results), 'OS inventory absent')
    require(any(r.get('Class') == 'lang-pkgs' and r.get('Packages') for r in results), 'Language inventory absent')
    counts = {}
    for result in results:
        for finding in result.get('Vulnerabilities') or []:
            level = finding.get('Severity', 'UNKNOWN')
            counts[level] = counts.get(level, 0) + 1
    return {'schema': 1, 'counts': counts, 'blocking_findings': counts.get('HIGH', 0) + counts.get('CRITICAL', 0),
            'scan_gate_passed': not (counts.get('HIGH', 0) + counts.get('CRITICAL', 0)),
            'suppressed_findings': 0, 'packages': sum(len(r.get('Packages') or []) for r in results),
            'targets': len(results), 'deployable': False, 'site_configured': proof.get('site_configured') is True}


def stable_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def scan_file_evidence(layout, selection, proof, check):
    """Collect actual final bytes without granting a vulnerability exclusion."""
    effective = oci.effective_files(layout, selection, proof, check)
    tree = effective['files']
    binding = {'source_commit': proof['source_commit'], 'archive_sha256': proof['archive_sha256'],
               'archive_bytes': proof['archive_bytes'], 'image_index': proof['image_index'],
               'runtime_manifest': proof['platform']['manifest'], 'config_digest': proof['platform']['config'],
               'platform': {'os': 'linux', 'architecture': 'amd64'}}
    components, errors = {}, []
    def record(path):
        return {key: value for key, value in oci.effective_regular(tree, path).items() if key != 'text'}
    for name in ('headers', 'runc', 'libbpf', 'gstreamer'):
        try:
            if name == 'headers':
                package = oci.package_files(effective, 'linux-libc-dev', 'amd64', '6.8.0-147.147')
                package['selected_header'] = record('usr/include/linux/netfilter/nf_conntrack_common.h')
                components[name] = package
            elif name == 'runc':
                components[name] = record(RUNC_PATH)
            elif name == 'gstreamer':
                prefix = 'usr/local/share/victron-gstreamer/'
                spec = SCAN_POLICY['gstreamer']
                packages = {package: oci.package_files(effective, package, 'amd64',
                    spec['source_manifest']['downstream_version'], baseline['symlinks'])
                    for package, baseline in spec['baselines']['amd64'].items()}
                components[name] = {'packages': packages, 'library': record(oci.GSTREAMER_LIBRARY),
                    'files': {name: record(prefix + name) for name in
                              ('source-manifest.json', 'backport.patch', 'build-provenance.json')},
                    'source_manifest': oci.json_document(oci.effective_text(tree, prefix + 'source-manifest.json')),
                    'provenance': oci.json_document(oci.effective_text(tree, prefix + 'build-provenance.json'))}
            else:
                library = 'usr/lib/x86_64-linux-gnu/libbpf.so.1.3.0'
                prefix = 'usr/local/share/victron-libbpf/'
                package = oci.package_files(effective, 'libbpf1', 'amd64', '1:1.3.0-2build2+victron1',
                    {'usr/lib/x86_64-linux-gnu/libbpf.so.1': 'libbpf.so.1.3.0'})
                metadata = {name: record(prefix + name) for name in (
                    'build-provenance.json', 'source-manifest.json', 'package.buildinfo',
                    'dynamic-symbols.txt', '0001-restrict-core-poisoning.patch',
                    'debian-libbpf1.symbols', 'src-libbpf.map')}
                components[name] = {'package': package, 'library': record(library), 'files': metadata,
                    'provenance': oci.json_document(oci.effective_text(tree, prefix + 'build-provenance.json')),
                    'source_manifest': oci.json_document(oci.effective_text(tree, prefix + 'source-manifest.json')),
                    'buildinfo': oci.effective_text(tree, prefix + 'package.buildinfo'),
                    'symbols': oci.effective_text(tree, prefix + 'dynamic-symbols.txt'),
                    'debian_symbols': oci.effective_text(tree, prefix + 'debian-libbpf1.symbols')}
        except (ValueError, KeyError, TypeError, UnicodeError):
            # A missing or changed component is evidence for a failed
            # prerequisite, not permission to omit it from the full scan.
            errors.append(name + '_evidence_invalid')
    kernel_paths = {p: r for p, r in tree.items() if r['type'] != 'directory' and
        (p.startswith(('boot/', 'lib/modules/', 'usr/lib/modules/')) or
         re.search(r'(^|/)(vmlinux|vmlinuz)([.\-]|$)', p) or
         re.search(r'\.ko(?:\.(?:gz|xz|zst))?$', p))}
    perf_paths = {p: r for p, r in tree.items() if p.rsplit('/', 1)[-1] == 'perf'}
    return {'schema': 1, 'binding': binding, 'components': components, 'errors': errors,
            'kernel_paths': kernel_paths, 'perf_paths': perf_paths}


def assessment_findings(data, proof, expected_input):
    """Bind each raw blocking finding to its exact scanned package, not a title.

    This performs no exemption and does not change the raw scan summary. A
    later applicability decision must separately verify the effective image
    files and an explicitly reviewed policy. Missing/ambiguous identity fails.
    """
    summary = scan_summary(data, proof)
    require(isinstance(expected_input, str) and expected_input and
            data.get('ArtifactName') == expected_input, 'Scan input identity differs')
    os_info = data.get('Metadata', {}).get('OS', {})
    layer_pairs = {(layer['digest'], diff) for layer, diff in
                   zip(proof['platform']['layers'], proof['platform']['diff_ids'], strict=True)}
    findings, seen = [], set()
    for result in data['Results']:
        for finding in result.get('Vulnerabilities') or []:
            if finding.get('Severity') not in {'HIGH', 'CRITICAL'}:
                continue
            require(isinstance(result.get('Target'), str) and result['Target'] and
                    result.get('Class') in {'os-pkgs', 'lang-pkgs'} and
                    isinstance(result.get('Type'), str) and result['Type'], 'Missing scan target identity')
            if result['Class'] == 'os-pkgs':
                require(os_info.get('Family') == result['Type'] and
                        isinstance(os_info.get('Name'), str) and os_info['Name'] and
                        result['Target'] == expected_input + ' (' + os_info['Family'] + ' ' + os_info['Name'] + ')',
                        'OS target does not match the verified scanner input')
            required = ('VulnerabilityID', 'PkgID', 'PkgName', 'InstalledVersion')
            require(all(isinstance(finding.get(key), str) and finding[key] for key in required),
                    'Missing finding identity')
            purl = finding.get('PkgIdentifier', {}).get('PURL')
            require(isinstance(purl, str) and purl.startswith('pkg:'), 'Missing finding package URL')
            packages = [package for package in result.get('Packages') or []
                        if package.get('ID') == finding['PkgID']]
            require(len(packages) == 1, 'Missing or ambiguous scanned package')
            package = packages[0]
            version = package.get('Version', '')
            if package.get('Release'):
                version += '-' + package['Release']
            require(package.get('Name') == finding['PkgName'] and version == finding['InstalledVersion'] and
                    package.get('Identifier', {}).get('PURL') == purl, 'Finding/package identity differs')
            layer = finding.get('Layer', {})
            require(set(layer) == {'Digest', 'DiffID'} and (layer['Digest'], layer['DiffID']) in layer_pairs,
                    'Finding layer is not part of the verified image')
            # Preserve every advisory field, including future fields. Only
            # image/package-location fields are excluded from its policy hash.
            advisory = {key: value for key, value in finding.items() if key not in {
                'PkgID', 'PkgName', 'PkgIdentifier', 'InstalledVersion', 'Layer', 'PkgPath', 'Fingerprint'}}
            row = {'target': result['Target'], 'class': result['Class'], 'type': result['Type'],
                   'vulnerability': finding['VulnerabilityID'], 'severity': finding['Severity'],
                   'package_id': finding['PkgID'], 'package': finding['PkgName'],
                   'version': finding['InstalledVersion'], 'purl': purl,
                   'source_package': package.get('SrcName'), 'source_version': package.get('SrcVersion'),
                   'source_release': package.get('SrcRelease'), 'architecture': package.get('Arch'),
                   'status': finding.get('Status'), 'fixed_version': finding.get('FixedVersion'),
                   'package_path': finding.get('PkgPath'), 'layer': layer,
                   'advisory_sha256': hashlib.sha256(json.dumps(advisory, sort_keys=True,
                       separators=(',', ':')).encode()).hexdigest()}
            key = (row['target'], row['class'], row['type'], row['package_id'], row['vulnerability'])
            require(key not in seen, 'Duplicate blocking finding')
            seen.add(key)
            findings.append(row)
    require(len(findings) == summary['blocking_findings'], 'Blocking finding inventory differs')
    return findings


def validate_scan_context(data, proof, context):
    require(set(context) == {'source_commit', 'run_id', 'run_attempt', 'stage', 'artifact_name',
                            'raw_scan_sha256', 'scan_input_sha256', 'scanner', 'databases'},
            'Scan acceptance context differs')
    require(context['source_commit'] == proof['source_commit'] and
            re.fullmatch(r'[0-9a-f]{40}', context['source_commit']) and
            all(isinstance(context[k], str) and re.fullmatch(r'[1-9][0-9]*', context[k])
                for k in ('run_id', 'run_attempt')) and context['stage'] in {'public', 'private', 'site'},
            'Scan acceptance source/run differs')
    require(context['stage'] == proof['stage'] or
            (context['stage'] == 'site' and proof['stage'] == 'private'), 'Scan acceptance stage differs')
    require(isinstance(context['artifact_name'], str) and context['artifact_name'].startswith('/') and
            data.get('ArtifactName') == context['artifact_name'], 'Scan acceptance input differs')
    for key in ('raw_scan_sha256', 'scan_input_sha256'):
        require(isinstance(context[key], str) and re.fullmatch(r'[0-9a-f]{64}', context[key]),
                'Scan acceptance hash missing')
    scanner = context['scanner']
    require(scanner.get('version') == '0.74.0', 'Scanner version differs')
    if scanner.get('kind') == 'oci-image':
        require(set(scanner) == {'kind', 'version', 'image'} and
                re.fullmatch(r'ghcr.io/aquasecurity/trivy@sha256:[0-9a-f]{64}', scanner['image']),
                'Scanner image identity missing')
    else:
        require(set(scanner) == {'kind', 'version', 'binary_sha256'} and scanner.get('kind') == 'native' and
                re.fullmatch(r'[0-9a-f]{64}', scanner['binary_sha256']), 'Native scanner identity missing')
    databases = context['databases']
    main = {'db/trivy.db', 'db/metadata.json'}
    java = {'java-db/trivy-java.db', 'java-db/metadata.json'}
    require(set(databases) in (main, main | java) and
            all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in databases.values()),
            'Scanner database identity missing')
    if set(databases) == main:
        require(all(result.get('Type') in {'ubuntu', 'gobinary', 'dotnet-core', 'node-pkg'}
                    for result in data['Results']), 'Java database absence is not qualified for these targets')
    return {**context, 'archive_sha256': proof['archive_sha256'], 'archive_bytes': proof['archive_bytes'],
            'image_index': proof['image_index'], 'runtime_manifest': proof['platform']['manifest'],
            'config_digest': proof['platform']['config'], 'platform': {'os': 'linux', 'architecture': 'amd64'}}


def assess_components(file_evidence):
    """Check immutable payload evidence; a package version alone grants nothing."""
    components = file_evidence['components']
    result = {}
    for component in ('headers', 'runc', 'libbpf', 'gstreamer'):
        try:
            item = components[component]
            if component == 'headers':
                expected = SCAN_POLICY['headers']
                require(all(item.get(k) == expected[k] for k in (
                    'package', 'architecture', 'version', 'source_package',
                    'filelist_sha256', 'md5sums_sha256', 'inventory_sha256')),
                    'Header package identity changed')
                require(stable_digest(item['inventory']) == expected['inventory_sha256'],
                        'Header inventory changed')
                selected = item['selected_header']
                require(all(selected.get(k) == v for k, v in expected['selected_header'].items()),
                        'Sanitized exported header changed')
                require(not file_evidence['kernel_paths'] and not file_evidence['perf_paths'],
                        'Kernel or perf payload requires review')
            elif component == 'runc':
                require(all(item.get(k) == v for k, v in SCAN_POLICY['runc'].items()),
                        'Actual runc binary changed')
            elif component == 'gstreamer':
                spec = SCAN_POLICY['gstreamer']
                source, files, provenance = spec['source_manifest'], item['files'], item['provenance']
                library = item['library']
                relative = 'usr/lib/x86_64-linux-gnu/' + source['library']
                require(item['source_manifest'] == source and provenance['source'] == source and
                        files['source-manifest.json']['sha256'] == spec['source_manifest_sha256'] and
                        files['backport.patch']['sha256'] == source['patch']['sha256'] and
                        all(record['type'] == 'regular' and record['mode'] == 420 and record['elf'] is False
                            for record in files.values()), 'GStreamer source evidence changed')
                require(type(provenance['schema']) is int and provenance['schema'] == 1 and
                        provenance['architecture'] == 'amd64' and
                        provenance['uid'] == 10001 and provenance['builder_sha256'] == spec['builder_sha256'] and
                        provenance['build_command'] == spec['build_command'] and
                        provenance['patched_parser_sha256'] == source['patch']['after_sha256'] and
                        provenance['ordinary_parser_allocation_smoke_passed'] is True and
                        provenance['library_path'] == relative and provenance['soname'] == source['soname'],
                        'GStreamer build evidence changed')
                require(library['type'] == 'regular' and library['mode'] == 420 and library['elf'] is True and
                        0 < library['bytes'] <= 8 * 1024**2 and
                        library['sha256'] == provenance['library_sha256'] and
                        library['sha256'] != source['original_library_sha256']['amd64'] and
                        provenance['original_library_sha256'] == source['original_library_sha256']['amd64'] and
                        library['elf_info'] == spec['baselines']['amd64']['libgstreamer-plugins-bad1.0-0']['original_elf_info'],
                        'Actual fixed GStreamer ELF differs')
                require(set(item['packages']) == set(provenance['packages']) == set(spec['baselines']['amd64']),
                        'GStreamer package pair differs')
                for package, baseline in spec['baselines']['amd64'].items():
                    actual, declared = item['packages'][package], provenance['packages'][package]
                    old, installed = declared['original_payload'], declared['installed_payload']
                    require(actual['package'] == package and actual['architecture'] == 'amd64' and
                            actual['version'] == source['downstream_version'] and
                            actual['source_package'] in {'gst-plugins-bad1.0',
                                'gst-plugins-bad1.0 (' + source['downstream_version'] + ')'} and
                            stable_digest(actual['inventory']) == actual['inventory_sha256'] and
                            stable_digest(old) == baseline['payload_sha256'] and len(old) == baseline['entries'] and
                            declared['original_package_sha256'] == source['binaries'][
                                package + '_' + source['version'] + '_amd64.deb']['sha256'] and
                            re.fullmatch(r'[0-9a-f]{64}', declared['package_sha256']),
                            'Original or installed GStreamer package identity differs')
                    projected = {}
                    for path, record in actual['inventory'].items():
                        kind = record['type']
                        require(kind in {'regular', 'directory', 'symlink'}, 'GStreamer package file type differs')
                        value = {'type': 'file' if kind == 'regular' else kind, 'mode': record['mode']}
                        if kind == 'regular':
                            value.update(bytes=record['bytes'], sha256=record['sha256'])
                        elif kind == 'symlink':
                            value['target'] = record['target']
                        projected[path] = value
                    changed = [relative] if package == 'libgstreamer-plugins-bad1.0-0' else []
                    require(projected == installed and set(old) == set(installed) and
                            declared['changed_payload'] == changed and
                            {path for path in old if old[path] != installed[path]} == set(changed),
                            'Unrelated GStreamer plugin payload changed')
                    if changed:
                        require(installed[relative] == {'type': 'file', 'mode': 420,
                                'bytes': library['bytes'], 'sha256': library['sha256']},
                                'Installed GStreamer library does not match package inventory')
                    control, status = declared['control'], {}
                    current = None
                    for line in actual['status'].splitlines():
                        if line.startswith((' ', '\t')):
                            require(current is not None, 'Invalid package status continuation')
                            status[current] += '\n' + line
                        else:
                            key, separator, value = line.partition(': ')
                            require(separator and key not in status, 'Invalid package status field')
                            status[key], current = value, key
                    require(status.get('Status') == 'install ok installed' and
                            all(status.get(key) == value for key, value in control.items() if key != 'Source') and
                            status.get('Source') == actual['source_package'] and
                            control['Source'] == 'gst-plugins-bad1.0 (' + source['downstream_version'] + ')' and
                            control['Maintainer'] == 'alvit <272257197+4alvit@users.noreply.github.com>',
                            'GStreamer installed control metadata differs')
                    preserved = {key: value for key, value in control.items()
                                 if key not in {'Source', 'Version', 'Maintainer', 'Installed-Size'}}
                    if package == 'gstreamer1.0-plugins-bad':
                        dependency = 'libgstreamer-plugins-bad1.0-0 (= ' + source['downstream_version'] + ')'
                        require(preserved['Depends'].count(dependency) == 1, 'GStreamer paired dependency differs')
                        preserved['Depends'] = preserved['Depends'].replace(dependency,
                            'libgstreamer-plugins-bad1.0-0 (= ' + source['version'] + ')')
                    require(stable_digest(preserved) == baseline['preserved_control_sha256'],
                            'Unrelated GStreamer dependencies or metadata changed')
            else:
                spec, files, provenance = SCAN_POLICY['libbpf'], item['files'], item['provenance']
                package, library = item['package'], item['library']
                require(item['source_manifest'] == spec['source_manifest'] and
                        files['source-manifest.json']['sha256'] == spec['source_manifest_sha256'] and
                        provenance['source'] == spec['source_manifest'], 'Backport source changed')
                require(package['package'] == 'libbpf1' and package['architecture'] == 'amd64' and
                        package['version'] == spec['source_manifest']['downstream_binary_version'] and
                        stable_digest(package['inventory']) == package['inventory_sha256'],
                        'Backported package identity changed')
                installed = package['inventory']['usr/lib/x86_64-linux-gnu/libbpf.so.1.3.0']
                require(installed == {k: v for k, v in library.items() if k != 'layer'} and
                        library['type'] == 'regular' and library['elf'] is True and library['mode'] == 420 and
                        0 < library['bytes'] <= 10 * 1024**2 and
                        library['sha256'] == provenance['library_sha256'] and
                        library['sha256'] != spec['unpatched_library_sha256'],
                        'Actual installed backport is absent')
                require(provenance['architecture'] == 'amd64' and provenance['uid'] == 10001 and
                        provenance['soname'] == 'libbpf.so.1' and
                        provenance['ordinary_userspace_smoke_passed'] is True and
                        provenance['kernel_bpf_operations'] is False and
                        provenance['build_command'] == spec['build_command'] and
                        provenance['package_fields'] == ['Package: libbpf1',
                            'Version: ' + package['version'], 'Architecture: amd64'] and
                        re.fullmatch(r'[0-9a-f]{64}', provenance['package_sha256']),
                        'Backport build proof changed')
                for name, sha in spec['fixed_files'].items():
                    require(files[name]['type'] == 'regular' and files[name]['sha256'] == sha,
                            'Backport source evidence changed')
                require(all(record['type'] == 'regular' and record['mode'] == 420 and
                            record['elf'] is False for record in files.values()), 'Backport metadata type or mode changed')
                for name, key in [('package.buildinfo', 'buildinfo_sha256'),
                                  ('dynamic-symbols.txt', 'symbol_proof_sha256')]:
                    require(files[name]['sha256'] == provenance[key], 'Build output proof changed')
                for name, key in [('src/relo_core.c', 'before_sha256')]:
                    require(provenance['original_source_files'][name] ==
                            [spec['source_manifest']['patch'][key], 420], 'Original source proof changed')
                for text_key, name in [('buildinfo', 'package.buildinfo'), ('symbols', 'dynamic-symbols.txt'),
                                       ('debian_symbols', 'debian-libbpf1.symbols')]:
                    raw = item[text_key].encode()
                    require(hashlib.sha256(raw).hexdigest() == files[name]['sha256'] and
                            len(raw) == files[name]['bytes'], 'ABI evidence text changed')
                section = item['buildinfo'].split('\nChecksums-Sha256:\n')
                require(len(section) == 2, 'Buildinfo package checksum section missing')
                lines = section[1].splitlines()
                checksum_lines = []
                for line in lines:
                    if not line.startswith(' '):
                        break
                    checksum_lines.append(line.split())
                expected_deb = 'libbpf1_' + spec['source_manifest']['downstream_source_version'] + '_amd64.deb'
                require(len([fields for fields in checksum_lines if len(fields) == 3 and
                            fields[0] == provenance['package_sha256'] and fields[1].isdigit() and
                            int(fields[1]) > 0 and fields[2] == expected_deb]) == 1,
                        'Buildinfo does not bind the rebuilt binary package')
                expected = {line.split()[0] for line in item['debian_symbols'].splitlines()
                            if line.startswith(' ')}
                actual = set()
                for line in item['symbols'].splitlines():
                    if line.strip():
                        symbol = line.split()[-1].replace('@@', '@')
                        if symbol.startswith('LIBBPF_') and '@' not in symbol:
                            symbol += '@' + symbol
                        actual.add(symbol)
                require(expected and expected == actual and len(actual) == provenance['abi_symbol_count'],
                        'Versioned public ABI differs')
            result[component] = True
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            result[component] = False
    return result


def scan_acceptance(data, proof, file_evidence, context):
    """Keep raw findings and separately assess this exact scanned image payload.

    Callers must produce file_evidence with scan_file_evidence against the
    verified OCI graph. This pure function neither reads files nor trusts a
    supplied acceptance boolean. Every advisory change requires review.
    """
    binding = validate_scan_context(data, proof, context)
    expected = {key: binding[key] for key in ('source_commit', 'archive_sha256', 'archive_bytes',
        'image_index', 'runtime_manifest', 'config_digest', 'platform')}
    require(type(file_evidence.get('schema')) is int and file_evidence['schema'] == 1 and
            file_evidence.get('binding') == expected and
            set(file_evidence) == {'schema', 'binding', 'components', 'errors', 'kernel_paths', 'perf_paths'},
            'Effective file evidence binding differs')
    require(isinstance(file_evidence['errors'], list) and
            set(file_evidence['errors']) <= {'headers_evidence_invalid', 'runc_evidence_invalid',
                                           'libbpf_evidence_invalid', 'gstreamer_evidence_invalid'},
            'Unknown effective evidence error')
    components = assess_components(file_evidence)
    # The scanner currently attributes this CVE to Linux headers, although a
    # separate installed library contains affected code. Require the actual
    # backport even if no scanner finding happens to mention that library.
    failed = [] if components['libbpf'] else ['installed_libbpf_backport_not_verified']
    if not components['gstreamer']:
        failed.append('installed_gstreamer_backport_not_verified')
    if not components['runc'] or runc_packages(data) != SCAN_POLICY['runc_packages']:
        failed.append('accepted_runc_inventory_not_verified')
    rows = assessment_findings(data, proof, context['artifact_name'])
    assessed, unresolved = [], []
    for row in rows:
        gst = SCAN_POLICY['gstreamer']
        if row['vulnerability'] == gst['advisory']:
            package, version = row['package'], gst['source_manifest']['downstream_version']
            identity = {'class': 'os-pkgs', 'type': 'ubuntu', 'version': version,
                        'package_id': package + '@' + version, 'source_package': 'gst-plugins-bad1.0',
                        'source_version': '1.24.2', 'source_release': '1ubuntu4+victron1',
                        'architecture': 'amd64', 'package_path': None,
                        'purl': 'pkg:deb/ubuntu/' + package + '@' + version.replace('+', '%2B') +
                                '?arch=amd64&distro=ubuntu-24.04'}
            matches = (package in gst['baselines']['amd64'] and
                       row['target'] == context['artifact_name'] + ' (ubuntu 24.04)' and
                       all(row[key] == value for key, value in identity.items()))
            reason = ('finding_identity_changed' if not matches else
                      'advisory_content_changed' if row['advisory_sha256'] != gst['advisory_sha256'] else
                      'effective_component_changed')
            if matches and row['advisory_sha256'] == gst['advisory_sha256'] and components['gstreamer']:
                assessed.append({**row, 'rule_id': 'installed_gstreamer_h265_backport',
                    'evidence_canonical_sha256': stable_digest(file_evidence['components']['gstreamer'])})
            else:
                unresolved.append({**row, 'reason': reason})
            continue
        policy = SCAN_POLICY['advisories'].get(row['vulnerability'])
        reason = 'unreviewed_advisory'
        if policy:
            component, expected_hash, rule = policy
            identity = SCAN_POLICY['identities'][component]
            target = context['artifact_name'] + ' (ubuntu 24.04)' if component == 'headers' else RUNC_PATH
            matches = row['target'] == target and all(row[k] == v for k, v in identity.items())
            reason = 'finding_identity_changed' if not matches else 'advisory_content_changed'
            if matches and row['advisory_sha256'] == expected_hash:
                reason = 'effective_component_changed'
                valid = components[component]
                if component == 'runc':
                    valid = valid and row['layer']['Digest'] == file_evidence['components'][component].get('layer')
                if rule == 'headers_with_separately_repaired_libbpf':
                    valid = valid and components['libbpf']
                if valid:
                    evidence = {'component': file_evidence['components'][component]}
                    if rule == 'headers_with_separately_repaired_libbpf':
                        evidence['libbpf'] = file_evidence['components']['libbpf']
                    assessed.append({**row, 'rule_id': rule,
                        'evidence_canonical_sha256': stable_digest(evidence)})
                    continue
        unresolved.append({**row, 'reason': reason})
    return {'schema': 1, 'policy_sha256': stable_digest(SCAN_POLICY),
            'binding': {**binding, 'file_evidence_canonical_sha256': stable_digest(file_evidence)},
            'raw_blocking_findings': len(rows), 'assessed_not_affected': assessed,
            'unresolved_findings': unresolved, 'unresolved_count': len(unresolved),
            'failed_prerequisites': failed, 'acceptance_passed': not unresolved and not failed}


def check_cgroup_receipt(build):
    proof = build.get('cgroup_proof', {})
    require(proof.get('cgroup_version') == 2 and proof.get('namespace') == 'private' and
            proof.get('run_inherits_builder_limits') is True and
            proof.get('effective_limits') == {'cpu.max': '200000 100000',
                'memory.max': str(4 * GIB), 'memory.swap.max': '0'}, 'Cgroup enforcement evidence missing')
    require(proof['container_id'] == build['builder_id'], 'Cgroup proof Docker identity differs')
    cgroup.require_run_inside(proof['run_process'], proof)


def check_patch_preflights(build, manifest):
    command = build['command']
    name = 'runner-artifact-public-' + build['run_id'] + '-' + build['run_attempt']
    require(command[command.index('--builder') + 1] == name and Path(command[-1]).is_absolute(),
            'Public build command identity differs')
    elapsed = build.get('patch_preflight_elapsed_seconds')
    require(build.get('patch_preflight_budget_seconds') == 900 and type(elapsed) in (int, float) and
            0 <= elapsed <= 900, 'Canonical patch preflight budget differs')
    for component in ('libbpf', 'gstreamer'):
        key = component + '_preflight_'
        timeout = build.get(key + 'timeout_seconds')
        require(build.get(key + 'completed') is True and
                build.get(key + 'command') == patch_preflight_command(manifest, name, Path(command[-1]), component)
                and type(timeout) in (int, float) and 0 < timeout <= 900,
                'Canonical ' + component + ' preflight is incomplete')


def validate_parent(parent, identity, manifest):
    build = document(parent / 'build-receipt.json')
    for key, value in identity.items():
        require(build.get(key) == value, 'Public artifact belongs to another source/run/attempt')
    require(build.get('stage') == 'public' and build.get('build_success') is True and
            build.get('oci_verified') is True and build.get('scan_completed') is True and
            build.get('builder_removed') is True and build.get('scanner_removed') is True,
            'Public artifact is incomplete')
    require(build.get('builder_cleanup_state', {}).get('state_volume_removed') is True,
            'Public builder cache removal is unconfirmed')
    check_cgroup_receipt(build)
    accepted.check_receipt(build.get('accepted_builder', {}), accepted.read_lock(), 'amd64')
    check_patch_preflights(build, manifest)
    require(document(parent / 'cgroup-verification.json') == build['cgroup_proof'], 'Parent cgroup proof differs')
    require(build.get('manifest_sha256') == oci.digest_file(MANIFEST), 'Public manifest binding differs')
    require(build.get('canonical_context') == manifest['contexts']['public']['files'] and
            build.get('canonical_source') == manifest['canonical_source'], 'Parent canonical inputs differ')
    proof = oci.verify(parent / 'image.oci.tar', build['archive_sha256'], build['archive_bytes'],
                       identity['source_commit'], 'public', SENTINEL)
    require(proof == document(parent / 'oci-verification.json'), 'Public OCI evidence differs')
    raw_scan = document(parent / 'scan.json', 128 * 1024**2)
    require(oci.digest_file(parent / 'scan.json') == build['scan_sha256'], 'Public scan changed')
    require(scan_summary(raw_scan, proof) == document(parent / 'scan-summary.json'), 'Public scan summary differs')
    check_scan_receipt(parent, build, proof)
    check_scan_acceptance(parent, build, proof, manifest)
    return proof


def execute(stage, parent=None):
    identity = validate_event(os.environ, document(Path(os.environ['GITHUB_EVENT_PATH'])))
    data = document(MANIFEST)
    validate_manifest(data)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    work = temporary / ('runner-image-work-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt'])
    output = temporary / ('runner-image-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt'])
    work.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    (work / 'home').mkdir(mode=0o700)
    env = clean_env(work / 'home')
    record = {**identity, 'schema': 1, 'stage': stage, 'started_at': now(), 'build_success': False,
              'oci_verified': False, 'scan_completed': False, 'builder_removed': False, 'scanner_removed': False,
              'deployable': False, 'site_configured': False, 'keys_transferred': False, 'automatic_retry': False,
              'canonical_source': data['canonical_source'], 'canonical_context': data['contexts'][stage]['files'],
              'manifest_sha256': oci.digest_file(MANIFEST), 'limits': LIMITS,
              'executor_sha256': oci.digest_file(Path(__file__)),
              'cgroup_observer_sha256': oci.digest_file(Path(cgroup.__file__)),
              'verifier_sha256': oci.digest_file(Path(oci.__file__)), 'sentinel_sha256': hashlib.sha256(SENTINEL).hexdigest()}
    # Docker and containerd may have separate filesystems. Measure each existing root.
    paths = [temporary, ROOT] + [Path(p) for p in ('/var/lib/docker', '/var/lib/containerd') if Path(p).exists()]
    sup = Supervisor(env, paths, output / 'build.log', output / 'disk-observations.jsonl')
    name = 'runner-artifact-' + stage + '-' + identity['run_id'] + '-' + identity['run_attempt']
    image = data['tools']['buildkit']['image']
    builder_lock = accepted.read_lock()
    builder_spec = builder_lock['architectures']['amd64']
    builder_inputs = work / 'accepted-builder'
    record['accepted_builder'] = {}
    owner = name
    created = False
    volume_absent_before = False
    def observe_cgroup(observation):
        with (output / 'cgroup-observations.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), **observation}, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    try:
        require(sup.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture=True) == identity['source_commit'],
                'Checkout differs from dispatched commit')
        record['initial_free_bytes'] = sup.check(START_FREE, force_record=True)
        record['docker_version'] = sup.run(['docker', '--version'], capture=True)
        record['buildx_version'] = sup.run(['docker', 'buildx', 'version'], capture=True)
        record['accepted_builder'] = accepted.prepare(builder_lock, 'amd64', builder_inputs, sup.check)
        require(record['accepted_builder'].get('transport', {}).get('lock_sha256') ==
                data['tools']['buildkit']['transport_sha256'],
                'Prepared builder transport differs from validated manifest')
        accepted.load(sup, builder_inputs, builder_spec, record['accepted_builder'])
        context = work / 'context'
        prepare_context(ROOT, context, data['contexts'][stage], stage == 'public', env)
        base, layout = None, None
        if stage == 'private':
            require(parent is not None and parent.resolve().is_relative_to(temporary), 'Unexpected parent artifact directory')
            base = validate_parent(parent, identity, data)
            layout = work / 'public-layout'
            sup.check(RESERVE + base['archive_bytes'], force_record=True)
            oci.materialize(parent / 'image.oci.tar', base, layout, sup.check)
            record['base_index'] = base['image_index']
            record['base_archive_sha256'] = base['archive_sha256']
            # Original remains durably in the prior job's artifact. Remove this owned
            # download only after complete validation/materialization to avoid 3 copies.
            (parent / 'image.oci.tar').unlink()
        config = work / 'buildkitd.toml'
        config.write_text('[worker.oci]\n  max-parallelism = 1\n')
        require(probe_volume(sup, state_volume(name)) is None, 'Builder state volume already exists')
        volume_absent_before = True
        record['state_volume_absent_before_creation'] = True
        command = ['docker', 'buildx', 'create', '--name', name, '--driver', 'docker-container',
                   '--buildkitd-config', str(config), '--driver-opt', 'image=' + image,
                   '--driver-opt', 'memory=4g', '--driver-opt', 'memory-swap=4g',
                   '--driver-opt', 'cpu-period=100000', '--driver-opt', 'cpu-quota=200000',
                   '--driver-opt', 'restart-policy=no', '--driver-opt', 'env.RUNNER_ARTIFACT_OWNER=' + owner]
        # A timeout may occur after Docker/Buildx accepted creation. Reconcile
        # this exact owned name in finally even if the create client fails.
        record['builder_creation_started'] = True
        created = True
        sup.run(command)
        record['builder_created'] = True
        sup.run(['docker', 'buildx', 'inspect', '--builder', name, '--bootstrap'], seconds=180)
        observed = inspect_builder(sup, name, owner, image)
        record['builder_id'] = observed['Id']
        record['builder_image'] = image
        public_recipe = subprocess.check_output(['git', '-C', str(ROOT), 'show',
            'HEAD:deploy/runner-reserve/Dockerfile'], env=env, timeout=20).decode()
        public_hash = hashlib.sha256(public_recipe.encode()).hexdigest()
        require(public_hash == data['contexts']['public']['files']['deploy/runner-reserve/Dockerfile']['sha256'],
                'Preflight source differs from canonical recipe')
        record['cgroup_proof'] = qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe_cgroup,
                                                 cgroup_reader='/usr/bin/oci-cgroup-reader')
        save(output / 'cgroup-verification.json', record['cgroup_proof'])
        def check_build():
            current = inspect_builder(sup, name, owner, image)
            require(current['Id'] == observed['Id'], 'Builder identity changed')
            cgroup.sample(current['State']['Pid'], record['cgroup_proof'], observe=observe_cgroup)
        if stage == 'public':
            run_patch_preflights(sup, data, name, context, record, check_build)
        observe_builder_storage(sup, name, owner, image, record, 'before-build')
        command = build_command(stage, data, identity, name, context, output / 'image.oci.tar', layout, base,
                                tools_layout=builder_inputs / 'layout')
        record['command'] = command
        sup.run(command, seconds=2700, inspect=check_build)
        check_build()
        final = inspect_builder(sup, name, owner, image)
        record['builder_state'] = {key: final['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error')}
        record['build_success'] = True
        record['archive_bytes'] = (output / 'image.oci.tar').stat().st_size
        record['archive_sha256'] = oci.digest_file(output / 'image.oci.tar')
        # Releasing only our builder's state reclaims compiled Go caches before scan.
        record['builder_cleanup_state'] = remove_builder(sup, name, owner, image,
                                                        volume_absent_before=volume_absent_before,
                                                        observations=record.setdefault('builder_cleanup_steps', []))
        created = False
        record['builder_removed'] = True
        proof = oci.verify(output / 'image.oci.tar', record['archive_sha256'], record['archive_bytes'],
                           identity['source_commit'], stage, SENTINEL, base)
        save(output / 'oci-verification.json', proof)
        record['oci_verified'] = True
        run_scan(sup, data, work, output, owner, proof, record)
    except BaseException as exc:
        record['failure_type'] = type(exc).__name__
        if record.get('builder_id'):
            observe_builder_storage(sup, name, owner, image, record, 'failed')
        # Do not include arbitrary command stderr/environment in structured metadata.
        raise
    finally:
        if created:
            try:
                record['builder_cleanup_state'] = remove_builder(sup, name, owner, image,
                                                                volume_absent_before=volume_absent_before,
                                                                observations=record.setdefault('builder_cleanup_steps', []))
                record['builder_removed'] = True
            except BaseException as exc:
                record['cleanup_failure_type'] = type(exc).__name__
        try:
            accepted.cleanup(sup, builder_spec, record['accepted_builder'])
        except BaseException as exc:
            record['import_cleanup_failure_type'] = type(exc).__name__
            if 'failure_type' not in record:
                raise
        finally:
            record['minimum_free_bytes'] = sup.minimum
            record['finished_at'] = now()
            save(output / 'build-receipt.json', record)


def probe_scanner(sup, name, owner, image):
    result = subprocess.run(['docker', 'inspect', name], env=sup.env, capture_output=True, timeout=30)
    if container_absent(result, name):
        return None
    require(result.returncode == 0, 'Unable to determine scanner ownership')
    items = json.loads(result.stdout)
    require(len(items) == 1, 'Ambiguous scanner container')
    item = items[0]
    require(item['Config'].get('Labels', {}).get(OWNER) == owner and item['Name'] == '/' + name and
            item['Config'].get('Image') == image and re.fullmatch(r'[0-9a-f]{64}', item.get('Id', '')),
            'Foreign scanner container')
    return item


def remove_scanner(sup, name, owner, image, record):
    # Unknown daemon/transport failures are not evidence of absence. Cleanup
    # deliberately bypasses disk admission, just like owned builder cleanup.
    item = probe_scanner(sup, name, owner, image)
    if item is not None:
        record['scanner_state'] = {key: item['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error', 'Running')}
        subprocess.run(['docker', 'rm', '--force', item['Id']], env=sup.env, check=True, timeout=30)
    require(probe_scanner(sup, name, owner, image) is None, 'Owned scanner removal not confirmed')
    record['scanner_removed'] = True
    if item is not None:
        require(not item['State'].get('OOMKilled'), 'Scanner was OOM-killed')


def retain_scan_databases(cache, output, check):
    """Hash the actual stopped scanner's DBs before any owned cache cleanup."""
    names = ['db/trivy.db', 'db/metadata.json']
    java = ['java-db/trivy-java.db', 'java-db/metadata.json']
    if any((cache / name).exists() or (cache / name).is_symlink() for name in java):
        names += java
    hashes = {}
    for name in names:
        path = cache / name
        require(not path.parent.is_symlink(), 'Database directory must not be a link')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        raw, digest = bytearray(), hashlib.sha256()
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            maximum = 8 * 1024**2 if name.endswith('metadata.json') else 8 * GIB
            require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum, 'Database size/type differs')
            total = 0
            while block := stream.read(1024**2):
                check()
                total += len(block)
                require(total <= maximum, 'Database grew beyond bound')
                digest.update(block)
                if name.endswith('metadata.json'):
                    raw.extend(block)
            require(total == info.st_size and oci.identity(os.fstat(stream.fileno())) == oci.identity(info) and
                    oci.identity(path.stat(follow_symlinks=False)) == oci.identity(info), 'Database changed during hashing')
        hashes[name] = digest.hexdigest()
        if name == 'db/metadata.json':
            oci.json_document(raw)
            with (output / 'scan-database.json').open('xb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
    return hashes


def hosted_scan_context(build, manifest):
    command = build['scan_command']
    suffix = [manifest['tools']['trivy']['image'], 'image', '--input', '/input/image', '--cache-dir', '/cache',
              '--scanners', 'vuln', '--pkg-types', 'os,library', '--list-all-pkgs', '--format', 'json',
              '--output', '/output/scan.json', '--timeout', '15m', '--parallel', '2',
              '--ignorefile', '/dev/null', '--exit-code', '0']
    require(command[-len(suffix):] == suffix, 'Hosted full-scan command differs')
    return {key: build[key] for key in ('source_commit', 'run_id', 'run_attempt', 'stage')} | {
        'artifact_name': '/input/image', 'raw_scan_sha256': build['scan_sha256'],
        'scan_input_sha256': build['scan_input_sha256'],
        'scanner': {'kind': 'oci-image', 'version': manifest['tools']['trivy']['version'],
                    'image': manifest['tools']['trivy']['image']}, 'databases': build['scan_database_files']}


def run_scan(sup, data, work, output, owner, proof, record):
    layout = work / 'scan-layout'
    # An OCI tar is not a Docker archive. Trivy requires an OCI directory;
    # select the already verified executable manifest, irrespective of order.
    sup.check(RESERVE + proof['archive_bytes'], force_record=True)
    selection = oci.project_scan_input(output / 'image.oci.tar', proof, layout, sup.check)
    save(output / 'scan-input.json', selection)
    record['scan_input_sha256'] = oci.digest_file(output / 'scan-input.json')
    cache = work / 'scan-cache'
    cache.mkdir()
    (cache / 'tmp').mkdir()
    name = owner + '-scan'
    scan_output = work / 'scan-output'
    scan_output.mkdir()
    command = ['docker', 'run', '--name', name, '--label', OWNER + '=' + owner,
               '--user', str(os.getuid()) + ':' + str(os.getgid()), '--cpus', '2',
               '--memory', '4g', '--memory-swap', '4g', '--pids-limit', '256', '--read-only',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--mount', 'type=bind,source=' + str(layout) + ',target=/input/image,readonly',
               '--mount', 'type=bind,source=' + str(cache) + ',target=/cache',
               '--mount', 'type=bind,source=' + str(scan_output) + ',target=/output',
               '--env', 'TMPDIR=/cache/tmp', data['tools']['trivy']['image'],
               'image', '--input', '/input/image', '--cache-dir', '/cache',
               '--scanners', 'vuln', '--pkg-types', 'os,library', '--list-all-pkgs',
               '--format', 'json', '--output', '/output/scan.json', '--timeout', '15m',
               '--parallel', '2', '--ignorefile', '/dev/null', '--exit-code', '0']
    record['scan_command'] = command
    try:
        sup.run(command, seconds=1000)
    finally:
        # A killed docker client does not necessarily remove its container.
        remove_scanner(sup, name, owner, data['tools']['trivy']['image'], record)
    require(record.get('scanner_state', {}).get('ExitCode') == 0 and
            record['scanner_state'].get('Running') is False, 'Successful scanner exit was not observed')
    oci.verify_scan_input(layout, selection, proof, sup.check)
    require(oci.digest_file(output / 'image.oci.tar') == record['archive_sha256'], 'Archive changed during scan')
    require(not (output / 'scan.json').exists(), 'Scan output already exists')
    (scan_output / 'scan.json').rename(output / 'scan.json')
    raw = document(output / 'scan.json', 128 * 1024**2)
    summary = scan_summary(raw, proof)
    save(output / 'scan-summary.json', summary)
    record['scan_sha256'] = oci.digest_file(output / 'scan.json')
    record['scan_completed'] = True
    record['scan_gate_passed'] = summary['scan_gate_passed']
    record['scan_database_files'] = retain_scan_databases(cache, output, sup.check)
    try:
        evidence = scan_file_evidence(layout, selection, proof, sup.check)
        save(output / 'scan-file-evidence.json', evidence)
        record['scan_file_evidence_sha256'] = oci.digest_file(output / 'scan-file-evidence.json')
        acceptance = scan_acceptance(raw, proof, evidence, hosted_scan_context(record, data))
        save(output / 'scan-acceptance.json', acceptance)
        record['scan_acceptance_sha256'] = oci.digest_file(output / 'scan-acceptance.json')
        record['scan_acceptance_passed'] = acceptance['acceptance_passed']
    finally:
        shutil.rmtree(layout)
        record['scan_input_removed'] = not layout.exists()


def check_scan_receipt(directory, build, proof):
    require(oci.digest_file(directory / 'scan-input.json') == build.get('scan_input_sha256'),
            'Scan input receipt changed')
    oci.check_scan_input(document(directory / 'scan-input.json'), proof)
    require(build.get('scan_input_removed') is True, 'Owned scan input cleanup unconfirmed')


def check_scan_acceptance(directory, build, proof, manifest):
    for name, key in [('scan-file-evidence.json', 'scan_file_evidence_sha256'),
                       ('scan-acceptance.json', 'scan_acceptance_sha256')]:
        require(oci.digest_file(directory / name) == build.get(key), 'Scan acceptance evidence changed')
    require(oci.digest_file(directory / 'scan-database.json') == build['scan_database_files']['db/metadata.json'],
            'Original scanner database metadata changed')
    raw = document(directory / 'scan.json', 128 * 1024**2)
    result = scan_acceptance(raw, proof, document(directory / 'scan-file-evidence.json'),
                             hosted_scan_context(build, manifest))
    require(result == document(directory / 'scan-acceptance.json') and
            result['acceptance_passed'] is build.get('scan_acceptance_passed'), 'Scan acceptance changed')
    return result


def gate(directories):
    require(len(directories) == 2, 'Both stages required')
    manifest = document(MANIFEST)
    validate_manifest(manifest)
    builds = [document(p / 'build-receipt.json') for p in directories]
    require({b.get('stage') for b in builds} == {'public', 'private'}, 'Missing stage')
    for key in ('source_commit', 'run_id', 'run_attempt', 'manifest_sha256'):
        require(len({b.get(key) for b in builds}) == 1 and builds[0].get(key), 'Stage identity differs')
    for directory, build in zip(directories, builds):
        check_cgroup_receipt(build)
        accepted.check_receipt(build.get('accepted_builder', {}), accepted.read_lock(), 'amd64')
        if build['stage'] == 'public':
            check_patch_preflights(build, manifest)
        require(document(directory / 'cgroup-verification.json') == build['cgroup_proof'], 'Cgroup proof differs')
        summary = document(directory / 'scan-summary.json')
        require(oci.digest_file(directory / 'scan.json') == build.get('scan_sha256'), 'Raw scan changed before gate')
        proof = document(directory / 'oci-verification.json')
        require(scan_summary(document(directory / 'scan.json', 128 * 1024**2), proof) == summary,
                'Scan summary changed')
        check_scan_receipt(directory, build, proof)
        require(build['manifest_sha256'] == oci.digest_file(MANIFEST), 'Gate source manifest differs')
        acceptance = check_scan_acceptance(directory, build, proof, manifest)
        require(build.get('build_success') is True and build.get('oci_verified') is True and
                build.get('scan_completed') is True and build.get('builder_removed') is True and
                build.get('scanner_removed') is True and
                build.get('builder_cleanup_state', {}).get('state_volume_removed') is True and
                not build.get('cleanup_failure_type') and not build.get('import_cleanup_failure_type'),
                'Incomplete artifact production')
        require(build.get('scan_gate_passed') is summary['scan_gate_passed'], 'Raw scanner gate changed')
        require(acceptance['acceptance_passed'] is True, 'Unresolved findings or image prerequisites require review')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['public', 'private', 'gate'])
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.operation == 'gate':
        require(args.parent and args.output, 'Both artifact directories required')
        gate([args.parent, args.output])
    else:
        execute(args.operation, args.parent)


if __name__ == '__main__':
    main()
