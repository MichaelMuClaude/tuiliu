"""Web Push for 推流 (copied from the user's 水与步 app): VAPID keys, subscriptions, and sending one encrypted message.

A home-screen web app on iOS 16.4+ can receive pushes. The page asks for permission, subscribes with
our VAPID public key and posts the subscription here; to notify, we encrypt the message for that
subscription (RFC 8291, aes128gcm) and POST it to the push service named in it (Apple's, for an
iPhone), signed with our VAPID key (RFC 8292). Uses the `cryptography` package (on the Surface it
comes with the system Python), nothing else.

Files in DATA_DIR: vapid.pem (private key, made on first use), push.json ({endpoint: subscription}).
"""
import base64, json, os, struct, time, urllib.error, urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import DATA_DIR

KEY = str(DATA_DIR / "vapid.pem")
SUBS = str(DATA_DIR / "push.json")
# Apple wants a contact in the VAPID claim; set TUILIU_PUSH_CONTACT (mailto: or https:) to use your own.
CONTACT = os.environ.get("TUILIU_PUSH_CONTACT", "https://github.com/MichaelMuClaude/tuiliu")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key():
    if not os.path.exists(KEY):
        os.makedirs(os.path.dirname(KEY), exist_ok=True)
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
    with open(KEY, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), None)


def _raw(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """The VAPID public key the page passes to pushManager.subscribe()."""
    return b64u(_raw(_key().public_key()))


def load_subs() -> dict:
    try:
        with open(SUBS) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save_subs(subs: dict) -> None:
    tmp = SUBS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(subs, fh, indent=1)
    os.replace(tmp, SUBS)


def _vapid(endpoint: str) -> str:
    aud = "/".join(endpoint.split("/")[:3])
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": CONTACT}).encode())
    signing = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(_key().sign(signing, ec.ECDSA(hashes.SHA256())))
    jwt = f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={public_key()}"


def _encrypt(sub: dict, plaintext: bytes) -> bytes:
    """RFC 8291 message encryption with the aes128gcm content coding (RFC 8188), one record."""
    ua_pub = unb64u(sub["keys"]["p256dh"])
    auth = unb64u(sub["keys"]["auth"])
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw(eph.public_key())
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)   # \x02 = last (and only) record
    return salt + struct.pack(">IB", 4096, len(as_pub)) + as_pub + body


def send_one(sub: dict, message: dict) -> int:
    """Send to one subscription; returns the push service's HTTP status (201 = accepted)."""
    req = urllib.request.Request(sub["endpoint"], data=_encrypt(sub, json.dumps(message, ensure_ascii=False).encode()),
                                 method="POST", headers={
        "Authorization": _vapid(sub["endpoint"]), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": "3600", "Urgency": "normal"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def send_all(title: str, body: str, url: str = "./") -> dict:
    """Push to every saved subscription; drops the ones the push service says are gone (404/410)."""
    subs, result = load_subs(), {}
    for endpoint, sub in list(subs.items()):
        try:
            code = send_one(sub, {"title": title, "body": body, "url": url})
        except OSError as e:
            code = str(e)
        result[endpoint[-12:]] = code
        if code in (404, 410):
            del subs[endpoint]
    save_subs(subs)
    return result
