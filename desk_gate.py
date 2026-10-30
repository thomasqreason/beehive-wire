"""The Market Desk gate (Oct 30, 2026).

The site is static files on GitHub Pages, so the only honest way to keep the desk behind a code is to
publish it encrypted. After render_site() has written site/markets/**/*.html, gate_site() replaces every
page with a small shell: the real page, deflated and sealed with AES-256-GCM under a fresh content key,
plus that key wrapped once per access code (PBKDF2-SHA256, 200k rounds, one salt per build). The shell
asks for a code, unwraps the key in the browser with WebCrypto, decrypts, and writes the page in place.
A right code is remembered in that browser (localStorage bw-desk-code) so every desk page opens itself.

Codes come from the MARKETS_ACCESS_CODES environment variable (a GitHub Actions secret): comma, space or
newline separated, case-insensitive, whitespace ignored. Nothing else issues a code. With no codes set the
desk is published locked for everyone and the log says so, which is the safe failure.

Not gated: the JSON the charts read (series/*.json, screen.json, rs_dist.json): bare numbers, no prose.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import zlib
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


def gate_site(cfg: dict, site_dir: Path, site_name: str, log=print) -> int:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    g = (cfg.get("desk") or {}).get("gate") or {}
    codes = access_codes()
    if not codes:
        log("market desk gate: MARKETS_ACCESS_CODES is empty -- the desk is published LOCKED for everyone")
    key, salt = os.urandom(32), os.urandom(16)
    wraps = []
    for c in codes:
        wk = hashlib.pbkdf2_hmac("sha256", c.encode("utf-8"), salt, ITERATIONS, dklen=32)
        iv = os.urandom(12)
        wraps.append({"iv": _b64(iv), "ct": _b64(AESGCM(wk).encrypt(iv, key, None))})
    env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("markets_gate.html")
    aes = AESGCM(key)
    n = 0
    for path in sorted(site_dir.rglob("*.html")):
        html = path.read_text(encoding="utf-8")
        if 'id="bw-gate"' in html:
            continue                                     # already a shell (a second render in one process)
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        title = m.group(1).strip() if m else site_name
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = comp.compress(html.encode("utf-8")) + comp.flush()
        iv = os.urandom(12)
        blob = {"salt": _b64(salt), "iter": ITERATIONS, "wraps": wraps, "iv": _b64(iv), "ct": _b64(aes.encrypt(iv, data, None))}
        depth = len(path.relative_to(site_dir).parts) - 1
        shell = tpl.render(site_name=site_name, title=title, blob=json.dumps(blob, separators=(",", ":")),
                           request_email=g.get("request_email") or "", root="../" * (depth + 1), desk=cfg.get("desk") or {})
        path.write_text(shell, encoding="utf-8")
        n += 1
    log(f"market desk gate: {n} pages sealed for {len(codes)} code(s)")
    return n
