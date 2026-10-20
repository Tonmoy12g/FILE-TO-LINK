"""Swappable file-upload providers.

The whole bot talks to storage through ONE function:

    url, provider_name = await upload_file(path, filename)

To switch the storage backend, either set the UPLOAD_PROVIDER environment
variable (gofile | catbox | litterbox) or replace the body of upload_file.
Nothing else in the bot needs to change.

Live-test status (verified from the build sandbox, 2026-10-20):
  * GoFile    - PASSED: anonymous upload returned HTTP 200, and the server
                reported an MD5 identical to the local file's MD5 for both a
                text file and a PNG. Links are DOWNLOAD-PAGE links
                (https://gofile.io/d/...), not raw file links.
  * Catbox    - FAILED from the sandbox: catbox.moe was completely
                unreachable (HTTP 000 on both GET and POST). The code is
                included and is the standard documented API, but it was NOT
                live-verified here.
  * Litterbox - FAILED from the sandbox for the same reason (catbox.moe
                domain family unreachable, HTTP 000). Not live-verified.
"""

import asyncio
import hashlib
import os

import httpx

PROVIDER = os.environ.get("UPLOAD_PROVIDER", "gofile").strip().lower()
CATBOX_USERHASH = os.environ.get("CATBOX_USERHASH", "").strip()

CATBOX_API = "https://catbox.moe/user/api.php"
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
GOFILE_SERVERS_API = "https://api.gofile.io/servers"

_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)


def _client_kwargs():
    """httpx.AsyncClient kwargs that also survive proxied environments.

    - If a proxy is configured via env vars, pass it explicitly and disable
      trust_env (httpx can choke parsing IPv6 entries in NO_PROXY).
    - Prefer the operating system's CA bundle when present (some networks
      intercept TLS with a CA that only exists in the system store).
    On a normal host (e.g. Render) with no proxy vars this changes nothing.
    """
    kwargs = {"timeout": _TIMEOUT}
    ca_bundle = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    if os.path.exists(ca_bundle):
        kwargs["verify"] = ca_bundle
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["trust_env"] = False
    return kwargs

PROVIDER_LABELS = {
    "gofile": "GoFile",
    "catbox": "Catbox",
    "litterbox": "Litterbox (Litterbox files are temporary, ~72h)",
}


class UploadError(Exception):
    """Raised when an upload definitively failed (after retries)."""


def _md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ GoFile

async def _upload_gofile(path, filename):
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        r = await client.get(GOFILE_SERVERS_API)
        r.raise_for_status()
        servers = r.json()["data"]["servers"]
        if not servers:
            raise UploadError("GoFile returned no upload servers")
        server = servers[0]["name"]
        # httpx streams the file object; the whole file is never in memory.
        with open(path, "rb") as f:
            r = await client.post(
                f"https://{server}.gofile.io/contents/uploadfile",
                files={"file": (filename, f)},
            )
        r.raise_for_status()
        payload = r.json()
    if payload.get("status") != "ok":
        raise UploadError(f"GoFile error: {str(payload)[:200]}")
    data = payload["data"]
    url = data.get("downloadPage")
    if not url:
        raise UploadError("GoFile response had no downloadPage")
    remote_md5 = data.get("md5")
    if remote_md5 and remote_md5.lower() != _md5_of_file(path).lower():
        raise UploadError("GoFile MD5 mismatch - uploaded file is corrupted")
    return url


# ------------------------------------------------------------------ Catbox

async def _upload_catbox(path, filename):
    data = {"reqtype": "fileupload"}
    if CATBOX_USERHASH:
        data["userhash"] = CATBOX_USERHASH
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        with open(path, "rb") as f:
            r = await client.post(
                CATBOX_API, data=data, files={"fileToUpload": (filename, f)}
            )
    text = r.text.strip()
    if r.status_code == 200 and text.startswith("https://files.catbox.moe/"):
        return text
    raise UploadError(f"Catbox HTTP {r.status_code}: {text[:200]}")


# ---------------------------------------------------------------- Litterbox

async def _upload_litterbox(path, filename):
    data = {"reqtype": "fileupload", "time": "72h"}
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        with open(path, "rb") as f:
            r = await client.post(
                LITTERBOX_API, data=data, files={"fileToUpload": (filename, f)}
            )
    text = r.text.strip()
    if r.status_code == 200 and text.startswith("https://litter.catbox.moe/"):
        return text
    raise UploadError(f"Litterbox HTTP {r.status_code}: {text[:200]}")


_PROVIDERS = {
    "gofile": _upload_gofile,
    "catbox": _upload_catbox,
    "litterbox": _upload_litterbox,
}


# ------------------------------------------------------------- public API

async def upload_file(path, filename, provider=None):
    """Upload `path` and return (url, provider_name). Retries 3 times."""
    name = (provider or PROVIDER).lower()
    fn = _PROVIDERS.get(name)
    if fn is None:
        raise UploadError(f"Unknown UPLOAD_PROVIDER: {name}")
    last_error = None
    for attempt in range(1, 4):
        try:
            url = await fn(path, filename)
            return url, name
        except Exception as exc:  # network error or provider error -> retry
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)  # 2s, then 4s backoff
    raise UploadError(f"{name} upload failed after 3 attempts: {last_error}")


async def verify_link(url):
    """Check the returned link actually answers (HEAD, else ranged GET)."""
    try:
        async with httpx.AsyncClient(
            **{**_client_kwargs(), "timeout": 30.0, "follow_redirects": True}
        ) as client:
            try:
                r = await client.head(url)
                if r.status_code in (200, 206):
                    return True
            except httpx.HTTPError:
                pass
            r = await client.get(url, headers={"Range": "bytes=0-0"})
            return r.status_code in (200, 206)
    except Exception:
        return False
