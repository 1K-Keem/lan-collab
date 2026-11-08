"""Development certificate authority and server certificate generator.

Usage (needs the developer-only ``cryptography`` package, see requirements-dev.txt)::

    python -m lancollab.common.devcerts --out certs --bundle-ca

Creates ``ca.pem`` (give this to clients via ``--ca-file``), ``server.pem`` and
``server-key.pem`` (server only) and ``ca-key.pem`` (keep offline). The server certificate
carries SAN ``DNS:lan-collab.local`` so clients verify the same name regardless of the
server's DHCP address. Private keys are written with mode 0600 and must never be committed
(``*.pem`` and ``certs/`` are in .gitignore). Pure-OpenSSL equivalents are in docs/tls-setup.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from .tls import BUNDLED_CA_FILE, SERVER_HOSTNAME, validate_ca_file


@dataclass(frozen=True)
class DevCerts:
    ca_cert: Path
    ca_key: Path
    server_cert: Path
    server_key: Path


def _write(path: Path, data: bytes, *, private: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    if private:
        try:
            path.chmod(0o600)
        except OSError:  # e.g. Windows ACL-based filesystems
            pass


def generate_dev_certs(
    out_dir: str | Path,
    *,
    hostnames: tuple[str, ...] = (SERVER_HOSTNAME,),
    ip_addresses: tuple[str, ...] = (),
    ca_common_name: str = "LAN Collab Development CA",
    leaf_days: int = 365,
    ca_days: int = 825,
    force: bool = False,
) -> DevCerts:
    """Create a CA and a server certificate chained to it (EC P-256, SHA-256)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "generating certificates needs the 'cryptography' package "
            "(pip install -r requirements-dev.txt) or use the openssl commands in docs/tls-setup.md"
        ) from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = DevCerts(out / "ca.pem", out / "ca-key.pem", out / "server.pem", out / "server-key.pem")
    targets = (paths.ca_cert, paths.ca_key, paths.server_cert, paths.server_key)
    existing = [p for p in targets if p.exists()]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite {existing[0]} (use --force)")

    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)  # tolerate small clock skew

    def pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_common_name)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=ca_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = [x509.DNSName(name) for name in hostnames]
    names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ip_addresses]
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=leaf_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )

    pem = serialization.Encoding.PEM
    _write(paths.ca_cert, ca_cert.public_bytes(pem), private=False)
    _write(paths.ca_key, pem_key(ca_key), private=True)
    _write(paths.server_cert, leaf_cert.public_bytes(pem), private=False)
    _write(paths.server_key, pem_key(leaf_key), private=True)
    return paths


def install_bundled_ca(
    ca_pem: str | Path, destination: Path = BUNDLED_CA_FILE, *, force: bool = False
) -> Path:
    """Copy the *public* CA certificate into the client package so every client carries it."""
    source = Path(validate_ca_file(ca_pem))  # refuses anything containing a private key
    data = source.read_bytes()
    if destination.exists() and destination.read_bytes() != data and not force:
        raise FileExistsError(f"{destination} holds a different CA (use --force to replace it)")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "Certificate generator")
    parser.add_argument("--out", default="certs", help="output directory (default: certs)")
    parser.add_argument(
        "--san", action="append", default=[], help="extra DNS name for the server certificate"
    )
    parser.add_argument(
        "--ip", action="append", default=[], help="extra IP address SAN (optional convenience)"
    )
    parser.add_argument(
        "--bundle-ca",
        action="store_true",
        help="also copy the public ca.pem into lancollab/client/ so clients ship with it",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)
    paths = generate_dev_certs(
        args.out,
        hostnames=(SERVER_HOSTNAME, *args.san),
        ip_addresses=tuple(args.ip),
        force=args.force,
    )
    print(f"CA certificate (distribute to clients): {paths.ca_cert}")
    print(f"Server certificate / key (server only): {paths.server_cert} / {paths.server_key}")
    print(f"CA private key (keep offline):          {paths.ca_key}")
    if args.bundle_ca:
        bundled = install_bundled_ca(paths.ca_cert, force=args.force)
        print(f"Bundled public CA into the client:      {bundled}  (commit this file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
