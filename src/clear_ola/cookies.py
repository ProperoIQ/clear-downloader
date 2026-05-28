"""Get a CookieJar of Clear / ClearTax cookies for use with requests.

Strategy (in order):

1. **Manual JSON export** at `.auth/clear-cookies.json` (Cookie-Editor format).
   Bulletproof against Chrome's App-Bound Encryption (v127+). One-time
   setup, refresh when Clear's session actually expires.

2. **`browser-cookie3` direct read** of the Chrome profile's cookies DB.
   Works on older Chrome / Edge / Firefox where App-Bound Encryption isn't in
   the way. Fails on current Chrome (v127+).

If neither works, we surface a clear error pointing the user at option 1."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import browser_cookie3
import requests


CLEAR_DOMAINS = ("app.clear.in", ".clear.in", "clear.in", ".cleartax.in", "cleartax.in")

# Default location for a manual cookie export (Cookie-Editor or similar extension)
DEFAULT_COOKIE_FILE = Path(".auth/clear-cookies.json")


# ---- Win32 helpers for reading files locked by Chrome ----

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _read_locked_file(src: Path) -> bytes:
    """Read a file even if another process holds an exclusive Win32 lock.

    Uses CreateFileW with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    so we cooperate with Chrome's existing file handle.
    """
    kernel32 = ctypes.windll.kernel32
    CreateFileW = kernel32.CreateFileW
    CreateFileW.restype = ctypes.wintypes.HANDLE
    CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,  # lpFileName
        ctypes.wintypes.DWORD,    # dwDesiredAccess
        ctypes.wintypes.DWORD,    # dwShareMode
        ctypes.c_void_p,          # lpSecurityAttributes
        ctypes.wintypes.DWORD,    # dwCreationDisposition
        ctypes.wintypes.DWORD,    # dwFlagsAndAttributes
        ctypes.wintypes.HANDLE,   # hTemplateFile
    ]
    ReadFile = kernel32.ReadFile
    CloseHandle = kernel32.CloseHandle
    GetFileSizeEx = kernel32.GetFileSizeEx

    handle = CreateFileW(
        str(src),
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise OSError(
            f"CreateFileW failed for {src!s} "
            f"(WinError {ctypes.get_last_error()})"
        )
    try:
        size = ctypes.c_longlong(0)
        if not GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError(f"GetFileSizeEx failed for {src!s}")
        chunk = 65536
        buf = ctypes.create_string_buffer(chunk)
        out = bytearray()
        read_total = 0
        while read_total < size.value:
            n = ctypes.wintypes.DWORD(0)
            if not ReadFile(handle, buf, chunk, ctypes.byref(n), None):
                raise OSError(f"ReadFile failed at {read_total}/{size.value}")
            if n.value == 0:
                break
            out += buf.raw[: n.value]
            read_total += n.value
        return bytes(out)
    finally:
        CloseHandle(handle)


class ChromeRunningError(RuntimeError):
    """Chrome is open and holds an exclusive file lock on the cookies DB.
    Distinct from "cookies are corrupted / encrypted" — caller's recovery is to
    close Chrome briefly and retry."""


def _shadow_copy(src: Path, dst: Path) -> None:
    """Copy `src` -> `dst`. If Chrome holds an exclusive lock, raise
    ChromeRunningError with an actionable message rather than the raw
    PermissionError / OSError.
    """
    try:
        shutil.copy2(src, dst)
        return
    except PermissionError:
        pass
    # Fallback: try a Win32 shared handle. This only helps if Chrome happens
    # to have opened with FILE_SHARE_READ (it typically doesn't, but worth
    # a try before giving up).
    try:
        data = _read_locked_file(src)
    except OSError as e:
        raise ChromeRunningError(
            f"Chrome is currently holding an exclusive lock on the cookies "
            f"database at {src!s}. Close all Chrome windows briefly and "
            f"re-run. (Underlying Win32 error: {e})"
        ) from e
    dst.write_bytes(data)


def chrome_user_data_root() -> Path:
    """Return Chrome's User Data root on the current Windows machine."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if not localappdata:
        raise RuntimeError("LOCALAPPDATA env var not set; cannot locate Chrome data.")
    return Path(localappdata) / "Google" / "Chrome" / "User Data"


def load_clear_cookies(
    profile: str,
    cookie_file: Path | None = None,
) -> requests.cookies.RequestsCookieJar:
    """Get a CookieJar of Clear cookies, trying available methods in order.

    Resolution order:
        1. If `cookie_file` (default `.auth/clear-cookies.json`) exists and is
           fresh, load it. **Recommended path** — bypasses Chrome's App-Bound
           Encryption entirely.
        2. Otherwise, try `browser-cookie3` against the Chrome profile.
           Works on older Chrome / Edge / Firefox. Fails on Chrome v127+
           (App-Bound Encryption).

    Args:
        profile: Chrome profile folder name (e.g. "Default", "Profile 10").
                 Used only by the browser-cookie3 path.
        cookie_file: Path to a Cookie-Editor JSON export.
                     Defaults to `.auth/clear-cookies.json`.

    Raises:
        RuntimeError: with a clear actionable message if no method worked.
    """
    if cookie_file is None:
        cookie_file = DEFAULT_COOKIE_FILE

    # Path 1: manual JSON export (bulletproof)
    if cookie_file.exists():
        jar, warning = load_cookies_from_json(cookie_file)
        if warning:
            # Print to stderr-equivalent; caller's logger will pick it up
            import sys
            print(f"[cookies] WARNING: {warning}", file=sys.stderr)
        return jar

    # Path 2: browser-cookie3 (best-effort)
    return _load_via_browser_cookie3(profile)


def load_cookies_from_json(path: Path) -> tuple[requests.cookies.RequestsCookieJar, str | None]:
    """Load a Cookie-Editor JSON export into a CookieJar.

    Cookie-Editor exports an array of objects shaped like:
        {
          "domain": ".clear.in",
          "name": "...",
          "value": "...",
          "path": "/",
          "expirationDate": 1735689600,     # unix seconds (optional)
          "secure": true,
          "httpOnly": true,
          "sameSite": "no_restriction",
          ...
        }

    We accept that shape and a couple of common variants.

    Returns:
        (jar, warning) — `warning` is non-None if anything looked off, e.g.
        cookies appear expired.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError(
            f"Cookie file {path} should contain a JSON array; got {type(raw).__name__}. "
            f"Did you copy the wrong thing from Cookie-Editor?"
        )

    jar = requests.cookies.RequestsCookieJar()
    now = time.time()
    soonest_expiry: float | None = None
    clear_count = 0

    for entry in raw:
        domain = entry.get("domain") or ""
        name = entry.get("name")
        value = entry.get("value")
        if not name or value is None:
            continue
        # Only keep cookies under the Clear domains; ignore everything else
        if not any(d.lstrip(".") in domain.lstrip(".")
                   or domain.lstrip(".") in d.lstrip(".")
                   for d in CLEAR_DOMAINS):
            continue
        clear_count += 1
        expires = entry.get("expirationDate")
        if expires:
            try:
                expires = float(expires)
                soonest_expiry = (expires if soonest_expiry is None
                                  else min(soonest_expiry, expires))
            except (TypeError, ValueError):
                expires = None
        jar.set(
            name=name,
            value=value,
            domain=domain,
            path=entry.get("path") or "/",
            secure=bool(entry.get("secure")),
            rest={"HttpOnly": str(entry.get("httpOnly", False)).lower()},
            expires=int(expires) if expires else None,
        )

    if clear_count == 0:
        raise RuntimeError(
            f"Cookie file {path} contained no clear.in / cleartax.in cookies. "
            f"When you exported, were you on app.clear.in?"
        )

    warning: str | None = None
    if soonest_expiry is not None:
        if soonest_expiry < now:
            warning = (
                f"At least one cookie in {path} has already expired "
                f"({datetime.fromtimestamp(soonest_expiry, tz=timezone.utc).isoformat()}). "
                f"Re-export from Cookie-Editor and try again."
            )
        elif soonest_expiry - now < 3600:
            warning = (
                f"Some cookies in {path} expire within the next hour. "
                f"Your run may hit a 401 mid-flight."
            )
    return jar, warning


def _load_via_browser_cookie3(profile: str) -> requests.cookies.RequestsCookieJar:
    """Original browser-cookie3 path. Kept as a fallback for non-ABE Chrome."""
    user_data = chrome_user_data_root()
    cookie_db = user_data / profile / "Network" / "Cookies"
    if not cookie_db.exists():
        # On some setups it's still in the legacy location
        legacy = user_data / profile / "Cookies"
        if legacy.exists():
            cookie_db = legacy
        else:
            raise RuntimeError(
                f"Cookies DB not found at {cookie_db!r} (or legacy path). "
                f"Check that the profile name '{profile}' is correct."
            )

    # Shadow-copy the cookies DB (+ any WAL/SHM/journal sidecars) so we can
    # read while Chrome is open. `_shadow_copy` falls back to a shared-mode
    # Win32 handle if shutil.copy2 hits a PermissionError.
    tmpdir = Path(tempfile.mkdtemp(prefix="clear-ola-cookies-"))
    try:
        copy_path = tmpdir / "Cookies"
        _shadow_copy(cookie_db, copy_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = cookie_db.with_name(cookie_db.name + suffix)
            if sidecar.exists():
                _shadow_copy(sidecar, copy_path.with_name(copy_path.name + suffix))

        jar = requests.cookies.RequestsCookieJar()
        found_any = False
        last_error: Exception | None = None
        for domain in CLEAR_DOMAINS:
            try:
                cj = browser_cookie3.chrome(
                    cookie_file=str(copy_path),
                    domain_name=domain,
                )
            except Exception as e:  # noqa: BLE001 — collect, surface after loop
                last_error = e
                continue
            for c in cj:
                jar.set_cookie(c)
                found_any = True

        if not found_any:
            base = (f"No clear.in / cleartax.in cookies could be read from "
                    f"profile {profile!r}.")
            if last_error is not None:
                raise RuntimeError(
                    f"{base} Likely cause: Chrome's App-Bound Encryption "
                    f"(v127+) is blocking decryption. "
                    f"Underlying error: {last_error!r}\n\n"
                    f"FIX: use the manual cookie-export path. Run "
                    f"`python -m clear_ola cookies-import` for step-by-step "
                    f"instructions. (One-time setup; you re-export only when "
                    f"Clear's session actually expires.)"
                )
            raise RuntimeError(
                f"{base} Are you actually logged into ClearGST in that profile?"
            )

        return jar
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
