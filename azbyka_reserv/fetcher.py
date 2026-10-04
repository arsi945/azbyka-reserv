"""HTTP-загрузчик на urllib: вежливый темп, докачка, куки, без авто-редиректов."""

from __future__ import annotations

import email.message
import gzip
import hashlib
import http.client
import http.cookiejar
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from urllib.parse import unquote

CHUNK = 256 * 1024


class RateLimiter:
    """Общий для всех потоков интервал между началами запросов + глобальная пауза."""

    def __init__(self, min_delay: float) -> None:
        self.min_delay = min_delay
        self._lock = threading.Lock()
        self._next = 0.0
        self._pause_until = 0.0

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    def paused_for(self) -> float:
        return max(0.0, self._pause_until - time.monotonic())

    def wait(self, stop: threading.Event | None = None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                start = max(now, self._next, self._pause_until)
                if start <= now:
                    self._next = now + self.min_delay
                    return
                delay = start - now
            if stop is not None and stop.wait(min(delay, 1.0)):
                return
            elif stop is None:
                time.sleep(min(delay, 1.0))


class Bandwidth:
    """Глобальное ограничение скорости (байт/с) для потокового скачивания."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._allowance = float(limit)
        self._last = time.monotonic()

    def consume(self, n: int) -> None:
        if self.limit <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._allowance = min(self.limit, self._allowance + (now - self._last) * self.limit)
                self._last = now
                if self._allowance >= n or self._allowance >= self.limit:
                    self._allowance -= n
                    return
                need = (n - self._allowance) / self.limit
            time.sleep(min(need, 1.0))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib поднимет HTTPError с кодом 3xx — обработаем сами


@dataclass
class Response:
    url: str
    status: int
    headers: email.message.Message
    ctype: str = ""
    location: str | None = None
    filename: str | None = None
    length: int | None = None
    body: bytes | None = None  # для страниц (в памяти)
    tmp_path: str | None = None  # для потоковых файлов
    size: int = 0
    sha1: str = ""
    partial_resumed: bool = False
    extra: dict = field(default_factory=dict)


class FetchError(Exception):
    def __init__(self, msg: str, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


def _disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    msg = email.message.Message()
    msg["content-disposition"] = value
    name = msg.get_filename()
    if name:
        # get_filename раскрывает RFC 2231 (filename*=UTF-8''...)
        try:
            # некоторые серверы шлют UTF-8 байты в latin-1
            fixed = name.encode("latin-1").decode("utf-8")
            name = fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        name = unquote(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
        return name or None
    return None


def load_cookies(path: str) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.MozillaCookieJar()
    if path:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    return jar


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float,
        limiter: RateLimiter,
        bandwidth: Bandwidth | None = None,
        cookies_file: str = "",
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.limiter = limiter
        self.bandwidth = bandwidth or Bandwidth(0)
        self.jar = load_cookies(cookies_file)
        ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
            _NoRedirect(),
        )

    def _request(self, url: str, headers: dict[str, str]) -> urllib.request.Request:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        }
        h.update(headers)
        return urllib.request.Request(url, headers=h, method="GET")

    def open(self, url: str, headers: dict[str, str] | None = None, stop: threading.Event | None = None):
        """Открывает URL. Возвращает (status, headers, fp|None). Ошибки сети -> FetchError."""
        self.limiter.wait(stop)
        req = self._request(url, headers or {})
        try:
            fp = self.opener.open(req, timeout=self.timeout)
            return fp.status, fp.headers, fp
        except urllib.error.HTTPError as e:
            status = e.code
            hdrs = e.headers
            try:
                e.read(64 * 1024)
            except Exception:  # noqa: BLE001
                pass
            e.close()
            return status, hdrs, None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise FetchError(f"network: {reason!r}") from e

    def _decoder(self, headers):
        enc = (headers.get("Content-Encoding") or "").strip().lower()
        if enc == "gzip" or enc == "x-gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        if enc == "deflate":
            return zlib.decompressobj()
        return None

    def get(
        self,
        url: str,
        *,
        to_file: str | None = None,
        max_memory: int = 30 * 1024 * 1024,
        max_size: int = 0,
        etag: str | None = None,
        last_modified: str | None = None,
        stop: threading.Event | None = None,
    ) -> Response:
        """GET. Если задан ``to_file`` — пишет потоково в ``to_file`` (с докачкой
        по Range, если файл уже частично скачан); иначе тело в памяти."""
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resume_from = 0
        if to_file and os.path.exists(to_file):
            resume_from = os.path.getsize(to_file)
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
                headers["Accept-Encoding"] = "identity"
        status, hdrs, fp = self.open(url, headers, stop)
        resp = Response(url=url, status=status, headers=hdrs)
        resp.ctype = hdrs.get("Content-Type", "") if hdrs else ""
        if status in (301, 302, 303, 307, 308):
            resp.location = hdrs.get("Location") if hdrs else None
            return resp
        if fp is None:
            return resp
        try:
            resp.filename = _disposition_filename(hdrs.get("Content-Disposition"))
            cl = hdrs.get("Content-Length")
            resp.length = int(cl) if cl and cl.isdigit() else None
            if max_size and resp.length and resp.length + resume_from > max_size:
                raise FetchError(f"файл больше лимита: {resp.length} байт", retryable=False)
            dec = self._decoder(hdrs)
            if to_file is None:
                resp.body = self._read_memory(fp, dec, max_memory, stop)
                resp.size = len(resp.body)
                resp.sha1 = hashlib.sha1(resp.body).hexdigest()
            else:
                self._read_to_file(fp, dec, to_file, resp, resume_from if status == 206 else 0, max_size, stop)
        finally:
            fp.close()
        return resp

    def _read_memory(self, fp, dec, max_memory: int, stop) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            if stop is not None and stop.is_set():
                raise FetchError("остановлено пользователем")
            try:
                data = fp.read(CHUNK)
            except (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError, OSError) as e:
                raise FetchError(f"network read: {e!r}") from e
            if not data:
                break
            if dec is not None:
                data = dec.decompress(data)
            chunks.append(data)
            total += len(data)
            self.bandwidth.consume(len(data))
            if total > max_memory:
                raise FetchError(f"страница больше {max_memory} байт", retryable=False, status=413)
        if dec is not None:
            chunks.append(dec.flush())
        return b"".join(chunks)

    def _read_to_file(self, fp, dec, path: str, resp: Response, resume_from: int, max_size: int, stop) -> None:
        mode = "ab" if resume_from > 0 else "wb"
        resp.partial_resumed = resume_from > 0
        expected = (resp.length + resume_from) if resp.length is not None and dec is None else None
        written = resume_from
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, mode) as out:
            while True:
                if stop is not None and stop.is_set():
                    raise FetchError("остановлено пользователем (файл докачается при следующем запуске)")
                try:
                    data = fp.read(CHUNK)
                except (socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError, OSError) as e:
                    raise FetchError(f"network read: {e!r}") from e
                if not data:
                    break
                if dec is not None:
                    data = dec.decompress(data)
                out.write(data)
                written += len(data)
                self.bandwidth.consume(len(data))
                if max_size and written > max_size:
                    raise FetchError(f"файл больше лимита {max_size} байт", retryable=False)
            if dec is not None:
                out.write(dec.flush())
        if expected is not None and written != expected:
            raise FetchError(f"обрыв: получено {written} из {expected} байт")
        resp.size = written
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        resp.sha1 = h.hexdigest()


def maybe_gunzip(body: bytes, url: str, ctype: str) -> bytes:
    """Карты сайта .xml.gz приходят как application/x-gzip — распаковать для разбора."""
    if body[:2] == b"\x1f\x8b" and (url.lower().endswith(".gz") or "gzip" in ctype.lower()):
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body
