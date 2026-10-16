"""Real OpenSSL handshakes establish the optional strict transport profile."""

from contextlib import contextmanager
import http.server
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from scripts import tls_profile


class StrictTLSProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        extensions = cls.root / 'extensions'
        extensions.write_text(
            'subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\n'
            'extendedKeyUsage=serverAuth\n'
            'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
        )
        for name, bits in [('ca', 3072), ('weak', 2047), ('strong', 3072)]:
            key = cls.root / (name + '.key')
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt',
                            f'rsa_keygen_bits:{bits}', '-out', str(key)],
                           check=True, capture_output=True, timeout=30)
            if name == 'ca':
                args = ['req', '-new', '-x509', '-key', str(key), '-days', '1',
                        '-subj', '/CN=Synthetic strict TLS root',
                        '-addext', 'basicConstraints=critical,CA:TRUE',
                        '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                        '-out', str(cls.root / 'ca.pem')]
            else:
                csr = cls.root / (name + '.csr')
                subprocess.run(['openssl', 'req', '-new', '-key', str(key),
                                '-subj', '/CN=localhost', '-out', str(csr)],
                               check=True, capture_output=True, timeout=15)
                args = ['x509', '-req', '-in', str(csr), '-CA', str(cls.root / 'ca.pem'),
                        '-CAkey', str(cls.root / 'ca.key'), '-set_serial', str(bits),
                        '-days', '1', '-extfile', str(extensions),
                        '-out', str(cls.root / (name + '.pem'))]
            subprocess.run(['openssl', *args], check=True, capture_output=True, timeout=15)

    @contextmanager
    def server(self, name, version):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'verified')

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_ciphers('DEFAULT:@SECLEVEL=0')  # Permit only synthetic weak test keys.
        context.minimum_version = context.maximum_version = version
        context.load_cert_chain(self.root / (name + '.pem'), self.root / (name + '.key'))
        with http.server.HTTPServer(('127.0.0.1', 0), Handler) as server:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f'https://localhost:{server.server_port}/', received
            finally:
                server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def strict(self):
        return patch.dict(os.environ, {
            'VENUS_STRICT_TLS': '1', 'SSL_CERT_FILE': str(self.root / 'ca.pem'),
            'HTTPS_PROXY': '', 'https_proxy': '', 'ALL_PROXY': '', 'all_proxy': '',
            'NO_PROXY': '*', 'no_proxy': '*',
        })

    def test_strong_peer_passes_and_weak_peer_gets_no_credentials(self):
        for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
            with self.subTest(version=version), self.strict():
                for name in ('strong', 'weak'):
                    with self.subTest(name=name), self.server(name, version) as (url, received):
                        request = urllib.request.Request(url, headers={'Authorization': 'synthetic-only'})
                        if name == 'strong':
                            with tls_profile.open_url(request, timeout=5) as response:
                                self.assertEqual(response.read(), b'verified')
                            self.assertEqual(received, ['synthetic-only'])
                        else:
                            with self.assertRaises(urllib.error.URLError) as error:
                                tls_profile.open_url(request, timeout=5)
                            self.assertIsInstance(error.exception.reason, ssl.SSLCertVerificationError)
                            self.assertEqual(received, [])

    def test_wrong_hostname_and_untrusted_ca_get_no_credentials(self):
        with self.strict(), self.server('strong', ssl.TLSVersion.TLSv1_3) as (url, received):
            with self.assertRaises(urllib.error.URLError):
                tls_profile.open_url(url.replace('localhost', '127.0.0.1'), timeout=5)
            with patch.dict(os.environ, {'SSL_CERT_FILE': ''}):
                with self.assertRaises(urllib.error.URLError):
                    tls_profile.open_url(url, timeout=5)
            self.assertEqual(received, [])

    def test_invalid_or_unsupported_profile_fails_closed(self):
        with patch.dict(os.environ, {'VENUS_STRICT_TLS': 'invalid'}):
            with self.assertRaises(ValueError):
                tls_profile.urlopen_options()
        with self.strict(), patch.object(ssl, 'OPENSSL_VERSION', 'LibreSSL 1.0'):
            with self.assertRaises(RuntimeError):
                tls_profile.urlopen_options()

    def test_profile_keeps_validation_and_stricter_defaults(self):
        with self.strict():
            context = tls_profile.urlopen_options()['context']
            self.assertGreaterEqual(context.security_level, 3)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertTrue(all(c['kea'] != 'kx-rsa' for c in context.get_ciphers()))

    def test_stricter_platform_policy_is_preserved(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_ciphers('ECDHE-RSA-AES256-GCM-SHA384:@SECLEVEL=4')
        with self.strict(), patch.object(ssl, 'create_default_context', return_value=context):
            configured = tls_profile.urlopen_options()['context']
        self.assertEqual(configured.minimum_version, ssl.TLSVersion.TLSv1_3)
        self.assertEqual(configured.security_level, 4)
        self.assertEqual([c['name'] for c in configured.get_ciphers()
                          if c['protocol'] != 'TLSv1.3'], ['ECDHE-RSA-AES256-GCM-SHA384'])

    def test_default_uses_normal_urlopen(self):
        with patch.dict(os.environ, {'VENUS_STRICT_TLS': '0'}), \
                patch.object(urllib.request, 'urlopen') as open_request:
            tls_profile.open_url('https://example.invalid/', timeout=7)
            open_request.assert_called_once_with('https://example.invalid/', timeout=7)

    def test_strict_profile_refuses_plaintext_and_credential_redirects(self):
        with self.strict(), self.assertRaises(urllib.error.URLError):
            tls_profile.open_url('http://example.invalid/', timeout=5)
        handler = tls_profile._HTTPSRedirect()
        request = urllib.request.Request('https://example.invalid/', headers={'Authorization': 'synthetic'})
        for target in ('http://example.invalid/', 'https://other.invalid/'):
            with self.subTest(target=target), self.assertRaises(urllib.error.URLError):
                handler.redirect_request(request, None, 302, 'Found', {}, target)
        self.assertEqual(handler.redirect_request(request, None, 302, 'Found', {},
                                                  'https://example.invalid/next').full_url,
                         'https://example.invalid/next')
