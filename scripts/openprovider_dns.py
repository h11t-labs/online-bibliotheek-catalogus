#!/usr/bin/env python3
"""Idempotently point a domain at the Fly app via the Openprovider DNS API.

Reads from the environment:
  OPENPROVIDER_USERNAME / OPENPROVIDER_PASSWORD  - API login
  OBC_DOMAIN                                     - the registered domain (apex)
  FLY_IP4 / FLY_IP6                              - A / AAAA targets (either may be empty)
  FLY_HOST                                       - CNAME target for www (e.g. app.fly.dev)

Ensures: ``A @ -> FLY_IP4``, ``AAAA @ -> FLY_IP6``, ``CNAME www -> FLY_HOST``.
Creates the DNS zone if it doesn't exist yet; otherwise upserts only the records
that are missing or changed. Safe to run on every deploy (idempotent).

No third-party deps (urllib only) so it runs on a bare CI runner.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.openprovider.eu"
TTL = 3600


def _call(method: str, path: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        print(f"network error calling {path}: {e}")
        return 0, {}


def _norm(value: str) -> str:
    return str(value or "").rstrip(".").lower()


def main() -> int:
    user = os.environ.get("OPENPROVIDER_USERNAME", "")
    pw = os.environ.get("OPENPROVIDER_PASSWORD", "")
    domain = os.environ.get("OBC_DOMAIN", "").strip().lower()
    ip4 = os.environ.get("FLY_IP4", "").strip()
    ip6 = os.environ.get("FLY_IP6", "").strip()
    host = os.environ.get("FLY_HOST", "").strip().rstrip(".")
    if not (user and pw and domain and host):
        print("missing OPENPROVIDER_*/OBC_DOMAIN/FLY_HOST — nothing to do")
        return 0

    st, d = _call("POST", "/v1beta/auth/login", body={"username": user, "password": pw})
    token = (d.get("data") or {}).get("token")
    if not token:
        print(f"login failed ({st}): {d}")
        return 1

    desired = []
    if ip4:
        desired.append({"name": domain, "type": "A", "value": ip4, "ttl": TTL, "prio": 0})
    if ip6:
        desired.append({"name": domain, "type": "AAAA", "value": ip6, "ttl": TTL, "prio": 0})
    desired.append({"name": f"www.{domain}", "type": "CNAME", "value": host, "ttl": TTL, "prio": 0})

    st, d = _call("GET", f"/v1beta/dns/zones/{domain}", token)
    if st == 200:
        existing = (d.get("data") or {}).get("records", []) or []

        def find(name: str, typ: str):
            for e in existing:
                if _norm(e.get("name")) == _norm(name) and (e.get("type", "").upper() == typ):
                    return e
            return None

        add, remove = [], []
        for w in desired:
            e = find(w["name"], w["type"])
            if e is None:
                add.append(w)
            elif _norm(e.get("value")) != _norm(w["value"]):
                remove.append({"name": e["name"], "type": e["type"], "value": e["value"]})
                add.append(w)
            else:
                print(f"ok: {w['type']} {w['name']} -> {w['value']} (unchanged)")
        if not add and not remove:
            print("DNS already up to date.")
            return 0
        body = {"name": domain, "records": {"add": add, "remove": remove}}
        st, d = _call("PUT", f"/v1beta/dns/zones/{domain}", token, body)
        print(f"updated zone ({st}): +{len(add)} -{len(remove)} :: {d}")
        return 0 if st in (200, 201) else 1

    # zone doesn't exist yet -> create it with our records
    sld, _, ext = domain.partition(".")
    body = {"domain": {"name": sld, "extension": ext}, "type": "master", "records": desired}
    st, d = _call("POST", "/v1beta/dns/zones", token, body)
    print(f"created zone ({st}): {d}")
    return 0 if st in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
