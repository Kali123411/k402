#!/usr/bin/env python3
"""Agent auto-routing over the live k402 service exchange.

Shows the decision `ChannelPayer.pay_best` makes: discover providers for a capability, rank them by
a policy, and preflight each (a cheap unpaid probe) to see which are live k402 pay-gates right now —
the check that lets pay_best skip dead endpoints *before* opening an on-chain channel, and fail over.

Dry-run by default: no key, no node, no funds — it discovers and previews routing against production.

    python examples/route_demo.py summarize --policy cheapest
    python examples/route_demo.py llm:chat  --policy reputation --min-rep 1

The real call is one line (needs a funded payer key + a node):

    res = await payer.pay_best("summarize", json_body={"text": "..."}, policy="cheapest")
    print(res.response.json(), "served by", res.provider["payee_pubkey"][:8])
"""
import argparse
import asyncio

from k402 import ChannelPayer

REGISTRY = "https://x402-compute.68cxgfyr0.workers.dev"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Preview k402 auto-routing for a capability.")
    ap.add_argument("capability", nargs="?", default="summarize", help="e.g. summarize, llm:chat, zk-prove")
    ap.add_argument("--policy", default="cheapest", choices=["registry", "cheapest", "reputation"])
    ap.add_argument("--max-price", type=float, default=None, help="max $/call")
    ap.add_argument("--min-rep", type=float, default=0.0, help="min reputation (KAS settled)")
    args = ap.parse_args()

    # dry-run: a throwaway key, no opener/backend — we only discover() and preflight(), never open a channel
    payer = ChannelPayer("11" * 32, opener=None, backend=None, registry_url=REGISTRY)
    try:
        found = await payer.discover(args.capability, max_price_usd=args.max_price, min_reputation_kas=args.min_rep)
        providers = payer.rank(found, args.policy)
        if not providers:
            print(f"\nno providers for '{args.capability}' (try a different capability or loosen filters)\n")
            return

        print(f"\n{len(providers)} provider(s) for '{args.capability}'  ·  policy = {args.policy}\n")
        print(f"  {'#':<3}{'$/call':<11}{'rep KAS':<9}{'live':<6}{'payee':<15}endpoint")
        print(f"  {'-' * 74}")
        chosen = None
        for i, p in enumerate(providers, 1):
            live = await payer.preflight(p)
            if live and chosen is None:
                chosen = p
            rep = (p.get("reputation") or {}).get("settled_kas", 0)
            print(f"  {i:<3}{str(p.get('price_usd', '')):<11}{str(rep):<9}{('✓' if live else '✕'):<6}"
                  f"{p['payee_pubkey'][:12]}…  {p['endpoint']}")

        print()
        if chosen:
            print(f"→ pay_best routes to {chosen['payee_pubkey'][:12]}…  ({chosen['endpoint']})"
                  f"  at ${chosen.get('price_usd')}/call")
            print("  …and fails over to the next live provider if that call errors.\n")
        else:
            print("→ no live provider for this capability right now.\n")
    finally:
        await payer.aclose()


if __name__ == "__main__":
    asyncio.run(main())
