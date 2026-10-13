"""为自托管虚拟域签发和撤销 mTLS 客户端证书。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


UTC = timezone.utc
REGISTRY_NAME = "credentials.json"
CA_CERT_NAME = "ca-cert.pem"
CA_KEY_NAME = "ca-key.pem"
CRL_NAME = "client-crl.pem"
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class CertificateManagerError(ValueError):
    """证书签发、撤销或状态文件无效。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _naive_utc(value: datetime) -> datetime:
    """Return UTC as a naive datetime for cryptography's portable builder API."""

    return value.astimezone(UTC).replace(tzinfo=None)


def _authority_paths(directory: str | Path) -> tuple[Path, Path, Path, Path]:
    root = Path(directory).expanduser().resolve()
    return (
        root,
        root / CA_CERT_NAME,
        root / CA_KEY_NAME,
        root / REGISTRY_NAME,
    )


def _validate_label(label: str) -> str:
    normalized = str(label).strip()
    if not _SAFE_LABEL.fullmatch(normalized) or normalized.upper() in _WINDOWS_RESERVED_NAMES:
        raise CertificateManagerError(
            "证书名称只能使用 1–48 位英文字母、数字、点、短横线或下划线，且不能是系统保留名"
        )
    return normalized


def _write_new_file(path: Path, content: bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if private else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows access control is inherited from the containing directory.
            pass


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CertificateManagerError("证书颁发机构尚未初始化")
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificateManagerError("证书登记文件无法读取") from exc
    if registry.get("version") != 1 or not isinstance(registry.get("credentials"), list):
        raise CertificateManagerError("证书登记文件格式无效")
    return registry


def _load_ca_key(path: Path, passphrase: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=passphrase.encode("utf-8"),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise CertificateManagerError("无法解锁 CA 私钥，请检查文件和口令") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise CertificateManagerError("CA 私钥算法不受支持")
    return key


def _load_ca_cert(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CertificateManagerError("CA 证书无法读取") from exc


def _build_crl(
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    credentials: list[dict[str, Any]],
    *,
    crl_number: int,
) -> bytes:
    now = _now()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(_naive_utc(now - timedelta(minutes=5)))
        .next_update(_naive_utc(now + timedelta(days=30)))
        .add_extension(x509.CRLNumber(crl_number), critical=False)
    )
    for item in credentials:
        revoked_at = item.get("revoked_at")
        if not revoked_at:
            continue
        revoked_time = datetime.fromisoformat(str(revoked_at))
        if revoked_time.tzinfo is None:
            revoked_time = revoked_time.replace(tzinfo=UTC)
        revoked = (
            x509.RevokedCertificateBuilder()
            .serial_number(int(item["serial_number"]))
            .revocation_date(_naive_utc(revoked_time))
            .add_extension(x509.CRLReason(x509.ReasonFlags.unspecified), critical=False)
            .build()
        )
        builder = builder.add_revoked_certificate(revoked)
    crl = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
    return crl.public_bytes(serialization.Encoding.PEM)


def initialize_authority(directory: str | Path, passphrase: str) -> dict[str, str]:
    """创建加密保存的本地根 CA、登记文件和初始空 CRL。"""

    if len(passphrase) < 12:
        raise CertificateManagerError("CA 私钥口令至少需要 12 个字符")
    root, cert_path, key_path, registry_path = _authority_paths(directory)
    if any(path.exists() for path in (cert_path, key_path, registry_path, root / CRL_NAME)):
        raise CertificateManagerError("目标目录已包含 CA 文件；为避免覆盖，拒绝重新初始化")
    root.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Virtual Domain Local Client CA")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_naive_utc(now - timedelta(minutes=5)))
        .not_valid_after(_naive_utc(now + timedelta(days=3650)))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    encrypted_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    registry = {"version": 1, "crl_number": 1, "credentials": []}
    try:
        _write_new_file(key_path, encrypted_key, private=True)
        _write_new_file(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        _write_json_atomic(registry_path, registry)
        _write_new_file(
            root / CRL_NAME,
            _build_crl(cert, key, [], crl_number=1),
        )
    except Exception:
        # Avoid leaving a partially initialized authority that looks usable.
        for path in (key_path, cert_path, registry_path, root / CRL_NAME):
            path.unlink(missing_ok=True)
        raise
    return {
        "directory": str(root),
        "ca_certificate": str(cert_path),
        "ca_private_key": str(key_path),
        "client_crl": str(root / CRL_NAME),
    }


def issue_client_certificate(
    directory: str | Path,
    output_directory: str | Path,
    name: str,
    passphrase: str,
    *,
    validity_days: int = 365,
) -> dict[str, str]:
    """签发一个仅用于 TLS 客户端认证的证书和本地私钥。"""

    label = _validate_label(name)
    if not 1 <= int(validity_days) <= 3650:
        raise CertificateManagerError("有效期必须在 1 到 3650 天之间")
    root, ca_cert_path, ca_key_path, registry_path = _authority_paths(directory)
    registry = _read_registry(registry_path)
    ca_cert = _load_ca_cert(ca_cert_path)
    ca_key = _load_ca_key(ca_key_path, passphrase)

    credential_id = uuid.uuid4().hex
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Virtual Domain"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"{label}-{credential_id[:8]}"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_naive_utc(now - timedelta(minutes=5)))
        .not_valid_after(_naive_utc(now + timedelta(days=int(validity_days))))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"urn:virtual-domain:client:{credential_id}")]
            ),
            critical=False,
        )
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    serial = str(cert.serial_number)
    created_at = now.isoformat()
    output = Path(output_directory).expanduser().resolve()
    prefix = f"client-{label}-{credential_id[:8]}"
    cert_path = output / f"{prefix}.pem"
    key_path = output / f"{prefix}-key.pem"
    ca_copy_path = output / "ca-cert.pem"
    manifest_path = output / f"{prefix}.json"
    files = (cert_path, key_path, ca_copy_path, manifest_path)
    if any(path.exists() for path in files):
        raise CertificateManagerError("证书输出路径已经存在文件；为避免覆盖，拒绝写入")

    entry = {
        "credential_id": credential_id,
        "name": label,
        "serial_number": serial,
        "created_at": created_at,
        "not_after": (now + timedelta(days=int(validity_days))).isoformat(),
        "revoked_at": None,
    }
    registry["credentials"].append(entry)
    registry["crl_number"] = int(registry.get("crl_number", 0)) + 1
    crl_bytes = _build_crl(
        ca_cert,
        ca_key,
        registry["credentials"],
        crl_number=registry["crl_number"],
    )
    manifest = {
        "credential_id": credential_id,
        "name": label,
        "serial_number": serial,
        "created_at": created_at,
        "not_after": entry["not_after"],
        "certificate": str(cert_path),
        "private_key": str(key_path),
        "ca_certificate": str(ca_copy_path),
    }
    output.mkdir(parents=True, exist_ok=True)
    wrote: list[Path] = []
    try:
        _write_new_file(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        wrote.append(cert_path)
        _write_new_file(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            private=True,
        )
        wrote.append(key_path)
        _write_new_file(ca_copy_path, ca_cert.public_bytes(serialization.Encoding.PEM))
        wrote.append(ca_copy_path)
        _write_new_file(
            manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        wrote.append(manifest_path)
        _write_json_atomic(registry_path, registry)
        _write_json_atomic(
            root / "crl-state.json",
            {"crl_number": registry["crl_number"], "updated_at": _now().isoformat()},
        )
        crl_path = root / CRL_NAME
        temp_crl = root / f".{CRL_NAME}.{uuid.uuid4().hex}.tmp"
        _write_new_file(temp_crl, crl_bytes, private=False)
        os.replace(temp_crl, crl_path)
    except Exception:
        for path in wrote:
            path.unlink(missing_ok=True)
        raise
    return manifest


def list_client_certificates(directory: str | Path) -> list[dict[str, Any]]:
    """列出签发状态，不输出私钥或口令。"""

    _, _, _, registry_path = _authority_paths(directory)
    registry = _read_registry(registry_path)
    now = _now()
    credentials = []
    for item in registry["credentials"]:
        expires_at = datetime.fromisoformat(str(item["not_after"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        revoked_at = item.get("revoked_at")
        status = "revoked" if revoked_at else "expired" if expires_at <= now else "active"
        credentials.append(
            {
                "credential_id": item["credential_id"],
                "name": item["name"],
                "serial_number": item["serial_number"],
                "created_at": item["created_at"],
                "not_after": item["not_after"],
                "revoked_at": revoked_at,
                "status": status,
            }
        )
    return credentials


def _regenerate_crl(directory: str | Path, passphrase: str) -> dict[str, str]:
    root, ca_cert_path, ca_key_path, registry_path = _authority_paths(directory)
    registry = _read_registry(registry_path)
    ca_cert = _load_ca_cert(ca_cert_path)
    ca_key = _load_ca_key(ca_key_path, passphrase)
    registry["crl_number"] = int(registry.get("crl_number", 0)) + 1
    crl_bytes = _build_crl(
        ca_cert,
        ca_key,
        registry["credentials"],
        crl_number=registry["crl_number"],
    )
    crl_path = root / CRL_NAME
    temporary = root / f".{CRL_NAME}.{uuid.uuid4().hex}.tmp"
    _write_new_file(temporary, crl_bytes)
    os.replace(temporary, crl_path)
    _write_json_atomic(registry_path, registry)
    _write_json_atomic(
        root / "crl-state.json",
        {"crl_number": registry["crl_number"], "updated_at": _now().isoformat()},
    )
    return {"client_crl": str(crl_path), "crl_number": str(registry["crl_number"])}


def revoke_client_certificate(
    directory: str | Path,
    credential_id: str,
    passphrase: str,
) -> dict[str, str]:
    """撤销指定证书，并原子更新服务端需要加载的 CRL。"""

    root, ca_cert_path, ca_key_path, registry_path = _authority_paths(directory)
    registry = _read_registry(registry_path)
    ca_cert = _load_ca_cert(ca_cert_path)
    ca_key = _load_ca_key(ca_key_path, passphrase)
    item = next(
        (entry for entry in registry["credentials"] if entry["credential_id"] == credential_id),
        None,
    )
    if item is None:
        raise CertificateManagerError("找不到该客户端证书")
    if item.get("revoked_at"):
        raise CertificateManagerError("该客户端证书已经撤销")
    item["revoked_at"] = _now().isoformat()
    registry["crl_number"] = int(registry.get("crl_number", 0)) + 1
    crl_bytes = _build_crl(
        ca_cert,
        ca_key,
        registry["credentials"],
        crl_number=registry["crl_number"],
    )
    crl_path = root / CRL_NAME
    temporary = root / f".{CRL_NAME}.{uuid.uuid4().hex}.tmp"
    _write_new_file(temporary, crl_bytes)
    os.replace(temporary, crl_path)
    _write_json_atomic(registry_path, registry)
    _write_json_atomic(
        root / "crl-state.json",
        {"crl_number": registry["crl_number"], "updated_at": _now().isoformat()},
    )
    return {
        "credential_id": credential_id,
        "serial_number": str(item["serial_number"]),
        "client_crl": str(crl_path),
        "crl_number": str(registry["crl_number"]),
    }


def _read_passphrase(confirm: bool = False) -> str:
    first = getpass.getpass("CA 私钥口令：")
    if confirm:
        second = getpass.getpass("再次输入 CA 私钥口令：")
        if first != second:
            raise CertificateManagerError("两次输入的 CA 私钥口令不一致")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description="虚拟域本地 mTLS 客户端证书管理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="创建本地 CA")
    init_parser.add_argument("--dir", required=True, help="CA 私有目录")
    issue_parser = subparsers.add_parser("issue", help="签发 mTLS 客户端证书")
    issue_parser.add_argument("--dir", required=True, help="CA 私有目录")
    issue_parser.add_argument("--out-dir", required=True, help="证书输出目录")
    issue_parser.add_argument("--name", required=True, help="凭证用途标签")
    issue_parser.add_argument("--days", type=int, default=365, help="证书有效期（默认 365 天）")
    list_parser = subparsers.add_parser("list", help="列出已签发凭证")
    list_parser.add_argument("--dir", required=True, help="CA 私有目录")
    revoke_parser = subparsers.add_parser("revoke", help="撤销凭证并更新 CRL")
    revoke_parser.add_argument("--dir", required=True, help="CA 私有目录")
    revoke_parser.add_argument("--id", required=True, help="凭证 ID")
    refresh_parser = subparsers.add_parser("refresh-crl", help="续期 CRL 的有效期")
    refresh_parser.add_argument("--dir", required=True, help="CA 私有目录")
    args = parser.parse_args()

    try:
        if args.command == "init":
            passphrase = _read_passphrase(confirm=True)
            if len(passphrase) < 12:
                raise CertificateManagerError("CA 私钥口令至少需要 12 个字符")
            result: Any = initialize_authority(args.dir, passphrase)
        elif args.command == "issue":
            result = issue_client_certificate(
                args.dir,
                args.out_dir,
                args.name,
                _read_passphrase(),
                validity_days=args.days,
            )
        elif args.command == "list":
            result = {"credentials": list_client_certificates(args.dir)}
        elif args.command == "revoke":
            result = revoke_client_certificate(args.dir, args.id, _read_passphrase())
        else:
            result = _regenerate_crl(args.dir, _read_passphrase())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except CertificateManagerError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
