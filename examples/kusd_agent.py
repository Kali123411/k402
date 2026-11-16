# An agent buying one call from the kUSD market: discover kUSD-priced providers for a capability, pay the best
# one from its own kUSD wallet (x402 first, k402 fallback), print the result and what it cost in full.
#
#   python examples/kusd_agent.py --registry http://127.0.0.1:8791 --capability summarize --text "…" [--max-cents 10]
#   (env as kusd_common: the agent's KUSD_KEY_FILE etc.)
import argparse
import asyncio
import json
import os
import sys

from k402 import KusdPayer

sys.path.insert(0, os.path.dirname(__file__))
from kusd_common import NETWORK, wallet  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--capability", default="summarize")
    ap.add_argument("--text", required=True)
    ap.add_argument("--max-cents", type=int, default=10)
    ap.add_argument("--policy", default="registry", choices=["registry", "cheapest", "reputation"])
    a = ap.parse_args()
    payer = KusdPayer(wallet(), registry_url=a.registry, network=NETWORK)
    route = await payer.pay_best(a.capability, json_body={"text": a.text, "max_words": 40}, max_price_cents=a.max_cents, policy=a.policy)
    res = route.result
    print(json.dumps({"provider": route.provider["payee_pubkey"], "endpoint": route.provider["endpoint"], "skipped": route.attempts,
                      "http": res.response.status_code, "result": res.response.json(), "txid": res.txid, "cents": res.cents,
                      "wire": res.wire, "attempts": res.attempts, "topped_up": res.topped_up,
                      "outlay": {"price_cents": res.outlay.price_cents, "box_kas": res.outlay.box_kas_sompi / 1e8, "fee_kas_max": res.outlay.fee_sompi / 1e8}}, indent=1))
    await payer.aclose()


asyncio.run(main())
