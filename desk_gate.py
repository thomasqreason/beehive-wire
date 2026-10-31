"""The Market Desk gate (Oct 30, 2026).

The site is static files on GitHub Pages, so the only honest way to keep the desk behind a code is to
publish it encrypted. After render_site() has written site/markets/**/*.html, gate_site() replaces every
page with a small shell: the real page, deflated and sealed with AES-256-GCM under a fresh content key,
plus that key wrapped once per access code (PBKDF2-SHA256, 200k rounds, one salt per build). The shell
asks for a code, unwraps the key in the browser with WebCrypto, decrypts, and writes the page in place.
A right code is remembered in that browser (localStorage bw-desk-code) so every desk page opens itself.

Codes come from two places. Hand codes in the MARKETS_ACCESS_CODES environment variable (a GitHub Actions
secret: comma, space or newline separated, case-insensitive) are wrapped into the page itself and work even
if the relay is down. Everything else lives in the quote relay's KV store (relay/quote-worker.js, /gate/):
codes Todd mints on the keys page and codes Stripe buyers claim after checkout. The build fetches the
relay's public key and seals each edition's content key to it (RSA-OAEP); the sealed key rides in the page,
and the relay opens it only for an active code. No secret is shared between GitHub and the relay. With
neither hand codes nor a reachable relay the desk is published locked for everyone, the safe failure.

Not gated: the JSON the charts read (series/*.json, screen.json, rs_dist.json): bare numbers, no prose.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path

ITERATIONS = 200_000


def access_codes() -> list[str]:
    raw = os.environ.get("MARKETS_ACCESS_CODES", "")
    seen, out = set(), []
    for c in re.split(r"[,\s]+", raw):
        c = re.sub(r"\s+", "", c).upper()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _jwk_to_pem(jwk: dict) -> bytes:
    """An RSA public JWK (n, e) as PEM, which `cryptography` loads directly."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    def num(s: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), "big")
    return rsa.RSAPublicNumbers(num(jwk["e"]), num(jwk["n"])).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def gate_site(cfg: dict, site_dir: Path, site_name: str, log=print) -> int:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
                      autoescape=select_autoescape(["html"]))
    env_tpl = env.get_template
    tpl = env.get_template("markets_gate.html")
    desk = cfg.get("desk") or {}
    g = desk.get("gate") or {}
    codes = access_codes()
    relay = (desk.get("quote_relay") or "").rstrip("/")
    key, salt = os.urandom(32), os.urandom(16)
    build = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()
    wrapped = ""
    if relay:
        try:
            import requests
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            r = requests.get(relay + "/gate/pubkey", timeout=15)
            r.raise_for_status()
            jwk = r.json()["jwk"]
            pub = serialization.load_pem_public_key(_jwk_to_pem(jwk))
            wrapped = _b64(pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))
            log(f"market desk gate: build {build} sealed to the relay's key")
        except Exception as e:  # noqa: BLE001
            log(f"market desk gate: could not fetch the relay's public key ({e}); only hand codes will open this build")
    if not codes and not wrapped:
        log("market desk gate: no hand codes and no relay key -- the desk is published LOCKED for everyone")
    # Two unsealed pages: the welcome page a Stripe checkout lands on, and Todd's keys page.
    for name, sub in (("markets_welcome.html", "welcome"), ("markets_keys.html", "keys")):
        d = site_dir / sub
        d.mkdir(exist_ok=True)
        (d / "index.html").write_text(env_tpl(name).render(site_name=site_name, desk=desk, gate_url=relay, root="../../"), encoding="utf-8")
    wraps = []
    for c in codes:
        wk = hashlib.pbkdf2_hmac("sha256", c.encode("utf-8"), salt, ITERATIONS, dklen=32)
        iv = os.urandom(12)
        wraps.append({"iv": _b64(iv), "ct": _b64(AESGCM(wk).encrypt(iv, key, None))})
    aes = AESGCM(key)
    n = 0
    for path in sorted(site_dir.rglob("*.html")):
        html = path.read_text(encoding="utf-8")
        if 'id="bw-gate"' in html or "bw-nogate" in html:
            continue                                     # already a shell, or a page meant to stay open
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        title = m.group(1).strip() if m else site_name
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = comp.compress(html.encode("utf-8")) + comp.flush()
        iv = os.urandom(12)
        blob = {"build": build, "salt": _b64(salt), "iter": ITERATIONS, "wraps": wraps, "wrapped": wrapped, "iv": _b64(iv), "ct": _b64(aes.encrypt(iv, data, None))}
        depth = len(path.relative_to(site_dir).parts) - 1
        shell = tpl.render(site_name=site_name, title=title, blob=json.dumps(blob, separators=(",", ":")),
                           request_email=g.get("request_email") or "", payment_link=g.get("payment_link") or "",
                           price_label=g.get("price_label") or "", video_id=g.get("video_id") or "", gate_url=relay,
                           root="../" * (depth + 1), desk=desk)
        path.write_text(shell, encoding="utf-8")
        n += 1
    log(f"market desk gate: {n} pages sealed for {len(codes)} code(s)")
    return n
