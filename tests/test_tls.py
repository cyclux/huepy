"""Tests for TLS verification against Signify's private Hue root CAs."""

import ssl

import pytest

from huepy.client.tls import (
    HUE_BRIDGE_ROOT_CA,
    InsecureTlsWarning,
    UnverifiedBridgeIdentityWarning,
    build_ssl_context,
)
from huepy.config import TlsMode

BRIDGE_ID = "001788fffe25b8f8"


def _ca_common_names(context: ssl.SSLContext) -> set[str]:
    names: set[str] = set()
    for cert in context.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.add(value)
    return names


class TestBundle:
    def test_bundle_holds_exactly_the_two_signify_roots(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=HUE_BRIDGE_ROOT_CA)
        # Exactly the two Signify roots -- the system trust store is not loaded.
        assert _ca_common_names(context) == {"root-bridge", "Hue Root CA 01"}


class TestBuildSslContext:
    def test_verified_with_bridge_id_pins_the_common_name(self):
        context, hostname = build_ssl_context(TlsMode.VERIFIED, BRIDGE_ID)
        assert hostname == BRIDGE_ID
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert len(context.get_ca_certs()) == 2

    def test_verified_without_bridge_id_is_ca_only_and_warns(self):
        with pytest.warns(UnverifiedBridgeIdentityWarning):
            context, hostname = build_ssl_context(TlsMode.VERIFIED, None)
        assert hostname is None
        # Chain still verified against Signify; only the identity is unpinned.
        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert len(context.get_ca_certs()) == 2

    def test_insecure_disables_verification_and_warns(self):
        with pytest.warns(InsecureTlsWarning):
            context, hostname = build_ssl_context(TlsMode.INSECURE, BRIDGE_ID)
        assert hostname is None
        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_NONE

    def test_verified_does_not_warn_when_pinned(self, recwarn):
        _ = build_ssl_context(TlsMode.VERIFIED, BRIDGE_ID)
        assert not recwarn.list
