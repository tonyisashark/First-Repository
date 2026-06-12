import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.auth import KalshiSigner


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key, pem


def test_headers_shape_and_verifiable_signature(keypair):
    key, pem = keypair
    signer = KalshiSigner.from_pem("my-key-id", pem)
    headers = signer.headers("get", "/trade-api/v2/portfolio/balance",
                             timestamp_ms=1_234_567_890_000)

    assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1234567890000"

    message = "1234567890000GET/trade-api/v2/portfolio/balance"
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises on mismatch


def test_load_from_path(tmp_path, keypair):
    _, pem = keypair
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    signer = KalshiSigner.load("kid", private_key_path=str(path))
    assert signer.headers("POST", "/x")["KALSHI-ACCESS-KEY"] == "kid"
    with pytest.raises(ValueError):
        KalshiSigner.load("kid")
