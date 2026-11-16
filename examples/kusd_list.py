# List a kUSD-priced service in the market, paying the registry's kUSD listing fee through k402 — the listing is
# a provider's own act, signed with the same key that owns its kUSD boxes, so payments reach the lister.
#
#   python examples/kusd_list.py --registry http://127.0.0.1:8791 --endpoint http://host:8792/summarize \
#       --capability summarize --cents 5 [--description "..."]   (env as kusd_common)
import argparse
import asyncio
import json
import os
import sys

from k402 import KusdPayer
from k402.channel import payer_pubkey_from_privkey
from k402.registry import Listing

sys.path.insert(0, os.path.dirname(__file__))
from kusd_common import NETWORK, wallet  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--capability", default="summarize")
    ap.add_argument("--cents", type=int, required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--max-fee-cents", type=int, default=25)
    a = ap.parse_args()
    w = wallet()
    priv = open(os.path.expanduser(os.environ["KUSD_KEY_FILE"])).read().strip()
    pub = payer_pubkey_from_privkey(priv)
    assert pub == w.payee_xonly(), "the listing key must be the kUSD wallet key, or payments go elsewhere"
    listing = Listing(capability=a.capability, endpoint=a.endpoint, payee_pubkey=pub, price_usd=a.cents / 100,
                      network=NETWORK, currency="kUSD", price_cents=a.cents, schemes=["kusd-utxo", "kaspa-utxo"],
                      description=a.description, meta={"wires": ["x402", "k402"]}).sign(priv)
    del priv
    payer = KusdPayer(w, network=NETWORK)
    res = await payer.pay({"endpoint": f"{a.registry}/registry/list"}, json_body=listing.to_dict(), max_price_cents=a.max_fee_cents)
    print(json.dumps({"http": res.response.status_code, "registry": res.response.json(), "fee_txid": res.txid,
                      "fee_cents": res.cents, "wire": res.wire, "topped_up": res.topped_up}, indent=1))
    await payer.aclose()


asyncio.run(main())
