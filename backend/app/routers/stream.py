"""HLS relay for the unified viewer.

Why a relay exists at all
-------------------------
Three separate reasons, none of them "we wanted to touch the video":

1. **Auth.** The gateway's HLS endpoints are behind a session cookie obtained from
   ``POST /auth/login``. A browser cannot hold that cookie — it is issued for a
   different origin and the login is a form POST with a shared credential we are not
   putting in front-end JavaScript.
2. **CORS.** The gateway sends no ``Access-Control-Allow-Origin``, so ``fetch`` of the
   playlist from the console's origin fails before it starts. hls.js needs ``fetch``.
3. **User-Agent.** The gateway 403s any request without a browser-shaped UA
   (``settings.stream_user_agent``).

What it deliberately is *not*
-----------------------------
It is not storage. Bytes pass through and are never written to disk or to MinIO.
That is Model 2's architectural promise: the departmental system keeps its own video,
and this platform centralises *metadata* — the relay exists so an operator can watch a
feed, not so we can keep one.

SSRF
----
A URL-taking proxy inside a police network is an SSRF gateway to every internal
service unless the target set is closed. This one does not accept URLs at all.

Playlists are fetched from an address built server-side out of
``settings.sentinel_base_url`` and the camera's ``external_id`` read from the database.
While rewriting a playlist, each segment and key URI is resolved, checked to be on the
gateway's own scheme+host+port, and replaced with an opaque HMAC token; the token→URL
map is the *only* way a URL can be reached. A token the relay did not mint resolves to
nothing, so there is no client-supplied string anywhere on the path to the HTTP client
— not a host, not a path, not a query.

The token is also scoped to the camera it was minted for, so it cannot be replayed
against a different camera's URL space to sidestep a per-camera authorisation check.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse, StreamingResponse

from backend.app.common import resolve_camera
from backend.app.config import settings
from backend.app.security import Operator, current_operator

log = logging.getLogger("setu.api.stream")
router = APIRouter(prefix="/stream", tags=["stream"])

# One process-lifetime secret. Tokens must not survive a restart: they are handles
# into an in-memory map that does not either, and a token minted by a previous
# process would resolve to nothing anyway.
_TOKEN_SECRET = secrets.token_bytes(32)

# Tokens carry their own URL and are signed; there is no table to look them up in.
#
# There used to be one — a bounded LRU of 8,192 entries, sized by a comment reading
# "a live playlist window is a handful of segments". This gateway does not serve a
# live window. It serves `#EXT-X-PLAYLIST-TYPE:VOD` with 4,306 segments, so a single
# camera's playlist mints 4,307 tokens and a second camera evicts the first camera's
# oldest entries. The AES key is the first URI in a playlist, so it was the first
# token minted and the first one evicted: opening two cameras produced a player that
# fetched its playlist successfully and then 404ed on the key and the early segments,
# which reads as a dead camera rather than a full cache.
#
# Signing the URL into the token removes the failure mode rather than resizing it: a
# 30-camera estate would need 129,000 entries, and a second API process would not
# share them. A token now verifies arithmetically, so it cannot expire early, cannot
# be evicted, and works across processes — which is also what a deployment behind
# more than one worker needs.


def _origin(url: str) -> tuple[str, str, Optional[int]]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname or "", parts.port


def _gateway_origin() -> tuple[str, str, Optional[int]]:
    return _origin(settings.sentinel_base_url)


def _same_origin(url: str) -> bool:
    """Scheme, host *and* port must match the configured gateway.

    Host alone is not enough: ``https://gw:443`` and ``http://gw:8080`` are different
    services, and on an internal network the second one is often an admin panel.
    """
    want = _gateway_origin()
    got = _origin(url)
    want_port = want[2] or (443 if want[0] == "https" else 80)
    got_port = got[2] or (443 if got[0] == "https" else 80)
    return got[0] == want[0] and got[1].lower() == want[1].lower() and got_port == want_port


def _mint(url: str, camera: str) -> str:
    """A signed, self-contained handle for one upstream URL.

    The camera is signed in but not carried: a token minted while rewriting one
    camera's playlist cannot be replayed against another camera's endpoint, and
    tampering with the URL invalidates the signature. Since only this process can
    mint, a token that verifies is one this process built from a URL that already
    passed `_same_origin` — so redeeming it cannot be turned into a fetch of
    somewhere else.
    """
    payload = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    sig = hmac.new(_TOKEN_SECRET, f"{camera}\x00{url}".encode(), hashlib.sha256).digest()[:16]
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def _unpad(b64: str) -> bytes:
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))


def _redeem(token: str, camera: str) -> str:
    try:
        payload, sig = token.rsplit(".", 1)
        url = _unpad(payload).decode()
        presented = _unpad(sig)
    except Exception:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Malformed stream token") from None

    expected = hmac.new(_TOKEN_SECRET, f"{camera}\x00{url}".encode(), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(presented, expected):
        # Covers a tampered URL, a token minted for another camera, and a token
        # from a previous process — all of which are the client's cue to re-read
        # the playlist rather than a reason to fetch anything.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Stream token is not valid here")

    # Belt and braces. The signature already implies this URL passed `_same_origin`
    # when it was minted, but the gateway origin is configuration and configuration
    # changes; re-checking costs a string compare and keeps the guarantee local to
    # the function that acts on it.
    if not _same_origin(url):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token does not address the gateway")
    return url


# ── Gateway session ─────────────────────────────────────────────────────────
# ml.auth owns the login. It knows the one subtle thing about this gateway — the
# cookie arrives in the Set-Cookie of a 302, so redirects must not be followed —
# and the worker already depends on that behaviour. A second implementation here
# would be a second thing to get wrong.

_cookie: Optional[str] = None
_cookie_at: float = 0.0
_cookie_lock = asyncio.Lock()
_COOKIE_TTL = 1800.0

# How long to stop trying after a login the gateway refused. See _session_cookie: this
# is the difference between one failed login per minute and one per relayed request.
_LOGIN_COOLDOWN_S = 60.0
# How long the relay is willing to wait for that login. ml.auth allows 30 s and retries
# three times, which is right for the batch worker that owns it — a worker can wait half
# a minute to start a shift. An operator opening a camera tile cannot, and the wait was
# measured at 15.7 s on this sandbox. Bounded here rather than in ml.auth so the two
# callers keep the deadline each of them can actually afford.
_LOGIN_TIMEOUT_S = 8.0
_login_failed_at: float = 0.0
_login_error: str = ""


async def _session_cookie(force: bool = False) -> Optional[str]:
    """The gateway cookie, cached with a TTL and refreshable on demand.

    A TTL as well as on-401 refresh, because the failure this gateway actually
    produces on an expired cookie is a 200 carrying an HTML login page rather than a
    401 — which hls.js would faithfully hand to the demuxer.
    """
    global _cookie, _cookie_at, _login_failed_at, _login_error
    async with _cookie_lock:
        now = time.time()
        fresh = _cookie is not None and (now - _cookie_at) < _COOKIE_TTL
        if fresh and not force:
            return _cookie

        # Negative cache, and it is load-bearing. The failure path leaves `_cookie`
        # None, so without a cooldown the freshness check above can never short-circuit
        # and *every* relayed request pays a full login round-trip. One player polling
        # one live playlist is a login every few seconds; a 30-tile wall is a login
        # storm aimed at the one endpoint whose rate limiting is the likeliest reason it
        # started refusing us. Measured on this sandbox: login latency escalated 7.5s ->
        # 21.7s under exactly that load while still returning 403. The relay must fail
        # fast and quietly instead of making a throttled gateway into a stalled console.
        if _cookie is None and (now - _login_failed_at) < _LOGIN_COOLDOWN_S:
            return None

        try:
            from ml.auth import get_cookie
            _cookie = await asyncio.wait_for(
                asyncio.to_thread(get_cookie, refresh=True), timeout=_LOGIN_TIMEOUT_S,
            )
            _cookie_at = now
            _login_failed_at, _login_error = 0.0, ""
        except Exception as exc:
            # Logged once per cooldown rather than once per request, so the reason stays
            # findable instead of being buried under its own repetitions.
            log.warning("gateway login failed (%s) — relaying without a cookie for %.0fs",
                        exc, _LOGIN_COOLDOWN_S)
            _cookie, _cookie_at = None, now
            # A bare `TimeoutError: ` tells an operator nothing, and the timeout is the
            # branch most likely to be the one they hit.
            detail = (f"gateway login did not answer within {_LOGIN_TIMEOUT_S:.0f}s"
                      if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                      else f"{type(exc).__name__}: {exc}")
            _login_failed_at, _login_error = now, detail[:200]
        return _cookie


def _headers(cookie: Optional[str]) -> dict:
    h = {"User-Agent": settings.stream_user_agent, "Accept": "*/*"}
    if cookie:
        h["Cookie"] = f"sentinel={cookie}"
    return h


_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    """One pooled client for the whole relay.

    ``follow_redirects=False`` is a security property, not a preference: a followed
    redirect is a target chosen by the upstream server, which is exactly the hole the
    origin check above closes.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.stream_relay_timeout_s, connect=5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _looks_like_login(body: bytes, content_type: str) -> bool:
    return "html" in content_type.lower() or body[:200].lstrip().lower().startswith(b"<!doctype html")


# ── Playlist rewriting ──────────────────────────────────────────────────────

def _rewrite(playlist: str, base_url: str, camera: str, prefix: str) -> tuple[str, dict]:
    """Point every URI in a playlist back at this relay.

    Resolution follows the HLS rule — every URI is resolved against the playlist's own
    URL — rather than string-concatenating a base. That is what makes absolute,
    root-relative (``/key/…``, which is the shape this gateway's key URI takes) and
    plain relative segment names all come out right from one code path.

    A URI that resolves off the gateway's origin is dropped rather than rewritten. It
    is not something this relay is willing to fetch, and emitting it unchanged would
    hand the browser a cross-origin request that fails without CORS anyway.
    """
    out: list[str] = []
    stats = {"segments": 0, "keys": 0, "variants": 0, "rejected": 0}
    expect_variant = False

    for raw in playlist.splitlines():
        line = raw.strip()

        if line.startswith("#"):
            # Tags carrying a quoted URI= attribute: EXT-X-KEY (AES key),
            # EXT-X-MAP (fMP4 init segment), EXT-X-SESSION-KEY.
            if 'URI="' in line and line.split(":", 1)[0] in (
                "#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-SESSION-KEY",
            ):
                head, rest = line.split('URI="', 1)
                uri, tail = rest.split('"', 1)
                absolute = urljoin(base_url, uri)
                if not _same_origin(absolute):
                    stats["rejected"] += 1
                    out.append(line)
                    continue
                kind = "key" if "KEY" in head else "seg"
                token = _mint(absolute, camera)
                out.append(f'{head}URI="{prefix}/{kind}/{token}"{tail}')
                stats["keys" if kind == "key" else "segments"] += 1
                continue
            if line.startswith("#EXT-X-STREAM-INF") or line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
                expect_variant = True
            out.append(line)
            continue

        if not line:
            out.append(raw)
            continue

        absolute = urljoin(base_url, line)
        if not _same_origin(absolute):
            stats["rejected"] += 1
            expect_variant = False
            continue

        # A variant URI is another playlist, and a playlist served through the segment
        # endpoint would reach the browser with its own URIs unrewritten — i.e. it
        # would not play. It gets the playlist endpoint so it is rewritten in turn.
        kind = "playlist" if expect_variant else "seg"
        out.append(f"{prefix}/{kind}/{_mint(absolute, camera)}")
        stats["variants" if expect_variant else "segments"] += 1
        expect_variant = False

    return "\n".join(out) + "\n", stats


_PLAYLIST_HEADERS = {
    # A live playlist that gets cached is a player stuck on a window that has moved on.
    "Cache-Control": "no-store, no-transform",
    "Access-Control-Allow-Origin": "*",
}
_SEGMENT_HEADERS = {
    # Segment names are content addresses in this gateway's scheme, so a segment is
    # safe to cache hard. It is also the difference between one fetch and one fetch
    # per player on the same camera.
    "Cache-Control": "public, max-age=30",
    "Access-Control-Allow-Origin": "*",
}


def _relay_prefix(request: Request, camera_ref: str) -> str:
    """Root-relative base for this camera's relay URLs, e.g. ``/api/stream/cam04``.

    From ``url_for`` rather than a hardcoded string so the ``/api`` mount point lives in
    one place (``main.py``), and *path only* rather than the absolute URL ``url_for``
    returns: behind a reverse proxy that terminates TLS, the scheme in an absolute URL
    is whatever this process was spoken to on, and a playlist full of ``http://`` URIs
    is a page of mixed-content errors. A relative URI cannot be wrong about the scheme.
    """
    return urlsplit(str(request.url_for("segment", camera_ref=camera_ref, token="_"))).path.rsplit("/", 2)[0]


def _needs_auth(r: httpx.Response, *, body: bytes = b"") -> bool:
    """Does this response mean "log in again" rather than "here is your video"?

    Three shapes, because this gateway uses all three: a 401/403, a 3xx to the login
    page (``follow_redirects=False``, so a redirect arrives here rather than being
    chased), and — the one that catches people out — a 200 carrying the sign-in HTML,
    which a player would hand straight to the demuxer.

    ``body`` is optional so this is usable on a *streamed* response, whose content
    cannot be read without consuming the stream the caller is about to forward. The
    content-type alone settles the streaming case: a media segment is not text/html.
    """
    if r.status_code in (401, 403) or 300 <= r.status_code < 400:
        return True
    return r.status_code == 200 and _looks_like_login(body, r.headers.get("content-type", ""))


async def _fetch(url: str) -> httpx.Response:
    """GET with the gateway session, retrying once on an expired cookie.

    On the short timeout, not the relay's full one. A *live* playlist describes a
    window of a few seconds; a fetch that takes twenty of them has not been slow, it
    has been answering a question about a moment that is gone. hls.js has given up and
    the operator is watching a spinner either way, so failing in six seconds with a
    stated reason beats stalling a tile for twenty with none. Segments keep the long
    timeout — they are hundreds of kilobytes and worth waiting for.
    """
    cookie = await _session_cookie()
    timeout = httpx.Timeout(settings.stream_playlist_timeout_s, connect=5.0)
    r = await _http().get(url, headers=_headers(cookie), timeout=timeout)
    if _needs_auth(r, body=r.content):
        refreshed = await _session_cookie(force=True)
        # Only retry if the credential actually changed. Inside the login cooldown the
        # refresh hands back what we already had — usually None — and re-issuing the
        # same GET with the same headers does nothing but double the latency of a
        # failure the operator is waiting on.
        if refreshed != cookie:
            r = await _http().get(url, headers=_headers(refreshed), timeout=timeout)
    return r


def _propagate_query_token(playlist: str, request: Request) -> str:
    """Append this request's ``access_token`` to the URLs the playlist points at.

    Only for the native-HLS case. Safari's media element fetches the AES key and
    every segment itself, with no hook to set a header, so a playlist whose URLs
    carry no credential yields a 401 on each one — a black player and no error.
    hls.js never reaches this: it sends a header and `request.query_params` is
    empty, so the playlist is returned untouched.
    """
    token = request.query_params.get("access_token")
    if not token:
        return playlist
    from urllib.parse import quote
    q = f"access_token={quote(token, safe='')}"
    out: list[str] = []
    for line in playlist.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY") and 'URI="' in stripped:
            head, _, rest = stripped.partition('URI="')
            uri, _, tail = rest.partition('"')
            sep = "&" if "?" in uri else "?"
            out.append(f'{head}URI="{uri}{sep}{q}"{tail}')
        elif stripped and not stripped.startswith("#"):
            sep = "&" if "?" in stripped else "?"
            out.append(f"{stripped}{sep}{q}")
        else:
            out.append(line)
    return "\n".join(out)


@router.get("/{camera_ref}/index.m3u8", response_class=PlainTextResponse)
async def playlist(
    camera_ref: str,
    request: Request,
    _: Operator = Depends(current_operator),
) -> Response:
    """The relayed media playlist. This is the URL the player is given."""
    camera = await resolve_camera(camera_ref)
    external = camera["external_id"]
    upstream = f"{settings.sentinel_base_url.rstrip('/')}/{external}/index.m3u8"

    try:
        r = await _fetch(upstream)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Camera gateway unreachable: {type(exc).__name__}") from exc
    # Checked before the status code, because the shape that needs saying most loudly
    # arrives as a 302 or even a 200: without this the login page is rewritten into an
    # empty playlist and the operator is told "no playable segments" about a camera that
    # is fine, when what actually happened is that this service cannot log in.
    if _needs_auth(r):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Camera gateway did not accept this service's session ({r.status_code})"
            + (f": {_login_error}" if _login_error else ""),
        )
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Camera gateway returned {r.status_code}")

    body, stats = _rewrite(r.text, upstream, external, _relay_prefix(request, camera_ref))
    if not stats["segments"] and not stats["variants"]:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Gateway playlist contained no playable segments")
    # A player that authenticated this request with a query token cannot
    # authenticate the URLs inside the playlist any other way — a media element
    # fetching its own key and segments has nowhere to put a header. So the
    # token is carried onto them. Only when the caller used one: hls.js sends a
    # header and its playlist stays clean.
    body = _propagate_query_token(body, request)
    return PlainTextResponse(
        body,
        media_type="application/vnd.apple.mpegurl",
        headers={**_PLAYLIST_HEADERS, "X-Setu-Segments": str(stats["segments"])},
    )


@router.get("/{camera_ref}/playlist/{token}", response_class=PlainTextResponse, name="nested_playlist")
async def nested_playlist(
    camera_ref: str,
    token: str,
    request: Request,
    _: Operator = Depends(current_operator),
) -> Response:
    """A variant playlist reached from a master. Rewritten the same way."""
    camera = await resolve_camera(camera_ref)
    url = _redeem(token, camera["external_id"])
    try:
        r = await _fetch(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, type(exc).__name__) from exc
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Gateway returned {r.status_code}")
    body, _stats = _rewrite(r.text, url, camera["external_id"],
                            _relay_prefix(request, camera_ref))
    return PlainTextResponse(body, media_type="application/vnd.apple.mpegurl",
                             headers=_PLAYLIST_HEADERS)


@router.get("/{camera_ref}/seg/{token}", name="segment")
async def segment(
    camera_ref: str,
    token: str,
    _: Operator = Depends(current_operator),
) -> Response:
    """One media segment, streamed through.

    Streamed rather than buffered: a segment is a few hundred kilobytes and there may
    be a dozen players open, and holding all of that in the event loop's memory to
    hand it on unchanged is pure overhead. Nothing here decrypts — the browser has the
    key (relayed below) and hls.js does AES-128 natively, so the ciphertext passes
    through untouched and this process never holds decoded video.
    """
    camera = await resolve_camera(camera_ref)
    url = _redeem(token, camera["external_id"])
    cookie = await _session_cookie()
    client = _http()
    req = client.build_request("GET", url, headers=_headers(cookie))
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, type(exc).__name__) from exc
    # Checked here too, and this is the call site where it matters most: every other
    # upstream response is parsed before it is forwarded, so there a login page merely
    # becomes an empty playlist. A segment is passed through untouched — without this
    # the sign-in HTML reaches the demuxer as MPEG-TS and the player reports a codec
    # error for what is actually an expired session.
    if _needs_auth(upstream) or upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Gateway returned {upstream.status_code}")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()

    headers = dict(_SEGMENT_HEADERS)
    if "content-length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["content-length"]
    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "video/mp2t"),
        headers=headers,
    )


@router.get("/{camera_ref}/key/{token}")
async def decryption_key(
    camera_ref: str,
    token: str,
    _: Operator = Depends(current_operator),
) -> Response:
    """The AES-128 key for an encrypted stream.

    Relaying the key is what lets the browser decrypt in the player rather than making
    this process decode and re-encode video. It is the same trust boundary as the
    segments themselves — anyone entitled to watch the feed is entitled to the key for
    it — and it stays behind this API's authentication, which is more than the gateway's
    own cookie offers.
    """
    camera = await resolve_camera(camera_ref)
    url = _redeem(token, camera["external_id"])
    try:
        r = await _fetch(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, type(exc).__name__) from exc
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Gateway returned {r.status_code}")
    return Response(
        content=r.content,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, max-age=300", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/{camera_ref}/probe")
async def probe(
    camera_ref: str,
    request: Request,
    _: Operator = Depends(current_operator),
) -> dict:
    """Is this camera actually watchable right now?

    Separate from ``/api/cameras`` health because the two answer different questions:
    that one reports whether the pipeline has seen frames recently, this one reports
    whether the gateway will serve a playlist to *this* process at this moment. A
    camera can be perfectly healthy in the index and unwatchable because the session
    expired.
    """
    camera = await resolve_camera(camera_ref)
    upstream = f"{settings.sentinel_base_url.rstrip('/')}/{camera['external_id']}/index.m3u8"
    started = time.monotonic()
    try:
        r = await _fetch(upstream)
    except httpx.HTTPError as exc:
        # Same shape as the success path. A client that reads `reason` to decide what to
        # show must not get a differently-keyed object on the one branch it most needs
        # to render — a timeout is exactly when the tile has something to say.
        return {
            "camera": camera["external_id"], "playable": False, "reason": "unreachable",
            "detail": type(exc).__name__, "authenticated": _cookie is not None,
            "status": None, "segments": 0, "encrypted": False,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "relay_url": _relay_prefix(request, camera_ref) + "/index.m3u8",
        }
    needs_auth = _needs_auth(r)
    segments = 0 if (needs_auth or r.status_code >= 400) else sum(
        1 for line in r.text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    # "playable: false" on its own sends an operator to look at a camera when the fault
    # is this service's session, so the reason is named. The three cases are genuinely
    # different jobs: fix the gateway credential, fix the camera, or wait for the
    # gateway to refill a window it is currently serving empty.
    if needs_auth:
        reason = "gateway_auth"
    elif r.status_code >= 400:
        reason = "gateway_error"
    elif segments == 0:
        reason = "no_segments"
    else:
        reason = None
    return {
        "camera": camera["external_id"],
        "playable": reason is None,
        "reason": reason,
        "detail": _login_error if reason == "gateway_auth" and _login_error else None,
        "authenticated": _cookie is not None,
        "status": r.status_code,
        "segments": segments,
        "encrypted": "#EXT-X-KEY" in r.text,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "relay_url": _relay_prefix(request, camera_ref) + "/index.m3u8",
    }
