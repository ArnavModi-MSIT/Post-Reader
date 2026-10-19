"""Strict posting identity shared by discovery, checkpoints and history."""
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


def posting_key(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return None
        host = (parts.hostname or "").removeprefix("www.")
        if host == "linkedin.com":
            match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?([0-9]+)/?", parts.path)
            identifier = match.group(1) if match else None
            if not identifier and parts.path.rstrip("/") in {"/jobs/search", "/jobs/search-results"}:
                identifier = parse_qs(parts.query).get("currentJobId", [None])[0]
            if identifier and identifier.isascii() and identifier.isdigit():
                return "linkedin", identifier
        if host == "internshala.com":
            # The URL exposes a numeric suffix, not an unambiguous slug/ID delimiter.
            # Keep the whole suffix; do not guess a fixed ID length.
            match = re.fullmatch(r"/(internship|job|fresher-job)/detail/[^/]*?([0-9]+)/?", parts.path)
            if match:
                return "internshala", ("" if match.group(1) == "internship" else "job:") + match.group(2)
    except ValueError:
        pass
    return None


def canonical_job_url(url):
    key = posting_key(url)
    if not key:
        return None
    if key[0] == "linkedin":
        return f"https://www.linkedin.com/jobs/view/{key[1]}/"
    parts = urlsplit(url)
    return urlunsplit(("https", "internshala.com", parts.path.rstrip("/"), "", ""))
