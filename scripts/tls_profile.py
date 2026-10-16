"""Opt-in strict HTTPS settings for toolkit-owned Python download paths."""

import os
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def urlopen_options():
    """Require OpenSSL 3 and conservative key strength when explicitly selected."""
    value = os.environ.get("VENUS_STRICT_TLS", "0")
    if value not in {"0", "1"}:
        raise ValueError("VENUS_STRICT_TLS must be 0 or 1")
    if value == "0":
        return {}
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3."):
        raise RuntimeError("Strict TLS requires OpenSSL 3")
    context = ssl.create_default_context()
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Level 3 conservatively requires >=128-bit security (RSA >=3072, EC >=256).
    # Preserve a stricter platform default and omit static RSA key exchange.
    level = max(3, context.security_level)
    # Start with the platform-enabled suites instead of enabling DEFAULT suites.
    suites = ":".join(cipher["name"] for cipher in context.get_ciphers()
                      if cipher["protocol"] != "TLSv1.3")
    context.set_ciphers(f"{suites}:!kRSA:!aNULL:!eNULL:!PSK:!SRP:@SECLEVEL={level}")
    return {"context": context}


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, target):
        old, new = urlsplit(request.full_url), urlsplit(target)
        if new.scheme != "https":
            raise urllib.error.URLError("Strict TLS refuses a non-HTTPS redirect")
        if request.has_header("Authorization") and (
            old.hostname, old.port or 443
        ) != (new.hostname, new.port or 443):
            raise urllib.error.URLError("Strict TLS refuses a credential redirect")
        return super().redirect_request(request, fp, code, message, headers, target)


def open_url(request, *, timeout):
    """Keep normal behavior unless selected, then preserve HTTPS across redirects."""
    options = urlopen_options()
    if not options:
        return urllib.request.urlopen(request, timeout=timeout)
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if urlsplit(url).scheme != "https":
        raise urllib.error.URLError("Strict TLS requires an HTTPS URL")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=options["context"]), _HTTPSRedirect()
    )
    return opener.open(request, timeout=timeout)
