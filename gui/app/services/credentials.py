"""Windows user-bound encryption for saved shop passwords."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class CredentialError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _convert(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise CredentialError("账号密码加密存储只支持 Windows。")
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise CredentialError("无法加密或读取账号密码，请使用当前 Windows 用户重新导入。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel32.LocalFree(result.data)


def protect(password: str) -> bytes:
    if not password:
        raise CredentialError("密码不能为空。")
    return _convert(password.encode("utf-8"))


def unprotect(blob: bytes) -> str:
    try:
        return _convert(bytes(blob), decrypt=True).decode("utf-8")
    except UnicodeError:
        raise CredentialError("保存的密码无法解密，请重新导入。") from None
