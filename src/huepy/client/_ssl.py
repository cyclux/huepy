"""Shared SSL context for reaching the bridge without verification.

The insecure context lives here, apart from :mod:`huepy.client.tls`, so a future
unauthenticated probe (bridge discovery, which must connect before any identity
is known) can reuse it rather than re-rolling ``CERT_NONE``.
"""

import ssl


def unverified_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that accepts any certificate.

    A genuine bridge is reached over HTTPS with a certificate signed by
    Signify's private CA; this context skips that check entirely. It backs the
    explicit ``insecure`` opt-out.

    Returns:
        A context with hostname and certificate verification disabled.

    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context
