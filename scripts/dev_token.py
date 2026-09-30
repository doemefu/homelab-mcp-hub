"""Local image check helper: throwaway RSA key, JWKS, secrets dir and test tokens. Not for production."""

import argparse
import json
import shutil
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ROOT = Path(__file__).resolve().parents[1]
KID = "dev-key-1"
SUBJECT = "dev-user"
CASES = {
    "valid": ({}, {}),
    "typ-jwt": ({"typ": "JWT"}, {}),
    "wrong-aud": ({}, {"aud": ["claude-mcp-hub"]}),
    "mail-only": ({}, {"scope": ["mail:read"]}),
    "expired": ({}, {"exp": int(time.time()) - 3600}),
}


def init(directory: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (directory / "jwks").mkdir(parents=True, mode=0o755)
    (directory / "secrets").mkdir(mode=0o755)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    (directory / "key.pem").write_bytes(pem)
    (directory / "key.pem").chmod(0o600)
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": KID, "use": "sig", "alg": "RS256"}
    (directory / "jwks" / "jwks.json").write_text(json.dumps({"keys": [public]}))
    shutil.copy(ROOT / "tests" / "fixtures" / "accounts.example.json", directory / "secrets" / "accounts.json")
    (directory / "secrets" / "allowed-subjects").write_text(SUBJECT + "\n")
    for ref in ("icloud-username", "icloud-app-password"):
        (directory / "secrets" / ref).write_text("dev-placeholder")
    for path in [*(directory / "secrets").iterdir(), directory / "jwks" / "jwks.json"]:
        path.chmod(0o644)


def mint(directory: Path, case: str) -> str:
    header, overrides = CASES[case]
    key = serialization.load_pem_private_key((directory / "key.pem").read_bytes(), password=None)
    now = int(time.time())
    claims = {
        "iss": "https://auth.furchert.ch",
        "sub": SUBJECT,
        "aud": ["https://mcp.furchert.ch/mcp"],
        "client_id": "claude-mcp-hub",
        "scope": ["mail:read", "calendar:read"],
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "jti": uuid.uuid4().hex,
    } | overrides
    return jwt.encode(claims, key, algorithm="RS256", headers={"typ": "at+jwt", "kid": KID} | header)  # type: ignore[arg-type]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "mint"])
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--case", choices=sorted(CASES), default="valid")
    args = parser.parse_args()
    if args.command == "init":
        init(args.dir)
    else:
        print(mint(args.dir, args.case))


if __name__ == "__main__":
    main()
