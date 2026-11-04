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
    from . import runner_image_cgroup as cgroup
    from . import runner_image_oci as oci
else:
    import runner_image_cgroup as cgroup
    import runner_image_oci as oci

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / '.github/containers/runner-artifacts.json'
GIB = 1024**3
RESERVE = 2 * GIB
START_FREE = 10 * GIB
SENTINEL = b'UNCONFIGURED SITE CA\n'
OWNER = 'io.victron.runner-artifact-owner'
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
                     'public_rationale_document_sha256': '30a993166cea8f8cde4c668ce704df0ace2dcb849fbc40b1384b6c3ff1e7b8e3',
                     'official_header_provenance_sha256': '210990b813fe81a35a8f598ebb0fe77ef743327751cb8b15801e199c71449db2',
                     'ubuntu_deb_sha256': 'fec8e4a308c03cd98dace67370a8d5a8031c36741796065c9f6e3f9548ab0cd2',
                     'ubuntu_signing_fingerprint': 'F6ECB3762474EDA9D21B7022871920D1991BC93C',
                     'runc_source_commit': '29dd3dc2b13b4123162e5fe132504bb4b15569f1',
                     'runc_source_archive_sha256': '5386e1968f70cc1482b4b2a93dc1a88ea7fa5517fba49b9e0fafe614d5a4c97a'},
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
          'bytes': 14131248,
          'mode': 493,
          'sha256': '6043541aca928890ab8ad0630cafd2b272c3880779d8ada1b3fe81bc22e80d20',
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
 'advisory_sha256': '951064a860f4d9f2274785202be652efdbf01f4e034c1770dcce8b43488588dd',
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
                            'package_path': None},
                'runc': {'class': 'lang-pkgs',
                         'type': 'gobinary',
                         'package_id': 'golang.org/x/net@v0.55.0',
                         'package': 'golang.org/x/net',
                         'version': 'v0.55.0',
                         'purl': 'pkg:golang/golang.org/x/net@v0.55.0',
                         'source_package': None,
                         'source_version': None,
                         'source_release': None,
                         'architecture': None,
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
                                   '48ad2922396d252a5c8e855a16ec692a0bfce159ca8cc87590ef5e9758202ed4',
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
                                   '81aaefa4673dad333d663ef12730f60f39c95eacd115ea738d7482d4d1c49357',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-37776': ['headers',
                                   '98874f1fc8b80236441090d3afeae9d75a8b92746292bc2ba686164c86539fa3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-37906': ['headers',
                                   'e321f8350f4f3892c2d6441676063c6f4b19febd55ac605c0e7aaca75257c10b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-38204': ['headers',
                                   'bdc4cc798c9db2a236c5a9ef20f8c9368ecd23640252d47d169acf9bc919346b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-38717': ['headers',
                                   '50cc0b64fecfd99edf8a5ecc50e21410be5c1be24aa9e62b1bec5a63d1b4afb8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-39933': ['headers',
                                   '128781379c5209d31cb8e7b04003fd79b1fed6365478e89aa92273e4fee2c83d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40025': ['headers',
                                   '759493f8b32f2801d609dd0200e7365c8aa8cc658fa6f7c4be7fefdea78d30c4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40064': ['headers',
                                   'a548e9e6d8a140756ec4eb4910beba9cc24eed0fce62c37f1fcc6a2fe79c24d8',
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
                                   '36ba6cd45f466c118c7f9bd93b05a1dd2d9e44ef64f5d5765667c57dd7362e99',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40168': ['headers',
                                   '05ff8d300d887e5d66421a732e59f6c6bc6b3e1c671a0745c03e02eab7afee6a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40170': ['headers',
                                   'bacebc833b372812ac5a111f0495aa62c563dad07ed60a3535ada8f5766c0887',
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
                                   '172c541675ae143f01ceafbe7a84f5da2c5a990cb13a2d1b119084ed9f3796ba',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71073': ['headers',
                                   'ed0a15a8c7373de1e58764f159111cb6ab03e0ecda741e7acb1c95df74941acd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71074': ['headers',
                                   'f869eeda53cd955da278c674a1b8bd06b337590d06d6a50d94188fa1a378b541',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71202': ['headers',
                                   '81998cc0b1b8a763143b50c396b99f74c56e73d2aebc7b7229fe535ee66e6e44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71315': ['headers',
                                   '7457d1aa33fc649ff115107e6f0aebbe2d458878c9e6ea554afb6e3ed10fef1b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-100074': ['headers',
                                    '75a11488928cef8469088c8a6f4b8efa68d3ea8a9b854390f5e3169f75c33478',
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
                                   '323111839e29ef0703bd63b623bf661671de4c339649dc903568c832ff5e48bc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23385': ['headers',
                                   'e3f766ff8d7f6d08be1244dbaba90c4fe03ce9260db07041d4f8a5746b3fec22',
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
                                   'f986c98b5baea066384074c621f671ffe066b9a4ddebf6b1343af2c4e0e49a96',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43009': ['headers',
                                   'ada6b96a118561cb9e8b8e3063cecff1eefa99c484a5285e381bbeccb6cdbceb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43034': ['headers',
                                   'ac1f6795749c930defe36e6a5209a084931985152aa921172c0d7020212921cf',
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
                                   'ee102d0e9b0e7181fd78b09e6a8709ddf04dbbd8ccb5756f20839c7cf7ee0462',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46181': ['headers',
                                   'fb4142c2936d7308aff7ad52b1541e29cccb488ce61d25274451e0d706cffbf4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46330': ['headers',
                                   '042c3cd0693ddc43c81eae2a9ed8e9c42f1911ae67180a6f8a467acab76a4943',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46600': ['runc',
                                   '7a34ce76597bada6319b5c98df6a70b417c6f8532c1e325c25250eea34a4c5c8',
                                   'runc_affected_packages_not_linked'],
                'CVE-2026-52946': ['headers',
                                   '351299a17887bb06efd87e403b3e1e0323630292385ec35ce9906977e44670ed',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-52991': ['headers',
                                   '587974b721a5142bd982e94ebd0eb14eb6fecc39506f43ce7bf448141bd2f3c4',
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
                                   'f4442a49039a2396c5f9b1dcc7804eab88f130bba977e2139949152308f16037',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53377': ['headers',
                                   'd583e2de7f2a3d73b75e56b505dd744c2b97f4b156083eb077e64f64c30b3c59',
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
                                   '3315022bdded0ca654e482be2c50adc699023df98754cbea08851904aab5168c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64036': ['headers',
                                   '09c27a5b2854edf13dc77c87017af1498be1824134aeab2755f63287025f89b4',
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
                                   '080b0244cdbcb26bdc2c0567329689ba62e2662b0e6a8445d9095c7daa4cc5ab',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68081': ['headers',
                                   'f599ccef7c4b79294f5fc6348cf36bc32a2d9b07edcc63a4d4845e32451871de',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68103': ['headers',
                                   '061b53ee8c456b2f70ad027be0268ae0fa6edddb57104ca0fcd72495b50f36ab',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68152': ['headers',
                                   '52c31c419066b85b34ba096fdd3a726c606da4dd3616a9295f9c5fd19d82810f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68198': ['headers',
                                   '64d89d1121bde28845fe37252ae25b48f1e3f2c100f5e9ef3f535b2594d778c7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68245': ['headers',
                                   '4536823d7c29802d44c5f3b945f80f78c8a5ec078b4b2cf70ba6288cfb08990d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68257': ['headers',
                                   '27fb5054b9ccc64a221864f3e56dc47ffb652b60e0e0fe7227cd6e0468dbc81f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68323': ['headers',
                                   '43bad046bc13c0fbf3bad72fe12215c588d5ba9e7b8970559844f923fc5b2777',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68401': ['headers',
                                   '8fe7b91967dc77ce9b5c4d4beab261152403d363c28989d415c012b6916e54e6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68404': ['headers',
                                   '49eea1fc1e701a042fcd314d422aed6f9fa5d0cf41e2e65f8f635908cdbfac43',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68409': ['headers',
                                   '783a723a3f6d161b82cb61ffbe99ddb67e1ff8e1dd571e86343a82f62c10bced',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68426': ['headers',
                                   'e79c74a553ec8ff62c0c5c7873aef929435f86601926b94105e072e4657268db',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68447': ['headers',
                                   '6d2298cd1573a77640400ed673f89a1f61f5148dc54205b723cb6f96120c09fc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68451': ['headers',
                                   '2941758cea02af413441ffd8d6db6ab992628bb92d2fc341bb9f7af22b311763',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68470': ['headers',
                                   '7785171c1cb9b8d86448897d6073f39038a440c33bb9d569d943941bae4d1053',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68471': ['headers',
                                   '55b25cbd59e1e48bd8a50e32bb96e0d6e84e08a900980aad128d1261d231eb75',
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
                                   '5c95e936f68f0440322965a31e0bf0155ebe860b41cbdf12d268d8f54f1dcaa4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72375': ['headers',
                                   '327f7ee58a7035b04b230f14e22809c7405b087c8ab30a596fbd18411ff06170',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72380': ['headers',
                                   '4310a189b209e2340e4f36fffeaee7f4e605f258d0dfe24b2d87b796a0565812',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72383': ['headers',
                                   '69e3002029192985352c003fe925e348492d071ff90913f0a6e38f250ea81571',
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
                                   'fbc7570b2127ec52a318185077c677006e35b00199cee5521ea2880a3a5c830d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74268': ['headers',
                                   '014d3163a2f68b8fc90847b78cccffe7209b95bd9e4e6f9d0c2bcfc962ca9da8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74269': ['headers',
                                   'c72fc796f06c7ef062e470918dce6628737494ad01a62cf71f462e9be6baa6bd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74314': ['headers',
                                   '00be392e686604b726613b8155e10787a3092f52970c9baf228b5fa6207e2c2a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74317': ['headers',
                                   '1509021234e74d8d1b9182bb24624b521c9fbb748327adec67bcd0d6d10e6ef1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74334': ['headers',
                                   'c52de62df71af63b6841b875f7f8b7c54369ee94554b3837bdeb2fc1e9de71bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74344': ['headers',
                                   '52f955c377ec1fa6222c36affc0584b7de8e20d15966f39e16a26a4eac47dfc4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74347': ['headers',
                                   'e423803e6c518fe17f8fb2a13c025eed063946a8a1f1eaba2bab0c85576175a4',
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
                                   '507f6bd689849594f0793669e7b5e39350757770e9e85b793eba4fea95f9e812',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74454': ['headers',
                                   '1db00bc1c89a6ebbf6151f9aa009394ecd53feed19ea00bdf2e0ab773aa41561',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74465': ['headers',
                                   '75da1da8377e8a83d0d58e84b2e273c875ef8155e0d48ed66893845fdccf8e9b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74470': ['headers',
                                   'cfab9819851dcefb5736aa1cc381f5a140735fc87ffb57fab6fa6e2ae1c02456',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74506': ['headers',
                                   '22240190a2d619da2e8bea934016cdf4c71d71b645e2eb3fce53eed6dca210f1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74510': ['headers',
                                   '5fc46490c7c0244f584cc8fdd5501574a57372433e9f4391c35692d73074b61e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74522': ['headers',
                                   '22978c9578fa4bce9ff1e25264ffac1eb2298a0c7f063fcc47657d75549a2c7f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74530': ['headers',
                                   '6783ae17dbd621bb644135a79009d69bc73d3f9db7695cbed2baeb0cd703beda',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74533': ['headers',
                                   '4445eaa12eba74773ddd5d07afa26a71297d865b61ee58bea73788df7e1ebccd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74534': ['headers',
                                   '4174ae108bf8af8a64c44c5226343bae52b2882421f380734041c663bd7b13ce',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74535': ['headers',
                                   'ff234d92a9be90af85cbe89d56c0036d053d83b9af9d3644f3ac8eea02201e58',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74537': ['headers',
                                   '8bc96bebb095cdbc82870a3c2d076bd28fb4e661805845bbcb3f31469f45eb43',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74565': ['headers',
                                   'df40316580b337399a2fca7e01a9e8449e14e897b0882a5424b5a28e845da324',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74578': ['headers',
                                   '056ec6a15c783bac3aaf12955f312db3bfc19d53a8df71b68b41a4d1208aa38b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74677': ['headers',
                                   '7dd57fbeedb62968bbedd0b1e9763eaf29a8f01e80c253e9fc269103fc78ee6f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74684': ['headers',
                                   '49602daba71d6c9628f3c210f336e6b11decef313df731efc3930dcbafdbc81d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74700': ['headers',
                                   'd4ea46b4ba7dc5002ebe47631359cea6cf7576f5bc32be4e434c5363f6be8f79',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74714': ['headers',
                                   '91a5d85c3c4c1f3a52b70c946af6121ee80cf8f563d67166303ffeb29dd4c651',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74715': ['headers',
                                   '58b31513af4db84c6f9df903c1c15ea8957e79c1b321a0bcd2353952e228f0ad',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74723': ['headers',
                                   '348a3f90316df2f2befc3f1ff6a2f7e571f15ecc5596ba15f870c6dc9ab52c6d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74724': ['headers',
                                   '98364d565eee79cc85f33d37a1d0062392ff4b2ea9333ce064742b6456b5ea35',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74744': ['headers',
                                   '82a1749e5cc020a5487791aae06929691d6f3674bdb0fce7075e599bfa5c2b0d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74752': ['headers',
                                   'aeb6547a4d4c5ad44ffb165853e19227e76045935764e5b9b6de9901713c4067',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-78669': ['runc',
                                   '842d8f452eca3e7aae30e1281a24286fbb284619b5c49a52431214fb54c784f1',
                                   'runc_affected_packages_not_linked'],
                'CVE-2026-80551': ['headers',
                                   '2c9ca59968767b6cd82c54107afa045c42f4eb0f65d50e94e29bb986872ed8c6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80554': ['headers',
                                   'bf0467c55cf998c6e708da18e83024a0d02ed6baaec904ee97d99ac93141b328',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80631': ['headers',
                                   '3382e2ae315e723ba0ac2e38965cb8c5929b52fa5f47818ccf5a0cea33f9faa4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80634': ['headers',
                                   'c4f236690b1f56f272d20512c766298f7c7c51a19008bc4b2e88153587948b9b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80668': ['headers',
                                   '809733fc8e70b3f4329b4d7912f756bca4b831e8b6c8e26ed2ac855d931c656d',
                                   'sanitized_uapi_header'],
                'CVE-2026-80671': ['headers',
                                   '7aa9234b5f0e7adf1a8d5cdb3ec57ece45e5921f95ef83de277c5730451a3c34',
                                   'perf_payload_absent'],
                'CVE-2026-80681': ['headers',
                                   '092bffac07a4b124228ef6b95a9fccc9099802af69239952547b528064b49edb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80691': ['headers',
                                   '77136baf18022d8e91b7bf3ec5cf4c08ca41ccad6d1fc53560b44158f5fbbe8c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80692': ['headers',
                                   '0c0120a2b87f095f285ca611fa2fda5412c7dac646abd32aecce79ac9f0e0018',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80693': ['headers',
                                   'f38ac9cb6a670c5b4fa3b2b319c5a2e37f3b109443899594b296ce9cffcfd6df',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80700': ['headers',
                                   'a7f2d4aa145fd8d8696d0a51462b2d6f4f6508f630f231cf5ae7ad080e96e94c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80702': ['headers',
                                   'aa90bc93a3ee602b5ba36718c1dd1c323e69318c8adf518edde807897bee866d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80710': ['headers',
                                   '4df8d0aade4c11cb48fe207869920762d3bc848efdda56130d449daf3912f5aa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80714': ['headers',
                                   'fa6755fbff35189dcdcf41b5603f93e0cd8cfc15f763341b942370474e6df7b8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80716': ['headers',
                                   'c006637d5324459ce9b534ba3de117911fba348d0509c9aa9ac74454e2ca2cd9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80718': ['headers',
                                   '421c1878fcfcfa74bf0db0a68ba53cd018c2fa5efcdd5517f461762da77ccc64',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80721': ['headers',
                                   'd3a4a407eac21d45326cae8032227fef30f71826494673e0e8c16a41df1b1665',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80725': ['headers',
                                   '0d1a6e9f7de8b95083ad05bbb520b76bf5329e673b56bd62832d9169a017fae6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80837': ['headers',
                                   '438f832661c0699f977d864eb0b88f57d41b8bf83b5e3c4e7330fe1d37424ec0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80841': ['headers',
                                   '09ef91029101ed38c72a77f918a560b338529c1d838697994a6bb6cea8b00b5b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80852': ['headers',
                                   'b8fa1f817df31f72e02aada680006e5547607f4e24cbc0484b667978dc5781da',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80864': ['headers',
                                   'dcdf7962c1d3e276f9f64b6afc0ce160f6e209c8954932f6af5d429f32e73462',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80891': ['headers',
                                   '327d03ec4adb2fd825f2a6f4b1da23b9313358680c5c89011f2207f68e818d92',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80905': ['headers',
                                   '2079489d12d64508cfb02968e3fb7691d7ca6b7f1a0bcc1c8169677874d98ae5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80937': ['headers',
                                   'b862717003cccad0d2cbf7de9bb17a72fc8532e0e6810a76ad84f90307ef0816',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80980': ['headers',
                                   '931116f1957fd048121371a09efe070049c21788ed68d464037188a7bb9a3732',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80982': ['headers',
                                   '54ab06b0bca7fc71347a3985dcaa39b30482dc05a8523e25fad749cfe2f16e12',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80991': ['headers',
                                   'f7e5dab08f53c822ff6ecf709e1bf91c78bfc80f40eac3547c6258d121aff206',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89448': ['headers',
                                   '256c7b6e7224806531711c87a209aad15dfca24ec7286a2bb15470f703c020c4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89481': ['headers',
                                   '98fba05b0394aaafcbfaf4aadbd40c8595c51d3a085605a42e84fb9707d13648',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89492': ['headers',
                                   '61a75320ba08720c22859ddc6bd6fca3eb5180dfe7d17bc578baed2722266003',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89520': ['headers',
                                   'f3321b8f7b635a6725b7590fcb314fdf49f4486abe07b1ff2e431a0e6057d3bc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89526': ['headers',
                                   '593b91a36b85827f156243c27b7553c55b5f2dd12a87a0ad125ab3dd2f50e479',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89528': ['headers',
                                   'af88f1c11e0217c5d945f1ac3eca9406411660fa7372ced8410d25570bcff888',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89530': ['headers',
                                   '0f53b82511b73553f50e99cdb47d23fd60f7cacedca69c335b8e9eee0d8ee936',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89531': ['headers',
                                   '081a427f0944baf60489468d5d6fb8bba2cf85bc6edf8c1263550ac5f2f62135',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89537': ['headers',
                                   'd3eabc535ca4989ae07a1a40c44247d1589b0091eae5c33ee44469a5fe45a83c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89544': ['headers',
                                   '12fb989466de623e044236c345415c417b9dd6ad582c2b4f19bdfbdd91927ab6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89550': ['headers',
                                   '32076f87c7e57cb50e55d740c1e1c3708475c2b1c9239eceabf3cb2c707e17e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89564': ['headers',
                                   '04aa950077a58f8d0ed5abc8c469e6030c53c1570b72b2dcd466c9698e2cb5c0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89565': ['headers',
                                   '56494435f3af2fb39eab117a5977988ea681dfe21e27d342de68342f321d70db',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89580': ['headers',
                                   '5834f9caf59b8c3a768a79768b269e5b152f3d831f3fc136354b1431acade470',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89622': ['headers',
                                   '02d521cb59501942823b75335b033178caf426403d7e84164e6e457904cac35e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89631': ['headers',
                                   '3cbc6f5e969754fdf7d182a21811664e99a53fdda10567486614e5e969f88d30',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89633': ['headers',
                                   '1060b78e8b9c78cf180a96f6689cf6709ca06a24921ff6b0e9d4fc4117bc6982',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89637': ['headers',
                                   'a32a9db28750095c329785c13db9702bc270ba444bb99a9c18fdfbce624b8dda',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89638': ['headers',
                                   '0bc33ef0f66c4230bd15ad230fa6036119f91594dc75afeb1aedc894c436081e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89652': ['headers',
                                   '30e41a5c2fb32d59f8dedf4551e687992b7609f02960a1e78367446f75986903',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89659': ['headers',
                                   '7397cb1d1ad2f1207a82f94989f2e9f6ca202dc466cbecd8663f10b0d5c51708',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89675': ['headers',
                                   'd6c1381a95128e880578d13077773ce97f7cec8da053875684d266e3d0b31a08',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89676': ['headers',
                                   '3c58bd91fc144cc9299e005c5455a0acb0782e14e889e90dd79b696a3934e0ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89680': ['headers',
                                   'dee4837cedeb13437e29776ebdac5d5f5abbbdd471ebbb0f58910fee8edbf4e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89685': ['headers',
                                   '6029110182e6c7fcc3ba1965be2d75ef34fde3c79752b07d1537c4844cbfd9d8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89707': ['headers',
                                   '902065925206f9a6ff2233c5303a9d0cacddad8d5f0b3c955485508f7bd3cb13',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89708': ['headers',
                                   '1bcb36d498e022cecf603527f7f57d3ae1591e6b7f7e55319f8ad6e801e27978',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89788': ['headers',
                                   'a3dd52ad8d9251fe2329ba41a67da7b8dcdb1ec6a10ea9001868be350d91ed39',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89803': ['headers',
                                   '1e4a85f67295015d93d535d7a610a574d9725ec2cc52e9bea73a1429da750f29',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89810': ['headers',
                                   'fe530d21c41772e0c091476af7e9eba34f1c8f5ea4361410a58891fd307ea2f4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89837': ['headers',
                                   'c48da667c345cadc1a2364b8a9ddf38469bb5a6dec0d38e974f5a156d0eee95e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89913': ['headers',
                                   'c6cf390f1e2adabb5c8ca7302c6d1b40675686d3cddf6655410fb97db70d4dd9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89922': ['headers',
                                   'fde2d7ffe090cd18b1b997bb7f91e851f97d865232a1da23e17c72ae1410b547',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90003': ['headers',
                                   'f72d3ef824a2dc93f13abfbdc63d17390be8ae84cea1ceffdc6f9baa34d4e5d4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90037': ['headers',
                                   'edcee6a4b866c19fb6347e9ae7a01e11f2ffa458983086b16810774d02c3e2d1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90048': ['headers',
                                   'cc69b6196206def0fd565aef4233a29c6af2e18c9de2fe92d49f6a5fa4e2eff0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90057': ['headers',
                                   '71180e05c4666e45024f5a086ff1217c651f6e5a7215d334fc87d25d0bb1d8e8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90092': ['headers',
                                   'ee637f16489cbcb3f2ea253e85c4b53193d13e6b1963f2cbebabab8ac248fbf6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90093': ['headers',
                                   '0d44fb074e4f9b562f7a1af0033d42c7181609ac4fb4d8fcd09d69f0536611c0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90104': ['headers',
                                   '384bcd134edad001682c262e1e0a01e78efbc9bf1169c2aa38fc20c068fb7a4e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90108': ['headers',
                                   '499f49d7a8eada1884fc75b0ac612381cb75e5debfda7be529d93d210a23f991',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90111': ['headers',
                                   'aa6af319fd17fc34d72279d436843b5335555d5950919c7eda940b1233466db2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90148': ['headers',
                                   '24db0b16047c169372c3516bc9bc3392da8c04a896067a68bad1968bd5abf409',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90149': ['headers',
                                   '484551e6944fdb86e214e476deb72b446836807b208ae6e9031f26589240e6a2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90150': ['headers',
                                   'b859901fbe4a3c3acd3f9decd78a49985a7a5d49e895e604e9e93927ba33d236',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90152': ['headers',
                                   '9bc1d85a80d95da9e0e14179167aa73b9a21076865ba56251987e1adc9793f78',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90155': ['headers',
                                   '0000817b0bb76b2a5a896704b2d9a28582df027844bb49f3df012b4b4bb2aec7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90161': ['headers',
                                   '2779b83b1cf0473538e5c19050b0f32b3575ab34a471bbc95e3224f48e55cad9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90162': ['headers',
                                   '8154eafc9cc88141f13273de8102037e3b1665e0562704d8b65ffbb16f83e500',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90167': ['headers',
                                   '728c62053d7d4172a472650bdaf2453969fbd7c31051c6a9a95da768dcd89013',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90169': ['headers',
                                   '04ab9346d5708d2f00c16de544231ef2ccc3e6509d8685eb10be69a4aad625bb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90199': ['headers',
                                   'cbd93d85195d57900e10413cdad1f762cc03a78cd8d462ff2c00d9bbd4968854',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90200': ['headers',
                                   '1dc7cc031fee67c3bd01c4295443493f6e28511d553ceb815c6501ad1391e90e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90204': ['headers',
                                   '8c4778082094d2e5914c2ff3a5b13f41db6a197adb53d49d71585267854f754a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90205': ['headers',
                                   '5170aedaac4eb5d15d6a899fe4ff1c2360b71221a2a2c44f355f9695b6d04e3e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90217': ['headers',
                                   '9c1300e148c8a576e1600c616aacd917acdd5e571d8fda7a5a4514e958bf309d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90227': ['headers',
                                   'beff6cd53aef37f6e90514b9616ec4a55257048d0810e14910ffd93bcec7d91d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90237': ['headers',
                                   '13b9dfd12dea3ed41cd32b1c84b753bf59e13cfef9be49abcfc796d95fc6dfe6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90256': ['headers',
                                   '791effef378220ce3d8e2df24a35670e05d49cdf1ea57e39fb1264cda2c353c5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90313': ['headers',
                                   '6d96b21e6964a1e159151a84f49d132406ebb6b0da2b25823b4e7e9065a76753',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90317': ['headers',
                                   '23b1c5bc0a624abbf70b2d6dd173922e4c2fd4ff9614832061e11cb385348364',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90320': ['headers',
                                   '6160c448275b6587d2a554ccd05eba6002b6605b78f4d0fde191a085f3a06d42',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90321': ['headers',
                                   '024cc8b282855369466989653ed3412cb436f4b25b2cb5a18847545e24fd7aaa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90324': ['headers',
                                   '4d708b171c5466999d146d39a849477b39e6692355292fc64afd4c1379d18ac1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90343': ['headers',
                                   '6d527ac583b1f4f2dced2da75409bdea9fe85d701bbc6a48496e17008ece3b7e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90375': ['headers',
                                   'aef5eb14fcce010b46e1d5a6f4ecf0479b89c523ecf625923540796cb66c5b92',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90380': ['headers',
                                   '00631aad8df17990085d8ad255759360b39cad985c8c6f4ff3e59353f502824a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90392': ['headers',
                                   '45cd93116517b6ed00f445bdcafaf85088d7cdc6e12a5b33b4da380a32312bdc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90398': ['headers',
                                   'b28843caabdcbc3f60c0e5cfa520df58d8d78083c7c691959b03720d5395e46e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90417': ['headers',
                                   'ea83f7b506f3c8678c53f0724909b80a92728789d8effe291dc58dec521cd803',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92499': ['headers',
                                   '4c2801a7240fb8b03f6dd734ad3521bdcd709ee27c816b3ab40c5ce6d68da4be',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92507': ['headers',
                                   '5f4b17f76eebb2359dda8f11f4f3484949352c21841b4e923320989df81d54d2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92509': ['headers',
                                   'df4160121c7d244988117a7bab54effe9a65ca243972319e7802143c5f381a44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92510': ['headers',
                                   'b7416b8d71872d8ba45a070688b7806cf22fc0bd589d1904283e6fbe24bdd8b6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92511': ['headers',
                                   '6454169fe9c481d0cf46fa03290b073a1db79f99d2b9885dfff731c5f23aae50',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92512': ['headers',
                                   '6cf9a796588ad1d334c2f2bd23fecd756bb7f1471545fdf6d627ae0956076909',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93105': ['headers',
                                   'e1b26b5a4837f94df76978e741a2ca7d03186f635492e34f88287b3454e719e5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93125': ['headers',
                                   '495d70639bd6e9204760a9bd946d2969ba1a2dd996eac30f17fe3f71dbc6dd15',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93144': ['headers',
                                   '6c1647e5e1719f87ea89807289d361cc121e94fef3a52b727f1a4b105e4d35e3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93148': ['headers',
                                   '84d20b0587a65c5e2c0d6510b5026eb0b82c72142d908cfdef96a7a9db4e8051',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93154': ['headers',
                                   '418b38eadc0ce3ac851d43f3f92624f53f3b488157e3e003b6d62348b7e18777',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93175': ['headers',
                                   '1adbe2d3b2b6544a5232f8d39dc3a61d57f25a6f69e7a4243bd2d0ab62c446b7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93176': ['headers',
                                   '80892b73b5edc42c51df2004802d24cdccde765e7541e7657dd4c03c0d5e2cfd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93192': ['headers',
                                   'eb8e2c5ce2021e70fcb087a87458a2e3dbf82375f22832165e2ae2b397e9d050',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93196': ['headers',
                                   '4b6186f8f70051f67a2550cb6f1ccd8db50453a7f2b70a982de6aafd9c0f3554',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93199': ['headers',
                                   '25d29fb712189735a3cb46429c190c59b9f19559645c28565568c6e6e4afaff9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93210': ['headers',
                                   'edafda6ee8db4ee5d3b016a8e7321c4f46a205a0ee5563b11d94d1b0b4472a83',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93221': ['headers',
                                   '0c4b0200d26d39ec54857f6650dfc9f542d6c4f9c4c0bb878ef34bc33f5f8c61',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93282': ['headers',
                                   '11dcc568bed189ccf1bca3616d2c585502be8737a5567331ae3d0d45755c4936',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93787': ['headers',
                                   '3dbb47438c9d3ff55f49df6c0633815955765ece61e44f83303ef174c44ee116',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93790': ['headers',
                                   '7a8bf6deffe35e7142f28e58def79c9129948bb425011684f4ba76e0b9dfc8fa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93792': ['headers',
                                   '59cf94b5d51858bf668789cfc8b1fc3ee3fab08f63729cf7508c613b2d2d5f54',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93796': ['headers',
                                   '93e50f47c8959946a403e8c9183018c48c298b2b91366a4ba6695d6966a328d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93806': ['headers',
                                   'c7e8353c1fb195c511a5cb2b3ed41e6ff986bc689851f3ac60205c8ccdefb6ba',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93817': ['headers',
                                   'b09187a2a2a4e75da782ab268e8613dbb82d886805c1e3a258051a5abf26ce39',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93824': ['headers',
                                   '9f386bb3eaed3669bd486f1e0d2c2e72920c63cdf6c47363a0a66554fb359613',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93828': ['headers',
                                   '6c20fc827249548bf98a983115943fe30a885e6e69342c90bdd1e3a574fb953d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97415': ['headers',
                                   '94fbfb04c66e4cdd613839e257ae3d3c8dd9622e9cf9573fea26b2abb5e3ca73',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97429': ['headers',
                                   'a0648eabf535306e1a2520027130d7e36f15840b16fff0257771e3691ddba6bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97442': ['headers',
                                   '1e5271cb04c3960dc4172e55e856bbcf2d413841190901602ff3e8fbe8d0db14',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97493': ['headers',
                                   'aa193934296b45e0d629f1b6c6788ddf3687e75d46d7b601ff158c4bf8854f97',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97497': ['headers',
                                   'be1d2b558b87aab0d14be1f08734cbf9055a7862c0ea167f51af2bf967152ce4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97513': ['headers',
                                   '30af879db58e681da4f7c97140c0a481be9f547c62caf4a80ca7463346dcdeb0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97530': ['headers',
                                   'bfad651dfc4edc4f469998d202195896a0a59bc258f88344a7f5005183aba716',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97555': ['headers',
                                   '6b6fbf230900aea9e95b280056c5a9b9f875354633c16b4a227ffafc4928e610',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97557': ['headers',
                                   'f6116d7c7c1c9827e56e3410edc7733adb3c0e9c937a31d15b4b79bdf87d710d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97562': ['headers',
                                   'c7d9ee5ead119e8ef08c0caf8770c22b6a856dc638375df6a1e9b56c2203dd64',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97564': ['headers',
                                   'bfa187ce28530ed1b2f49094279ba056886fdeaded760825bb0566be99826e17',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97565': ['headers',
                                   'd6335260505ab5ad7fe88a60953e18f356a4a86e6d2a86acbbf64a94d531149a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97581': ['headers',
                                   '66330be6d09ea9226725ef944418be431f938f0099f68e3a67eb2625e1bced8d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97603': ['headers',
                                   '181cfd51a238d8db6fe9af8b40275fd9e3695c935f63ec0251c53beb6db8b5c3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97615': ['headers',
                                   '00114f4c1c750879c9b152c1d404922c24b515557f364b4ba3ba612a75a79ec7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97906': ['headers',
                                   '57fc4b069f5bf2e10bcdc3ad013bf3d87da4f982e71b711df0cff1d81e20cea2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97960': ['headers',
                                   'd2faf66576d0001226057ecba1355632eed2519d121bad7394fba003f55ffd08',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97988': ['headers',
                                   '323d78eaf5e3919eb4c1eadf12e95a3c4f9a8db65c4675cfb83336805c099990',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98017': ['headers',
                                   'e3b17f33807e8871adaf38d529a1d7f928f775bda99c9f031238748972f279bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98019': ['headers',
                                   '398afd8cf113c65cf9b544b0924b0108aca022d66e9a62bef691a191762e5002',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98033': ['headers',
                                   'c919abf1b72f6a35c6c54bd82add9424269b73cd81f8963732ae8009efa656c8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98034': ['headers',
                                   '637c3dbe39a9593f0d011ba627268da6fe76d0b06b68b7ea41b136f942102952',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98041': ['headers',
                                   '410548cd3cf0019ea302a14a85be0e24addb0abdea08928f9aeca0f80872975c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98056': ['headers',
                                   '9c0eac7fac39473630cda0604656879495c9029c6875161e83ab1d4899277f1c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98084': ['headers',
                                   '8e712d82981f15071b23404ebece0f0509e1f76e2332fdd0db084a580e464c14',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98085': ['headers',
                                   'debd00edb6164ef0b61f7a9efa5444c46952440fcf46d7714770d2d895ff0f0b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98104': ['headers',
                                   '9b5984a88f5efb5c1b9cdc2d6ee1d3d16efcff1bd117c8eb66a0331e8606bfa9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98107': ['headers',
                                   '104219252dd38fe24a6dc35474e613fbc2e89a82dba4ccab7f84d1ee7ed20378',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98115': ['headers',
                                   '3a00db35e343c2af1766f1c97782cc818fccacf446e8ea824e287badcbaf5dca',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98134': ['headers',
                                   '86db76f011ec55350ed4a4bd7e85535849c60c2876335da5d9d3dd7bdea37a53',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98164': ['headers',
                                   '4f153a8a7cd70eac9e1c7bac286861e34c650982b89da41ea53666d56aba8b61',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98167': ['headers',
                                   '070163b87cfc22476e4ee84120cd765db74e89a5d73d4024c0157c0a53c88ca8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98171': ['headers',
                                   '0d8749a5904bc508e6b158d2bd42b0209645810fa6a37d364d2cc8549109c028',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98222': ['headers',
                                   'dd9044a09bd7afdc3965a6b649235182b8b088e237b19785770c81b25577ad8a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98229': ['headers',
                                   'd762512f5574e4d477a0bb0f6c533325f351c00ee1abef0eeaa8434c4af7afdf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98260': ['headers',
                                   '6ada9c1a0eb3936a9553e1d2c506259c1966e2dd8bc586afce08d2e741a4c1cc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98283': ['headers',
                                   '0f879efd9a595c1b1c3abc008a4d65be3f79a8c081f3dceb60e6fd048838591e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98332': ['headers',
                                   '669864f358fb5196e6995eee2dbc3acec95f7b2d95f7853912f05cb9d93cf90d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98333': ['headers',
                                   '2c4b9f65d684b769f9fd0db424f9205f721fdce97fb7eff9e3c879bdc98b53c2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98341': ['headers',
                                   'f4c03f6dc64da9e78d7043035c7ad2095f315f1ca920d62bed68e01117a979f6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98359': ['headers',
                                   '6e5fd8be030fcfeacf171ca0c0ae4e99dc539cb10748273c973bd6fcff6cd5ee',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98364': ['headers',
                                   '8aa47af4556021412e6b14896b8b201eabfc1b2da9d159e8fb9aa96eb3f3ee1d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98365': ['headers',
                                   'b76373dea4c7fc365cbb798e19274022ec2454dcaa5efa93950274b2344544bc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98420': ['headers',
                                   'c8c29070bd72a2cb96bb8f0f35e954c5a7ddf949acfd062a819f341bba4d3894',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98432': ['headers',
                                   '42232f98e413f7b3ebc5415f65d0e0f86446edb09740ccb7959c859b65a16f83',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98439': ['headers',
                                   'f636f1376dbff235998c1ff48aad86f37ba0b9035b0f229f3d79ad51dbcde2b7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98441': ['headers',
                                   '56044de40147e48e8ba4aec101bb69ea6e8623d125c3368330ac49fca82bfcdc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98442': ['headers',
                                   'bf47c15663a0927fb194fbe0e87f828c76e1706f7186508fc9475505141798ed',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98454': ['headers',
                                   '03955ea32f613616d5176c0c7f964c48a2e3c6c9bcf5798dc5e0c9da63fb70c9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98456': ['headers',
                                   '0af212f3e774053157f7ed6035fa387a11bdfd137ac5ad3f99eda0fadc50b855',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98457': ['headers',
                                   'bdaacb954277741d3a6788efc85ddca33e16027d385c53c2711cd60282f80010',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98459': ['headers',
                                   'a0d92ba88ba744e226150298935a13f4ad7dde5194931528bab958190c905489',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98462': ['headers',
                                   '40864443f5fb4e6a6b2bd8c0b0b7cb4c90611c11f9e2829cbccb828d1d93beaf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98464': ['headers',
                                   '5ca9b44f0d11dae0906ae3bd4013672ed38a69b1788abcd14b1ce7a0250f2576',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98479': ['headers',
                                   '1a17327950554fe4a53833055149b893c6c23f5693f12a4490957ed535c87dd9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98487': ['headers',
                                   'e3cfc9ab229a2dd6cb445f35ce6b51315eef2e613352f6f81b9a4ce03a4f3c1d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98490': ['headers',
                                   '42c035b67cb5ced09717c9f269c22ea41d6dea1457cd2231ee79197c1ecf1a00',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98491': ['headers',
                                   '3a2fc503523e681d6a81bfbc491603c6e92cbc910d9df797b45f272ebf8c4036',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98503': ['headers',
                                   '0bcc799c422ad879cc5b93830930c3a47a12c7bfb65bdd9c2addc1480c97e164',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98515': ['headers',
                                   '54f9002db07b015a37e8dd50829906425f7121dc7190123df9ecb6b4a3da1d9f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98524': ['headers',
                                   '96b181c54a7437abd6fcbb101db27bf66eb29e87ef97a5d521fd2e9962c22fa9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98526': ['headers',
                                   '2ad8a655ba0075d06faaf5aa9fafa8d141f2ac4f34a7d4077744fa3061b244a4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98532': ['headers',
                                   '5b16d676494585824de963636241daded83686f5a78dabd1d31503725eeee094',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98548': ['headers',
                                   '997f0cfe9a739f48b5b6e2173ca5aa7f16bf570c330310689d447fff8dd59124',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98561': ['headers',
                                   'f30bdb0a834f10893b56a1d8eb8a24cc0edc697ed3e525f8135285dfc6f4a841',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98567': ['headers',
                                   '0e4fb5e381af524fedf6da4b301a09686a2792779a4dc6b7591654ebcecd4402',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98568': ['headers',
                                   '32b78a59e5182b67072fa285ba71d2ed9a520b1167060cc4131735d8ab0d5d0d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98570': ['headers',
                                   'f5fab4eb4f305b27a0ffc76bc48ed2726451a9b6671049025caa549b4aa4ad30',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98572': ['headers',
                                   'e4ba7eb7cb0398d85b1ba5fac3094a57f7282a707e24b53e7ea1cdc14e9c9e45',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98573': ['headers',
                                   '5854510058fcca2bd4f2f3835b523792da8ecd05f28fe516d3c13539065abd6c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98580': ['headers',
                                   '2730056ff1171300009f9571fa265891373caa9941e0499e5357a7d38187d79c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98586': ['headers',
                                   'dcdd1282f09bd140b27fb143d79feffd749e80cff5573876c26d0f46b809975f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98588': ['headers',
                                   'a317b87921fe9d05c70924418301ce53d706abdf649fb1fafbf9197edb7e5009',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98589': ['headers',
                                   '147af4af52658412e6263df0df58393d9729d87191dcc614ee3e2afa0adfb12b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98590': ['headers',
                                   'e29216e245f27fd19a2ab5607e6bc408ef1757b34fbf96c6d61e7b55974ccdcf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98591': ['headers',
                                   'd2bd71470958c8d6d2a0dad2fc18f457b086e55e926f682cd902739f2139ef7b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98599': ['headers',
                                   'a0a0de8f7f4ed4bfd4cad349f39349dc0acec2679de3dd6de6daba00846c3370',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98606': ['headers',
                                   '069f8101df211c5e903c8089adfb75007258d6b75a4eabb6c5adaa6d4694d4ba',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98621': ['headers',
                                   '963daf07f19af4ea935ee59102cc769f31ed4f5ff96c2d429a908d49df200ff3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98624': ['headers',
                                   'f50aa9f319cab67b3e35e0e3e046d00f89be14a3f1f3e781250f1f924420c7f9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98625': ['headers',
                                   'b5f478e62a2c20e61460315e9d18b284e2f2013a70f307a4a0032808d40fcd97',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98626': ['headers',
                                   'c24847de6219fc5c1a7b4e888d1af864706ad8ebaafdf028ed15e6e91f9fc38a',
                                   'headers_with_separately_repaired_libbpf'],
                'CVE-2026-98632': ['headers',
                                   'e09eb25a08476efc36af505a3be63b8704fe0c0f5611fd8b5fe4a70eee2aabc2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98634': ['headers',
                                   '32e179d68cf231bc588a75d2dca1ea79ed5957ac73fbd6712d4ed8c65cf97b0f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98642': ['headers',
                                   'ccb37edcbf2e3816482135fe45c55bbb41b47eb417701eba165585aa87a3c3e5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98652': ['headers',
                                   '88a0e6bac660fe35dd84a8bf6357d071e749bab8f9552fc6c61ff00cd852821d',
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
                                   '274d32de29f0b027924ded3367c95c264e0f6bdf66580193258550b482a9edba',
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
    for name, prefix in [('buildkit', 'docker.io/moby/buildkit'), ('trivy', 'ghcr.io/aquasecurity/trivy')]:
        require(re.fullmatch(re.escape(prefix) + r'@sha256:[0-9a-f]{64}', data['tools'][name]['image']),
                'Immutable official tool image required')


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
        finally:
            self.check(force_record=True)


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


def remove_builder(sup, name, owner, image, *, volume_absent_before=False):
    item = probe_builder(sup, name, owner, image)
    volume = state_volume(name)
    associated = False
    if item is not None:
        mounts = [mount for mount in item.get('Mounts', []) if mount.get('Destination') == '/var/lib/buildkit']
        require(len(mounts) == 1 and mounts[0].get('Type') == 'volume' and mounts[0].get('Name') == volume and
                mounts[0].get('RW') is True, 'Owned BuildKit state volume association differs')
        associated = True
        # Remove by immutable container ID, including an unhealthy owned instance.
        subprocess.run(['docker', 'rm', '--force', item['Id']], env=sup.env, check=True, timeout=60)
    metadata = Path(sup.env['DOCKER_CONFIG']) / 'buildx/instances' / name
    if metadata.exists():
        require(metadata.is_file() and not metadata.is_symlink(), 'Unsafe owned builder metadata')
        # This private DOCKER_CONFIG was exclusively created by this invocation.
        # Remove only that instance's metadata. An inactive Buildx instance may
        # retain its volume, so cache removal is independently verified below.
        require(probe_builder(sup, name, owner, image) is None, 'Builder name reappeared during cleanup')
        subprocess.run(['docker', 'buildx', 'rm', '--force', name], env=sup.env, check=True, timeout=60)
    require(probe_builder(sup, name, owner, image) is None, 'Owned builder removal not confirmed')
    if probe_volume(sup, volume) is not None:
        require(associated or volume_absent_before, 'State volume ownership is not established')
        # No force: Docker refuses removal if another container references it.
        subprocess.run(['docker', 'volume', 'rm', volume], env=sup.env, check=True, timeout=60)
    require(probe_volume(sup, volume) is None, 'Owned state volume removal not confirmed')
    result = {key: item['State'].get(key) for key in ('OOMKilled', 'ExitCode', 'Error')} if item else {'absent': True}
    return {**result, 'state_volume': volume, 'state_volume_removed': True}


def build_command(stage, data, identity, builder, context, output, layout=None, base=None):
    command = ['docker', 'buildx', 'build', '--builder', builder, '--platform', 'linux/amd64',
               '--progress', 'plain', '--provenance=mode=max', '--network=default',
               '--label', 'org.opencontainers.image.revision=' + identity['source_commit'],
               '--label', 'org.opencontainers.image.source=https://github.com/' + identity['repository'],
               '--label', 'io.victron.runner.stage=' + stage,
               '--output', 'type=oci,dest=' + str(output), '--tag', 'localhost/runner-' + stage + ':artifact',
               '--file', str(context / data['contexts'][stage]['recipe'])]
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
                components[name] = record('usr/local/bin/runc')
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
            target = context['artifact_name'] + ' (ubuntu 24.04)' if component == 'headers' else 'usr/local/bin/runc'
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
        record['cgroup_proof'] = qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe_cgroup)
        save(output / 'cgroup-verification.json', record['cgroup_proof'])
        def check_build():
            current = inspect_builder(sup, name, owner, image)
            require(current['Id'] == observed['Id'], 'Builder identity changed')
            cgroup.sample(current['State']['Pid'], record['cgroup_proof'], observe=observe_cgroup)
        if stage == 'public':
            run_patch_preflights(sup, data, name, context, record, check_build)
        command = build_command(stage, data, identity, name, context, output / 'image.oci.tar', layout, base)
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
                                                        volume_absent_before=volume_absent_before)
        created = False
        record['builder_removed'] = True
        proof = oci.verify(output / 'image.oci.tar', record['archive_sha256'], record['archive_bytes'],
                           identity['source_commit'], stage, SENTINEL, base)
        save(output / 'oci-verification.json', proof)
        record['oci_verified'] = True
        run_scan(sup, data, work, output, owner, proof, record)
    except BaseException as exc:
        record['failure_type'] = type(exc).__name__
        # Do not include arbitrary command stderr/environment in structured metadata.
        raise
    finally:
        if created:
            try:
                record['builder_cleanup_state'] = remove_builder(sup, name, owner, image,
                                                                volume_absent_before=volume_absent_before)
                record['builder_removed'] = True
            except BaseException as exc:
                record['cleanup_failure_type'] = type(exc).__name__
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
                not build.get('cleanup_failure_type'), 'Incomplete artifact production')
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
