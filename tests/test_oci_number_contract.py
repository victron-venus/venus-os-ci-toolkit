"""Accepted OCI numbers must remain representable by canonical JSON."""

import unittest

from scripts import merge_oci_archives as oci


class OciNumberContractTests(unittest.TestCase):
    def test_rejects_overflowing_exponents_at_every_nesting_level(self):
        for number in ("1e999", "-1e999", "1.8e308", "-1.8e308"):
            for template in ('{"n":%s}', '{"nested":{"n":%s}}', '{"list":[%s]}'):
                with self.subTest(number=number, template=template):
                    raw = (template % number).encode()
                    with self.assertRaisesRegex(ValueError, "must remain finite"):
                        oci.document(raw)

    def test_finite_extremes_and_underflow_round_trip(self):
        for number in ("1.7976931348623157e308", "-1.7976931348623157e308", "1e-999", "-1e-999", "0.25"):
            with self.subTest(number=number):
                value = oci.document(('{"nested":[%s]}' % number).encode())
                self.assertEqual(oci.document(oci.canonical(value)), value)


if __name__ == "__main__":
    unittest.main()
