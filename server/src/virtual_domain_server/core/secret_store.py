"""Encrypted local storage for server-side model credentials."""

from __future__ import annotations

import json
import os
import tempfile
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - installation-dependent
    Fernet = None  # type: ignore[assignment,misc]

    class InvalidToken(Exception):
        pass


class EncryptedConfigStore:
    """Store JSON model settings encrypted on disk.

    The encryption key can be supplied through ``VD_MODEL_VAULT_KEY`` and kept
    in the service manager's secret store. Otherwise a random key is written to
    a separate owner-readable key file in the same private data directory.
    """

    def __init__(
        self,
        data_directory: str | Path,
        *,
        filename: str = "model-profiles.enc",
        key_filename: str = "model-profiles.key",
        key_env: str = "VD_MODEL_VAULT_KEY",
    ) -> None:
        self.directory = Path(data_directory)
        self.path = self.directory / filename
        self.key_path = self.directory / key_filename
        self.key_env = key_env
        self.directory.mkdir(parents=True, exist_ok=True)
        if Fernet is None:
            raise RuntimeError(
                "加密模型配置需要安装 cryptography；请运行 pip install -e '.[models]'"
            )
        try:
            self._cipher = Fernet(self._load_key())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("模型配置加密密钥格式无效") from exc

    def _load_key(self) -> bytes:
        supplied = os.environ.get(self.key_env, "").strip()
        if supplied:
            try:
                return supplied.encode("ascii")
            except UnicodeEncodeError as exc:
                raise RuntimeError("模型配置加密密钥必须是 Fernet Base64 格式") from exc

        if self.path.exists() and not self.key_path.exists():
            raise RuntimeError(
                "模型配置加密密钥文件缺失；请恢复原密钥或设置 VD_MODEL_VAULT_KEY"
            )
        try:
            encoded = Fernet.generate_key()
            key_file_content = self._protect_local_key(encoded)
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return self._read_key_file()
        except OSError as exc:
            raise RuntimeError("无法创建模型配置加密密钥文件") from exc

        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key_file_content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                self.key_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("无法保存模型配置加密密钥文件") from exc
        return encoded

    def _read_key_file(self) -> bytes:
        try:
            key_file_content = self.key_path.read_bytes()
            # DPAPI output is opaque binary data; stripping it can remove a
            # valid trailing byte when it happens to match ASCII whitespace.
            # Keep the legacy Fernet key-file format tolerant of a text newline.
            if not key_file_content.startswith(b"VD-USER-DPAPI-V1\n"):
                key_file_content = key_file_content.strip()
            encoded = self._unprotect_local_key(key_file_content)
            # Validate here to return a clear local configuration error rather
            # than leaking implementation details from Fernet.
            Fernet(encoded)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("无法读取模型配置加密密钥文件") from exc
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return encoded

    @staticmethod
    def _protect_local_key(encoded: bytes) -> bytes:
        if os.name != "nt":
            return encoded
        prefix = b"VD-USER-DPAPI-V1\n"
        try:
            source_buffer = ctypes.create_string_buffer(encoded)

            class DataBlob(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte)),
                ]

            source = DataBlob(
                len(encoded),
                ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)),
            )
            target = DataBlob()
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            protect = crypt32.CryptProtectData
            protect.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            protect.restype = wintypes.BOOL
            if not protect(
                ctypes.byref(source),
                "Virtual Domain model profile key",
                None,
                None,
                None,
                0x1,
                ctypes.byref(target),
            ):
                raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
            try:
                protected = ctypes.string_at(target.pbData, target.cbData)
            finally:
                kernel32.LocalFree(target.pbData)
            return prefix + protected
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Windows 无法保护模型配置加密密钥") from exc

    @staticmethod
    def _unprotect_local_key(value: bytes) -> bytes:
        prefix = b"VD-USER-DPAPI-V1\n"
        if not value.startswith(prefix):
            if value.startswith(b"VD-USER-DPAPI-"):
                raise RuntimeError("模型配置密钥文件使用了不兼容的系统保护格式")
            return value
        if os.name != "nt":
            raise RuntimeError("该模型配置密钥由 Windows 用户保护，需在原 Windows 用户环境中解密")
        protected = value[len(prefix):]
        try:
            source_buffer = ctypes.create_string_buffer(protected)

            class DataBlob(ctypes.Structure):
                _fields_ = [
                    ("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte)),
                ]

            source = DataBlob(
                len(protected),
                ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)),
            )
            target = DataBlob()
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            unprotect = crypt32.CryptUnprotectData
            unprotect.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            unprotect.restype = wintypes.BOOL
            if not unprotect(
                ctypes.byref(source),
                None,
                None,
                None,
                None,
                0x1,
                ctypes.byref(target),
            ):
                raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
            try:
                return ctypes.string_at(target.pbData, target.cbData)
            finally:
                kernel32.LocalFree(target.pbData)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("当前 Windows 用户无法解密模型配置密钥") from exc

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            plaintext = self._cipher.decrypt(self.path.read_bytes())
            value = json.loads(plaintext.decode("utf-8"))
        except InvalidToken as exc:
            raise RuntimeError(
                "模型配置无法解密；请确认服务端使用原加密密钥启动"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("模型配置文件损坏或无法读取") from exc
        if not isinstance(value, dict):
            raise RuntimeError("模型配置内容必须是 JSON 对象")
        return value

    def save(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise ValueError("模型配置内容必须是 JSON 对象")
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            ciphertext = self._cipher.encrypt(serialized)
        except (TypeError, ValueError) as exc:
            raise ValueError("模型配置包含无法保存的数据") from exc

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.directory,
        )
        temporary_path = Path(temporary_name)
        try:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("无法安全保存模型配置文件") from exc
