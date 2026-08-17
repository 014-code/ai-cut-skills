from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any


SESSION_CACHE_VERSION = 1
DPAPI_ENTROPY = b"aivideoeditor-usergrowth-session-v1"


class UserGrowthSessionCacheError(RuntimeError):
    """Raised when the encrypted local UserGrowth session cache cannot be processed."""


def account_fingerprint(account: str) -> str:
    normalized = str(account or "").strip().casefold().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def default_session_cache_path(account: str) -> Path | None:
    """Return an account-scoped cache path without exposing the account name."""
    if sys.platform != "win32" or not str(account or "").strip():
        return None
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    suffix = account_fingerprint(account)[:24]
    return root / "AIVideoEditor" / "UserGrowth" / "sessions" / f"{suffix}.bin"


def load_session_cache(path: Path, account: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, UserGrowthSessionCacheError):
        return None
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != SESSION_CACHE_VERSION:
        return None
    if not secrets.compare_digest(
            str(payload.get("account_fingerprint") or ""),
            account_fingerprint(account),
    ):
        return None
    state = payload.get("storage_state")
    if not isinstance(state, dict):
        return None
    if not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
        return None
    return state


def save_session_cache(path: Path, account: str, storage_state: dict[str, Any]) -> None:
    cookies = storage_state.get("cookies")
    origins = storage_state.get("origins")
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise UserGrowthSessionCacheError("UserGrowth 登录会话格式无效")
    payload = {
        "version": SESSION_CACHE_VERSION,
        "account_fingerprint": account_fingerprint(account),
        "storage_state": storage_state,
    }
    protected = _dpapi_protect(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as fp:
            fp.write(protected)
            fp.flush()
            os.fsync(fp.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt >= 7:
                    raise
                time.sleep(min(0.01 * (2 ** attempt), 0.25))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def clear_session_cache(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(data), pointer), buffer


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=True)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=False)


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise UserGrowthSessionCacheError("UserGrowth 登录会话加密缓存仅支持 Windows DPAPI")
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = list(crypt32.CryptProtectData.argtypes)
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    _ = input_buffer, entropy_buffer
    if not success:
        raise UserGrowthSessionCacheError(f"Windows DPAPI 处理失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
