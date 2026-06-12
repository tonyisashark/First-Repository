"""Kalshi API-key request signing.

Scheme (per https://docs.kalshi.com/getting_started/api_keys):

    message   = f"{timestamp_ms}{HTTP_METHOD}{path}"
    signature = base64( RSA-PSS-SHA256(private_key, message) )  # salt = digest length

where ``path`` is the URL path *including* the ``/trade-api/v2`` prefix and
*excluding* any query string. Headers sent with every authenticated request:
``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-SIGNATURE``, ``KALSHI-ACCESS-TIMESTAMP``.
"""

from __future__ import annotations

import base64
import time
from typing import Dict, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key) -> None:
        self.api_key_id = api_key_id
        self._private_key = private_key

    @classmethod
    def from_pem(cls, api_key_id: str, pem_bytes: bytes,
                 password: Optional[bytes] = None) -> "KalshiSigner":
        return cls(api_key_id, load_pem_private_key(pem_bytes, password=password))

    @classmethod
    def load(
        cls,
        api_key_id: str,
        private_key_path: Optional[str] = None,
        private_key_pem: Optional[str] = None,
    ) -> "KalshiSigner":
        if private_key_path:
            with open(private_key_path, "rb") as fh:
                pem = fh.read()
        elif private_key_pem:
            pem = private_key_pem.encode("utf-8")
        else:
            raise ValueError("an API private key (path or PEM contents) is required")
        return cls.from_pem(api_key_id, pem)

    def sign(self, message: str) -> str:
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, path: str,
                timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(f"{ts}{method.upper()}{path}"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
