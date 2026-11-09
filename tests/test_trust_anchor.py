"""Bundled trust anchor: resolution order, safety checks and repository hygiene."""

import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from lancollab.common.contracts import Envelope
from lancollab.common.socket_transport import SocketTransport
from lancollab.common.tls import (
    BUNDLED_SERVER_CERT,
    TRUST_ENV_VAR,
    create_server_context,
    resolve_trusted_cert,
    validate_trusted_cert,
)

try:
    from lancollab.common.devcerts import generate_dev_certs, install_bundled_cert

    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

REPO = Path(__file__).resolve().parents[1]
# A real PEM private-key header. Code that merely *mentions* the words is not a match.
PEM_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


@unittest.skipUnless(HAVE_CRYPTO, "needs the developer-only 'cryptography' package")
class ResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.good = generate_dev_certs(base / "good")
        cls.other = generate_dev_certs(base / "other")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_generator_creates_only_self_signed_server_identity(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        self.assertEqual(
            {path.name for path in self.good.server_cert.parent.iterdir()},
            {"server-public.pem", "server-secret.pem"},
        )
        cert = x509.load_pem_x509_certificate(self.good.server_cert.read_bytes())
        cert.verify_directly_issued_by(cert)
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        key = serialization.load_pem_private_key(self.good.server_key.read_bytes(), password=None)
        self.assertEqual(cert.public_key().public_numbers(), key.public_key().public_numbers())
        with self.assertRaises(FileExistsError):
            generate_dev_certs(self.good.server_cert.parent)

    def test_precedence_explicit_then_env_then_bundled(self) -> None:
        env = {TRUST_ENV_VAR: str(self.other.server_cert)}
        bundled = self.good.server_cert
        self.assertEqual(
            resolve_trusted_cert(str(self.good.server_cert), environ=env, bundled=bundled),
            str(self.good.server_cert),
        )
        self.assertEqual(
            resolve_trusted_cert(None, environ=env, bundled=bundled), str(self.other.server_cert)
        )
        self.assertEqual(resolve_trusted_cert(None, environ={}, bundled=bundled), str(bundled))

    def test_missing_trust_fails_closed(self) -> None:
        missing = Path(self.tmp.name) / "no-such-server-public.pem"
        with self.assertRaises(FileNotFoundError):
            resolve_trusted_cert(None, environ={}, bundled=missing)

    def test_named_but_bad_files_are_errors_not_fallbacks(self) -> None:
        bundled = self.good.server_cert
        with self.assertRaises(FileNotFoundError):
            resolve_trusted_cert("/nonexistent/server-public.pem", environ={}, bundled=bundled)
        with self.assertRaises(FileNotFoundError):
            resolve_trusted_cert(None, environ={TRUST_ENV_VAR: "/nonexistent"}, bundled=bundled)
        junk = Path(self.tmp.name) / "junk.pem"
        junk.write_text("this is not a certificate")
        with self.assertRaises(ValueError):
            resolve_trusted_cert(str(junk), environ={}, bundled=bundled)

    def test_a_private_key_is_refused_as_a_trust_anchor(self) -> None:
        for key_file in (self.good.server_key,):
            with self.subTest(file=key_file.name), self.assertRaises(ValueError) as ctx:
                validate_trusted_cert(key_file)
            self.assertIn("private key", str(ctx.exception))
        both = Path(self.tmp.name) / "both.pem"
        both.write_text(self.good.server_cert.read_text() + self.good.server_key.read_text())
        with self.assertRaises(ValueError):
            validate_trusted_cert(both)

    def test_bundled_certificate_is_enough_to_connect_and_a_foreign_one_is_not(self) -> None:
        server_ctx = create_server_context(str(self.good.server_cert), str(self.good.server_key))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        self.addCleanup(listener.close)

        def serve() -> None:
            while True:
                try:
                    raw, _ = listener.accept()
                except OSError:
                    return
                try:
                    transport = SocketTransport.accept_verified(server_ctx, raw)
                    msg = transport.receive(timeout=3)
                    transport.send(Envelope("CHAT_ACK", msg.request_id, {"status": "queued"}))
                    transport.close()
                except Exception:  # noqa: BLE001  failed handshakes are expected here
                    pass

        threading.Thread(target=serve, daemon=True).start()
        port = listener.getsockname()[1]

        bundled = Path(self.tmp.name) / "client" / "server-public.pem"
        install_bundled_cert(self.good.server_cert, bundled)
        trusted = resolve_trusted_cert(None, environ={}, bundled=bundled)
        with SocketTransport.connect_verified("127.0.0.1", port, trusted) as transport:
            transport.send(Envelope("CHAT_GLOBAL", "r1", {"text": "hi"}))
            self.assertEqual(transport.receive(timeout=3).payload, {"status": "queued"})

        foreign = resolve_trusted_cert(str(self.other.server_cert), environ={}, bundled=bundled)
        with self.assertRaises(ssl.SSLCertVerificationError):
            SocketTransport.connect_verified("127.0.0.1", port, foreign)

    def test_install_bundled_cert_rules(self) -> None:
        dest = Path(self.tmp.name) / "inst" / "client" / "server-public.pem"
        install_bundled_cert(self.good.server_cert, dest)
        self.assertEqual(dest.read_bytes(), self.good.server_cert.read_bytes())
        install_bundled_cert(self.good.server_cert, dest)  # same certificate again: fine
        with self.assertRaises(FileExistsError):  # a different certificate needs --force
            install_bundled_cert(self.other.server_cert, dest)
        install_bundled_cert(self.other.server_cert, dest, force=True)
        self.assertEqual(dest.read_bytes(), self.other.server_cert.read_bytes())
        with self.assertRaises(ValueError):  # keys can never be bundled
            install_bundled_cert(self.good.server_key, Path(self.tmp.name) / "evil.pem")
        self.assertFalse((Path(self.tmp.name) / "evil.pem").exists())


class RepositoryHygieneTests(unittest.TestCase):
    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "certs", "data", "downloads", "uploads"}

    def test_no_private_key_material_anywhere_in_the_source_tree(self) -> None:
        offenders = []
        for path in REPO.rglob("*"):
            if not path.is_file() or self.SKIP_DIRS & set(path.relative_to(REPO).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                if PEM_PRIVATE_KEY.search(text):
                    offenders.append(str(path.relative_to(REPO)))
            except OSError:
                continue
        self.assertEqual(offenders, [], "private key material must never be committed")

    @unittest.skipUnless(HAVE_CRYPTO, "needs the developer-only 'cryptography' package")
    def test_the_scan_really_detects_a_pem_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            keys = generate_dev_certs(Path(tmp))
            for key_file in (keys.server_key,):
                self.assertTrue(PEM_PRIVATE_KEY.search(key_file.read_text()))
            self.assertFalse(PEM_PRIVATE_KEY.search(keys.server_cert.read_text()))

    def test_bundled_certificate_is_self_signed_server_certificate(self) -> None:
        if not BUNDLED_SERVER_CERT.is_file():
            self.skipTest("no certificate bundled yet (run devcerts --bundle-cert)")
        validate_trusted_cert(BUNDLED_SERVER_CERT)
        if HAVE_CRYPTO:
            from cryptography import x509

            cert = x509.load_pem_x509_certificate(BUNDLED_SERVER_CERT.read_bytes())
            self.assertFalse(
                cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            )
            cert.verify_directly_issued_by(cert)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignore_tracks_the_public_certificate_but_never_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(REPO / ".gitignore", root / ".gitignore")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)

            def ignored(rel: str) -> bool:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x")
                result = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root)
                return result.returncode == 0

            self.assertFalse(
                ignored("lancollab/client/server-public.pem"),
                "server-public.pem must be committable",
            )
            for secret in (
                "certs/direct-trust/server-secret.pem",
                "certs/direct-trust/server-public.pem",
                "lancollab/client/server-secret.pem",
                "server-secret.pem",
                "lancollab/server/server.key",
            ):
                with self.subTest(secret=secret):
                    self.assertTrue(ignored(secret), f"{secret} must stay out of Git")


if __name__ == "__main__":
    unittest.main()
