# An agent buying one call from the kUSD market: discover kUSD-priced providers for a capability, pay the best
# one from its own kUSD wallet (x402 first, k402 fallback), print the result and what it cost in full — inside
# the bounds its operator set.
#
#   python examples/kusd_agent.py --registry http://127.0.0.1:8791 --capability summarize --text "…" [--max-cents 10]
#   (env as kusd_common: the agent's KUSD_KEY_FILE etc.; plus the operator's spending controls:
#    KUSD_SPEND_POLICY = JSON file (SpendPolicy), KUSD_SPEND_LEDGER = sqlite path (default ~/.kusd-market/agent-spend.db).
#    Without KUSD_SPEND_POLICY the agent is bounded only by --max-cents.)
import argparse
import asyncio
import json
import os
import sys

from k402 import KusdPayer, SpendLedger, SpendPolicy, SpendRefused

sys.path.insert(0, os.path.dirname(__file__))
from kusd_common import NETWORK, wallet  # noqa: E402


def spend_controls() -> dict:
    """policy + ledger from the operator's env, or nothing (then KusdPayer runs unbounded, as before)."""
    policy_path = os.environ.get("KUSD_SPEND_POLICY")
    if not policy_path:
        return {}
    return {"policy": SpendPolicy.load(os.path.expanduser(policy_path)),
            "ledger": SpendLedger(os.path.expanduser(os.environ.get("KUSD_SPEND_LEDGER", "~/.kusd-market/agent-spend.db")))}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--capability", default="summarize")
    ap.add_argument("--text", required=True)
    ap.add_argument("--max-cents", type=int, default=10)
    ap.add_argument("--policy", default="registry", choices=["registry", "cheapest", "reputation"])
    a = ap.parse_args()
    payer = KusdPayer(wallet(), registry_url=a.registry, network=NETWORK, **spend_controls())
    try:
        route = await payer.pay_best(a.capability, json_body={"text": a.text, "max_words": 40}, max_price_cents=a.max_cents, policy=a.policy)
    except SpendRefused as e:
        print(json.dumps({"refused": e.code, "reason": e.reason, "spend": e.status}, indent=1))
        return 3
    finally:
        await payer.aclose()
    res = route.result
    print(json.dumps({"provider": route.provider["payee_pubkey"], "endpoint": route.provider["endpoint"], "skipped": route.attempts,
                      "http": res.response.status_code, "result": res.response.json(), "txid": res.txid, "cents": res.cents,
                      "wire": res.wire, "attempts": res.attempts, "topped_up": res.topped_up,
                      "outlay": {"price_cents": res.outlay.price_cents, "box_kas": res.outlay.box_kas_sompi / 1e8, "fee_kas_max": res.outlay.fee_sompi / 1e8},
                      "spend": payer.spend_status()}, indent=1))
    return 0


raise SystemExit(asyncio.run(main()))
