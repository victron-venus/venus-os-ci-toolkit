"""Docker layouts bind raw diff IDs or the exact already-verified OCI blobs."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import oci_builder_image as image  # noqa: E402


def sha(data):
    return hashlib.sha256(data).hexdigest()


def document(value):
    return json.dumps(value, sort_keys=True).encode()


def packed(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o644
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


class DockerExportFormats(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def exports(self, *, compressed=(True, True), normalized=False, arch='amd64'):
        raw = [packed({name: value}) for name, value in [('first', b'one\n'), ('second', b'two\n')]]
        blobs = [gzip.compress(data, mtime=0) if flag else data for data, flag in zip(raw, compressed)]
        config = document(dict(os='linux', architecture=arch, config={},
            rootfs=dict(type='layers', diff_ids=['sha256:' + sha(data) for data in raw])))
        descriptors = [dict(mediaType=image.oci.LAYER + ('+gzip' if flag else ''),
                            digest='sha256:' + sha(data), size=len(data))
                       for data, flag in zip(blobs, compressed)]
        manifest = document(dict(schemaVersion=2, layers=descriptors,
            config=dict(mediaType=image.oci.CONFIG, digest='sha256:' + sha(config), size=len(config))))
        files = {'oci-layout': document(dict(imageLayoutVersion='1.0.0')),
            'index.json': document(dict(schemaVersion=2, manifests=[dict(mediaType=image.oci.MANIFEST,
                digest='sha256:' + sha(manifest), size=len(manifest))])),
            'blobs/sha256/' + sha(config): config, 'blobs/sha256/' + sha(manifest): manifest}
        files.update({'blobs/sha256/' + sha(blob): blob for blob in blobs})
        oci = self.root / 'image.oci.tar'
        oci.write_bytes(packed(files))
        # No invented proof: the real bounded OCI reader validates all layers.
        proof = image.verify_oci(oci, arch, {'first': sha(b'one\n'), 'second': sha(b'two\n')}, lambda: None)
        docker_blobs = raw if normalized else blobs
        names = ['blobs/sha256/' + sha(blob) for blob in docker_blobs]
        files.update(dict(zip(names, docker_blobs)))
        docker_manifest = [dict(Config='blobs/sha256/' + sha(config), RepoTags=['local/proof:test'], Layers=names)]
        files['manifest.json'] = document(docker_manifest)
        return proof, files, docker_manifest, raw

    def verify(self, proof, files):
        path = self.root / 'image.docker.tar'
        path.write_bytes(packed(files))
        return image.verify_docker_export(path, proof, 'local/proof:test')

    def test_real_raw_gzip_and_mixed_layouts_on_both_architectures(self):
        for arch in ('amd64', 'arm64'):
            for compression in ((False, False), (True, True), (False, True), (True, False)):
                with self.subTest(arch=arch, compressed=compression):
                    proof, files, _, _ = self.exports(compressed=compression, arch=arch)
                    bridge = self.verify(proof, files)
                    self.assertTrue(bridge['same_solve'])
                    self.assertEqual(bridge['config'], proof['config'])
                    self.assertEqual(bridge['diff_ids'], proof['diff_ids'])

    def test_legacy_uncompressed_members_from_gzip_oci_remain_supported(self):
        proof, files, _, _ = self.exports(normalized=True)
        self.assertTrue(self.verify(proof, files)['same_solve'])

    def test_corrupted_compressed_member_is_rejected(self):
        proof, files, manifest, _ = self.exports()
        name = manifest[0]['Layers'][0]
        files[name] = files[name][:-1] + bytes([files[name][-1] ^ 1])
        with self.assertRaisesRegex(ValueError, 'Docker layer bytes differ'):
            self.verify(proof, files)

    def test_recompression_with_same_diff_id_is_not_exact_verified_blob(self):
        proof, files, manifest, raw = self.exports()
        changed = gzip.compress(raw[0], mtime=1)
        self.assertEqual(gzip.decompress(changed), raw[0])
        name = 'blobs/sha256/' + sha(changed)
        files[name] = changed
        manifest[0]['Layers'][0] = name
        files['manifest.json'] = document(manifest)
        with self.assertRaisesRegex(ValueError, 'Docker layer bytes differ'):
            self.verify(proof, files)

    def test_reordered_or_repeated_compressed_layers_are_rejected(self):
        for repeat in (False, True):
            proof, files, manifest, _ = self.exports()
            if repeat:
                manifest[0]['Layers'][1] = manifest[0]['Layers'][0]
            else:
                manifest[0]['Layers'].reverse()
            files['manifest.json'] = document(manifest)
            with self.subTest(repeat=repeat), self.assertRaisesRegex(ValueError, 'Docker layer bytes differ'):
                self.verify(proof, files)

    def test_missing_or_short_verified_layer_inventory_is_rejected(self):
        for missing in (False, True):
            proof, files, _, _ = self.exports()
            if missing:
                del proof['layers']
            else:
                proof['layers'].pop()
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'Verified OCI layer inventory differs'):
                self.verify(proof, files)

    def test_wrong_verified_blob_size_or_digest_or_media_type_is_rejected(self):
        for field, value in [('size', 1), ('size', True), ('size', '1'),
                             ('digest', 'sha256:' + '0' * 64), ('mediaType', image.oci.LAYER + '+zstd')]:
            proof, files, _, _ = self.exports()
            proof['layers'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'Docker layer bytes differ'):
                self.verify(proof, files)

    def test_missing_member_or_changed_layer_count_is_rejected(self):
        for missing in (False, True):
            proof, files, manifest, _ = self.exports()
            if missing:
                del files[manifest[0]['Layers'][0]]
            else:
                manifest[0]['Layers'].pop()
                files['manifest.json'] = document(manifest)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.verify(proof, files)

    def test_changed_config_or_tag_cannot_use_blob_acceptance(self):
        for change in ('config', 'tag'):
            proof, files, manifest, _ = self.exports()
            if change == 'config':
                name = manifest[0]['Config']
                data = json.loads(files[name]); data['config']['User'] = 'foreign'
                files[name] = document(data)
            else:
                manifest[0]['RepoTags'] = ['foreign/tag']
                files['manifest.json'] = document(manifest)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(proof, files)

    def test_original_archive_cap_is_enforced(self):
        proof, files, _, _ = self.exports()
        with patch.object(image, 'MAX_ROOTFS', 1), self.assertRaisesRegex(ValueError, 'Oversized Docker export'):
            self.verify(proof, files)


if __name__ == '__main__':
    unittest.main()
