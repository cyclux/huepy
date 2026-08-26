"""TLS verification against Signify's private Hue Bridge root CAs.

The v2 API is HTTPS-only, and a genuine bridge presents a certificate signed by
one of Signify's two private root CAs, carrying the bridge id as its subject
common name. The operating system's trust store never contains those roots, so
verification has to run against the bundle embedded here -- and the hostname has
to be checked against the bridge id, not the IP address the socket dials.

That is why the default is not ``ssl.create_default_context()``: the system
store would reject every real bridge. It is also why plain "verify against the
IP" cannot work -- the certificate's common name is the bridge id.

Typical usage example:

    context, server_hostname = build_ssl_context(TlsMode.VERIFIED, bridge_id)
"""

import ssl
import warnings

from huepy.client._ssl import unverified_ssl_context
from huepy.config import TlsMode

__all__ = [
    "HUE_BRIDGE_ROOT_CA",
    "InsecureTlsWarning",
    "UnverifiedBridgeIdentityWarning",
    "build_ssl_context",
]

HUE_BRIDGE_ROOT_CA = """-----BEGIN CERTIFICATE-----
MIICMjCCAdigAwIBAgIUO7FSLbaxikuXAljzVaurLXWmFw4wCgYIKoZIzj0EAwIw
OTELMAkGA1UEBhMCTkwxFDASBgNVBAoMC1BoaWxpcHMgSHVlMRQwEgYDVQQDDAty
b290LWJyaWRnZTAiGA8yMDE3MDEwMTAwMDAwMFoYDzIwMzgwMTE5MDMxNDA3WjA5
MQswCQYDVQQGEwJOTDEUMBIGA1UECgwLUGhpbGlwcyBIdWUxFDASBgNVBAMMC3Jv
b3QtYnJpZGdlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjNw2tx2AplOf9x86
aTdvEcL1FU65QDxziKvBpW9XXSIcibAeQiKxegpq8Exbr9v6LBnYbna2VcaK0G22
jOKkTqOBuTCBtjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNV
HQ4EFgQUZ2ONTFrDT6o8ItRnKfqWKnHFGmQwdAYDVR0jBG0wa4AUZ2ONTFrDT6o8
ItRnKfqWKnHFGmShPaQ7MDkxCzAJBgNVBAYTAk5MMRQwEgYDVQQKDAtQaGlsaXBz
IEh1ZTEUMBIGA1UEAwwLcm9vdC1icmlkZ2WCFDuxUi22sYpLlwJY81Wrqy11phcO
MAoGCCqGSM49BAMCA0gAMEUCIEBYYEOsa07TH7E5MJnGw557lVkORgit2Rm1h3B2
sFgDAiEA1Fj/C3AN5psFMjo0//mrQebo0eKd3aWRx+pQY08mk48=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIBzDCCAXOgAwIBAgICEAAwCgYIKoZIzj0EAwIwPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTAgFw0y
NTAyMjUwMDAwMDBaGA8yMDUwMTIzMTIzNTk1OVowPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABFfOO0jfSAUXGQ9kjEDzyBrcMQ3ItyA5krE+
cyvb1Y3xFti7KlAad8UOnAx0FBLn7HZrlmIwm1QnX0fK3LPM13mjYzBhMB0GA1Ud
DgQWBBTF1pSpsCASX/z0VHLigxU2CAaqoTAfBgNVHSMEGDAWgBTF1pSpsCASX/z0
VHLigxU2CAaqoTAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAKBggq
hkjOPQQDAgNHADBEAiAk7duT+IHbOGO4UUuGLAEpyYejGZK9Z7V9oSfnvuQ5BQIg
IYSgwwxHXm73/JgcU9lAM6c8Bmu3UE3kBIUwBs1qXFw=
-----END CERTIFICATE-----
"""
"""Signify's two private Hue Bridge root CAs, active root first.

Transcribed verbatim from the developer portal's "Using HTTPS" page: the active
``root-bridge`` root (valid to 2038) on top and the secondary ``Hue Root CA 01``
(valid to 2050) the bridges may switch to underneath. A genuine bridge
certificate chains to one of these; nothing signed by a public CA does.
"""


class InsecureTlsWarning(UserWarning):
    """The bridge connection skips certificate verification entirely.

    Raised when ``tls="insecure"``: the transport accepts any certificate, so a
    spoofed bridge could capture the application key. Intended for development
    against proxies or emulators, never production.
    """


class UnverifiedBridgeIdentityWarning(UserWarning):
    """The bridge certificate is trusted, but its identity is not pinned.

    Verification confirmed the peer holds a genuine Signify-signed Hue bridge
    certificate, so a non-Hue interceptor is rejected. But with no bridge id the
    common name could not be checked, so *which* bridge answered is not
    asserted. Supply ``bridge_id`` to pin it.
    """


def build_ssl_context(
    tls: TlsMode | None,
    bridge_id: str | None,
) -> tuple[ssl.SSLContext, str | None]:
    """Build the SSL context and TLS hostname for a bridge connection.

    Args:
        tls: The verification mode. None is treated as the verified default, so
            an unresolved value never silently disables verification.
        bridge_id: The bridge id to pin the certificate's common name against,
            when it is known.

    Returns:
        The context, and the hostname to verify against -- the bridge id when
        pinning, otherwise None so the request hostname (the IP) is left alone.

    """
    if tls == TlsMode.INSECURE:
        message = (
            "TLS verification is disabled (tls='insecure'); the bridge "
            "certificate is not checked and its identity is not verified."
        )
        warnings.warn(message, InsecureTlsWarning, stacklevel=2)
        return unverified_ssl_context(), None

    # PROTOCOL_TLS_CLIENT verifies the chain and checks the hostname by default;
    # load ONLY the Signify roots, never the system store, which cannot vouch
    # for a bridge's private-CA certificate.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=HUE_BRIDGE_ROOT_CA)
    if bridge_id:
        # Full pinning: the cert must chain to a Signify root AND its common
        # name must equal the bridge id (passed as server_hostname below).
        context.check_hostname = True
        return context, bridge_id

    # CA-only: still proves the peer is a genuine Signify-signed bridge (the
    # chain is verified), but without a bridge id the common name cannot be
    # pinned, so which bridge answered is not asserted.
    context.check_hostname = False
    message = (
        "Bridge certificate verified against Signify's roots, but its identity "
        "is not pinned: no bridge_id was supplied. Pass bridge_id to pin the "
        "exact bridge."
    )
    warnings.warn(message, UnverifiedBridgeIdentityWarning, stacklevel=2)
    return context, None
