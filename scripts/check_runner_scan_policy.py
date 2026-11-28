#!/usr/bin/env python3
"""Compare scan-policy identity pins with the packages a dispatch will resolve.

The runner image installs its Ubuntu packages from the live release pockets, so a
version pinned in `SCAN_POLICY` can drift without any change in this repository.
Today that drift only surfaces as `finding_identity_changed` after a full hosted
build, when the applicability gate rejects an otherwise complete image.

This read-only check asks the same question first, from the package indexes the
build resolves from and from an optional retained scan report. It changes no
policy data and performs no exemption.

Exit status is 0 when every checked source agrees with the pins, 1 when a pin is
stale or the policy documents disagree, and 2 when the pins could not be
verified (an absent package, an unreadable index or a failed download).
"""

from __future__ import annotations

import argparse
import gzip
import json
import lzma
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_runner_image_artifacts as build  # noqa: E402

# `deploy/runner-reserve/Dockerfile` starts from ubuntu:24.04 (noble) and installs
# the base packages from these pockets. A different base requires editing both.
RELEASE = 'noble'
ARCHITECTURE = 'amd64'
BASE_IMAGE = 'docker.io/library/ubuntu@sha256:'
DEFAULT_SOURCES = (
    ('noble', 'https://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.xz'),
    ('noble-updates', 'https://archive.ubuntu.com/ubuntu/dists/noble-updates/main/binary-amd64/Packages.xz'),
    ('noble-security', 'https://security.ubuntu.com/ubuntu/dists/noble-security/main/binary-amd64/Packages.xz'),
)
APPLICABILITY = ROOT / 'docs/RUNNER_SCAN_APPLICABILITY.json'
DOCKERFILE = ROOT / 'deploy/runner-reserve/Dockerfile'


def split_version(version):
    """Split `[epoch:]upstream[-revision]` as apt reports it in SrcVersion/SrcRelease."""
    _, separator, remainder = version.partition(':')
    remainder = remainder if separator else version
    upstream, separator, revision = remainder.partition('-')
    return upstream, revision if separator else None


def fragment_key(fragment):
    """Order the version fragments this policy uses (dotted numbers and suffixes)."""
    key = []
    for part in re.findall(r'\d+|[A-Za-z]+|~|.', fragment):
        if part == '~':
            key.append((-1, 0, ''))
        elif part.isdigit():
            key.append((1, int(part), ''))
        else:
            key.append((0, 0, part))
    return key


def version_key(version):
    """Order Ubuntu versions closely enough to pick a candidate among pockets."""
    epoch, separator, remainder = version.partition(':')
    if not separator:
        epoch, remainder = '0', version
    upstream, separator, revision = remainder.rpartition('-')
    if not separator:
        upstream, revision = remainder, ''
    return (int(epoch) if epoch.isdigit() else 0,
            fragment_key(upstream), fragment_key(revision))


def policy_pins(policy):
    """Ubuntu package identities whose version the release pockets can move."""
    pins = []
    for component, identity in policy['identities'].items():
        if identity.get('type') != 'ubuntu':
            continue
        version = identity.get('version', '')
        source_version, source_release = split_version(version)
        pins.append({
            'component': component,
            'package': identity.get('package'),
            'architecture': identity.get('architecture') or ARCHITECTURE,
            'version': version,
            'package_id': identity.get('package_id'),
            'purl': identity.get('purl'),
            'source_package': identity.get('source_package'),
            'source_version': identity.get('source_version'),
            'source_release': identity.get('source_release'),
            'derived_source': (source_version, source_release),
        })
    return pins


def parse_packages(text):
    """Read the stanzas of a Debian `Packages` index."""
    stanzas, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
                current = {}
            continue
        if line[0] in ' \t':
            continue
        key, separator, value = line.partition(': ')
        if separator:
            current[key] = value
    if current:
        stanzas.append(current)
    return stanzas


def index_versions(text, pin):
    """Versions a single index publishes for the pinned package and architecture."""
    return {stanza['Version'] for stanza in parse_packages(text)
            if stanza.get('Package') == pin['package'] and
            stanza.get('Architecture') in (pin['architecture'], 'all') and
            stanza.get('Version')}


def decompress(raw, name):
    if raw[:6] == b'\xfd7zXZ\x00':
        return lzma.decompress(raw)
    if raw[:2] == b'\x1f\x8b':
        return gzip.decompress(raw)
    if name.endswith('.xz'):
        raise ValueError(name + ' does not contain an xz stream')
    if name.endswith('.gz'):
        raise ValueError(name + ' does not contain a gzip stream')
    return raw


def load_index(path):
    """Read a local `Packages` file, compressed or plain."""
    return decompress(Path(path).read_bytes(), str(path)).decode('utf-8')


def fetch_index(url, timeout=60.0):
    """Download a `Packages` index from the release pockets."""
    request = urllib.request.Request(
        url, headers={'User-Agent': 'venus-os-ci-toolkit-scan-policy-check'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return decompress(response.read(), url).decode('utf-8')


def check_policy(policy, document, dockerfile):
    """Offline disagreements between the pins, their blocks and the review document."""
    problems = []
    pins = policy_pins(policy)
    if not pins:
        problems.append('the policy pins no Ubuntu package identity to compare')
    for pin in pins:
        expected_id = str(pin['package']) + '@' + pin['version']
        if pin['package_id'] != expected_id:
            problems.append(pin['component'] + ' identity names ' + str(pin['package_id']) +
                            ' but pins version ' + pin['version'])
        expected_purl = ('pkg:deb/ubuntu/' + str(pin['package']) + '@' + pin['version'] +
                         '?arch=' + pin['architecture'] + '&distro=ubuntu-24.04')
        if pin['purl'] != expected_purl:
            problems.append(pin['component'] + ' identity package URL does not match its version pin')
        if (pin['source_version'], pin['source_release']) != pin['derived_source']:
            problems.append(pin['component'] + ' identity source fields disagree with version ' +
                            pin['version'])
        block = policy.get(pin['component'])
        if isinstance(block, dict) and block.get('package') == pin['package']:
            for key in ('version', 'architecture'):
                if block.get(key) != pin[key]:
                    problems.append(pin['component'] + ' block pins ' + str(block.get(key)) +
                                    ' but the identity pins ' + str(pin[key]))
    provenance = document.get('header_provenance', {})
    for pin in pins:
        if pin['component'] != 'headers' or not provenance:
            continue
        for key, value in (('package', pin['package']), ('version', pin['version']),
                           ('architecture', pin['architecture'])):
            if provenance.get(key) != value:
                problems.append('docs/RUNNER_SCAN_APPLICABILITY.json header_provenance pins ' +
                                str(provenance.get(key)) + ' but SCAN_POLICY pins ' + str(value))
    if BASE_IMAGE not in dockerfile:
        problems.append('deploy/runner-reserve/Dockerfile no longer starts from the pinned Ubuntu '
                        'base image; the "' + RELEASE + '" package-index default may be wrong')
    return pins, problems


def check_indexes(sources, pins):
    """Compare the pins with the candidate versions of every `Packages` index."""
    problems, notes, unverified = [], [], []
    if not sources:
        notes.append('Ubuntu release-pocket indexes were not checked')
        return problems, notes, unverified
    published = {pin['component']: {} for pin in pins}
    for label, text in sources:
        for pin in pins:
            published[pin['component']][label] = index_versions(text, pin)
    for pin in pins:
        versions = set()
        for label, found in published[pin['component']].items():
            versions |= found
            notes.append(label + ': ' + pin['package'] + ' ' +
                         (max(found, key=version_key) if found else 'absent'))
        if not versions:
            unverified.append('no checked index publishes ' + pin['package'] + ' for ' +
                              pin['architecture'])
            continue
        candidate = max(versions, key=version_key)
        if candidate != pin['version']:
            problems.append('the release pockets resolve ' + pin['package'] + ' to ' + candidate +
                            ' but SCAN_POLICY pins ' + pin['version'])
    return problems, notes, unverified


def check_report(path, report, pins):
    """Compare the pins with the installed package records of a retained scan."""
    problems, notes, unverified = [], [], []
    occurrences = {pin['component']: [] for pin in pins}
    for result in report.get('Results') or []:
        if not isinstance(result, dict) or result.get('Class') != 'os-pkgs':
            continue
        for package in result.get('Packages') or []:
            if not isinstance(package, dict):
                continue
            for pin in pins:
                if package.get('Name') != pin['package']:
                    continue
                if package.get('Arch') not in (pin['architecture'], 'all'):
                    continue
                version = str(package.get('Version', ''))
                if package.get('Release'):
                    version += '-' + str(package['Release'])
                occurrences[pin['component']].append({
                    'target': result.get('Target'),
                    'version': version,
                    'source_version': package.get('SrcVersion'),
                    'source_release': package.get('SrcRelease'),
                })
    for pin in pins:
        rows = occurrences[pin['component']]
        if not rows:
            unverified.append(str(path) + ' has no ' + pin['package'] + ' package record')
            continue
        versions = sorted({row['version'] for row in rows})
        notes.append(str(path) + ': ' + pin['package'] + ' ' + ', '.join(versions))
        for version in versions:
            if version != pin['version']:
                problems.append(str(path) + ' records ' + pin['package'] + ' ' + version +
                                ' but SCAN_POLICY pins ' + pin['version'])
        source = (pin['source_version'], pin['source_release'])
        for row in rows:
            if (row['source_version'], row['source_release']) != source:
                problems.append(str(path) + ' records source ' + str(row['source_version']) + '-' +
                                str(row['source_release']) + ' for ' + str(row['target']) +
                                ' but SCAN_POLICY pins ' + '-'.join(
                                    str(part) for part in source))
                break
    return problems, notes, unverified


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ubuntu-index', action='append', default=[], type=Path,
                        metavar='PACKAGES',
                        help='compare with a local Packages index instead of the network')
    parser.add_argument('--scan-report', action='append', default=[], type=Path,
                        metavar='SCAN', help='compare with a retained Trivy scan.json')
    parser.add_argument('--no-network', action='store_true',
                        help='skip the Ubuntu release-pocket indexes')
    parser.add_argument('--timeout', type=float, default=60.0,
                        help='download timeout in seconds (default: 60)')
    args = parser.parse_args(argv)
    pins, problems = check_policy(
        build.SCAN_POLICY, json.loads(APPLICABILITY.read_text()), DOCKERFILE.read_text())
    notes, unverified = [], []
    for path in args.scan_report:
        report_problems, report_notes, report_unverified = check_report(
            path, json.loads(path.read_text()), pins)
        problems += report_problems
        notes += report_notes
        unverified += report_unverified
    sources = []
    if args.ubuntu_index:
        for path in args.ubuntu_index:
            try:
                sources.append((str(path), load_index(path)))
            except (OSError, ValueError, lzma.LZMAError) as error:
                unverified.append('could not read ' + str(path) + ': ' + str(error))
    elif not args.no_network:
        for label, url in DEFAULT_SOURCES:
            try:
                sources.append((label, fetch_index(url, args.timeout)))
            except (OSError, ValueError, lzma.LZMAError) as error:
                unverified.append('could not download ' + url + ': ' + str(error))
    index_problems, index_notes, index_unverified = check_indexes(sources, pins)
    problems += index_problems
    notes += index_notes
    unverified += index_unverified
    for pin in pins:
        print('pinned identity: ' + pin['component'] + ' = ' + str(pin['package']) + ' ' +
              pin['version'] + ' (' + pin['architecture'] + ')')
    for note in notes:
        print(note)
    for problem in dict.fromkeys(problems):
        print('mismatch: ' + problem, file=sys.stderr)
    for item in dict.fromkeys(unverified):
        print('unverified: ' + item, file=sys.stderr)
    if problems:
        print('scan-policy pins are stale: review docs/RUNNER_SCAN_APPLICABILITY.json and '
              'SCAN_POLICY together before dispatching; a dispatch only reports '
              'finding_identity_changed after the full image build.', file=sys.stderr)
        return 1
    if unverified:
        print('scan-policy pins were not verified; resolve the input above and run this check '
              'again.', file=sys.stderr)
        return 2
    print('scan-policy pins match every checked source.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
