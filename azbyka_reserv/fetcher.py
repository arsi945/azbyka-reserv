"""HTTP-загрузчик на urllib: вежливый темп, докачка, куки, без авто-редиректов."""

from __future__ import annotations

import email.message
import email.utils
import gzip
import hashlib
import http.client
import http.cookiejar
import io
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from urllib.parse import unquote

log = logging.getLogger("azbyka_reserv")

CHUNK = 256 * 1024
MAX_RETRY_AFTER = 900.0

# Типы, которые имеет смысл держать в памяти и разбирать на ссылки.
TEXTUAL_CTYPES = (
    "text/", "application/xhtml+xml", "application/xml", "application/json", "application/ld+json",
    "application/javascript", "application/x-javascript", "audio/x-mpegurl", "audio/mpegurl",
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/rss+xml", "application/atom+xml",
    "application/x-gzip", "application/gzip",
)


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
            if stop is not None:
                if stop.wait(min(delay, 1.0)):
                    return
            else:
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
    tmp_path: str | None = None  # тело записано в файл (большое/двоичное)
    size: int = 0
    sha1: str = ""
    partial_resumed: bool = False
    extra: dict = field(default_factory=dict)


class FetchError(Exception):
    """kind: network | tls | truncated | toolarge | stopped | other"""

    def __init__(self, msg: str, retryable: bool = True, status: int | None = None, kind: str = "other") -> None:
        super().__init__(msg)
        self.retryable = retryable
        self.status = status
        self.kind = kind


def _fix_mojibake(s: str) -> str:
    """UTF-8, ошибочно прочитанный как latin-1 (частая беда заголовков)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def disposition_filename(value: str | None) -> str | None:
    """Имя файла из Content-Disposition; filename* (RFC 5987/2231) важнее filename."""
    if not value:
        return None
    msg = email.message.Message()
    msg["content-disposition"] = value
    params = msg.get_params(header="content-disposition") or []
    star = plain = None
    for k, v in params[1:]:
        k = k.lower()
        if k == "filename":
            if isinstance(v, tuple):  # RFC 2231 (filename*=charset''...)
                star = email.utils.collapse_rfc2231_value(v)
            else:
                plain = v
        elif k == "filename*":
            raw = v if isinstance(v, str) else email.utils.collapse_rfc2231_value(v)
            m = re.match(r"(?i)([\w-]+)''(.*)", raw)
            if m:
                try:
                    star = unquote(m.group(2), encoding=m.group(1))
                except LookupError:
                    star = unquote(m.group(2))
            else:
                star = unquote(raw)
    name = star or plain
    if not name:
        return None
    if star is None:
        name = _fix_mojibake(unquote(name))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip().strip('"')
    if not name or set(name.rsplit(".", 1)[0]) <= {"_", ".", "-", " ", "?"}:
        return None  # ASCII-заглушка вроде «____.pdf» бесполезна — возьмём имя из URL
    return name


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, min(dt.timestamp() - time.time(), MAX_RETRY_AFTER))


def load_cookies(path: str) -> http.cookiejar.CookieJar:
    """cookies.txt (Netscape). Сессионные куки (expires=0) тоже отправляются;
    BOM в начале файла (выгрузка из Windows-редакторов) допускается."""
    jar = http.cookiejar.MozillaCookieJar()
    if not path:
        return jar
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError as e:
        log.error("Не удалось прочитать файл кук %s: %s — продолжаю без них", path, e)
        return jar
    if not text.lstrip().startswith("#"):
        text = "# Netscape HTTP Cookie File\n" + text
    try:
        jar._really_load(io.StringIO(text), path, ignore_discard=True, ignore_expires=True)  # noqa: SLF001
    except (http.cookiejar.LoadError, ValueError) as e:
        log.error("Файл кук %s в неверном формате (%s) — продолжаю без них. Нужен формат Netscape cookies.txt.", path, e)
        return jar
    for c in jar:
        if not c.expires:  # 0 или None: сессионная кука из браузера
            c.expires = None
            c.discard = False
    log.info("Загружено кук: %d", len(jar))
    return jar


def make_ssl_context() -> ssl.SSLContext:
    """Проверка сертификатов через системное хранилище.

    На Windows лучше всего работает пакет truststore (использует проверку самой
    Windows, с подгрузкой корневых сертификатов). Если он не установлен — обычный
    контекст Python плюс набор certifi, если он есть.
    """
    try:
        import truststore  # type: ignore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001
        pass
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore

        ctx.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001
        pass
    return ctx


_NET_EXC = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException,
            ssl.SSLError, OSError)


def _wrap_net_error(e: BaseException, where: str) -> FetchError:
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(e, ssl.SSLCertVerificationError):
        return FetchError(f"tls-cert: {reason!r}", retryable=True, kind="tls")
    return FetchError(f"network{where}: {reason!r}", retryable=True, kind="network")


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
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=make_ssl_context()),
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
        if stop is not None and stop.is_set():
            raise FetchError("остановлено пользователем", kind="stopped")
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
        except _NET_EXC as e:
            raise _wrap_net_error(e, "") from e

    @staticmethod
    def _decoder(headers):
        enc = (headers.get("Content-Encoding") or "").strip().lower()
        if enc in ("gzip", "x-gzip"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        if enc == "deflate":
            return zlib.decompressobj()
        return None

    @staticmethod
    def _textual(ctype: str) -> bool:
        base = (ctype or "").split(";", 1)[0].strip().lower()
        return not base or base.startswith(TEXTUAL_CTYPES)

    def get(
        self,
        url: str,
        *,
        to_file: str | None = None,
        spill_file: str | None = None,
        max_memory: int = 30 * 1024 * 1024,
        max_size: int = 0,
        etag: str | None = None,
        last_modified: str | None = None,
        stop: threading.Event | None = None,
    ) -> Response:
        """GET.

        * ``to_file`` — писать тело в этот файл (тяжёлые файлы), с докачкой по
          Range, если файл уже частично скачан и сервер подтверждает, что файл
          не изменился (If-Range).
        * иначе тело читается в память; если же ответ двоичный или больше
          ``max_memory``, а задан ``spill_file`` — оно сбрасывается в этот файл
          (``Response.tmp_path``), а не теряется.
        """
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resume_from = 0
        part_meta: dict = {}
        if to_file and os.path.exists(to_file):
            resume_from = os.path.getsize(to_file)
            part_meta = _read_part_meta(to_file)
            validator = part_meta.get("etag") or part_meta.get("last_modified")
            if resume_from > 0 and validator:
                headers["Range"] = f"bytes={resume_from}-"
                headers["If-Range"] = validator
                headers["Accept-Encoding"] = "identity"
            else:
                resume_from = 0  # без валидатора докачка небезопасна — заново
        status, hdrs, fp = self.open(url, headers, stop)
        resp = Response(url=url, status=status, headers=hdrs)
        resp.ctype = hdrs.get("Content-Type", "") if hdrs else ""
        if status in (301, 302, 303, 307, 308):
            loc = hdrs.get("Location") if hdrs else None
            resp.location = _fix_mojibake(loc) if loc else None
            return resp
        if fp is None:
            if status == 416 and hdrs is not None:
                m = re.match(r"bytes \*/(\d+)", hdrs.get("Content-Range") or "")
                if m:
                    resp.extra["total"] = int(m.group(1))
            return resp
        try:
            resp.filename = disposition_filename(hdrs.get("Content-Disposition"))
            cl = hdrs.get("Content-Length")
            resp.length = int(cl) if cl and cl.strip().isdigit() else None
            dec = self._decoder(hdrs)
            if to_file is not None:
                if status == 206:
                    m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", hdrs.get("Content-Range") or "")
                    if not m or int(m.group(1)) != resume_from:
                        raise FetchError("сервер вернул не тот кусок файла — начнём заново", kind="truncated")
                    start = resume_from
                else:
                    start = 0  # 200: файл целиком (в т.ч. если он изменился)
                total = (resp.length + start) if resp.length is not None else None
                if max_size and total and total > max_size:
                    raise FetchError(f"файл больше лимита: {total} байт", retryable=False, kind="toolarge")
                _write_part_meta(to_file, {"etag": hdrs.get("ETag"), "last_modified": hdrs.get("Last-Modified"),
                                           "total": total})
                self._read_to_file(fp, dec, to_file, resp, start, max_size, stop)
            else:
                big = resp.length is not None and resp.length > max_memory
                if spill_file and (big or not self._textual(resp.ctype)):
                    self._read_to_file(fp, dec, spill_file, resp, 0, max_size, stop)
                    resp.tmp_path = spill_file
                else:
                    resp.body = self._read_memory(fp, dec, max_memory, stop, spill_file, resp, max_size)
                    if resp.body is not None:
                        resp.size = len(resp.body)
                        resp.sha1 = hashlib.sha1(resp.body).hexdigest()
        finally:
            fp.close()
        return resp

    def _check_complete(self, fp, dec) -> None:
        left = getattr(fp, "length", None)
        if left:  # http.client уменьшает length по мере чтения: остаток = недополучено
            raise FetchError(f"обрыв: недополучено {left} байт", kind="truncated")
        if dec is not None and not dec.eof:
            raise FetchError("обрыв: сжатые данные не завершены", kind="truncated")

    def _read_chunk(self, fp, dec) -> bytes:
        try:
            data = fp.read(CHUNK)
        except _NET_EXC as e:
            raise _wrap_net_error(e, " read") from e
        if data and dec is not None:
            try:
                data = dec.decompress(data)
            except zlib.error as e:
                raise FetchError(f"обрыв: повреждённое сжатие ({e})", kind="truncated") from e
        return data

    def _read_memory(self, fp, dec, max_memory: int, stop, spill_file, resp, max_size) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            if stop is not None and stop.is_set():
                raise FetchError("остановлено пользователем", kind="stopped")
            data = self._read_chunk(fp, dec)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            self.bandwidth.consume(len(data))
            if total > max_memory:
                if not spill_file:
                    raise FetchError(f"страница больше {max_memory} байт", retryable=False, status=413, kind="toolarge")
                # слишком большое для памяти — дописываем в файл, ничего не теряя
                os.makedirs(os.path.dirname(spill_file) or ".", exist_ok=True)
                with open(spill_file, "wb") as out:
                    for c in chunks:
                        out.write(c)
                chunks = []
                self._read_to_file(fp, dec, spill_file, resp, total, max_size, stop, append=True)
                resp.tmp_path = spill_file
                return None
        if dec is not None:
            chunks.append(dec.flush())
        self._check_complete(fp, dec)
        return b"".join(chunks)

    def _read_to_file(self, fp, dec, path: str, resp: Response, resume_from: int, max_size: int, stop,
                      append: bool = False) -> None:
        mode = "ab" if (resume_from > 0) else "wb"
        resp.partial_resumed = resume_from > 0 and not append
        written = resume_from
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, mode) as out:
            while True:
                if stop is not None and stop.is_set():
                    raise FetchError("остановлено пользователем (файл докачается при следующем запуске)", kind="stopped")
                data = self._read_chunk(fp, dec)
                if not data:
                    break
                out.write(data)
                written += len(data)
                self.bandwidth.consume(len(data))
                if max_size and written > max_size:
                    raise FetchError(f"файл больше лимита {max_size} байт", retryable=False, kind="toolarge")
            if dec is not None:
                out.write(dec.flush())
        self._check_complete(fp, dec)
        resp.size = os.path.getsize(path)
        resp.sha1 = file_sha1(path)


def file_sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _part_meta_path(path: str) -> str:
    return path + ".json"


def _read_part_meta(path: str) -> dict:
    try:
        with open(_part_meta_path(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_part_meta(path: str, meta: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(_part_meta_path(path), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except OSError:
        pass


def remove_partial(path: str) -> None:
    for p in (path, _part_meta_path(path)):
        try:
            os.remove(p)
        except OSError:
            pass


def maybe_gunzip(body: bytes, url: str, ctype: str) -> bytes:
    """Карты сайта .xml.gz приходят как application/x-gzip — распаковать для разбора."""
    if body[:2] == b"\x1f\x8b" and (url.lower().endswith(".gz") or "gzip" in ctype.lower()):
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body
