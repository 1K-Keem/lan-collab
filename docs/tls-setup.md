# TLS setup: directly trusted self-signed server certificate

The admin and clients agree on a trusted repository containing the app and public
`lancollab/client/server-public.pem`. Clients trust that certificate locally, without contacting
GitHub during a handshake. The server proves possession of the matching private key.
Despite its filename, `server-public.pem` contains a complete X.509 certificate, not a raw
public key. `server-secret.pem` contains the private identity key, not TLS session keys.
This is direct certificate trust, not raw public-key pinning. No separate CA is created.

## Generate and distribute

From the project root, using the active virtual environment:

```bash
python -m pip install -r requirements-dev.txt
python -m lancollab.common.devcerts --out certs/direct-trust --bundle-cert
```

The generator creates only:

| File | Usage |
| --- | --- |
| `certs/direct-trust/server-public.pem` | Public self-signed server certificate |
| `certs/direct-trust/server-secret.pem` | Private server key; never distribute or commit |
| `lancollab/client/server-public.pem` | Public copy installed by `--bundle-cert`; distribute with app |

The certificate uses EC P-256, SHA-256, SAN `DNS:lan-collab.local`, serverAuth usage,
and `BasicConstraints(ca=False)`. It is valid for 365 days. Extra `--san` and `--ip` options
add names/addresses. Existing output files and a different bundled certificate are refused
unless `--force` is explicitly supplied. Never generate a new identity on each startup.
Run provisioning only on the server/admin machine; clients download the public certificate.
The CA-free TLS runtime uses standard-library `ssl`; `cryptography` is needed only for
provisioning and tests.

## Client trust selection

1. `--trusted-cert PATH`
2. `LANCOLLAB_TRUSTED_CERT`
3. Bundled `lancollab/client/server-public.pem`

Missing or invalid trust is an error. No fallback to system trust, plaintext TCP or disabled
verification. Public certificate files containing private keys are rejected.

```bash
python -m lancollab.client --host SERVER_IP
python -m lancollab.client --discover
python -m lancollab.client --host SERVER_IP --trusted-cert path/to/server-public.pem
```

The client requires a running LAN Collab server. The hostname verified is
`--server-name` (default `lan-collab.local`), independently of the IP or UDP discovery reply.
Username/password may be collected before connecting, but are sent only after verified TLS.

## API and verification

```python
from lancollab.common.tls import create_server_context, resolve_trusted_cert
from lancollab.common.socket_transport import SocketTransport

context = create_server_context(
    "certs/direct-trust/server-public.pem", "certs/direct-trust/server-secret.pem"
)
# After listener.accept(), in a server worker:
transport = SocketTransport.accept_verified(context, raw_socket)

# Client:
transport = SocketTransport.connect_verified(
    host="127.0.0.1",
    port=40000,
    trusted_cert=resolve_trusted_cert(),
    server_hostname="lan-collab.local",
)
```

`create_client_context()` loads the trusted server certificate using Python's `cafile`
parameter (the library's parameter name does not imply a separate CA in this setup).
`check_hostname=True`, `CERT_REQUIRED` and TLS 1.2+ remain enforced. OpenSSL checks certificate
trust, validity and hostname during `do_handshake()`. TLS proves possession of the private key.
The connector returns a message transport only after success. It does not perform raw
public-key comparison or silently accept a certificate received over the network.
See the [Python ssl documentation](https://docs.python.org/3/library/ssl.html).

## Verify a running server

Start `python -m lancollab.server` in one terminal. In another, use the same
connection simulator used for concurrency checks:

```bash
python -m scripts.simulate_clients --host 127.0.0.1 --clients 1
```

The simulator prints discovery/TCP/TLS details, including the verified certificate
fingerprint. It does not log in unless asked to send synthetic credentials with
`--send-auth`; that option tests rejection, not a successful account login. For a
remote server replace `127.0.0.1` with its address, and use `--trusted-cert PATH`
when its public certificate is not the bundled one. To test hostname rejection:

```bash
python -m scripts.simulate_clients --host 127.0.0.1 --server-name wrong.example
```

## Migration from the old CA model

The bundled CA has been removed. Use the self-signed pair in `certs/direct-trust` and
distribute only `lancollab/client/server-public.pem`. Old deployments should retire their CA files
and CA-signed server pair after switching to the new identity. Replace `--ca-file` with
`--trusted-cert`, `LANCOLLAB_CA_FILE` with `LANCOLLAB_TRUSTED_CERT`, and `--bundle-ca` with
`--bundle-cert`. Python callers use `trusted_cert`, `resolve_trusted_cert`,
`BUNDLED_SERVER_CERT`, and `install_bundled_cert`. DevCerts exposes only `server_cert` and
`server_key`. Update old handshake demos to use the self-signed certificate.

Only the public server certificate is allowed by the new `.gitignore` exception. Keep the
private key on the server; it cannot be recovered from a clone. Include the public certificate
as package data when building a wheel/executable.

## Renewal and failures

A changed server identity must be distributed through the agreed trusted repository before
clients connect. Do not fetch replacement trust from an unverified server. Provision the new
pair in a separate directory, review it, then install the public certificate with
`install_bundled_cert(new_cert, force=True)` and coordinate the server/client rollout.
Keep old keys until rollout is planned; do not use `--force` casually.

| Failure | Result |
| --- | --- |
| Different/untrusted server certificate | TLS certificate verification fails |
| Wrong hostname or expired certificate | TLS certificate verification fails |
| Certificate and private key mismatch | Server context fails to load |
| Missing trust file | Startup fails before prompting for credentials |
| Silent TLS peer | Absolute handshake deadline (default 5 seconds) |

Do not use CERT_NONE, disable hostname checking, or treat UDP replies as proof of identity.
These certificates work across Windows/macOS/Linux through Python/OpenSSL without installing
anything into the operating-system trust store.

## What authenticates the server?

1. Clients obtain the app and `server-public.pem` from the repository agreed with the admin.
   That trusted distribution establishes identity; a self-signature alone does not.
2. The client resolves an IP, connects by TCP, and starts TLS. Discovery supplies an address,
   never a new trust anchor.
3. OpenSSL verifies the presented certificate using the locally trusted certificate, checks
   its validity and the expected hostname, and verifies the server's handshake proof.
4. In a full certificate-authenticated TLS 1.3 handshake, CertificateVerify signs the handshake
   transcript using the server's long-lived private identity key from `server-secret.pem`.
   Copying the public certificate is insufficient to create that proof.
5. Ephemeral (EC)DHE key exchange establishes a shared secret. HKDF derives separate traffic
   keys; Finished authenticates the handshake. Application records use authenticated symmetric
   encryption (the negotiated cipher), not direct encryption with the identity public key.
6. Only after verified TLS completes does the client send AUTH_REQUEST. The CA-free runtime
   performs these cryptographic operations through Python ssl/OpenSSL, not custom crypto code.

The generator's identity key is EC P-256 and the certificate is self-signed with ECDSA/SHA-256.
The ephemeral exchange group and traffic cipher are negotiated separately. TLS 1.2 is also
allowed; the description above specifically illustrates TLS 1.3, as used in the local demo.
See [RFC 8446, authentication and key schedule](https://www.rfc-editor.org/rfc/rfc8446.html).

### Automatic first launch

`python -m lancollab.server` calls `ensure_server_certs()` for the default paths. If both
files are absent it generates an EC P-256 self-signed server certificate and private key,
using cryptography or the system OpenSSL CLI, then updates the public client bundle in this
checkout. Only the public PEM is copied. Existing pairs are not rotated; missing half-pairs
or mismatched keys fail closed. A provisioning lock prevents simultaneous startup generation.
Custom certificate paths still require manual provisioning. Share the new public bundle via
the group's trusted repository before clients on other machines connect.

## Diagnosing rejected connections

The server logs the TLS phase and OpenSSL reason, not just `SSLError`. Each new
source port is a separate connection attempt; it is not a file-transfer progress
update. A peer certificate-rejection alert means the client refused the server
certificate. Read that client's error for the precise verification failure.
The client also prints the trust-file path and expected server name.

Compare the certificate fingerprints on the server and the failing client:

```bash
# Server
openssl x509 -in certs/direct-trust/server-public.pem -noout -fingerprint -sha256
# Client (or substitute the file passed with --trusted-cert)
openssl x509 -in lancollab/client/server-public.pem -noout -fingerprint -sha256
```

If they differ, obtain the intended server's **public** certificate through the
agreed trusted repository/admin channel and use `--trusted-cert PATH`. Do not
copy the secret key or disable verification. Also check certificate expiry, system
clock, and `--server-name` (normally `lan-collab.local`). Restart the server after
changing its certificate files: an already running process retains its loaded
identity. `WRONG_VERSION_NUMBER` or `HTTP_REQUEST` may indicate plaintext traffic
or the wrong destination port; use the LAN Collab TLS client on the TCP service
port. Failed handshakes are closed and their worker slots are released.
