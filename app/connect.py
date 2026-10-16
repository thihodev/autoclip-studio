"""AutoClip Connect: connect TikTok without a developer app (the Worker in services/connect).

The Worker holds a TikTok developer app. Sign-in starts with a one-time ECDH (P-256) key made here; the Worker
sends the tokens back sealed for that key, so only this AutoClip can open them, and keeps nothing. Access tokens
last 24 hours and are renewed through the Worker (it has the client secret); posting goes straight to TikTok.
"""
import base64
import json
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import CONNECT_URL

SEAL_INFO = b"autoclip-connect v1"


def available() -> bool:
    return bool(CONNECT_URL)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A one-time key pair for one sign-in → (private key, public key for the Worker)."""
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, _b64u(raw)


def start_url(platform: str, return_url: str, public_key: str, state: str) -> str:
    return f"{CONNECT_URL}/{platform}/start?" + urlencode({"return": return_url, "key": public_key, "state": state})


def open_sealed(sealed: str, key: ec.EllipticCurvePrivateKey) -> dict:
    """Decrypt what the Worker sealed for `key`: ephemeral public key (65) | IV (12) | AES-GCM ciphertext."""
    blob = _b64d(sealed)
    eph, iv, ct = blob[:65], blob[65:77], blob[77:]
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph))
    aes = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph, info=SEAL_INFO).derive(shared)
    return json.loads(AESGCM(aes).decrypt(iv, ct, None))


async def refresh(platform: str, refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{CONNECT_URL}/{platform}/refresh", json={"refresh_token": refresh_token})
    data = r.json()
    if r.status_code >= 400 or "access_token" not in data:
        raise RuntimeError(f"AutoClip Connect couldn't renew the {platform} sign-in: "
                           f"{data.get('error_description') or data.get('error') or r.status_code}")
    return data
