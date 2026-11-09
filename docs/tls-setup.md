# TLS setup (verified server identity)

The client always verifies the server certificate **and** the name `lan-collab.local`, even
when it connects to a raw IP address. There is no `CERT_NONE` mode. Trust is a CA file passed
explicitly; nothing is installed into the operating-system trust store.

## 1. Generate development certificates (once, on one machine)

```bash
python -m pip install -r requirements-dev.txt   # provides the 'cryptography' generator
python -m lancollab.common.devcerts --out certs --bundle-ca
git add lancollab/client/ca.pem                 # the PUBLIC CA only, see section 1b
```

| File | Who gets it | Secret? |
| --- | --- | --- |
| `certs/ca.pem` | every client, via the bundled `lancollab/client/ca.pem` (or `--ca-file`) | no |
| `certs/server.pem`, `certs/server-key.pem` | the server only | **key is secret** |
| `certs/ca-key.pem` | nobody; keep offline | **secret** |

`certs/`, `*.pem` and `*.key` are in `.gitignore`. Never commit keys. Add
`--san other.name` or `--ip 192.168.1.20` for extra names; `--force` overwrites.

## 1b. How clients get `ca.pem` (no copying by hand)

`--bundle-ca` copies the public `ca.pem` to `lancollab/client/ca.pem`. Commit it: every clone,
download or release of the client then carries its own trust anchor, so 20 or 1000 clients need
no extra step. The server never sends it over the network (an attacker in the middle could swap
it); clients trust it because it came with the app.

The client picks its CA in this order:

1. `--ca-file PATH` (explicit; a missing or invalid file is an error, never skipped)
2. the `LANCOLLAB_CA_FILE` environment variable
3. the bundled `lancollab/client/ca.pem`
4. nothing found: the operating-system trust store (right for a publicly trusted certificate,
   wrong for this development CA, so the connection fails with a certificate error)

With the bundled CA, start the client with `python -m lancollab.client --host SERVER_IP`.
No `--ca-file` argument is needed. Use `--discover` instead of `--host SERVER_IP` for LAN
discovery. Certificate and hostname verification remain required in either mode.

Safety rails: `.gitignore` ignores `*.pem` but explicitly allows `lancollab/client/ca.pem`;
a file containing a private key is refused as a trust anchor; `tests/test_trust_anchor.py`
fails if any private key text appears anywhere in the source tree, and checks with `git
check-ignore` that the CA can be committed while every key file stays ignored.

Never run `--bundle-ca` with a CA you did not generate yourself. If the CA key leaks or you
regenerate the CA: run step 1 again with `--force` (it refuses to replace a different bundled CA
otherwise), commit the new `ca.pem`, and ship a new client. Clients holding the old CA will
reject the new server certificate, which is the intended behaviour. Packaging note: the project
runs from source today; if you later build a wheel or exe, include `lancollab/client/ca.pem`
as package data.

## 2. Without Python's `cryptography` (OpenSSL 1.1.1+ / 3.x)

```bash
mkdir certs && cd certs
openssl ecparam -name prime256v1 -genkey -noout -out ca-key.pem
openssl req -x509 -new -key ca-key.pem -sha256 -days 825 -subj "/CN=LAN Collab Development CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.pem
openssl ecparam -name prime256v1 -genkey -noout -out server-key.pem
openssl req -new -key server-key.pem -subj "/CN=lan-collab.local" -out server.csr
printf "subjectAltName=DNS:lan-collab.local\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n" > server.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
  -days 365 -sha256 -extfile server.ext -out server.pem
```

On Windows use Git Bash (ships OpenSSL) or WSL; the files work unchanged on Windows, macOS and
Linux because they are only read through Python's `ssl` module.

## 3. Use them

```python
from lancollab.common.tls import create_server_context
from lancollab.common.socket_transport import SocketTransport

# server (per accepted socket, inside a worker thread, never the accept loop)
ctx = create_server_context("certs/server.pem", "certs/server-key.pem")
transport = SocketTransport.accept_verified(ctx, raw_socket)  # 5 s absolute deadline

# client: connect by IP, verify the name lan-collab.local against the bundled/chosen CA
from lancollab.common.tls import resolve_ca_file

ca_file = resolve_ca_file(args.ca_file)  # --ca-file > $LANCOLLAB_CA_FILE > bundled
transport = SocketTransport.connect_verified("192.168.1.20", 40000, ca_file)
```

## 4. What a failure looks like

| Situation | Result on the client |
| --- | --- |
| Server cert signed by a different CA | `ssl.SSLCertVerificationError` |
| Server cert lacks SAN `lan-collab.local` | `ssl.SSLCertVerificationError` |
| Server never completes the handshake | `TimeoutError` after 5 s total |
| Plain-text client talks to the TLS port | server handshake fails; nothing is dispatched |

Expired certificate: regenerate with `--force` and redistribute `ca.pem` only if the CA itself
changed (the CA lives 825 days, the server certificate 365 days).

## 5. Evidence for the checkpoint

`tests/test_tls.py` proves trusted/untrusted/mismatched certificates, bounded handshakes and,
with a recording relay, that the captured TCP bytes contain neither the chat text nor the
message type. For a Wireshark capture run the demo server and client on two machines and
capture on the LAN interface: after the TLS handshake all application data is
`Application Data` records. Whether TLS alone satisfies the "encrypt/decrypt payload"
milestone is still the open question for the lecturer (protocol-spec section 8).

## Client connection call path

The CLI resolves the CA and endpoint, then calls
`SocketTransport.connect_verified(host=..., port=..., server_hostname=..., ca_file=...)`
directly. The smoke harness calls the same method with its explicit CA argument.
The method creates the TLS context, calls `connect_tls()`, and wraps the verified
TLS socket in a message transport. `connect_tls()` uses Python sockets (the OS
performs the TCP handshake), then completes TLS certificate and hostname verification.
Application framing stays in `SocketTransport`; credentials are sent only after
this call succeeds. There is no dynamic connector lookup or plaintext fallback.
