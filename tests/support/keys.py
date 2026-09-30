import json
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


@dataclass
class TestKey:
    __test__ = False
    kid: str
    private_key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    def jwk(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        data.update(kid=self.kid, use="sig", alg="RS256")
        return data


def jwks(*keys: TestKey) -> dict[str, Any]:
    return {"keys": [k.jwk() for k in keys]}
