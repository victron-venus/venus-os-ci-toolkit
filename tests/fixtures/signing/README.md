# Public signing test fixture

`ed448-synthetic.asc` is a **public, unencrypted, synthetic private key** used
only by the test suite. Never use it for a real identity, release or credential.
It was generated for this repository with GnuPG 2.4.4 on Ubuntu 24.04 and has
no relationship to any operator key. It is distributed under the repository's
MIT license.

Fingerprint: `CA45CAE879F19EAAF88BD03A5A7234CCD3A0E3BE6B42A0FFD43B3253497F2A26`.

The real-key tests inspect its Ed448 metadata, sign and verify a test message,
and reject a tampered message inside an isolated, temporary GnuPG home. The
agent is stopped before that directory is removed. Other SSH/OpenPGP fixtures
remain freshly generated for each test suite.

## Why this one fixture is stored

The Ubuntu 24.04 GnuPG 2.4.4 / libgcrypt 1.10.3 key generator intermittently fails
before the product is called: `signing failed: No secret key`. An isolated
512-generation reproduction observed nine failures. A separate 256-generation
run recorded failing native Ed448 public points starting with `0x02` and `0x03`.
The failure is in keygrip conversion: libgcrypt's
[`compute_keygrip`](https://github.com/gpg/libgcrypt/blob/libgcrypt-1.10.3/cipher/ecc.c#L1478-L1550)
can interpret those native Ed448 bytes as Weierstrass point-compression markers;
GnuPG then cannot locate the freshly generated secret key while self-signing.

A stable, explicitly public Ed448 fixture avoids depending on random third-party
key generation. It does not retry failures, skip Ed448 tests, loosen accepted
algorithms or replace the real signature verification with a mock.
