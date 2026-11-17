# Move a directory of x402 listings into the registry as attested listings: each one is submitted to
# POST /registry/submit, so each one is probed live and only the ones that still answer 402 with a valid
# offer are listed. Dead or misdeclared services are reported, not listed — this is the liveness sweep.
#
#   python examples/catalog_migrate.py --from https://www.kaspa-402.org/api/registry/list --registry http://127.0.0.1:8791
#   python examples/catalog_migrate.py --from listings.json ...        (a file with {"listings": [...]} or a list)
#
# Source entries use the site's shape: resource, serviceName, description, capability, scheme, network, payTo,
# amountSompi, tags, kind ('kaspa' | 'base'). Prints one line per entry and a summary; exit code 0 always
# (a dead service is a finding, not a failure of the migration).
import argparse
import asyncio
import json
import sys

import httpx


def load(src: str) -> list:
    if src.startswith("http://") or src.startswith("https://"):
        data = httpx.get(src, timeout=30).json()
    else:
        with open(src) as f:
            data = json.load(f)
    return data["listings"] if isinstance(data, dict) else data


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--only", default="", help="comma-separated resource URLs to migrate (default: all)")
    ap.add_argument("--trust-declared", action="store_true",
                    help="also send the source's payTo and amountSompi, which must then match the live offer; by default only "
                         "scheme and network are declared and the live offer's terms are listed (the source's figures may be stale)")
    a = ap.parse_args()
    entries = load(a.src)
    only = {u.strip() for u in a.only.split(",") if u.strip()}
    ok, bad = [], []
    async with httpx.AsyncClient(timeout=40) as http:
        for e in entries:
            if only and e.get("resource") not in only:
                continue
            body = {"resource": e.get("resource"), "serviceName": e.get("serviceName") or e.get("name") or e.get("resource"),
                    "description": e.get("description", ""), "capability": e.get("capability", ""), "scheme": e.get("scheme"),
                    "network": e.get("network"), "tags": e.get("tags") or [], "kind": e.get("kind", "kaspa")}
            if a.trust_declared:
                body.update({"payTo": e.get("payTo"), "amountSompi": e.get("amountSompi")})
            try:
                r = await http.post(f"{a.registry.rstrip('/')}/registry/submit", json=body)
                j = r.json()
            except (httpx.HTTPError, ValueError) as ex:
                bad.append((e.get("resource"), f"registry unreachable: {ex}"))
                print(f"ERR  {e.get('resource')}  registry: {ex}")
                continue
            if r.status_code == 200 and j.get("ok"):
                lst = j["listing"]
                ok.append(e.get("resource"))
                print(f"OK   {lst['endpoint']}  {lst['scheme']}/{lst['network']}  {(lst['amount_sompi'] or 0) / 1e8:g} KAS  {lst['latency_ms']} ms")
            else:
                bad.append((e.get("resource"), j.get("error", r.text[:200])))
                print(f"DEAD {e.get('resource')}  {j.get('error', r.text[:200])}")
    print(json.dumps({"listed": len(ok), "not_listed": len(bad), "dead": [{"resource": u, "why": w} for u, w in bad]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
