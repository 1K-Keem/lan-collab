# TLS setup: directly trusted self-signed server certificate

The admin and clients agree on a trusted repository containing the app and public
`lancollab/client/server.pem`. Clients trust that certificate locally, without contacting
GitHub during a handshake. The server proves possession of the matching private key.
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
| `certs/direct-trust/server.pem` | Public self-signed server certificate |
| `certs/direct-trust/server-key.pem` | Private server key; never distribute or commit |
| `lancollab/client/server.pem` | Public copy installed by `--bundle-cert`; distribute with app |

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
3. Bundled `lancollab/client/server.pem`

Missing or invalid trust is an error. No fallback to system trust, plaintext TCP or disabled
verification. Public certificate files containing private keys are rejected.

```bash
python -m lancollab.client --host SERVER_IP
python -m lancollab.client --discover
python -m lancollab.client --host SERVER_IP --trusted-cert path/to/server.pem
```

The full CLI still requires the TV1 production server. The hostname verified is
`--server-name` (default `lan-collab.local`), independently of the IP or UDP discovery reply.
Username/password may be collected before connecting, but are sent only after verified TLS.

## API and verification

```python
from lancollab.common.tls import create_server_context, resolve_trusted_cert
from lancollab.common.socket_transport import SocketTransport

context = create_server_context(
    "certs/direct-trust/server.pem", "certs/direct-trust/server-key.pem"
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

## Handshake-only demonstration

After provisioning, run in two terminals from the project root:

```bash
python -m scripts.handshake_server
```

```bash
python -m scripts.handshake_client
```

The server accepts one connection and exits. No AUTH or chat messages are exchanged. To test
rejection, restart the server and run the client with `--server-name wrong.example`.
Use `--cert`/`--key` on the demo server and `--trusted-cert` on the demo client to choose
other files. For two machines use server `--host 0.0.0.0` and client `--host SERVER_IP`.

## Migration from the old CA model

The bundled CA has been removed. Use the self-signed pair in `certs/direct-trust` and
distribute only `lancollab/client/server.pem`. Old deployments should retire their CA files
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
