# TLS setup (verified server identity)

The client always verifies the server certificate **and** the name `lan-collab.local`, even
when it connects to a raw IP address. There is no `CERT_NONE` mode. Trust is a CA file passed
explicitly; nothing is installed into the operating-system trust store.

## 1. Generate development certificates

```bash
python -m pip install -r requirements-dev.txt   # provides the 'cryptography' generator
python -m lancollab.common.devcerts --out certs
```

| File | Who gets it | Secret? |
| --- | --- | --- |
| `certs/ca.pem` | every client (`--ca-file certs/ca.pem`) | no |
| `certs/server.pem`, `certs/server-key.pem` | the server only | **key is secret** |
| `certs/ca-key.pem` | nobody; keep offline | **secret** |

`certs/`, `*.pem` and `*.key` are in `.gitignore`. Never commit keys. Add
`--san other.name` or `--ip 192.168.1.20` for extra names; `--force` overwrites.

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

# client: connect by IP, verify the name lan-collab.local against ca.pem
transport = SocketTransport.connect_verified("192.168.1.20", 40000, "certs/ca.pem")
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
