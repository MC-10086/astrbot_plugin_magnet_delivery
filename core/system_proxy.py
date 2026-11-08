"""Resolve the current user's Windows proxy for one HTTPS request."""

import ctypes
from ctypes import wintypes


class _UserProxy(ctypes.Structure):
    _fields_ = [
        ("auto_detect", wintypes.BOOL),
        ("pac_url", ctypes.c_void_p),
        ("proxy", ctypes.c_void_p),
        ("bypass", ctypes.c_void_p),
    ]


class _AutoProxyOptions(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD),
        ("detect_flags", wintypes.DWORD),
        ("pac_url", wintypes.LPCWSTR),
        ("reserved", ctypes.c_void_p),
        ("reserved_flags", wintypes.DWORD),
        ("auto_logon", wintypes.BOOL),
    ]


class _ProxyInfo(ctypes.Structure):
    _fields_ = [
        ("access", wintypes.DWORD),
        ("proxy", ctypes.c_void_p),
        ("bypass", ctypes.c_void_p),
    ]


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = args
    return function


def _string(pointer):
    return ctypes.wstring_at(pointer) if pointer else ""


def windows_proxy_rules(url: str) -> tuple[str, str]:
    """Return the proxy and bypass strings. An empty proxy means DIRECT."""
    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    free = _bind(ctypes.WinDLL("kernel32"), "GlobalFree", ctypes.c_void_p, ctypes.c_void_p)
    get_user = _bind(
        winhttp, "WinHttpGetIEProxyConfigForCurrentUser", wintypes.BOOL,
        ctypes.POINTER(_UserProxy),
    )
    user = _UserProxy()
    try:
        if not get_user(ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        pac_url, manual, bypass = map(_string, (user.pac_url, user.proxy, user.bypass))
    finally:
        for pointer in (user.pac_url, user.proxy, user.bypass):
            if pointer:
                free(pointer)
    if not pac_url and not user.auto_detect:
        return manual, bypass

    open_session = _bind(
        winhttp, "WinHttpOpen", ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    )
    close_session = _bind(winhttp, "WinHttpCloseHandle", wintypes.BOOL, ctypes.c_void_p)
    get_proxy = _bind(
        winhttp, "WinHttpGetProxyForUrl", wintypes.BOOL, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.POINTER(_AutoProxyOptions), ctypes.POINTER(_ProxyInfo),
    )
    no_proxy = 1  # WINHTTP_ACCESS_TYPE_NO_PROXY
    session = open_session("AstrBot", no_proxy, None, None, 0)
    if not session:
        raise ctypes.WinError(ctypes.get_last_error())
    flags = (1 if user.auto_detect else 0) | (2 if pac_url else 0)
    # WINHTTP_AUTO_DETECT_TYPE_DHCP | WINHTTP_AUTO_DETECT_TYPE_DNS_A.
    options = _AutoProxyOptions(flags, 3 if user.auto_detect else 0, pac_url or None, None, 0, False)
    result = _ProxyInfo()
    try:
        if not get_proxy(session, url, ctypes.byref(options), ctypes.byref(result)):
            error = ctypes.get_last_error()
            if not pac_url and error == 12180:  # No WPAD configuration found.
                return manual, bypass
            raise ctypes.WinError(error)
        if result.access == no_proxy:
            return "", ""
        return _string(result.proxy), _string(result.bypass)
    finally:
        for pointer in (result.proxy, result.bypass):
            if pointer:
                free(pointer)
        close_session(session)
