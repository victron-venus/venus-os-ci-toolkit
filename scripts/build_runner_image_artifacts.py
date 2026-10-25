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
                     'public_rationale_document_sha256': '3914c5761ce13c0f2ff7c5f433ffe93e0b2d04fcaecaf4837aeff851afd59a26',
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
                                   '971133040ef36c4cc0de7375341bc4a04e6c4191a67519ff898957a91dff0a10',
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
                                   '4bec2ffcc7456a2b91a1486d11673d848576c2c038e1cccba663ab9b0f0eb985',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-38717': ['headers',
                                   '50cc0b64fecfd99edf8a5ecc50e21410be5c1be24aa9e62b1bec5a63d1b4afb8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-39933': ['headers',
                                   '260559e8fd3e1f3f4b693f9586c3e2377cdad456c8e8b0fa096e85798a3b4cb3',
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
                                   '6207267b5e7b1850989ec0d01ad426f4845b11e1b3a268c5fa4f5107e0014c79',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40139': ['headers',
                                   '8867b629953fd481f9f12f858880545e208a16f4fb7ed1a5a7d8b78d05796f0f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40158': ['headers',
                                   'e675ac141b1283ddc2848fbf8a62600732985e63dae32048cb3d01f716afb440',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40168': ['headers',
                                   'f09eaf5f4b52f8fe869e174d5d7705b487e8f93b4c6c1da84a8c07753401754a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-40170': ['headers',
                                   'f348c1a78ea4a4205a619fcf5c1636c391d9532e7d3f7810557034768af35dbf',
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
                                   '73e34a70519344f6afbba6b136a7960b587ff21c9ce15505c302c2d5a03c20ca',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71073': ['headers',
                                   '65690637f61e0f7285550bf970865a4fd4f8e9a675e88d2f2f2c4659d331676e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71074': ['headers',
                                   '2a7bd8279d2329a9b451658204b92815948c802e08f20498b59ad35a229953fc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71202': ['headers',
                                   '81998cc0b1b8a763143b50c396b99f74c56e73d2aebc7b7229fe535ee66e6e44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2025-71315': ['headers',
                                   '7457d1aa33fc649ff115107e6f0aebbe2d458878c9e6ea554afb6e3ed10fef1b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-100074': ['headers',
                                    '254acce712beb4375d9b46a57fdc53dec5aa37b4080695dbe2e2a1fc68ee20e9',
                                    'kernel_code_absent_from_header_payload'],
                'CVE-2026-23208': ['headers',
                                   'e8d9a97ec34566f201049de3ff280866ee19be3956b50ab6e43d2fe8b79ed014',
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
                                   'c25ad87ec4d64695490687ea977cf399e601f908672ff2baaa63e34d65064ed1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23385': ['headers',
                                   'd37ca94b6505a7d2056b37e42723b1715b1933a71cbd8a47dea7abab178e384b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-23393': ['headers',
                                   '8e0b6292fcdc1e02f4ecbd9f5e828a66987fc9e37516e27efbc18e027ce72985',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31410': ['headers',
                                   '81a2f15391462421e4eb6c0b75f1faca58b63f5a5fd33b34dc5c590b248af4eb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31536': ['headers',
                                   'a82f101f905975e45053c03a5df918978570598de68b8cf07148cf965ba83345',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-31692': ['headers',
                                   '5fe5c4a43495800c26360cb900412c789e2513c823767b18aaae8e697f818a60',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43009': ['headers',
                                   '84f072f3fdd10db03946e05b20be90961d7caad1681006c3d1e4a753f556daf6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43034': ['headers',
                                   'ac1f6795749c930defe36e6a5209a084931985152aa921172c0d7020212921cf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43042': ['headers',
                                   'bb2c1bcba95c43e5218b511d60ac77fa10d2b5c893504906c72e53ed077c390d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43048': ['headers',
                                   'c2d9cf8844d886361b06dfdb1311854307717942974f6125be8c8e1cc1a3bf75',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-43213': ['headers',
                                   'ae0a42a651c8fd72a86f9cb94896f9290c4b10ab797f604a04b92d20955f6141',
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
                                   '0676b597a7f6222e85c9cf0e9c51f4f853b28b7fe1ad4bbd55f0210e7f663231',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46181': ['headers',
                                   'dd2bb0fa3faea7c32e7ba73480b32fbb254004b005c2d3b7c1e99032bf520beb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46330': ['headers',
                                   '14a3f8fbd3988e0bcf27dce5105806e52282a429364c764993c8ef5b44a4adb2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-46600': ['runc',
                                   '7a34ce76597bada6319b5c98df6a70b417c6f8532c1e325c25250eea34a4c5c8',
                                   'runc_affected_packages_not_linked'],
                'CVE-2026-52946': ['headers',
                                   '351299a17887bb06efd87e403b3e1e0323630292385ec35ce9906977e44670ed',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-52991': ['headers',
                                   'e406353d6933411de699ef887e076c1d589160355ae77e15a34a4ca1704f8f12',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53024': ['headers',
                                   '415d0fb69b31921573d58bdd2a45ef27faf2664fbe9dd395b744290e8c025926',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53178': ['headers',
                                   '01e34987026340806f2f442c55d9da9197f30df30392a72a172553596e81126d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53250': ['headers',
                                   '503f8f7fa9bce2e4e77c49ed548abc23d606d34992d8f2e99957826830f5ad22',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53281': ['headers',
                                   'f007890e920642dc95adaac09c340478bf1529a239a1d83f6d3870e5468b7261',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53377': ['headers',
                                   '9dacf1e8df1cffeafed3c1c836366908139530ecca64b56ef9246cc3ee01285b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-53401': ['headers',
                                   '07e555ed131516d919418b6ba9b9731a1957bf5262261b88fdbe7836bcdeae89',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63819': ['headers',
                                   '10b50d2255ec1812e9e8bc6a0fe346409be749a3a6533607a8d491673d36ae17',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63825': ['headers',
                                   '21131c47d7c0a88b9f5503a3eceb00acd833d2aca37a3d77a8ba4be3a15c2526',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-63879': ['headers',
                                   '127b1890ff8e8658573b57c5e1e6c1f3d654ec2b6331101a06a613f54c997775',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64036': ['headers',
                                   'b2fd81bc761d4624cb15265732c4086488b10c225fda2c735e4d4dbf1f04f2eb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64052': ['headers',
                                   '2b7edee0cb7e0d63776e38c081bbad0aba4b204f2820b7604d23fe15def23b76',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64077': ['headers',
                                   'b5b3b150b6d17fdc54dd778998a020415c697413b92e97bf46429b462d2ed2a5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64078': ['headers',
                                   '26353320cf3af140098018bf9adedae362b65c34d0de5460c9ee6d5978cb595e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-64523': ['headers',
                                   '7cc8453ddec701f7696e61445709af6ec5b88500e7fa9c6ac0b52f22c50b1ec4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68081': ['headers',
                                   '84ed0a9ef307de05dc9a47a6a3a5cf11e3cd6656c0b859fdb62382541344e87e',
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
                                   'dd6f03ee55fe538f79ab59992e421ecea90c39eea0c9e150fdcbebd375f389e8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68257': ['headers',
                                   '0e88bc610553f21ae5376a8559bb00f98cb3712e857a17526f2859018d11bdaa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68323': ['headers',
                                   '9c4846dc2110416ae5c66f65a29a1c94108e1b6fb1ad72821d9c50159951c3d6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68401': ['headers',
                                   '3bce7ce00e08b8476b895c4a86a0bda0a3bd5b63e26cfd0f9a35904177f84ec3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68404': ['headers',
                                   '6a082ef4a5c01a5c8f06b5c4739eace16ef8c4ea39a16716a6468dc49051799a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68409': ['headers',
                                   'd0e302660b80d216733c8355c5451b29bfffe475896c57239f21a795bf0aed1e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68426': ['headers',
                                   '65cc407c9e25b13ef1814d547120d4fb31774644a674537a8f806d7dba0adb10',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68447': ['headers',
                                   '6d2298cd1573a77640400ed673f89a1f61f5148dc54205b723cb6f96120c09fc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68451': ['headers',
                                   '51d909838ca86635b158f44e4c5fc22ffe0d37a743dcf9d58796cecbb28c430a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68470': ['headers',
                                   '7785171c1cb9b8d86448897d6073f39038a440c33bb9d569d943941bae4d1053',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-68471': ['headers',
                                   'b3660c1b33eb341124eb3bb2b129d408db37167cd9fc9842a84e0a46b7c08684',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72111': ['headers',
                                   'b768b2839f4c658f21a78e369165603dc68fd9e07e118deeeb0995cca6409601',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72162': ['headers',
                                   '98ff23f2217f5a42cad5581a9df6ccc6519abc9ad518b37d71d520968952dc17',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72198': ['headers',
                                   'e27b6f595a5f62a7c2ee2bbed1b4589e4cde8b25f482112a837731febbfb9851',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72288': ['headers',
                                   '1dd39393930214577fc8fdf6eacc221c2ea9a89c41e6e7d04ee9fc804f6230d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72375': ['headers',
                                   '019489da03e2a57ae120a40edfd34e5616b36029d5df1e47bb41f2badbea0f0d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72380': ['headers',
                                   '64e3707b583fcd2c0bf1c9414ef6145022588b7b5461c6a555d3a17d7af69cf4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72383': ['headers',
                                   '8f55b4ae63f63170e1622b9509cfb04383d5b741880afa6d4adaf551eeb25098',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72413': ['headers',
                                   'fbcc7d2ea241719fb6effbeeebd309d46dea75ba95cac95b08fd7e0e784b0762',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72423': ['headers',
                                   '45003a9ef9d4c7a28e156f388a8f7a321681ff6fa677543b2f55539ca90b041c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72472': ['headers',
                                   '3b66931bbc478122af38b1ddfd7b3e0906ec44a8059ae93ddc809a65026b15e7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72493': ['headers',
                                   '0a6123f855191d5efe8168a6b1fd4fc4e0ace0992ba30c52feff5cae12980456',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-72494': ['headers',
                                   'cd1d6156adfbdc2ac3516fe933f284f426dc9c5a729f566e4598e8e005ac2713',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74257': ['headers',
                                   '2fd688d501641aa5eef047971676a52e33582a2208cc2d4a10b35399312640e1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74268': ['headers',
                                   '014d3163a2f68b8fc90847b78cccffe7209b95bd9e4e6f9d0c2bcfc962ca9da8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74269': ['headers',
                                   '97a138a542984b9737bf71a42acd82ecfdf66f39b87ddd9c3e4e697cb018b43a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74314': ['headers',
                                   '00be392e686604b726613b8155e10787a3092f52970c9baf228b5fa6207e2c2a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74317': ['headers',
                                   'bc6aff58affb46c17bc4d2f9b2e1a95cef87048cc3f3d1e1938445bee2b758a9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74334': ['headers',
                                   'c52de62df71af63b6841b875f7f8b7c54369ee94554b3837bdeb2fc1e9de71bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74344': ['headers',
                                   '53b3b0a28b0c3a9d52053bbaadffb2a49a2da60e161949be6c8e0351c58acd8b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74347': ['headers',
                                   '9e2fefd8415574d91765a3fdc85c2f03314e31e64b2d5a1c68af9498b6e329b2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74350': ['headers',
                                   'e9d2f1d0254763e935948e600cadb5472adb708939e3e17fafc5dcc9f837605e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74385': ['headers',
                                   '430a10d694847a68ada86a6b080bbafdb02a22e1a84a80db7f00b41732c38afc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74388': ['headers',
                                   'cf22335e4fd83cfbe3b48b29d5bd8d3705fc8368c26c5117b124104a9ba2cf39',
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
                                   '7842b32b207c1ec129c448b18f7bec6fa9c01a1f846099a1a8ce16c64f056633',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74506': ['headers',
                                   '50d9ce5ace60f6b12c4ac31e21b9f459acdd74d7cc161dd790a367aedb0641f6',
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
                                   '0695506d0bdf4a795d95cdba43ed92f22df72a2f966717704027f8ae2b7313a7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74534': ['headers',
                                   '2e69e34828a76d4c94afff59d897a3ef9fe60c9e0bffcc042c024618c80441d8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74535': ['headers',
                                   'db3222ac0053619dd7eb478c423525c9a6ad2041b8a47f2dfa7f9041f0b14ee1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74537': ['headers',
                                   '7dea4bcaf762f17c57d5f87da817b928fbc7920609b14d0f55ec9db455611474',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74565': ['headers',
                                   '8d85ea5d34a414b2b8bf8a50b0a8c5640a284667ccd7735f01df586d6a5fed64',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74578': ['headers',
                                   '1330aab45f09155addede26081fed73c887a4878a90f2531b51b032394aa861e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74677': ['headers',
                                   '0281b0ddaa9437fd4369ca20e102d945a901b031eab678b901bff2001b1082f1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74684': ['headers',
                                   '60f8b6e7c69bdb3eab45bdc34b3fc87ac617744eed503ada6f7a1fbe3f3cbe0d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74700': ['headers',
                                   '74a2623b7e1c0decf4d8f16d7954c8602ab90b7d267b171b1c38ec64969234d9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74714': ['headers',
                                   'cfc97aab8996e80d08c86d1742f53da57215efb8a6d22a3c87c30020020b0c82',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74715': ['headers',
                                   '4c6d42584a49c3e0cdd3c86acf2b044dcb0c84d203b3bcf061471915cfa40c4e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74723': ['headers',
                                   '30779faa617f5c72e4b2740610bb24817722984464bb85a04eb5acb010bbd303',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74724': ['headers',
                                   '98364d565eee79cc85f33d37a1d0062392ff4b2ea9333ce064742b6456b5ea35',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74744': ['headers',
                                   '257c8b16a259024cbcea2c952cfb36dea436e728561273e7a72dc3ba31049d54',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-74752': ['headers',
                                   'bf917899254572f340b2a8996c6d18c2ce8bbeb0ca2f281d0b396211afc1631e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-78669': ['runc',
                                   '3859e5e01b6ef2bc1713e94a149179feb6e62ebbb1b1b873e239641688a31fff',
                                   'runc_affected_packages_not_linked'],
                'CVE-2026-80551': ['headers',
                                   '191bb34b8124e009b587f32bcdbb7a6012838218b3c2872b2c355fefe88ad754',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80554': ['headers',
                                   '159d37968fb1987003a524d7e1b0d3609dc8db5c316a0169618da92dbf3338bd',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80631': ['headers',
                                   '146652667daf4e8d0cd376419e776681c773003e4b2ec4ef522c6dd3255dfc07',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80634': ['headers',
                                   '64f5c41d3bc2ec6c387d5ce141338129bf3e4ee70ff55d11479f7a7b08d8cf7a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80668': ['headers',
                                   'ed3fca0e525f0a31d382762fe0aab1658dce3bd7dd16b55acb7f5b820eca3586',
                                   'sanitized_uapi_header'],
                'CVE-2026-80671': ['headers',
                                   '7aa9234b5f0e7adf1a8d5cdb3ec57ece45e5921f95ef83de277c5730451a3c34',
                                   'perf_payload_absent'],
                'CVE-2026-80681': ['headers',
                                   '57588f1fb8b74a0da6bf166aa41691b3164d908b5147fc24cbadc86cb77ad034',
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
                                   '17fac94da5a733600ea021b067e0cb22b25bd06b5700f78a465bb043a94500be',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80702': ['headers',
                                   '68aeed7a6b5221bbe37af97eaec4fdce377e0f386329ce9460b20c128f366b07',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80710': ['headers',
                                   '3d52639021f5e3c2bdb465fd364ddaeeefdc4d2473fb3b397e2d9c92fdda0ae8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80714': ['headers',
                                   '30c9b835ff4aa37e72566eaa31046b5acfa7a7843590d53f7b7890ae537ddafd',
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
                                   '7786c35536f85e7708f27007cf0d446d614992e57cf223423cd37acd2428fd11',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80841': ['headers',
                                   '316275c53f34a7cf50541530b864feb11ec59f6606d62068d12065a3a3a4f657',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80852': ['headers',
                                   'b18490231821bce47ea588fff0257d3ccd0e08ad62e52bf156507e5b23a75269',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80864': ['headers',
                                   '4d835024598e7d9e55566a307132f266c4b85f6bf30bcc354c50d5186199d3cf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80891': ['headers',
                                   '327d03ec4adb2fd825f2a6f4b1da23b9313358680c5c89011f2207f68e818d92',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80905': ['headers',
                                   '0873ce13adf3386c2e553337185125f7909f3ebae6af5237bba70e52203f23cb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80937': ['headers',
                                   '81fd6bc66dff38f6151be8f8a8b193a1370d2d4ffb3de949071b39e20369b78e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80980': ['headers',
                                   '31570007f995aa142ae91b7776bcd8bd320999d9a2012fad978cabe8ad6a5bc3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80982': ['headers',
                                   '54ab06b0bca7fc71347a3985dcaa39b30482dc05a8523e25fad749cfe2f16e12',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-80991': ['headers',
                                   'd1f64c3e5da4d62b81ac8b850b1c370d734c2bde2b66198bb9573044a3ed28d3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89448': ['headers',
                                   '022f3bfa03bf521fb31c5b6486e76dd513c5ac6863d71ffd57701b94906c7ca6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89481': ['headers',
                                   'c0659bbc453b810453d9e44d90b419a8e8c4517b57bce3aaa372dc106520a745',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89492': ['headers',
                                   'aada8da1fe0a7f2af3f122bd42350fd37038c14505cedff210f0404c2a347083',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89520': ['headers',
                                   'abc27475c8cc67fd7ce245c8ac29a66233ae3017e6339736b84eda7cc62c2610',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89526': ['headers',
                                   '70dc182953bc9e7f17207be41032e622d8e5109a19a7a3d49a261903a64353b5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89528': ['headers',
                                   '137dbfa08d0bcc68b928fc0a4ef88f66d233987dbef7428529e2fb8d87b4b9d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89530': ['headers',
                                   '6a45626fbd7b80cb61fb55ca53d544d38427d6a676d06df52d06af33692ded43',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89531': ['headers',
                                   'eb2fab3f03a29583aea0e8036b0a5d0ee47070baecf51a2019b5f3c01421fe73',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89537': ['headers',
                                   '15e6a001db57d307648ecf3290c6e7540ede69b5c3ca4c7e2d61c66ca5210f7a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89544': ['headers',
                                   '7d06498676bdb040175f47c4ef7631fc24d0fe5116ec771eae2c4940fcbe9890',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89550': ['headers',
                                   '2e2f4ad038d55172bbfe5158f95ac0c302760d85894f1fcb1764f1c85b55730f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89564': ['headers',
                                   '69cb04f13a993ba14c181280c3a6b20eda27159e5495030c1031a6f7114d7727',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89565': ['headers',
                                   '56494435f3af2fb39eab117a5977988ea681dfe21e27d342de68342f321d70db',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89580': ['headers',
                                   '8cb6643f080868908c0a5cd8d67408661b2dc613d094de44576c2d91295486b9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89622': ['headers',
                                   '20db1ae4d682fae9f7ca15f3a5f657dada3bbbba7470ec66087c7dfb10ca24d3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89631': ['headers',
                                   '6565a826b787124cd42f3d6606de2a094766d096e4e757395d13dba8820af753',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89633': ['headers',
                                   '037cbecf123b5cac6046b328e20bcbcbe78bc59e203fc1402ec1bc6fc3c1cdd4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89637': ['headers',
                                   'aa1dbcede2cd8288cfa43f9218f797f0541a57e7c19fd3d1b64b68dae93b03df',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89638': ['headers',
                                   '71853425a7123f54fcd23f268a42e9d6068c3b4e6955bc1399ecfa434d2f83e4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89652': ['headers',
                                   '553375c5d06c948a7ffeecd09ffaf19167d3b6b70e4200af9db68110550450b7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89659': ['headers',
                                   '3d8ffc3c484ab46c13da9c03a4b08e228a486e5295ab553ec05a79d10ae36eb5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89675': ['headers',
                                   'd6c1381a95128e880578d13077773ce97f7cec8da053875684d266e3d0b31a08',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89676': ['headers',
                                   'e4c0bd6540c66725845e41c4ececdd9100ab2e1683ae2723333926d145a3523f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89680': ['headers',
                                   '55f2ad5093d0f54a0b34c242ae378f98f62173a422979153ad5883c795b661f8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89685': ['headers',
                                   '766f71cb560c4cead127c87f15b3e74509e87e5962d3cc223f061ac86804d99d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89707': ['headers',
                                   '730f60c87775fd849d9a8a57e7ab9e84318d739c921ac79c05434628655ea75b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89708': ['headers',
                                   'd1710269ff58ac18444c9c98256421a3edad5efc5ca037ca99b34c6350655f81',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89788': ['headers',
                                   'b0d4c7ffce7b791060a1eb9eac755501940c45f0275603c341646c1e993b7f03',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89803': ['headers',
                                   '52f66596f6068a3507ae6c109b8ab0336a6d39ef0a46d335b4b7254d8765f677',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89810': ['headers',
                                   '40a72326da491355295e25431cfb423b79ca59ed71be811b1e0cafd6636b6f3d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89837': ['headers',
                                   '02bce379c42b01d9dc0d7d6e84d116bf3ba20cd4f0930d4cc8940102c7b14945',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89913': ['headers',
                                   'c6cf390f1e2adabb5c8ca7302c6d1b40675686d3cddf6655410fb97db70d4dd9',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-89922': ['headers',
                                   '8e81c1dfbb9f74bb18faa4a02f927a0f21d0762635722a5f3e19748e280716b7',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90003': ['headers',
                                   'f72d3ef824a2dc93f13abfbdc63d17390be8ae84cea1ceffdc6f9baa34d4e5d4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90037': ['headers',
                                   'bccb384f6e13b2d6d28c04d9051128a0408e1927ef75269fa2fc907eddc943d3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90048': ['headers',
                                   'cca44b1bee3f4eb93deb5794df657235ee11bf02fcfe0fd861e37665b25efc48',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90057': ['headers',
                                   '71180e05c4666e45024f5a086ff1217c651f6e5a7215d334fc87d25d0bb1d8e8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90092': ['headers',
                                   'ee637f16489cbcb3f2ea253e85c4b53193d13e6b1963f2cbebabab8ac248fbf6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90093': ['headers',
                                   '0322780034f978eca0c938b60a2cdd28cca11d0fb63cbdf159882b69d5e191fa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90104': ['headers',
                                   '9e080cb0f653beca9aea6d3df0fd4a2babddd937661296e42369f6c227790156',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90108': ['headers',
                                   '499f49d7a8eada1884fc75b0ac612381cb75e5debfda7be529d93d210a23f991',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90111': ['headers',
                                   '682e79d37c125790c210aeaae4fd392661bd2f519b3d79a10d8829a07226923d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90148': ['headers',
                                   '517d7a6b5428c13906b0cd90c830fbbe5c437d2715f53f4484f2303f5b5dd37d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90149': ['headers',
                                   'bb9eff36d4cb499888b829594c24b129f457172d9de941b44738613b303171d5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90150': ['headers',
                                   'e4a1ef4adabdcb30684b376f932c725a547c6d31669706bfb4fe36b2d5ad8f37',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90152': ['headers',
                                   '67c79ac3818a6f23a4dcbea9a8c93c94ddfa97db95afa1ddf58bd970a78149c8',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90155': ['headers',
                                   '9bdcb6a31a75b0b1e15f2295c05783b2b070800a51e4544b25590fbb5e68f1f5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90161': ['headers',
                                   '63c931b29968942dd4a6bb15e5c023bb69d9405be9137ebe7d92bec636442210',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90162': ['headers',
                                   '0338eee19e8b4c5700803ccccc032211facaa5316805197285e86f6bf3ee532e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90167': ['headers',
                                   '39cef8aaf4b88f36fb745f933c673476a6d2daf72803668c38ec507d4b69468a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90169': ['headers',
                                   '36eb1f75f5b130a6dbbda7267b438371423e5ed83bff1dc12af3398e96d21d39',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90199': ['headers',
                                   '43450d8ff7a6ab786df80e068efaa97a9ea6386def418ef4806bbb42206de66a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90200': ['headers',
                                   'd1d7fece1e7dfabef47e2412d91fd319f6d25aedd76781e66956a1ddb8aabdd2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90204': ['headers',
                                   '992d8af0af759557a7096a7bd42798e2daa959158d38cf9a7b29c71c578c0e3c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90205': ['headers',
                                   '31ce7c311d280f7777a7216b189150d0373acc61e758b7f3a17fdb2eb008e047',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90217': ['headers',
                                   '9c1300e148c8a576e1600c616aacd917acdd5e571d8fda7a5a4514e958bf309d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90227': ['headers',
                                   '6d42427fd7207cdade2f2fe4878f3a6957a1560aebdaa456b35aa77edbb4fdae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90237': ['headers',
                                   '13b9dfd12dea3ed41cd32b1c84b753bf59e13cfef9be49abcfc796d95fc6dfe6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90256': ['headers',
                                   '791effef378220ce3d8e2df24a35670e05d49cdf1ea57e39fb1264cda2c353c5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90313': ['headers',
                                   'cbe98e7579a7c99ba91b14eee7829a5756e7996a33ee5748b9a2523fdb72ecbc',
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
                                   '7d7c90f4e4956bfdcaacdf7c5473fb3e0d2fbac08700e471e9ae50391ab71bf0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90375': ['headers',
                                   '110aa73992d6d15ccd21c5a3698fd903c449bea1ee8e2d5756c7bc2e4f532c3e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90380': ['headers',
                                   'c859ab0daf7142623de6e7e3774641ffcc92b5615f80505381803a2c0e17a229',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90392': ['headers',
                                   '862a60848773e272bcb7c9bfcbde984bb00602406f8e6ecaea9c5b678c6996a4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90398': ['headers',
                                   'baed9ccde43ddbe5cccc1a12334fe17bbf3bca6926c99fcd2627a77becb75609',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-90417': ['headers',
                                   '1202452a09a92f38a87c9950fb1eccbe78dddbcc2d1ef3fe39093e670777982f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92499': ['headers',
                                   '4c2801a7240fb8b03f6dd734ad3521bdcd709ee27c816b3ab40c5ce6d68da4be',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92507': ['headers',
                                   'fc7be3c4f636c3c00d8a53c1da84fdcd68f25cc0e5ef738c508a3468b7d29317',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92509': ['headers',
                                   'df4160121c7d244988117a7bab54effe9a65ca243972319e7802143c5f381a44',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92510': ['headers',
                                   'b7416b8d71872d8ba45a070688b7806cf22fc0bd589d1904283e6fbe24bdd8b6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92511': ['headers',
                                   'fffcceafd05fd5f0d4f6bcf5ac6fa3997961f452cc1630839f1e500a83280e12',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-92512': ['headers',
                                   '6cf9a796588ad1d334c2f2bd23fecd756bb7f1471545fdf6d627ae0956076909',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93105': ['headers',
                                   'fc4d77a90141428c3c4fa4e846ef4303763052c62d9cbb802ede5e1c0357f7f6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93125': ['headers',
                                   '2398c6d9ad95c5cd9dafdf0ea4cca806f6d7b291b5020a3e32f02a5f3c4f30a3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93144': ['headers',
                                   '6c1647e5e1719f87ea89807289d361cc121e94fef3a52b727f1a4b105e4d35e3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93148': ['headers',
                                   'b2d79c1dd6c13ee4b375dfa9928dfbcd9f7d4ad2c45ccd72f1781053c34a6e61',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93154': ['headers',
                                   '49c933a93d3311b90fd37c15e710c68a0d5e6acb522203d509b713871d04766e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93175': ['headers',
                                   'b64cbda8e726d5516ae6b1e2ac3805278c931bdec638e8de8d741f115ec8db91',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93176': ['headers',
                                   '5b5fdb1c480d40aea732bcb0a162aa4cf39e09c11a4fdc1f7d83d0a9bcfa3e7e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93192': ['headers',
                                   '96cb24904edbe6b62f808a1d44713a0e0b51eda9c89cb6aa3b77329787662148',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93196': ['headers',
                                   '6380923930a20301bdc5479faa9f00297665e75238d24ab193c58de1ffb1229c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93199': ['headers',
                                   'fc019cc088006990c9c3bebf44b9aadf71b825c6308c19f9cbc5324f38459af0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93210': ['headers',
                                   'edafda6ee8db4ee5d3b016a8e7321c4f46a205a0ee5563b11d94d1b0b4472a83',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93221': ['headers',
                                   'c65a63006834b6b5485e05448c36771086b2fef2dc59415cf5b3563c08a51117',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93282': ['headers',
                                   '49b3c33c5cb430409f1bd26004212622ea10767046092e4c632af1b2cafdd10e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93787': ['headers',
                                   '42bcb8ccd163f6c600e80241ab301b94b87d3dc41d170f737910bf165d397c2c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93790': ['headers',
                                   '7867009aec31ac444f24092f579d8acce3c7b6af5f4b2578ee03aad6ff250ab2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93792': ['headers',
                                   '0de8b6a15d8cce43fa6a9d0df2efbc397ffa3cd1584d5b2c2329990d1b7913d1',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93796': ['headers',
                                   '6437403eb6bbfd65279ed9efee532d274b8b413b120d36f2a09b80676f485e9e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93806': ['headers',
                                   'c7e8353c1fb195c511a5cb2b3ed41e6ff986bc689851f3ac60205c8ccdefb6ba',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93817': ['headers',
                                   '4f7f3f8063722d473fcbb1e857963bdc4b42d28fabf9ed8464bd3de78e67979c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93824': ['headers',
                                   '0c6db7f9d7dc7a5f27617cc98c8b837bb5673574b2b75dce77b6379da7f66586',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-93828': ['headers',
                                   'e6d621ba572cb0bb486cab9ddb65a850841602da980f444b15277931833d46ec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97415': ['headers',
                                   '7bf44b4b54f430c0bd5d379086e2eb45fcbb6796c5f68fbcfc2aafea2da474c6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97429': ['headers',
                                   'a0648eabf535306e1a2520027130d7e36f15840b16fff0257771e3691ddba6bf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97442': ['headers',
                                   '1e5271cb04c3960dc4172e55e856bbcf2d413841190901602ff3e8fbe8d0db14',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97493': ['headers',
                                   'ac61354124e039bc14340aa541ea429f03a206bb1e413d826cc59db18fc40856',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97497': ['headers',
                                   'be1d2b558b87aab0d14be1f08734cbf9055a7862c0ea167f51af2bf967152ce4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97513': ['headers',
                                   '30af879db58e681da4f7c97140c0a481be9f547c62caf4a80ca7463346dcdeb0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97530': ['headers',
                                   'b7cee8d395dc8e0f5cd802a42bd177da7e847bc5cda98be6fe2b92cefe3922ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97555': ['headers',
                                   '9edef6f9bbac7574f1b40ca385b64c7cde33787338dccd3e00d12db93b09799e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97557': ['headers',
                                   '512f51ca35f991246cc7bdb381f998521adc8873d2afe64eebe18e6f2d72a15e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97562': ['headers',
                                   '05d2281b17c415f8e846faadd01ee32e86196ac77e8952eeedde77c2e97479f3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97564': ['headers',
                                   '545ef1e7862a78668a288bc92af53889ead3ef273bb17bd255aa300b3c87c456',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97565': ['headers',
                                   '9e2629a2915c6a3fec40db4a6fcf64c84fd2fec2153f2ec121c398da22216e1d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97581': ['headers',
                                   '66330be6d09ea9226725ef944418be431f938f0099f68e3a67eb2625e1bced8d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97603': ['headers',
                                   '181cfd51a238d8db6fe9af8b40275fd9e3695c935f63ec0251c53beb6db8b5c3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-97615': ['headers',
                                   '219b2c63231c51a889710e112d1f8498a0e2af816898921f43adad25ddea55aa',
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
                                   'ada5cfc8a99a0b14e6b968683ae4747abc6753698c8a8b844abe6197e950447b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98019': ['headers',
                                   '398afd8cf113c65cf9b544b0924b0108aca022d66e9a62bef691a191762e5002',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98033': ['headers',
                                   'aab9d2039accd1d059961f69a31d49bb9120060bc5d13d150fe8227f85861c34',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98034': ['headers',
                                   '0dab6b9e0b12d8235f5ebb47a799ceeb1f9d7528d0f64e67130b5074800a680c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98041': ['headers',
                                   '74ac3dbfb5f9b03312b287d6964a5b86b1f654389efc6260d1f02583d58aaeb4',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98056': ['headers',
                                   '687cd44923a91c7bd2f41df2af6455e87b50abdd5cfc76b17616490fc45d7eb0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98084': ['headers',
                                   '25207bf2456c80e0d81f038a1fbbcc32f3daec1d9b1a308accc8681bb9be3f01',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98085': ['headers',
                                   'debd00edb6164ef0b61f7a9efa5444c46952440fcf46d7714770d2d895ff0f0b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98104': ['headers',
                                   '5e287ade25c9a3d50f0b681e6c9ddf78e334f1b187162c64a60d256aa74a522b',
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
                                   '421a5103a9497446947832e95699c0278db76ac35882f44e5ba2c187a9c5c9eb',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98222': ['headers',
                                   '924d5868e449123f5aa452f9e37848b82fc6a46f5014109a0286055d8d39ecbe',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98229': ['headers',
                                   'd762512f5574e4d477a0bb0f6c533325f351c00ee1abef0eeaa8434c4af7afdf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98260': ['headers',
                                   'da41219080c7c717d61d97df73392c3f2454e6732e4d757ecce13fda58b3b41d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98283': ['headers',
                                   '72424038e042ff43bf2d2a94f697b5ac768ac5c788ca08d8753585d4a6158cb8',
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
                                   'aeece115a1c8bb422c102ed61a46ebddf2b682a428a7a35cb7ddbab42a425b2d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98364': ['headers',
                                   '1b5855782287218ba0c9a6c2ddd0e375c23de5a323f48c7a35128a9f72cb81c0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98365': ['headers',
                                   '8485f54fe053f92f6e45a206dec27f04844c6df6ec6efba7c8e7bf2ec8a6f91b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98420': ['headers',
                                   'c8c29070bd72a2cb96bb8f0f35e954c5a7ddf949acfd062a819f341bba4d3894',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98432': ['headers',
                                   'de0163c444914be1fa450f7d138866c2e42415cbd329b4acf6576ac11468d20e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98439': ['headers',
                                   'f8c2c6e0506baddad85ab74ed879dd3e679b3eaa08a415dafafef5b001c9f081',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98441': ['headers',
                                   '56044de40147e48e8ba4aec101bb69ea6e8623d125c3368330ac49fca82bfcdc',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98442': ['headers',
                                   '8132d17d7afcec0cbe989b732eec954afcdd98fc05c9b69705ce2f1cdf5b3963',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98454': ['headers',
                                   'a34ea4949bfeb90e884bc3cbe7fb80a501895a4a051ed17b600c26b2a5e4e1e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98456': ['headers',
                                   'be264208d36e7386cbd970c35403dff33fab9ac708693a3cdb5cab0047b01097',
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
                                   'ea88a7926737fb353a2ffc79e54aa876edbf123aae7f1cc9c409e567d978bbf0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98479': ['headers',
                                   '4d7a5331b75583d05ed4337ec299bdf3e02641e5e8c84e8f530e155ec4b51d90',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98487': ['headers',
                                   '3bce8bad5517f72d7d5c4913b7e7c9725388d60166a9e340cf43aa7c70ec2a99',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98490': ['headers',
                                   'a079b51f02ceef13c66165c75b4eb7a8f566d0f7e28cacb7b4eeaedd13814061',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98491': ['headers',
                                   '367ec8351f4696235be4661c133ba7a2746f25d378d6a1dea3fefcaaa6af87c6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98503': ['headers',
                                   '3c91e98ebbfd5c519d76fd4f95ec972fb9ac0dadffa9ead9eaeb4eed1425a1a8',
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
                                   'a6339c6b503c3e8f8ff88d90ba691c72fa9a746466c40d112b1842b1ae09a991',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98548': ['headers',
                                   'd58e12d1ba2d9d47584fb59de65387c6cad38eff87b89bf570e182af16950a84',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98561': ['headers',
                                   'f30bdb0a834f10893b56a1d8eb8a24cc0edc697ed3e525f8135285dfc6f4a841',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98567': ['headers',
                                   'd37dbe7e4bcb49244a7c6ba16bcd922fbb960368c1fe02c425e11340afe19080',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98568': ['headers',
                                   'b30881b34978132f5324c82ec8af5210d6dcf07bdcdf629d6c7652f1efb46aa3',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98570': ['headers',
                                   'f5fab4eb4f305b27a0ffc76bc48ed2726451a9b6671049025caa549b4aa4ad30',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98572': ['headers',
                                   'e4ba7eb7cb0398d85b1ba5fac3094a57f7282a707e24b53e7ea1cdc14e9c9e45',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98573': ['headers',
                                   '2903377e110d8d4aaad880ea7fc1afc6291edbe58639ea640bc8569ca86942b6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98580': ['headers',
                                   '2730056ff1171300009f9571fa265891373caa9941e0499e5357a7d38187d79c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98586': ['headers',
                                   'dc6445f7a2c72c73fc1c483d24ceb5354c22f5c48ecead7fd2c0086be8eef4df',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98588': ['headers',
                                   '18180d6bfd2da221e15a292b064e326dfacbc233c29bed72f9056e3c5eed2967',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98589': ['headers',
                                   '147af4af52658412e6263df0df58393d9729d87191dcc614ee3e2afa0adfb12b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98590': ['headers',
                                   'c889d3de9457701d1c9e01faf2d5f600391f75902cacc9baf278293b5ed16eb0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98591': ['headers',
                                   '20b3b9726333214cd9b4866a491dc096928e447b8c693dfcfcd068fc585a785b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98599': ['headers',
                                   'a0a0de8f7f4ed4bfd4cad349f39349dc0acec2679de3dd6de6daba00846c3370',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98606': ['headers',
                                   '454f9cd65ad295d04a4764a12664ff100d4b47cb3781a29474923e14a2e014f6',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98621': ['headers',
                                   '1dd0f2c2b3dca39a6a5e7fb71c5d705cd7565ee5f7d12a461845d207bb5547ec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98624': ['headers',
                                   '83d06fc0b3ceb42a2b288673d7ed888dbb351b5b68d33781593a896f7c447c9d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98625': ['headers',
                                   '1b199e7f603df97c99f4a22ce5367a29073e25b5696b3224b08a8505fbafac0c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98626': ['headers',
                                   '1ec1cf46d2928535aafcdef989b8fd75cf61fd5ba6b6fa8d4a4f20edb1c4ae5e',
                                   'headers_with_separately_repaired_libbpf'],
                'CVE-2026-98632': ['headers',
                                   'e09eb25a08476efc36af505a3be63b8704fe0c0f5611fd8b5fe4a70eee2aabc2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98634': ['headers',
                                   'afae57e89fc9d617d29c4a0d11c2de97288106708b84689182b89d79af3d6a6f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98642': ['headers',
                                   '5759927284da99b5980d6de61fa4cb63f67611a554f287fcd8192a27dbc9417a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98652': ['headers',
                                   '88a0e6bac660fe35dd84a8bf6357d071e749bab8f9552fc6c61ff00cd852821d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98699': ['headers',
                                   'c299a45abda9160f938bd0f8eeff03aaeef740fe39e4a2aa2c097d82d011a59b',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98701': ['headers',
                                   '50f0a49f5d3e8e5dab2af81c9a9ec974ab933077e45c0d2ad74e874e1db956aa',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98704': ['headers',
                                   '5944eb9836ad4377da28fa12a3b0dcd1c2fe251af1313c03c248f83c537f6081',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98706': ['headers',
                                   '1006d519861623008a37f8355dc30545517bd25b62fbff7078880ce0ee33d3ae',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98707': ['headers',
                                   'c49fe581520958068516675950188136d6437cbc4147ccd75efc74446cd2945a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98708': ['headers',
                                   '6e1df59a230677d47caad75bdbce3a4b7c3275ab69643e30776b230c96ba52e2',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98709': ['headers',
                                   '3787fde99f77d0ccd5f6c31cb6af783428cbe678312ce10b8a8179a39e176ddf',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98725': ['headers',
                                   '6be962d00ee2a36a4376b00e20112e49a1030e62b7562a8452b24fd07b4760ac',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98733': ['headers',
                                   '0295a25abed416468d20873f2dcf12f8327e73f44865b90a0b2d8bd1ee96f524',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98734': ['headers',
                                   '22336a5d82a8623ca3da84ab50419c2e5ef2533670321391cb6e3529d6b0a544',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98735': ['headers',
                                   '6254298f836c0c35d85155536f8c24b04f2e1659b657a7db1896c5c6a2c523ec',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98737': ['headers',
                                   '33aebdad55f04994c94ae0446b5e55873e70db219ca5c9c26498e6d76e88952d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98738': ['headers',
                                   '3620b7ccd73748f72ac5c1fa2333cb438811be1eec777e538bec6870ae650ae0',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98739': ['headers',
                                   '11b3a663a39901e0d195d02ff8c65a0c5a8db50d5f02f615452992937020836e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98741': ['headers',
                                   '4aded048a946d40a43e106f75af82406cdcd811dc8a0b13ff2f4f64ccb355e0a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98742': ['headers',
                                   'a994a8e8944efa732d125b2bcbacf4186b16bb569a1129338efcdf26c4ab91e5',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98752': ['headers',
                                   '055f890aa8526acfbc4a1f0b96854c118e555a9a5bb760a0ce6c3e1498c26b3a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98754': ['headers',
                                   'd60daa9fa276231b8b1a1e01d8d74720d20ff0bc3f9871a9208f113adaefcc13',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98763': ['headers',
                                   '7ba6be2af57543a48367c370c25d6fe7588c1d3e0956ccc37e91a5318a97253e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98792': ['headers',
                                   'e55c9f7f5f53a696c7399c92c0679aa5ecb0a2177a53ce45f4929a127650d344',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98794': ['headers',
                                   '7677925a75d8e018c83a5c067a27ec0dc9dc8796e878404f25b68b6474b5697e',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98797': ['headers',
                                   '89cbc4eefb308e97d0a83614e16fc3eae92e62207edcfce315e2f2c4ca270d32',
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
                                   '17eccc0e5df7ecda348371800b5977b234f2c71a0de2335f1e6ed264b69a711a',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98813': ['headers',
                                   'e837fd6edcc5aca3745700a3544b6133aefd45376b0f1c759e86bb8cfe368b0f',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98823': ['headers',
                                   '8b4b08a34ebd1dfc417cf72fc07136d8f2d7beb9d467a856433035e614a7efc3',
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
                                   '8a8dffa4098f5dd2812f59b93489d1ec94e9abdd738248c2b65c7f67e846e148',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98837': ['headers',
                                   '9a5f662247742dfb5a4d74d7b207a0c4cdaf2c58f397cce5d82d7028f9ccbe4c',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98846': ['headers',
                                   '88f9173cc0a627bbf9bbe585bbaa0b6c4a56731e0a7009665f09a961b7cf844d',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98862': ['headers',
                                   '9a5caad91d62d64c5473f24db1182c1849072a92f0034386ff2ab71e2f1b7873',
                                   'kernel_code_absent_from_header_payload'],
                'CVE-2026-98863': ['headers',
                                   'e06b3cbee4b2c905d65680cf451e432898d63d3edcd387c778c264a171e65ca1',
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


def qualify_cgroup(sup, name, owner, image, observed, work, public_recipe, observe):
    require(observed['HostConfig'].get('CgroupnsMode') == 'private', 'Private builder cgroup namespace required')
    pid = observed['State']['Pid']
    identity = cgroup.process_identity(pid)
    # PID 1 is the same process as Docker State.Pid, unlike the short-lived
    # `docker exec cat` process. BuildKit may move the daemon into /init while
    # both it and RUN workers still inherit the Docker-owned root's limits.
    inside = sup.run(['docker', 'exec', observed['Id'], 'cat', '/proc/1/cgroup'], capture=True)
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
    sup.run(['docker', 'buildx', 'build', '--builder', name, '--platform', 'linux/amd64',
             '--progress', 'plain', '--no-cache', str(context)], seconds=180, inspect=check)
    check()
    require(len(seen) == 1, 'Actual bounded RUN process was not observed')
    return {**binding, 'cgroup_version': 2, 'namespace': 'private',
            'effective_limits': limits, 'run_process': next(iter(seen.values())),
            'preflight_base': bases[0], 'run_inherits_builder_limits': True}


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
            'targets': len(results), 'deployable': False, 'site_configured': False}


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
