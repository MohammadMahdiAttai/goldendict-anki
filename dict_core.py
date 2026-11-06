#!/usr/bin/env python3
"""
goldendict-anki core, shared by the GoldenDict-NG program dictionaries.

    dict_full.py       Persian translation, English entry and pronunciation
    dict_eng_only.py   English entry and pronunciation

GoldenDict runs one of these scripts for each lookup and displays what it prints on
stdout. The script always prints something within TOTAL_BUDGET seconds, whatever the
network does. Several services can supply each part, and backup services are only
asked when the first choices fail. When something cannot be fetched the output says
so in a NOTE line, and the details of every request go to network.log, next to the
cache file.

Options, for a terminal:
    --check    test every service over every connection route and show the results
    --debug    write requests and errors to stderr

Standard library only. Python 3.8 or newer.
"""

import contextlib
import hashlib
import html as htmllib
import http.client
import json
import logging
import os
import platform
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Settings ───────────────────────────────────────────────────────────────
ENABLE_TRANSLATION = True        # Persian translation (dict_full.py)
ENABLE_DEFINITIONS = True        # English entry
ENABLE_AUDIO = True              # the Google voice button
RECORDING_BUTTON = False         # also a Recording button (dictionaryapi.dev's MP3, when it has one)
ENABLE_CACHE = True              # keep results on disk
TARGET_LANG = "fa"               # language code of the translation
SHOW_SOURCE_NOTES = False        # also write NOTE lines saying which service supplied a part (they go to network.log anyway)
DISABLED_SERVICES = ()           # services never used, e.g. ("mymemory", "datamuse")
MYMEMORY_EMAIL = ""              # optional: an e-mail address raises MyMemory's daily limit
PROXY = ""                       # optional: "http://127.0.0.1:10809" or "socks5://127.0.0.1:10808"
AUTO_PROXY = True                # look for a VPN app's local proxy port on 127.0.0.1
# Common default ports of popular VPN and proxy apps. Only 127.0.0.1 is ever probed.
LOCAL_PROXY_PORTS = (10808, 10809, 7890, 7891, 7897, 2080, 1080, 1089, 8889, 12334, 20170, 20171, 9150)
TOTAL_BUDGET = 15.0              # seconds: the script always answers within this time
HEDGE_DELAY = 2.0                # seconds before a backup service is asked, if the first choice is silent
OPTIONAL_GRACE = 4.0             # seconds in all that a better service gets once a lesser answer is in hand
EXTRA_GRACE = 2.0                # the same, for a service asked only to add an extra (phonetic spelling, examples)
SHORT_GRACE = 0.5                # the same, for a service that did not answer in recent lookups
DOWN_WINDOW = 600.0              # seconds a service counts as "not answering" after it failed
COOLDOWN_BASE = 600.0            # seconds a service that answered HTTP 429 or 403 is left alone ...
COOLDOWN_MAX = 3600.0            # ... doubling each time it does so again, up to this
ROUTE_TIMEOUT = 8.0              # seconds one network operation on a route may take
ROUTE_STAGGER = 1.2              # seconds before the next route starts alongside the earlier ones
MAX_DICT_WORDS = 2               # English entries are looked up for this many words or fewer
MAX_TRANSLATE_CHARS = 500
MYMEMORY_MAX_BYTES = 450         # MyMemory refuses longer text
MAX_POS_GROUPS = 4               # parts of speech shown
MAX_DEFS_PER_POS = 3             # definitions shown per part of speech
MAX_SYNONYMS = 8
MAX_EXAMPLES = 3
CACHE_TTL = 30 * 24 * 3600       # seconds a result stays fresh
BACKUP_TTL = 24 * 3600           # the same for a backup service's result, so a better service can take over
NEGATIVE_CACHE_TTL = 24 * 3600   # seconds a "not found" answer stays fresh
STALE_TTL = 365 * 24 * 3600      # seconds a saved result may still be used when the network fails
CACHE_MAX_ENTRIES = 5000
CACHE_VERSION = 5                # bump when the cached data layout changes
LOG_BLOCKS = 20                  # lookups kept in network.log
LOCK_WAIT = 1.0                  # seconds to wait for another script that is saving the cache file
LOCK_STALE = 10.0                # a lock older than this was left behind by a script that died
LOOKUP_WAIT = 8.0                # seconds a script waits for the other program that is looking up the same word
LOOKUP_LOCK_STALE = 20.0         # a word lock older than this was left behind by a script that died

TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
TTS_URL = "https://translate.google.com/translate_tts"
DICTIONARY_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{}"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
DATAMUSE_URL = "https://api.datamuse.com/words"
WIKTIONARY_URL = "https://en.wiktionary.org/api/rest_v1/page/definition/{}"
USER_AGENT = "Mozilla/5.0 (goldendict-anki)"

# Trimmed from both ends of a query: quotes, brackets, stray punctuation.
_EDGE_PUNCTUATION = " \t\r\n\"'“”‘’«»()[]{}<>.,;:!?¡¿…-–—_*`~|/\\"
_FLAGS = {
    "--no-translate": ("translate", False),
    "--no-define": ("define", False),
    "--no-cache": ("cache", False),
    "--debug": ("debug", True),
    "--check": ("check", True),
}
_POS_STYLE = (
    "color:#d2a8ff; font-size:11px; font-weight:bold; letter-spacing:1px; "
    "background:rgba(210,168,255,0.1); padding:3px 12px; border-radius:20px; "
    "border:1px solid rgba(210,168,255,0.3);"
)
LABELS = {
    "google": "Google",
    "dictionaryapi": "dictionaryapi.dev",
    "mymemory": "MyMemory",
    "wiktionary": "Wiktionary",
    "datamuse": "Datamuse",
}
# The best service first. A lesser answer is shown only when the better ones fail.
RANK = {
    "translation": ("google", "mymemory"),
    "entry": ("dictionaryapi", "google", "wiktionary", "datamuse"),
    # extras that an entry may lack, and the services that can supply them
    "phonetic": ("dictionaryapi", "datamuse"),
    "examples": ("dictionaryapi", "google", "wiktionary"),
    "synonyms": ("dictionaryapi", "google"),
}
COOLDOWN_REASONS = ("HTTP 429", "HTTP 403")
BACKUP_SERVICES = ("mymemory", "wiktionary", "datamuse")


class NotFound(Exception):
    """The service answered 404: the word is not in that dictionary."""


class RouteFailure(Exception):
    """No connection route got an answer.

    reason is short ('timeout', 'connection failed', 'HTTP 429', ...). detail lists
    what each route reported. retry_after is the server's Retry-After in seconds, if any.
    """

    def __init__(self, reason, detail="", retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.retry_after = retry_after


# ── Small helpers ──────────────────────────────────────────────────────────
def esc(value):
    return htmllib.escape(value, quote=True)


def clean_text(value):
    """Collapse whitespace. Anything that is not a string becomes ''."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def strip_tags(value):
    return re.sub(r"<[^>]+>", "", value)


def html_text(value):
    """The visible text of a snippet of HTML."""
    return clean_text(htmllib.unescape(strip_tags(value))) if isinstance(value, str) else ""


def as_list(value):
    return value if isinstance(value, list) else []


def has_letters(text):
    return any(ch.isalpha() for ch in text)


def has_latin(text):
    return re.search(r"[A-Za-z]", text) is not None


def safe_url(value):
    """The value as an http(s) address, or '' if it is anything else."""
    url = clean_text(value)
    if url.startswith("//"):
        url = "https:" + url
    return url if url.lower().startswith(("https://", "http://")) else ""


def normalize_query(raw):
    # Invalid UTF-8 in argv can leave lone surrogates; replace them so that
    # encoding the output later cannot fail.
    text = raw.encode("utf-8", "replace").decode("utf-8")
    return " ".join(text.split()).strip(_EDGE_PUNCTUATION)


def add_unique(items, value, limit):
    if value and value not in items and len(items) < limit:
        items.append(value)


def mask_credentials(value):
    """Hide a user name and password inside an address such as http://user:pass@host:port."""
    return re.sub(r"//[^/@\s]*@", "//***@", value)


def parse_retry_after(value):
    """Seconds from a Retry-After header (the date form is ignored), or None."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def describe_error(exc):
    """A short reason for a NOTE line."""
    if isinstance(exc, RouteFailure):
        return exc.reason
    if isinstance(exc, NotFound):
        return "not found"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "timeout" if "timed out" in str(exc) else "TLS error"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLError):
        return "TLS error"
    if isinstance(exc, (OSError, urllib.error.URLError)):
        return "connection failed"
    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)):
        return "unexpected response"
    return "error"


def reason_text(reason):
    """A reason as it is shown on the card."""
    known = {
        "HTTP 429": "rate limited (HTTP 429)",
        "HTTP 403": "refused (HTTP 403)",
        "cooling down": "skipped, it limited the requests recently",
    }
    if reason in known:
        return known[reason]
    return f"server error ({reason})" if reason.startswith("HTTP 5") else reason


def parse_argv(argv, translate=ENABLE_TRANSLATION):
    """Consume leading known flags. Everything after them is the query."""
    opts = {
        "translate": translate,
        "define": ENABLE_DEFINITIONS,
        "cache": ENABLE_CACHE,
        "debug": False,
        "check": False,
    }
    args = list(argv)
    while args:
        if args[0] == "--":
            args.pop(0)
            break
        if args[0] not in _FLAGS:
            break
        name, value = _FLAGS[args.pop(0)]
        opts[name] = value
    return opts, " ".join(args)


# ── Connection routes ──────────────────────────────────────────────────────
def _bracket(host):
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("the proxy closed the connection")
        data += chunk
    return data


def _socks5_connect(proxy, host, port, timeout):
    """A socket connected to host:port through a SOCKS5 proxy (no login). The proxy
    resolves the host name, so a blocked or poisoned local DNS does not matter."""
    sock = socket.create_connection(proxy, timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise OSError("not a SOCKS5 proxy, or it wants a login")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        head = _recv_exact(sock, 4)
        if head[0] != 5 or head[1] != 0:
            raise OSError(f"the SOCKS5 proxy refused the connection (code {head[1]})")
        if head[3] == 1:
            _recv_exact(sock, 4 + 2)
        elif head[3] == 4:
            _recv_exact(sock, 16 + 2)
        elif head[3] == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0] + 2)
        else:
            raise OSError("unexpected SOCKS5 reply")
        return sock
    except BaseException:
        sock.close()
        raise


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that only tries IPv4 addresses.

    On some networks IPv6 connections are accepted but never answered, and a
    normal connection then waits out the whole timeout before trying IPv4.
    """

    def connect(self):
        error = None
        for family, socktype, proto, _, address in socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        ):
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(address)
            except OSError as exc:
                sock.close()
                error = exc
                continue
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise error or OSError(f"no IPv4 address for {self.host}")


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context=None):
        super().__init__(context=context)

    def https_open(self, request):
        return self.do_open(_IPv4HTTPSConnection, request, context=self._context)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that goes through a SOCKS5 proxy."""

    def __init__(self, host, port=None, socks=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self._socks = socks

    def connect(self):
        sock = _socks5_connect(self._socks, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, socks, context=None):
        super().__init__(context=context)
        self._socks = socks

    def https_open(self, request):
        return self.do_open(_SocksHTTPSConnection, request, context=self._context, socks=self._socks)


def _opener_direct(context=None):
    """Connect straight to the service, ignoring any proxy settings."""
    handlers = [urllib.request.ProxyHandler({})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _opener_ipv4(context=None):
    """Connect straight to the service, over IPv4 only."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _IPv4HTTPSHandler(context))


def _opener_http_proxy(host, port, context=None):
    """Connect through an HTTP proxy (CONNECT)."""
    address = f"http://{_bracket(host)}:{port}"
    handlers = [urllib.request.ProxyHandler({"http": address, "https": address})]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _opener_socks5(host, port, context=None):
    """Connect through a SOCKS5 proxy."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SocksHTTPSHandler((host, port), context)
    )


def parse_proxy(value):
    """(kind, host, port) for an HTTP or SOCKS proxy address, or None.

    Accepts 'host:port', 'http://host:port' and 'socks5://host:port'. Every SOCKS
    version is treated as SOCKS5, which is what current VPN apps speak.
    """
    text = clean_text(value)
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError:
        return None
    if not host or not port:
        return None
    if scheme == "http":
        return "http", host, port
    if scheme.startswith("socks"):
        return "socks5", host, port
    return None


def probe_local_port(port, timeout=0.4):
    """'socks5' or 'http' for a program listening on 127.0.0.1:port, else None."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        try:
            sock.sendall(b"\x05\x01\x00")  # a SOCKS5 greeting; an HTTP proxy ignores or rejects it
            reply = sock.recv(2)
        except OSError:  # includes the read timeout of an HTTP proxy that waits for more
            reply = b""
        return "socks5" if reply[:1] == b"\x05" else "http"
    finally:
        sock.close()


def discover_local_proxies(ports=None, budget=0.6):
    """Addresses such as 'socks5://127.0.0.1:10808' of local programs that look like
    a proxy. A VPN app in proxy mode usually opens one. Closed ports cost nothing."""
    ports = tuple(LOCAL_PROXY_PORTS if ports is None else ports)
    found = {}
    lock = threading.Lock()

    def check(port):
        kind = probe_local_port(port)
        if kind:
            with lock:
                found[port] = kind

    threads = [threading.Thread(target=check, args=(port,), daemon=True) for port in ports]
    for thread in threads:
        thread.start()
    limit = time.monotonic() + budget
    for thread in threads:
        thread.join(max(0.0, limit - time.monotonic()))
    with lock:
        snapshot = dict(found)
    return [f"{snapshot[port]}://127.0.0.1:{port}" for port in ports if port in snapshot]


class Route:
    """A way to reach the internet: a name and a function that builds an opener.

    Routes of one group leave through the same address (a direct connection over
    IPv4 or IPv6, say), so a server that has refused one will refuse the others too.
    """

    def __init__(self, name, factory, group=None):
        self.name = name
        self.factory = factory
        self.group = group or name


class Network:
    """Sends requests over several routes. The first answer wins.

    Routes start one after another, a moment apart, so a route that hangs never
    holds up one that works. The route that worked is remembered for the next run.
    A route that gets an HTTP refusal (429, 403, ...) is not repeated over the same
    exit address: that only makes a rate limit worse.
    """

    def __init__(self, meta=None, context=None):
        self.meta = meta if meta is not None else {}
        self.context = context  # only tests pass one: a TLS context that trusts a test certificate
        self.lines = []         # diagnostic lines for network.log
        self._lock = threading.Lock()
        self._discovery_lock = threading.Lock()
        self._discovered = None
        self._started = time.monotonic()

    def note(self, text):
        line = f"{time.monotonic() - self._started:5.1f}s  {text}"
        with self._lock:
            self.lines.append(line)
        logging.debug(text)

    # -- the routes
    def _direct(self):
        return Route("direct", lambda: _opener_direct(self.context), group="direct")

    def _ipv4(self):
        return Route("ipv4", lambda: _opener_ipv4(self.context), group="direct")

    def _proxy(self, kind, host, port):
        name = f"{kind}://{_bracket(host)}:{port}"
        if kind == "socks5":
            return Route(name, lambda: _opener_socks5(host, port, self.context))
        return Route(name, lambda: _opener_http_proxy(host, port, self.context))

    def _route_named(self, name):
        if name == "direct":
            return self._direct()
        if name == "ipv4":
            return self._ipv4()
        parsed = parse_proxy(name)
        return self._proxy(*parsed) if parsed else None

    def configured_proxies(self):
        """Proxies named by PROXY or by the system settings, as (kind, host, port)."""
        if PROXY:
            candidates = [PROXY]
        else:
            try:
                system = urllib.request.getproxies()
            except Exception:
                system = {}
            candidates = [system.get("https"), system.get("http"), system.get("all")]
        found = []
        for candidate in candidates:
            parsed = parse_proxy(candidate)
            if parsed and parsed not in found:
                found.append(parsed)
        return found

    def discovered_proxies(self):
        """Local VPN proxies found by probing common ports (once per run)."""
        if not AUTO_PROXY:
            return []
        with self._discovery_lock:
            if self._discovered is None:
                parsed = (parse_proxy(url) for url in discover_local_proxies())
                self._discovered = [item for item in parsed if item]
                names = ", ".join(f"{kind}://{host}:{port}" for kind, host, port in self._discovered)
                self.note("local proxy ports found: " + (names or "none"))
            return list(self._discovered)

    def routes(self, service):
        """The routes in the order they should be tried: the one that worked last time,
        a direct connection, the configured proxies, local VPN proxies, then IPv4 only."""
        seen = set()

        def fresh(route):
            if route is None or route.name in seen:
                return None
            seen.add(route.name)
            return route

        remembered = self.meta.get("route")
        remembered = remembered.get(service) if isinstance(remembered, dict) else None
        if isinstance(remembered, str):
            route = fresh(self._route_named(remembered))
            if route:
                yield route
        route = fresh(self._direct())
        if route:
            yield route
        for parsed in self.configured_proxies():
            route = fresh(self._proxy(*parsed))
            if route:
                yield route
        for parsed in self.discovered_proxies():
            route = fresh(self._proxy(*parsed))
            if route:
                yield route
        route = fresh(self._ipv4())
        if route:
            yield route

    # -- the requests
    def _attempt(self, service, route, url, results):
        started = time.monotonic()
        retry_after = None
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with route.factory().open(request, timeout=ROUTE_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            outcome = ("ok", data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                outcome = ("notfound", None)
            else:
                outcome = ("error", f"HTTP {exc.code}")
                retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        except Exception as exc:  # timeouts, refused connections, bad JSON, ...
            outcome = ("error", describe_error(exc))
        seconds = time.monotonic() - started
        label = outcome[1] if outcome[0] == "error" else outcome[0]
        self.note(f"{service:<14}{route.name:<30}{label} ({seconds:.1f}s)")
        results.put((route.name, outcome[0], outcome[1], seconds, retry_after, route.group))

    def _race(self, service, url, deadline):
        """Start the routes one after another; the first answer wins.

        Returns ('ok', data), ('notfound', None) or
        ('failed', [(route, reason, retry_after), ...]).
        """
        results = queue.Queue()
        sequence = self.routes(service)
        started, failures, blocked = [], [], set()
        finished = 0
        exhausted = False
        next_start = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if not exhausted and now >= next_start:
                route = next(sequence, None)
                while route is not None and route.group in blocked:
                    route = next(sequence, None)  # the server already refused this exit address
                if route is None:
                    exhausted = True
                else:
                    started.append(route.name)
                    threading.Thread(
                        target=self._attempt, args=(service, route, url, results), daemon=True
                    ).start()
                    next_start = time.monotonic() + ROUTE_STAGGER
                continue
            if exhausted and finished >= len(started):
                return "failed", failures
            wait = deadline - now
            if not exhausted:
                wait = min(wait, next_start - now)
            try:
                name, kind, value, _, retry_after, group = results.get(timeout=max(wait, 0.0))
            except queue.Empty:
                continue
            finished += 1
            if kind == "ok":
                self._remember(service, name)
                return "ok", value
            if kind == "notfound":
                return "notfound", None
            failures.append((name, value, retry_after))
            if value.startswith("HTTP "):
                blocked.add(group)
            next_start = 0.0  # a route that failed lets the next one start at once
        answered = {name for name, _, _ in failures}
        failures.extend((name, "timeout", None) for name in started if name not in answered)
        if not failures:
            failures.append(("-", "timeout", None))
        return "failed", failures

    def _remember(self, service, name):
        routes = self.meta.setdefault("route", {})
        if isinstance(routes, dict):
            routes[service] = name

    def get_json(self, service, url, deadline):
        """The parsed JSON at url. Raises NotFound for a 404 and RouteFailure when no route answers."""
        for attempt in range(2):
            status, value = self._race(service, url, deadline)
            if status == "ok":
                return value
            if status == "notfound":
                raise NotFound(url)
            reasons = [reason for _, reason, _ in value]
            retry_after = max((after for _, _, after in value if after is not None), default=None)
            timed_out = "timeout" in reasons
            refused = any(reason.startswith("HTTP 4") for reason in reasons)
            detail = "; ".join(f"{name}: {reason}" for name, reason, _ in value)
            # A refusal (429 above all) must never be repeated. Only a quick network
            # failure or a server error (5xx) is worth one more try.
            if attempt == 0 and not timed_out and not refused and deadline - time.monotonic() > 4.0:
                self.note(f"{service}: every route failed quickly ({detail}); trying once more")
                time.sleep(0.5)
                continue
            reason = next((r for r in COOLDOWN_REASONS if r in reasons), None) or (
                "timeout" if timed_out else reasons[0]
            )
            self.note(f"{service}: no route answered ({detail})")
            raise RouteFailure(reason, detail, retry_after)
        raise RouteFailure("connection failed")  # not reached


# ── Services ───────────────────────────────────────────────────────────────
def google_url(text, with_dictionary):
    """One request to the public gtx endpoint: the translation and, for a single
    word, the English definitions, examples and synonyms."""
    params = [("client", "gtx"), ("sl", "auto"), ("tl", TARGET_LANG), ("hl", "en"), ("dt", "t")]
    if with_dictionary:
        params += [("dt", "md"), ("dt", "ex"), ("dt", "ss")]
    params.append(("q", text))
    return TRANSLATE_URL + "?" + urllib.parse.urlencode(params)


def mymemory_url(text):
    params = {"q": text, "langpair": f"en|{TARGET_LANG}"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL
    return MYMEMORY_URL + "?" + urllib.parse.urlencode(params)


def datamuse_url(word):
    # md=d: definitions, md=r with ipa=1: the pronunciation in IPA instead of ARPAbet
    return DATAMUSE_URL + "?" + urllib.parse.urlencode({"sp": word, "md": "dr", "ipa": 1, "max": 3})


def tts_url(word):
    return TTS_URL + "?" + urllib.parse.urlencode(
        {"ie": "UTF-8", "q": word, "tl": "en-US", "client": "tw-ob"}
    )


def _forms(text):
    """The spellings to look up, best first. A word whose only capital is its first letter
    is usually at the start of a sentence, so its lower-case form comes first; anything else
    with capitals (NASA, iPhone, New York) is looked up as typed first."""
    lower = text.lower()
    if text == lower:
        return [text]
    if text[:1].isupper() and text[1:] == lower[1:]:
        return [lower, text]
    return [text, lower]


# Every fetch function takes (text, word_mode, net, deadline) and returns
# {'translation': str or None, 'entry': dict or None}.
def fetch_google(text, word_mode, net, deadline):
    try:
        data = net.get_json("google", google_url(text, word_mode), deadline)
    except RouteFailure as exc:
        if not (word_mode and exc.reason == "HTTP 400"):
            raise
        # Google did not accept the extra fields: ask for the translation alone.
        data = net.get_json("google", google_url(text, False), deadline)
        word_mode = False
    return {
        "translation": parse_translation(data),
        "entry": google_entry(data) if word_mode else None,
    }


def fetch_dictionaryapi(text, word_mode, net, deadline):
    for form in _forms(text):
        try:
            data = net.get_json("dictionaryapi", DICTIONARY_URL.format(urllib.parse.quote(form, safe="")), deadline)
        except NotFound:
            continue
        entry = parse_definition(data)
        if entry:
            return {"translation": None, "entry": entry}
    return {"translation": None, "entry": None}


def fetch_wiktionary(text, word_mode, net, deadline):
    for form in _forms(text):
        try:
            data = net.get_json("wiktionary", WIKTIONARY_URL.format(urllib.parse.quote(form, safe="")), deadline)
        except NotFound:
            continue
        entry = parse_wiktionary(data)
        if entry:
            return {"translation": None, "entry": entry}
    return {"translation": None, "entry": None}


def fetch_datamuse(text, word_mode, net, deadline):
    data = net.get_json("datamuse", datamuse_url(text), deadline)
    return {"translation": None, "entry": parse_datamuse(data, text), "phonetic": datamuse_phonetic(data, text)}


def fetch_mymemory(text, word_mode, net, deadline):
    data = net.get_json("mymemory", mymemory_url(text), deadline)
    return {"translation": parse_mymemory(data, text), "entry": None}


PROVIDERS = {
    "google": fetch_google,
    "dictionaryapi": fetch_dictionaryapi,
    "mymemory": fetch_mymemory,
    "wiktionary": fetch_wiktionary,
    "datamuse": fetch_datamuse,
}


def capabilities(name, word_mode, need_translation):
    """What this service can supply in this lookup: 'translation', 'entry' and/or 'phonetic'."""
    caps = set()
    if need_translation and name in ("google", "mymemory"):
        caps.add("translation")
    if word_mode and name in ("google", "dictionaryapi", "wiktionary", "datamuse"):
        caps.add("entry")
    if word_mode and name in RANK["phonetic"]:
        caps.add("phonetic")
    if word_mode and name in RANK["examples"]:
        caps.add("examples")
    if word_mode and name in RANK["synonyms"]:
        caps.add("synonyms")
    return caps


def plan(text, word_mode, need_translation):
    """{service: tier} for this lookup. Tier 0 is asked at once; a higher tier only
    when the lower ones failed, stayed silent, or are known to be unreliable."""
    tiers = {}
    if need_translation or word_mode:
        # One Google request brings the translation and the dictionary data. Both programs
        # use it, so the English entry is the same in both.
        tiers["google"] = 0
    if need_translation and has_latin(text) and len(text.encode("utf-8")) <= MYMEMORY_MAX_BYTES:
        tiers["mymemory"] = 1
    if word_mode:
        tiers["dictionaryapi"] = 0
        tiers["wiktionary"] = 1
        tiers["datamuse"] = 1
    return {name: tier for name, tier in tiers.items() if name not in DISABLED_SERVICES}


# ── Parsing ────────────────────────────────────────────────────────────────
def parse_definition(data):
    """Turn a dictionaryapi.dev response into a compact dict, or None."""
    if not isinstance(data, list):
        return None
    phonetic, audio = "", ""
    groups, synonyms, examples = [], [], []
    for item in data:
        if not isinstance(item, dict):
            continue
        phonetic = phonetic or clean_text(item.get("phonetic"))
        for spelling in as_list(item.get("phonetics")):
            if not isinstance(spelling, dict):
                continue
            phonetic = phonetic or clean_text(spelling.get("text"))
            url = safe_url(spelling.get("audio"))
            # Prefer the American recording when there is a choice.
            if url and (not audio or ("-us" in url.lower() and "-us" not in audio.lower())):
                audio = url
        for meaning in as_list(item.get("meanings")):
            if not isinstance(meaning, dict):
                continue
            pos = clean_text(meaning.get("partOfSpeech"))
            group = next((g for g in groups if g["pos"] == pos), None)
            if group is None:
                if len(groups) >= MAX_POS_GROUPS:
                    continue
                group = {"pos": pos, "defs": []}
                groups.append(group)
            for word in as_list(meaning.get("synonyms")):
                add_unique(synonyms, clean_text(word), MAX_SYNONYMS)
            for definition in as_list(meaning.get("definitions")):
                if not isinstance(definition, dict):
                    continue
                add_unique(group["defs"], clean_text(definition.get("definition")), MAX_DEFS_PER_POS)
                add_unique(examples, clean_text(definition.get("example")), MAX_EXAMPLES)
                for word in as_list(definition.get("synonyms")):
                    add_unique(synonyms, clean_text(word), MAX_SYNONYMS)
    groups = [g for g in groups if g["defs"]]
    if not groups:
        return None
    return {
        "phonetic": phonetic,
        "audio": audio,
        "groups": groups,
        "synonyms": synonyms,
        "examples": examples,
    }


def parse_translation(data):
    """The translated text from a Google response ('' when the text is already Persian)."""
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise ValueError("unexpected translate response")
    pieces = [
        seg[0] for seg in data[0] if isinstance(seg, list) and seg and isinstance(seg[0], str)
    ]
    if len(data) > 2 and data[2] == TARGET_LANG:
        return ""
    return clean_text("".join(pieces))


def google_entry(data):
    """English entry from the extra fields of a Google response, or None.

    Layout of the public gtx endpoint with dt=md, dt=ex and dt=ss:
      data[12]  definitions: [part_of_speech, [[definition, ...], ...]], ...
      data[13]  examples:    [[[example_html, ...], ...]]
      data[11]  synonyms:    [part_of_speech, [[[word, ...], ...]]], ...
    """
    if not isinstance(data, list):
        return None
    groups = []
    for item in as_list(data[12] if len(data) > 12 else None):
        if not (isinstance(item, list) and len(item) >= 2 and isinstance(item[1], list)):
            continue
        pos = clean_text(item[0]) if isinstance(item[0], str) else ""
        group = next((g for g in groups if g["pos"] == pos), None)
        if group is None:
            if len(groups) >= MAX_POS_GROUPS:
                continue
            group = {"pos": pos, "defs": []}
            groups.append(group)
        for sense in item[1]:
            if isinstance(sense, list) and sense and isinstance(sense[0], str):
                add_unique(group["defs"], clean_text(sense[0]), MAX_DEFS_PER_POS)
    groups = [g for g in groups if g["defs"]]

    synonyms = []
    for item in as_list(data[11] if len(data) > 11 else None):
        if not (isinstance(item, list) and len(item) >= 2 and isinstance(item[1], list)):
            continue
        for sense in item[1]:
            if isinstance(sense, list) and sense and isinstance(sense[0], list):
                for word in sense[0]:
                    if isinstance(word, str):
                        add_unique(synonyms, clean_text(word), MAX_SYNONYMS)

    examples = []
    blocks = data[13] if len(data) > 13 else None
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], list):
        for item in blocks[0]:
            if isinstance(item, list) and item and isinstance(item[0], str):
                add_unique(examples, html_text(item[0]), MAX_EXAMPLES)

    if not groups:
        return None
    return {"phonetic": "", "audio": "", "groups": groups, "synonyms": synonyms, "examples": examples}


def parse_mymemory(data, source_text):
    """The translation from a MyMemory response ('' when it has nothing better than the
    original). Raises RouteFailure('HTTP 429') when the free quota is used up."""
    if not isinstance(data, dict):
        raise ValueError("unexpected MyMemory response")
    response = data.get("responseData")
    text = html_text(response.get("translatedText")) if isinstance(response, dict) else ""
    try:
        status = int(data.get("responseStatus", 200))
    except (TypeError, ValueError):
        status = 200
    if status == 429 or text.upper().startswith("MYMEMORY WARNING"):
        raise RouteFailure("HTTP 429", "MyMemory's free quota is used up")
    if status != 200:
        raise RouteFailure(f"HTTP {status}", clean_text(data.get("responseDetails")))
    return "" if text.lower() == source_text.lower() else text


_DATAMUSE_POS = {"n": "noun", "v": "verb", "adj": "adjective", "adv": "adverb"}


def parse_datamuse(data, word):
    """An entry from a Datamuse /words?md=d response, or None. Each definition is
    'pos<TAB>text'; only the item spelled like the word is used."""
    for item in _datamuse_items(data, word):
        groups = []
        for line in as_list(item.get("defs")):
            if not isinstance(line, str):
                continue
            code, _, text = line.partition("\t")
            pos = _DATAMUSE_POS.get(code.strip(), "")
            group = next((g for g in groups if g["pos"] == pos), None)
            if group is None:
                if len(groups) >= MAX_POS_GROUPS:
                    continue
                group = {"pos": pos, "defs": []}
                groups.append(group)
            add_unique(group["defs"], clean_text(text), MAX_DEFS_PER_POS)
        groups = [g for g in groups if g["defs"]]
        if groups:  # the first spelling that has definitions: one sense of the word, not a mixture
            return {"phonetic": datamuse_phonetic(data, word), "audio": "", "groups": groups,
                    "synonyms": [], "examples": []}
    return None


def _datamuse_items(data, word):
    """The items of a Datamuse response that are the word itself, in the order of the
    spellings to prefer (so 'Dispatched' finds 'dispatched' before the proper noun)."""
    if not isinstance(data, list):
        return []
    wanted = word.lower()
    forms = _forms(word)
    items = [item for item in data if isinstance(item, dict) and clean_text(item.get("word")).lower() == wanted]
    return sorted(items, key=lambda item: forms.index(clean_text(item.get("word")))
                  if clean_text(item.get("word")) in forms else len(forms))


_ARPABET = re.compile(r"^[A-Z]{1,3}[0-2]?(?: [A-Z]{1,3}[0-2]?)*$")


def datamuse_phonetic(data, word):
    """The IPA pronunciation of the word from a Datamuse response, as '/.../', or ''.
    With ipa=1 it is in the 'ipa_pron:' tag; a plain 'pron:' tag is used only if it is
    not ARPAbet, which is not worth showing."""
    for item in _datamuse_items(data, word):
        ipa = ""
        for tag in as_list(item.get("tags")):
            if not isinstance(tag, str):
                continue
            name, _, value = tag.partition(":")
            value = value.strip()
            if not value:
                continue
            if name == "ipa_pron" or (name == "pron" and not _ARPABET.match(value)):
                ipa = ipa or value
        if ipa:
            return ipa if ipa.startswith("/") else f"/{ipa.strip('/')}/"
    return ""


def parse_wiktionary(data):
    """An entry from a Wiktionary REST definition response, or None. The response is keyed
    by language; each English entry has a partOfSpeech and definitions, which hold HTML."""
    if not isinstance(data, dict):
        return None
    groups, examples = [], []
    for entry in as_list(data.get("en")):
        if not isinstance(entry, dict):
            continue
        pos = clean_text(entry.get("partOfSpeech")).lower()
        group = next((g for g in groups if g["pos"] == pos), None)
        if group is None:
            if len(groups) >= MAX_POS_GROUPS:
                continue
            group = {"pos": pos, "defs": []}
            groups.append(group)
        for definition in as_list(entry.get("definitions")):
            if not isinstance(definition, dict):
                continue
            add_unique(group["defs"], html_text(definition.get("definition")), MAX_DEFS_PER_POS)
            for example in as_list(definition.get("examples")):
                add_unique(examples, html_text(example), MAX_EXAMPLES)
            for parsed in as_list(definition.get("parsedExamples")):
                if isinstance(parsed, dict):
                    add_unique(examples, html_text(parsed.get("example")), MAX_EXAMPLES)
    groups = [g for g in groups if g["defs"]]
    if not groups:
        return None
    return {"phonetic": "", "audio": "", "groups": groups, "synonyms": [], "examples": examples}


# ── Output ─────────────────────────────────────────────────────────────────
def audio_buttons(audio):
    """One ▶ button per (label, url) pair. Each button has its own <audio> element."""
    buttons = []
    for index, (label, url) in enumerate(audio):
        element_id = f"gd_audio_{index}"
        buttons.append(
            f'<audio id="{element_id}" preload="none" src="{esc(url)}"></audio>'
            f'<a href="javascript:void(0);" '
            f"onclick=\"document.getElementById('{element_id}').play();\" "
            'style="text-decoration:none; margin-right:8px; font-size:11px; font-weight:bold; '
            'color:white; background:#3498db; padding:3px 9px; border-radius:4px;">'
            f"▶ {esc(label)}</a>"
        )
    return "".join(buttons)


def english_html(entry, audio):
    parts = []
    if entry["phonetic"]:
        parts.append(
            '<div style="text-align:center; color:#8b949e; font-size:13px; '
            'font-style:italic; margin-bottom:10px;">❖ PHONETIC: '
            + esc(entry["phonetic"]) + "</div>"
        )
    if audio:
        parts.append('<div style="margin-bottom:10px;">' + audio_buttons(audio) + "</div>")
    for group in entry["groups"]:
        if group["pos"]:
            parts.append(
                '<div style="margin-top:10px;"><b style="' + _POS_STYLE
                + '">❖ POS: ' + esc(group["pos"]) + "</b></div>"
            )
        items = "".join("<div>• " + esc(text) + "</div>" for text in group["defs"])
        parts.append('<div style="margin-top:6px;"><b>❖ MEANINGS:</b><br>' + items + "</div>")
    if entry["synonyms"]:
        parts.append(
            '<div style="margin-top:10px;"><b>❖ SYNONYMS:</b><br>'
            + esc(" · ".join(entry["synonyms"])) + "</div>"
        )
    if entry["examples"]:
        examples = "<br>".join("• " + esc(text) for text in entry["examples"])
        parts.append('<div style="margin-top:10px;"><b>❖ EXAMPLES:</b><br>' + examples + "</div>")
    return (
        '<div style="font-family:Arial,sans-serif; font-size:15px; '
        'text-align:left; direction:ltr;">' + "".join(parts) + "</div>"
    )


def translation_html(text):
    return (
        '<div style="direction:rtl; text-align:right; padding:10px 14px; '
        'border-right:4px solid #3498db; font-family:Tahoma,Arial,sans-serif; font-size:15px;">'
        "<b>❖ GOOGLE TRANSLATE:</b><br>" + esc(text) + "</div>"
    )


def note_html(text):
    return (
        '<div style="margin-bottom:8px; padding:6px 10px; border-left:3px solid #d29922; '
        'color:#d29922; font-family:Arial,sans-serif; font-size:13px; '
        'text-align:left; direction:ltr;">❖ NOTE: ' + esc(text) + "</div>"
    )


def render(translation, entry, audio, notes):
    parts = [note_html(text) for text in notes]
    if translation:
        parts.append(translation_html(translation))
    if entry:
        parts.append(english_html(entry, audio))
    return "".join(parts)


# ── Cache ──────────────────────────────────────────────────────────────────
def cache_path():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
    return Path(base) / "goldendict-anki" / "cache.json"


def log_path():
    return cache_path().parent / "network.log"


_DELETED = object()


@contextlib.contextmanager
def _file_lock(path):
    """Let one script at a time update the cache file. Best effort: after LOCK_WAIT
    seconds the caller goes ahead anyway, and a lock left by a dead script is removed."""
    fd = None
    give_up = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(str(path)) > LOCK_STALE:
                    os.unlink(str(path))
                    continue
            except OSError:
                pass
            if time.monotonic() >= give_up:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(str(path))
            except OSError:
                pass


class Cache:
    """A small JSON file with saved results and a little memory about the network:
    which route worked for each service, which service was not answering, and which
    one limited the requests.

    GoldenDict runs both programs for every search, at the same moment, so saving never
    overwrites the file: it re-reads it, adds only what this script learned, and writes
    the result atomically. Any problem is only a miss.
    """

    TABLES = ("route", "down", "cool")

    def __init__(self, path, enabled):
        self.path = Path(path)
        self.enabled = enabled
        self.entries = {}
        self.meta = {table: {} for table in self.TABLES}
        self.put_keys = set()  # the results this script found
        if enabled:
            self.entries, self.meta = self._read()
        self._meta_loaded = json.loads(json.dumps(self.meta))  # to see what this script changed

    def _read(self):
        """(entries, meta) from the file; empty if it is missing, damaged or in an older layout."""
        entries, meta = {}, {table: {} for table in self.TABLES}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return entries, meta
        except (OSError, ValueError) as exc:
            logging.debug("ignoring unreadable cache %s: %s", self.path, exc)
            return entries, meta
        if not (isinstance(data, dict) and isinstance(data.get("entries"), dict)):
            return entries, meta  # an older layout: start again
        entries = data["entries"]
        saved = data.get("meta")
        if isinstance(saved, dict):
            for table in self.TABLES:
                if isinstance(saved.get(table), dict):
                    meta[table] = {k: v for k, v in saved[table].items() if isinstance(k, str)}
        return entries, meta

    @staticmethod
    def _valid(item):
        return isinstance(item, dict) and isinstance(item.get("t"), (int, float))

    def lookup(self, key, ttl=None):
        """Return (state, value, saved_at).

        state is 'fresh' (use it, no network needed), 'stale' (use it only if the
        network fails) or 'missing'. ttl is how long a result stays fresh.
        """
        ttl = CACHE_TTL if ttl is None else ttl
        item = self.entries.get(key) if self.enabled else None
        if not self._valid(item):
            return "missing", None, None
        age = time.time() - item["t"]
        if item.get("v") is None:  # a "not found" answer: only worth keeping for a day
            return ("fresh", None, item["t"]) if age <= NEGATIVE_CACHE_TTL else ("missing", None, None)
        if age <= ttl:
            return "fresh", item["v"], item["t"]
        if age <= STALE_TTL:
            return "stale", item["v"], item["t"]
        return "missing", None, None

    def put(self, key, value):
        if self.enabled:
            self.entries[key] = {"t": time.time(), "v": value}
            self.put_keys.add(key)

    def _meta_changes(self):
        """{(table, service): new value or _DELETED} for what this script changed."""
        changes = {}
        for table in self.TABLES:
            before = self._meta_loaded.get(table) or {}
            after = self.meta.get(table)
            after = after if isinstance(after, dict) else {}
            for service in set(before) | set(after):
                if before.get(service) != after.get(service):
                    changes[(table, service)] = after[service] if service in after else _DELETED
        return changes

    def save(self):
        if not self.enabled:
            return
        changes = self._meta_changes()
        if not self.put_keys and not changes:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(self.path.with_name(self.path.name + ".lock")):
                entries, meta = self._read()  # what other scripts saved meanwhile
                for key in self.put_keys:
                    entries[key] = self.entries[key]
                for (table, service), value in changes.items():
                    if value is _DELETED:
                        meta[table].pop(service, None)
                    else:
                        meta[table][service] = value
                now = time.time()
                prefix = f"v{CACHE_VERSION}|"
                kept = sorted(
                    (
                        (key, item)
                        for key, item in entries.items()
                        if key.startswith(prefix) and self._valid(item) and now - item["t"] <= STALE_TTL
                    ),
                    key=lambda pair: pair[1]["t"],
                    reverse=True,
                )
                entries = dict(kept[:CACHE_MAX_ENTRIES])
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"entries": entries, "meta": meta}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            self.entries, self.meta, self.put_keys = entries, meta, set()
            self._meta_loaded = json.loads(json.dumps(meta))
        except OSError as exc:
            logging.debug("could not save cache %s: %s", self.path, exc)


def _table(meta, key):
    """One section of the network memory, or an empty one if it is damaged."""
    table = meta.get(key)
    return table if isinstance(table, dict) else {}


def _writable_table(meta, key):
    table = meta.get(key)
    if not isinstance(table, dict):
        table = meta[key] = {}
    return table


def is_down(meta, service):
    """True when the service failed or did not answer in the last few minutes."""
    stamp = _table(meta, "down").get(service)
    return isinstance(stamp, (int, float)) and 0 <= time.time() - stamp < DOWN_WINDOW


def mark_down(meta, service):
    _writable_table(meta, "down")[service] = time.time()


def mark_up(meta, service):
    """The service answered: forget that it was slow or had limited the requests."""
    for key in ("down", "cool"):
        _table(meta, key).pop(service, None)


def cooldown_left(meta, service):
    """Seconds left before a service that limited the requests may be asked again."""
    item = _table(meta, "cool").get(service)
    until = item.get("until") if isinstance(item, dict) else None
    return max(0.0, until - time.time()) if isinstance(until, (int, float)) else 0.0


def start_cooldown(meta, service, retry_after=None):
    """Leave a service alone after HTTP 429 or 403. Each repeat doubles the time,
    and a Retry-After from the server is respected. Asking again only makes it worse."""
    table = _writable_table(meta, "cool")
    previous = table.get(service)
    strikes = min((previous.get("strikes", 0) if isinstance(previous, dict) else 0) + 1, 10)
    seconds = min(COOLDOWN_MAX, COOLDOWN_BASE * 2 ** (strikes - 1))
    if retry_after:
        seconds = min(max(seconds, retry_after), 24 * 3600)
    table[service] = {"until": time.time() + seconds, "strikes": strikes}


def format_log(text, opts, seconds, net_lines, outcomes, stop_reason):
    mode = "Persian + English" if opts["translate"] else "English only"
    lines = [f"{time.strftime('%Y-%m-%d %H:%M:%S')}  lookup {text!r}  ({mode})  {seconds:.1f}s in total"]
    lines += [f"  {line}" for line in net_lines]
    lines += [f"  => {line}" for line in outcomes]
    lines.append(f"  => stopped waiting: {stop_reason}")
    return "\n".join(lines)


def write_log(path, block):
    """Keep the last LOG_BLOCKS lookups in a small text file. Problems are ignored."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            old = path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            old = ""
        blocks = [part for part in old.split("\n\n") if part.strip()]
        blocks.append(block)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text("\n\n".join(blocks[-LOG_BLOCKS:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logging.debug("could not write %s: %s", path, exc)


# ── Lookup ─────────────────────────────────────────────────────────────────
def has_data(value, capability):
    """Does this service result supply 'translation' or 'entry'?"""
    if not isinstance(value, dict):
        return False
    if capability == "translation":
        return value.get("translation") is not None
    if capability == "phonetic":
        entry = value.get("entry")
        return bool(value.get("phonetic") or (isinstance(entry, dict) and entry.get("phonetic")))
    if capability in ("examples", "synonyms"):
        entry = value.get("entry")
        return bool(isinstance(entry, dict) and entry.get(capability))
    return bool(value.get("entry"))


def entry_absent(tasks):
    """True when the word has no English entry: two services answered that they have nothing,
    and one of them is a broad one (Wiktionary or dictionaryapi.dev). Google and Datamuse alone
    are not enough, since they only know common words. A service that failed, or is still
    running, does not matter then."""
    answered = {name: task for name, task in tasks.items() if "entry" in task.caps and task.state == "ok"}
    if any(has_data(task.value, "entry") for task in answered.values()):
        return False  # somebody has an entry, so the word has one
    return len(answered) >= 2 and bool(set(answered) & {"dictionaryapi", "wiktionary"})


class Task:
    """One service in one lookup: running in a background thread, taken from the
    cache, skipped because the service limited the requests recently, or not started."""

    def __init__(self, name, key, tier, caps, function=None, args=(), stale=None):
        self.name = name
        self.key = key
        self.tier = tier
        self.caps = caps
        self.function = function
        self.args = args
        self.stale = stale          # (value, saved_at) of a saved copy to fall back on, or None
        self.from_cache = False
        self.skipped = False
        self.launched = False
        self.done = False
        self.value = None
        self.error = None           # a short reason, or None when an answer arrived
        self.detail = ""
        self.retry_after = None
        self.seconds = 0.0
        self.started = time.monotonic()
        self._changed = None

    @classmethod
    def cached(cls, name, key, tier, caps, value):
        task = cls(name, key, tier, caps)
        task.value, task.from_cache, task.launched, task.done = value, True, True, True
        return task

    @classmethod
    def skip(cls, name, key, tier, caps, stale, detail):
        task = cls(name, key, tier, caps, stale=stale)
        task.skipped, task.launched, task.done = True, True, True
        task.error, task.detail = "cooling down", detail
        return task

    @property
    def state(self):
        if self.done:
            return "ok" if self.error is None else "failed"
        return "running" if self.launched else "idle"

    def launch(self, changed):
        self.launched = True
        self._changed = changed
        self.started = time.monotonic()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.value = self.function(*self.args)
        except Exception as exc:  # every failure becomes a short reason, never a traceback
            self.error = describe_error(exc)
            self.detail = getattr(exc, "detail", "") or str(exc)
            self.retry_after = getattr(exc, "retry_after", None)
            logging.warning("%s failed: %s", self.name, self.detail or self.error)
        self.seconds = time.monotonic() - self.started
        self.done = True
        self._changed.set()


def wait_for_sources(tasks, needs, wants, meta, changed, started, deadline):
    """Start backup services when they are needed, and wait until the answer is
    complete enough to print. needs must be met; wants (the phonetic spelling) are
    waited for only briefly.

    Returns (reason, slow): why waiting ended, and the services that were still being
    awaited when it did.
    """

    def delivers(task, capability):
        return task.done and task.error is None and has_data(task.value, capability)

    def satisfied(capability):
        return any(delivers(task, capability) for task in tasks.values() if capability in task.caps)

    def moot(capability):
        """The word has no entry, so the entry and everything that belongs to it cannot be supplied."""
        return capability in ("entry", "phonetic", "examples", "synonyms") and entry_absent(tasks)

    def resolved(capability):
        return satisfied(capability) or (capability == "entry" and moot(capability))

    def best_rank(capability):
        ranks = [
            RANK[capability].index(task.name)
            for task in tasks.values() if capability in task.caps and delivers(task, capability)
        ]
        return min(ranks) if ranks else None

    def due(task, now):
        """Should a service that has not been started yet be started now?"""
        if task.tier == 0:
            if any(c in task.caps and not resolved(c) for c in needs):
                return True  # a part that is needed is still missing
            if is_down(meta, task.name):
                return False  # a service known to be down is not asked just to improve on saved answers
            # It may still add something: an extra that is missing, or a better answer for a need.
            if any(c in task.caps and not satisfied(c) and not moot(c) for c in wants):
                return True
            return any(
                c in task.caps and RANK[c].index(task.name) < best_rank(c) for c in needs if best_rank(c) is not None
            )
        for capability in needs + wants:
            if capability not in task.caps or satisfied(capability) or moot(capability):
                continue
            earlier = [t for t in tasks.values() if capability in t.caps and t.tier < task.tier]
            if all(t.done or is_down(meta, t.name) for t in earlier):
                return True
            if now - started >= task.tier * HEDGE_DELAY:
                return True
        return False

    settled_at = partial_at = None
    while True:
        now = time.monotonic()
        for task in tasks.values():
            if task.state == "idle" and due(task, now):
                task.launch(changed)
        pending = [t for t in tasks.values() if t.state == "running"]
        idle = [t for t in tasks.values() if t.state == "idle"]
        unsatisfied = [c for c in needs if not resolved(c)]
        if not unsatisfied:
            # Everything needed is in hand. A better service that is still running, or one
            # that could still supply the missing phonetic spelling, gets a short grace.
            better = [
                t for t in pending
                if any(c in t.caps and best_rank(c) is not None and RANK[c].index(t.name) < best_rank(c)
                       for c in needs)
            ]
            missing = [c for c in wants if not satisfied(c) and not moot(c)]
            extra = [t for t in pending if t not in better and any(c in t.caps for c in missing)]
            if not better and not extra:
                return "complete", set()
            settled_at = settled_at or now

            def patience(task):
                if is_down(meta, task.name):
                    return SHORT_GRACE
                return OPTIONAL_GRACE if task in better else EXTRA_GRACE

            # A better service gets OPTIONAL_GRACE seconds in all (EXTRA_GRACE if it was only asked
            # for an extra), counted from when it was asked, but never less than SHORT_GRACE
            # after the lesser answer arrived.
            give_up_at = max(
                max(settled_at + SHORT_GRACE, t.started + patience(t)) for t in better + extra
            )
            if now >= give_up_at:
                return "grace", {t.name for t in better + extra}
        else:
            helpers = [t for t in pending + idle if any(c in t.caps for c in unsatisfied)]
            if not helpers:
                return "complete", set()  # nothing left that could still supply the missing part
            if len(unsatisfied) < len(needs) and pending and all(is_down(meta, t.name) for t in helpers):
                partial_at = partial_at or now
                if now - partial_at >= SHORT_GRACE:
                    return "partial", {t.name for t in pending}
            else:
                partial_at = None
        if now >= deadline:
            return "deadline", {t.name for t in pending}
        changed.wait(timeout=min(0.05, max(deadline - now, 0.0)))
        changed.clear()


def complete_entry(entry, source, values):
    """A copy of the entry with its gaps filled from the other services' answers: the
    phonetic spelling (dictionaryapi.dev first, then Datamuse), and examples and synonyms
    from the other entries. Returns (entry, extras), where extras says which service
    supplied the phonetic spelling when it was not the one that supplied the entry."""
    merged = dict(entry)
    merged["synonyms"] = list(entry.get("synonyms") or [])
    merged["examples"] = list(entry.get("examples") or [])
    extras = {}
    if not merged.get("phonetic"):
        for name in RANK["phonetic"]:
            value = values.get(name)
            if has_data(value, "phonetic"):
                merged["phonetic"] = value.get("phonetic") or value["entry"]["phonetic"]
                if name != source:
                    extras["phonetic"] = name
                break
    for name in RANK["entry"]:
        other = (values.get(name) or {}).get("entry") if name != source else None
        if other:
            for item in other.get("examples") or []:
                add_unique(merged["examples"], item, MAX_EXAMPLES)
            for item in other.get("synonyms") or []:
                add_unique(merged["synonyms"], item, MAX_SYNONYMS)
    return merged, extras


@contextlib.contextmanager
def lookup_slot(text, enabled, wait=None):
    """GoldenDict starts both programs for a search at the same moment. Only one of them
    looks the word up. The other waits (at most LOOKUP_WAIT seconds), then reads what the
    first one saved, so both show the same entry and the services are asked once, not twice.
    Best effort: a lock left behind by a script that died is ignored after LOOKUP_LOCK_STALE
    seconds, and a script that has waited long enough goes ahead on its own."""
    if not enabled:
        yield False
        return
    path = cache_path().parent / ("lookup-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return
    fd = None
    give_up = time.monotonic() + (LOOKUP_WAIT if wait is None else wait)
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= give_up:
                break
            try:
                age = time.time() - os.path.getmtime(str(path))
            except OSError:
                continue  # it has just been released: try again at once
            if age > LOOKUP_LOCK_STALE:
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
                continue
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(str(path))
            except OSError:
                pass


def build_output(text, opts, net=None):
    """The HTML for one lookup. It always returns within TOTAL_BUDGET seconds."""
    word_mode = bool(opts["define"] and has_latin(text) and len(text.split()) <= MAX_DICT_WORDS)
    need_translation = bool(opts["translate"] and has_letters(text) and len(text) <= MAX_TRANSLATE_CHARS)
    if not (word_mode or need_translation):
        notes = []
        if opts["define"] and has_latin(text):
            notes.append("English entries are only looked up for one or two words.")
        return render("", None, [], notes)
    needs = tuple(
        capability for capability, wanted in (("translation", need_translation), ("entry", word_mode)) if wanted
    )
    wants = ("phonetic", "examples", "synonyms") if word_mode else ()

    started = time.monotonic()
    deadline = started + TOTAL_BUDGET
    # Waiting for the other program must not use up the whole budget.
    wait = min(LOOKUP_WAIT, max(0.0, deadline - time.monotonic() - 1.0))
    with lookup_slot(text, opts["cache"], wait):
        return _lookup(text, opts, net, word_mode, need_translation, needs, wants, started, deadline)


def _lookup(text, opts, net, word_mode, need_translation, needs, wants, started, deadline):
    cache = Cache(cache_path(), opts["cache"])
    meta = cache.meta
    if net is None:
        net = Network(meta)
    changed = threading.Event()
    # The requests may run a little past the deadline. Waiting for them stops at the
    # deadline, so that is always what decides, and the notes say so consistently.
    net_deadline = deadline + 1.0

    tasks = {}
    for name, tier in plan(text, word_mode, need_translation).items():
        key = f"v{CACHE_VERSION}|{name}|{'w' if word_mode else 'p'}|{text}"
        caps = capabilities(name, word_mode, need_translation)
        state, value, saved_at = cache.lookup(key, BACKUP_TTL if name in BACKUP_SERVICES else CACHE_TTL)
        if state == "fresh":
            tasks[name] = Task.cached(name, key, tier, caps, value)
            continue
        stale = (value, saved_at) if state == "stale" else None
        left = cooldown_left(meta, name)
        if left > 0:
            detail = f"it limited the requests recently; next try in about {max(1, round(left / 60))} min"
            tasks[name] = Task.skip(name, key, tier, caps, stale, detail)
        else:
            tasks[name] = Task(name, key, tier, caps, PROVIDERS[name], (text, word_mode, net, net_deadline), stale)

    stop_reason, slow = wait_for_sources(tasks, needs, wants, meta, changed, started, deadline)

    values, failures, saved_dates, outcomes = {}, {}, [], []
    for name, task in tasks.items():
        if task.state == "idle":
            outcomes.append(f"{name}: not needed")
            continue
        if task.state == "ok":
            values[name] = task.value
            if not task.from_cache:
                found = any(has_data(task.value, c) for c in ("translation", "entry", "phonetic"))
                cache.put(task.key, task.value if found else None)  # nothing found is kept for a day only
                mark_up(meta, name)
            outcomes.append(f"{name}: " + ("saved result" if task.from_cache else f"ok ({task.seconds:.1f}s)"))
            continue
        if task.state == "running" and stop_reason != "deadline" and name not in slow:
            outcomes.append(f"{name}: still running, no longer needed")
            continue
        if task.state == "failed":
            reason, detail = task.error, task.detail
            if not task.skipped:
                if reason in COOLDOWN_REASONS:
                    start_cooldown(meta, name, task.retry_after)
                else:
                    mark_down(meta, name)
        else:  # still running when the wait ended
            reason = f"no answer within {TOTAL_BUDGET:.0f} s" if stop_reason == "deadline" else "no answer in time"
            detail = ""
            mark_down(meta, name)
        line = f"{name}: {reason}" + (f" ({detail})" if detail else "")
        if task.stale is not None:
            values[name] = task.stale[0]
            saved_dates.append(task.stale[1])
            outcomes.append(line + "; using a saved copy")
        else:
            failures[name] = reason
            outcomes.append(line)

    def source_of(capability):
        for name in RANK[capability]:
            if name in tasks and capability in tasks[name].caps and has_data(values.get(name), capability):
                return name
        return None

    def explain(capability, before):
        """'Google: rate limited (HTTP 429); MyMemory: timeout' for the services ranked
        above `before` (or all of them) that did not supply this part."""
        parts = []
        for name in RANK[capability]:
            if name == before:
                break
            if name not in tasks or capability not in tasks[name].caps:
                continue
            if name in failures:
                parts.append(f"{LABELS[name]}: {reason_text(failures[name])}")
            elif name in values and not has_data(values[name], capability):
                parts.append(f"{LABELS[name]}: no {'entry' if capability == 'entry' else 'translation'}")
        return "; ".join(parts)

    # (kind, text): a 'problem' is always shown on the card; a 'source' note only if
    # SHOW_SOURCE_NOTES is on. Every note goes to network.log.
    items, unavailable = [], False
    if saved_dates:
        day = time.strftime("%Y-%m-%d", time.localtime(min(saved_dates)))
        items.append(("source", f"Some results are saved copies from {day}, because the network did not answer."))

    translation = ""
    if need_translation:
        source = source_of("translation")
        if source is None:
            items.append(("problem", f"Persian translation unavailable ({explain('translation', None) or 'no source'})."))
            unavailable = True
        else:
            translation = values[source]["translation"]
            if source != "google":
                items.append(("source", f"Persian translation from {LABELS[source]} ({explain('translation', source)})."))

    entry, audio = None, []
    if word_mode:
        source = source_of("entry")
        if source is None:
            if entry_absent(tasks):
                # The word, name or phrase has no entry. That is not a failure.
                items.append(("source", f"No English entry found ({explain('entry', None)})."))
            else:
                items.append(("problem", f"English entry not available ({explain('entry', None) or 'no source'})."))
                unavailable = unavailable or bool(failures)
        else:
            entry, extras = complete_entry(values[source]["entry"], source, values)
            fallback = source not in ("dictionaryapi", "google")
            text_note = f"English entry from {LABELS[source]}"
            if fallback and explain("entry", source):
                text_note += f" ({explain('entry', source)})"
            if "phonetic" in extras:
                text_note += f", with the phonetic spelling from {LABELS[extras['phonetic']]}"
            if fallback or "phonetic" in extras:
                items.append(("source", text_note + "."))
            if ENABLE_AUDIO:
                if RECORDING_BUTTON and entry.get("audio"):
                    audio.append(("Recording", entry["audio"]))
                audio.append(("Google voice", tts_url(text)))
    if unavailable:
        items.append(("problem", f"Network details: {log_path()}"))
    notes = [text_ for kind, text_ in items if kind == "problem" or SHOW_SOURCE_NOTES]
    outcomes += [f"note: {text_}" for kind, text_ in items if kind == "source"]

    cache.save()
    net_lines = list(getattr(net, "lines", []))
    write_log(log_path(), format_log(text, opts, time.monotonic() - started, net_lines, outcomes, stop_reason))
    return render(translation, entry, audio, notes)


# ── Diagnostics ────────────────────────────────────────────────────────────
def _check_dictionaryapi(data):
    return "ok" if parse_definition(data) else "answered, but no usable data"


def _check_google(data):
    translation, entry = parse_translation(data), google_entry(data)
    if translation and entry:
        return "ok"
    return "ok, but no definitions" if translation else "answered, but no usable data"


def _check_mymemory(data):
    try:
        return "ok" if parse_mymemory(data, "hello") else "answered, but no usable data"
    except RouteFailure as exc:
        return reason_text(exc.reason)


def _check_datamuse(data):
    if not parse_datamuse(data, "hello"):
        return "answered, but no usable data"
    return "ok" if datamuse_phonetic(data, "hello") else "ok, but no phonetic spelling"


def _check_wiktionary(data):
    return "ok" if parse_wiktionary(data) else "answered, but no usable data"


def run_checks():
    """Try every service over every connection route. Returns the report (ASCII only)."""
    net = Network()
    configured = net.configured_proxies()
    found = []
    if AUTO_PROXY:
        parsed = (parse_proxy(url) for url in discover_local_proxies())
        found = [item for item in parsed if item and item not in configured]
    routes = [net._direct()] + [net._proxy(*item) for item in configured + found] + [net._ipv4()]
    services = [
        ("dictionaryapi.dev", DICTIONARY_URL.format("hello"), _check_dictionaryapi),
        ("Google", google_url("hello", True), _check_google),
        ("MyMemory", mymemory_url("hello"), _check_mymemory),
        ("Datamuse", datamuse_url("hello"), _check_datamuse),
        ("Wiktionary", WIKTIONARY_URL.format("hello"), _check_wiktionary),
    ]
    jobs = [(label, url, check, route) for label, url, check in services for route in routes]
    rows = {}

    def run(index, label, url, check, route):
        results = queue.Queue()
        net._attempt("check", route, url, results)
        _, kind, value, seconds, _, _ = results.get()
        if kind == "ok":
            try:
                verdict = check(value)
            except Exception:
                verdict = "answered, but unreadable"
        elif kind == "notfound":
            verdict = "not found (HTTP 404)"
        else:
            verdict = reason_text(value)
        rows[index] = (label, route.name, verdict, f"{seconds:.1f}s")

    threads = [
        threading.Thread(target=run, args=(index,) + job, daemon=True) for index, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    limit = time.monotonic() + ROUTE_TIMEOUT + 4.0
    for thread in threads:
        thread.join(max(0.0, limit - time.monotonic()))

    try:
        system = sorted(set(str(v) for v in urllib.request.getproxies().values()))
    except Exception:
        system = []
    lines = [
        f"python {platform.python_version()} on {platform.system()}",
        "PROXY setting: " + (mask_credentials(PROXY) if PROXY else "none"),
        "system proxy: " + (", ".join(mask_credentials(item) for item in system) or "none"),
        "local proxy ports found: "
        + (", ".join(f"{kind}://{host}:{port}" for kind, host, port in found) or "none"),
        f"log file: {log_path()}",
        "",
        f"{'service':<20}{'route':<30}{'result':<30}time",
    ]
    reachable = {label: [] for label, _, _ in services}
    limited = {label: 0 for label, _, _ in services}
    for index, (label, url, check, route) in enumerate(jobs):
        row = rows.get(index, (label, route.name, "no answer", f"{ROUTE_TIMEOUT + 4:.1f}s"))
        lines.append(f"{row[0]:<20}{row[1]:<30}{row[2]:<30}{row[3]}")
        if row[2].startswith("ok"):
            reachable[label].append(route.name)
        elif "HTTP 429" in row[2]:
            limited[label] += 1
    lines += ["", "summary:"]
    for label, names in reachable.items():
        if names:
            verdict = "reachable via " + ", ".join(names)
        elif limited[label]:
            verdict = "LIMITING the requests (HTTP 429): wait, or try another VPN server"
        else:
            verdict = "NOT reachable by any route"
        lines.append(f"  {label:<20}{verdict}")
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────
def main(argv=None, translate=ENABLE_TRANSLATION):
    if sys.stderr is None:  # pythonw.exe has no console streams
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    opts, raw_query = parse_argv(sys.argv[1:] if argv is None else argv, translate)
    logging.basicConfig(
        level=logging.DEBUG if opts["debug"] else logging.WARNING,
        format="goldendict-anki %(levelname)s: %(message)s",
    )
    if opts["check"]:
        report = run_checks()
        if sys.stdout is not None:
            sys.stdout.write(report + "\n")
        return 0
    text = normalize_query(raw_query)
    if "%GDWORD%" in text:
        logging.error("GoldenDict did not replace %GDWORD%; check the program's command line")
        return 0
    if not has_letters(text):
        return 0
    try:
        output = build_output(text, opts)
    except Exception:  # never show a traceback inside GoldenDict
        logging.exception("unexpected error while looking up %r", text)
        output = render("", None, [], [
            "Unexpected error while looking up this word; run the script with --debug for details."
        ])
    if output and sys.stdout is not None:
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


def run(translate):
    """Entry point used by dict_full.py and dict_eng_only.py.

    It ends the process at once, so a request that is still hanging in the
    background can never delay GoldenDict.
    """
    code = main(None, translate)
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    os._exit(code)
