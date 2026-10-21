"""Regression guards for dependencies that handle real service credentials."""

import hashlib
import ssl

import certifi


def test_runtime_ca_bundle_excludes_emsign_root_with_compromised_intermediates():
    # GHSA-6q48-6c7w-9gw2: eight compromised intermediate keys chain to
    # emSign ECC Root CA - G3. Check the trust store, not just its version.
    blocked_root = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
    context = ssl.create_default_context(cafile=certifi.where())
    trusted_roots = {
        hashlib.sha256(certificate).hexdigest()
        for certificate in context.get_ca_certs(binary_form=True)
    }
    assert trusted_roots
    assert blocked_root not in trusted_roots
