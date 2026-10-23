"""Encrypt saved passwords with Windows user-bound DPAPI; never fall back to plaintext."""
import ctypes
from ctypes import wintypes
import os


class CredentialError(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _convert(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise CredentialError("账号密码加密存储目前支持 Windows，请在 Windows 上使用此功能。")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise CredentialError("无法读取或保存加密密码，请使用原 Windows 用户，或重新录入密码。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(result.data, 0, result.size)
        kernel.LocalFree(result.data)


def protect(password: str) -> bytes:
    if not password:
        raise CredentialError("密码不能为空。")
    return protect_bytes(password.encode("utf-8"))


def unprotect(blob: bytes) -> str:
    try:
        return unprotect_bytes(blob).decode("utf-8")
    except UnicodeError:
        raise CredentialError("保存的密码无法解密，请重新录入。") from None


def protect_bytes(value: bytes) -> bytes:
    """Protect non-empty binary application secrets for the current Windows user."""
    if not value:
        raise CredentialError("需要加密的内容不能为空。")
    return _convert(bytes(value))


def unprotect_bytes(blob: bytes) -> bytes:
    if not blob:
        raise CredentialError("加密内容为空。")
    return _convert(bytes(blob), decrypt=True)
