# TLS setup

LAN Collab uses TLS to encrypt client/server traffic and verify the server's identity.
Each server has a self-signed certificate and a matching private key. Clients receive
that server's public certificate from the admin or the group's trusted repository.
Python's `ssl` module verifies the certificate, its validity and the expected server
name before credentials or chat messages are sent. TLS 1.2 or newer is required.

## Certificate files

| File | Purpose |
| --- | --- |
| `certs/direct-trust/server-public.pem` | The server's public certificate |
| `certs/direct-trust/server-secret.pem` | The server's private key; keep it on the server |
| `lancollab/client/server-public.pem` | The public certificate bundled with the client |

`server-public.pem` contains an X.509 certificate. It can be distributed to clients.
`server-secret.pem` must never be shared or committed. The public certificate alone
cannot recreate the private key.

The generated certificate is valid for 365 days and uses EC P-256 with SHA-256.
Its default server name is `lan-collab.local`. Clients verify this name even when
connecting to an IP address, so an IP address change does not require a new certificate.

## Set up the server

Run commands from the project root in your active Python environment.

### Automatic setup

For a new server using the default certificate paths:

```bash
python -m pip install -r requirements-dev.txt
python -m lancollab.server
```

When both server certificate files are absent, the server generates them and copies
the public certificate into `lancollab/client/server-public.pem` in this checkout.
Generation uses `cryptography`, installed above, or the system OpenSSL command.
Later starts reuse the existing pair. If only one file is present or the key does
not match the certificate, startup fails so the pair can be restored.

Custom paths supplied through `--cert` and `--key` require certificates to be
provisioned beforehand.

### Manual generation

To deliberately create a new server identity and replace the bundled certificate:

```bash
python -m pip install -r requirements-dev.txt
python -m lancollab.common.devcerts --out certs/direct-trust --bundle-cert --force
python -m lancollab.server
```

`--bundle-cert` copies the public certificate into the client package. `--force`
allows replacement of existing output files and the bundled certificate, which
already exists in a clone of this repository. This creates a new private key and
certificate: use it for a new deployment or an intentional replacement, not on
each startup. Stop a running server before replacing its files.

The generator also accepts `--san NAME` and `--ip ADDRESS` to add certificate names
or IP addresses.

## Distribute the certificate and connect

After provisioning, distribute `lancollab/client/server-public.pem` through the
admin or the group's trusted repository. Clients need the certificate belonging
to the server they will connect to. Clients do not run the generator.

With the correct certificate bundled in the client:

```bash
python -m lancollab.client --host SERVER_IP --port 40000 --tui
```

To use a separately supplied public certificate:

```bash
python -m lancollab.client --host SERVER_IP --port 40000 --tui --trusted-cert path/to/server-public.pem
```

Clients choose their trusted certificate in this order:

1. `--trusted-cert PATH`
2. The `LANCOLLAB_TRUSTED_CERT` environment variable
3. Bundled `lancollab/client/server-public.pem`

If using the installed `lancollab` command, reinstall with `./setup` or Windows
`setup.bat` after updating the checkout's bundle, or pass `--trusted-cert` explicitly.

`--server-name` sets the expected certificate name and defaults to `lan-collab.local`.
`--discover` can find the server address on the LAN; certificate verification still
applies. Missing or invalid trust files cause an error. Clients never automatically
trust a replacement certificate received over the network, and no certificate needs
to be installed in the operating system's trust store.

## Verify a running server

With the server running, open another terminal:

```bash
python -m scripts.simulate_clients --host 127.0.0.1 --clients 1
```

Replace `127.0.0.1` with the server's address for a remote check. Add
`--trusted-cert PATH` if needed. The simulator reports the connection, negotiated
TLS version and cipher, and verified certificate fingerprint. It does not log in
by default.

## Renew or replace a certificate

Generate a replacement pair in a separate directory so the current pair remains
available during preparation:

```bash
python -m lancollab.common.devcerts --out certs/renewal
```

Distribute the new public certificate through the same trusted channel and
coordinate the change with clients. Stop the server, then start it with the new pair:

```bash
python -m lancollab.server --cert certs/renewal/server-public.pem --key certs/renewal/server-secret.pem
```

Clients must use the new certificate through `--trusted-cert` or an updated bundle.
Changing files alone does not update a running server's loaded certificate; restart
it to apply the change.

## Diagnosing rejected connections

Read the client's verification error and the server's TLS log. The client reports
the trust-file path and expected server name, which help identify incorrect settings.

If OpenSSL is installed, compare the public certificate fingerprints:

```bash
# Server: use the certificate loaded by the server
openssl x509 -in certs/direct-trust/server-public.pem -noout -fingerprint -sha256
# Client: use the actual trusted certificate path
openssl x509 -in lancollab/client/server-public.pem -noout -fingerprint -sha256
```

The fingerprints should match. If they differ, obtain the intended server's public
certificate from the admin or trusted repository and update the client's trust path.

| Problem | What to check |
| --- | --- |
| Untrusted certificate or peer rejected certificate | Client trust file matches the server certificate; read the client-side error |
| Wrong hostname | `--server-name` matches a name in the certificate, normally `lan-collab.local` |
| Expired or not-yet-valid certificate | Certificate validity dates and the machine's clock |
| Certificate/private-key mismatch or missing half of the pair | Restore a matching certificate and private key on the server |
| Missing or invalid trust file | `--trusted-cert`, environment variable or installed bundle path |
| Handshake timeout | Server address, port and reachability; the normal handshake deadline is 5 seconds |
| `WRONG_VERSION_NUMBER` or `HTTP_REQUEST` | Connect to the LAN Collab TLS port using the LAN Collab client |

Keep certificate and hostname verification enabled while resolving connection errors.
