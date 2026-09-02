# Accessing the gated Hue v2 developer docs

The authoritative v2 CLIP reference lives behind a login at
`https://developers.meethue.com/develop/hue-api-v2/`. The most useful page is the
full endpoint reference:

- `https://developers.meethue.com/develop/hue-api-v2/api-reference/` — **"Hue CLIP
  API"**, ~2.4 MB, every resource, query parameter, and HTTP status body.
- `.../core-concepts/`, `.../getting-started/`, `.../migration-guide-to-the-new-hue-api/`,
  `.../cloud2cloud-getting-started/` — supporting pages.

Credentials are in the repo-root `.env` as `MEETHUE_USER` / `MEETHUE_PW` (never commit
them — see *Security* below).

## TL;DR — the working method

curl-with-credentials **cannot** log in; the form is protected by Cloudflare
Turnstile plus a Shield "silent captcha". A human must solve Turnstile **once** in a
real browser, after which the WordPress session cookie lets plain `curl` read every
gated page.

1. Open the login page in the **Playwright MCP browser** (`browser_navigate` to
   `https://developers.meethue.com/login/`). Use the MCP browser as-is — do **not**
   hand-roll a Chrome launch with custom flags (see *Root cause* for why).
2. Ask the user to type nothing sensitive on your behalf beyond what they choose:
   fill `MEETHUE_USER` / `MEETHUE_PW`, tick **"Verify you are human"**, click **Login**.
   Turnstile in managed mode usually passes the MCP browser silently.
3. Export that browser's session cookies to a curl jar (snippet below).
4. Fetch anything with `curl -b meethue.jar <url>`.

The session cookies to look for are `wordpress_logged_in_*` and `wordpress_sec_*` on
`developers.meethue.com`.

## Exporting the session to a curl jar

The MCP Chrome profile lives at
`~/.cache/ms-playwright-mcp/mcp-chrome-<id>/`. On WSL2 there is **no D-Bus / keyring**,
so Chrome protects cookies with the keyring-less `v10` scheme (PBKDF2 of the constant
`"peanuts"`, 1 iteration, 16-space IV). `browser_cookie3` insists on the keyring and
crashes with `KeyError: 'DBUS_SESSION_BUS_ADDRESS'`; decrypt with the fallback key
directly. These are the user's own just-created cookies, decrypted locally under the
user's own authority.

```python
# uv run --with pycryptodomex python export_cookies.py
import shutil, sqlite3, tempfile
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from Cryptodome.Cipher import AES
from Cryptodome.Protocol.KDF import PBKDF2

PROFILE = next(
    Path.home().glob(".cache/ms-playwright-mcp/mcp-chrome-*")
)  # the logged-in one
KEY, IV = PBKDF2(b"peanuts", b"saltysalt", dkLen=16, count=1), b" " * 16


def decrypt(blob: bytes) -> str:
    if blob[:3] not in (b"v10", b"v11"):
        return blob.decode("utf-8", "replace")
    dec = AES.new(KEY, AES.MODE_CBC, IV).decrypt(blob[3:])
    dec = dec[: -dec[-1]]  # strip PKCS7 padding
    try:
        return dec.decode("utf-8")
    except UnicodeDecodeError:
        return dec[32:].decode(
            "utf-8", "replace"
        )  # Chrome >=130 prepends a 32-byte domain hash


tmp = Path(tempfile.mkdtemp()) / "Cookies"  # copy: the live browser locks the DB
shutil.copy2(PROFILE / "Default/Cookies", tmp)
jar = MozillaCookieJar("meethue.jar")
for host, name, enc, path, exp, secure in sqlite3.connect(tmp).execute(
    "select host_key,name,encrypted_value,path,expires_utc,is_secure "
    "from cookies where host_key like '%meethue%'"
):
    jar.set_cookie(
        Cookie(
            0,
            name,
            decrypt(enc),
            None,
            False,
            host,
            True,
            host.startswith("."),
            path,
            True,
            bool(secure),
            int(exp / 1_000_000 - 11_644_473_600) if exp else None,
            exp == 0,
            None,
            None,
            {"HttpOnly": None},
            False,
        )
    )
jar.save(ignore_discard=True, ignore_expires=True)
```

Then:

```console
curl -s -b meethue.jar -A "Mozilla/5.0 ... Chrome/149.0.0.0 Safari/537.36" \
  https://developers.meethue.com/develop/hue-api-v2/api-reference/ -o api-reference.html
# 200 if authenticated; a redirect to /login/ means the session expired — re-solve once.
```

## Root cause — use the MCP browser's warm profile, not a fresh one

Turnstile here is **managed mode**: it scores each visitor live. The decisive factor is
**profile warmth**, not launch flags — verified by diffing the full launch args of the
working MCP browser against a hand-rolled replica: they are identical (both carry
`--disable-blink-features=AutomationControlled`, so `navigator.webdriver === false` in
both). A hand-rolled window with matching flags but a **fresh, cold profile** is still
denied, even when a human solves the challenge; the MCP browser can be closed and
reopened and it passes again, because it reuses the **same persistent profile** that has
already solved the challenge and still holds a live WordPress session plus Shield's
`icwp-wpsf-notbot` cookie. A cold profile faces the full first-visit bot gauntlet with no
accumulated trust and gets rejected.

Practical consequence: **do the login in a warm, persistent profile — the Playwright MCP
browser (`browser_navigate`).** Do not spin up a throwaway profile per attempt. Once that
profile has a session, export it once (above) and reuse the jar until it expires.

One earlier self-inflicted defect, for the record: `--host-resolver-rules=MAP
*.challenges.cloudflare.com <ip>` (an attempted IPv6/DNS workaround) misroutes the
per-challenge backend and makes Turnstile fail with **error 600010**, plus a visible
"unsupported command-line flag" banner. It was the wrong fix — Chrome's own resolver
handles those names. **Never pin them.**

## Security

- **Never commit `.env`, the cookie jar, or cookie header files.** `.gitignore` covers
  `.env`, `*.jar`, and `cookie*.txt`; keep it that way.
- The session is **non-renewable and time-limited**: the `wordpress_logged_in` / `wordpress_sec`
  cookies carry a fixed expiry (~2 days by default, ~14 days if **"Keep me signed in"** is ticked
  at login), and authenticated requests do **not** extend them — the server never re-issues the
  auth cookie on use. When pages start redirecting to `/login/`, the only way back is a fresh
  human login in the warm MCP browser, then re-export the jar. Tick "Keep me signed in" to
  stretch the runway.
- Solving Turnstile requires a human. Never automate the challenge itself or route it
  through a solving service.
