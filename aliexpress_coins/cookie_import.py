"""Cookies pasted from the user's own browser, turned into Playwright cookies.

Accepted formats:
  - JSON array from a cookie extension (Cookie-Editor, EditThisCookie):
    [{"name", "value", "domain", "path", "expirationDate", "httpOnly", "secure", "sameSite"}, ...]
  - Playwright storage state: {"cookies": [{"name", "value", "domain", "expires", ...}]}
  - Netscape cookies.txt (the "Get cookies.txt" extensions, curl, yt-dlp)
  - A Cookie request header copied from DevTools: "name=value; name2=value2"

Only AliExpress cookies are kept, and the paste must hold the login ticket
(xman_t), or the import is refused with a message saying what is missing.
"""
import hashlib
import json
import re
import time

YEAR = 365 * 24 * 3600
LOGIN_COOKIE = "xman_t"
LOGIN_COOKIES = ("xman_t", "xman_us_t")  # the login ticket; same set as app.LOGIN_COOKIES
HEADER_DOMAIN = ".aliexpress.com"
SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def _expires(value, now):
    """Expiry in seconds since the epoch.

    A cookie without one (a session cookie, or anything from a Cookie header) gets
    a year: the add-on's Chromium drops session cookies each time it closes, and
    AliExpress still ends the session on its side when it wants to.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return now + YEAR
    if value > 1e11:  # milliseconds
        value /= 1000
    return value if value > 0 else now + YEAR


def _cookie(name, value, domain, path="/", expires=None, http_only=False, secure=True, same_site=None, now=0):
    if not name or value is None or not domain:
        return None
    c = {"name": str(name), "value": str(value), "domain": str(domain), "path": path or "/",
         "expires": _expires(expires, now), "httpOnly": bool(http_only), "secure": bool(secure)}
    same_site = SAMESITE.get(str(same_site or "").lower())
    # Chromium rejects SameSite=None without Secure; leave it to the browser's default (Lax).
    if same_site and not (same_site == "None" and not c["secure"]):
        c["sameSite"] = same_site
    return c


def _from_json(data, now):
    if isinstance(data, dict):
        data = data.get("cookies")
    if not isinstance(data, list):
        raise ValueError("The JSON is not a list of cookies")
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        expires = None if c.get("session") else c.get("expirationDate", c.get("expires"))
        out.append(_cookie(c.get("name"), c.get("value"), c.get("domain") or c.get("host"), c.get("path"),
                           expires, c.get("httpOnly"), c.get("secure", True), c.get("sameSite"), now))
    return out


def _from_netscape(text, now):
    out = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        f = line.rstrip("\r\n").split("\t")
        if len(f) != 7:
            continue
        domain, _, path, secure, expires, name, value = f
        out.append(_cookie(name, value, domain, path, expires, http_only, secure.upper() == "TRUE", None, now))
    return out


def _from_header(text, now):
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I)
    out = []
    for part in text.split(";"):
        name, eq, value = part.strip().partition("=")
        if eq:
            out.append(_cookie(name.strip(), value.strip(), HEADER_DOMAIN, now=now))
    return out


def parse(text, now=None):
    """Playwright cookies from pasted text; raises ValueError with a message for the user."""
    now = time.time() if now is None else now
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing was pasted")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"The JSON could not be read ({e})") from None
        cookies = _from_json(data, now)
    elif re.search(r"^[^\t\n]*(\t[^\t\n]*){6}$", text, re.M):
        cookies = _from_netscape(text, now)
    else:
        cookies = _from_header(text, now)
    by_key = {}
    for c in cookies:
        if c and "aliexpress" in c["domain"].lower() and c["expires"] > now:
            by_key[c["name"], c["domain"], c["path"]] = c
    cookies = list(by_key.values())
    if not cookies:
        raise ValueError("No AliExpress cookies found. Copy them while an aliexpress.com page is open")
    if not any(c["name"] == LOGIN_COOKIE for c in cookies):
        raise ValueError(f"These cookies have no login ({LOGIN_COOKIE} is missing). "
                         "Log in to AliExpress first, then copy them again")
    return cookies


def fingerprint(cookies):
    """A short hash of the login ticket, to tell a new login from one already imported."""
    ticket = sorted((c["name"], c["value"]) for c in cookies if c["name"] in LOGIN_COOKIES)
    return hashlib.sha256(json.dumps(ticket).encode()).hexdigest()[:16]
