# The kUSD market's registry on TN10: k402's service registry with kUSD listings, verified kUSD settled reports
# and a 10-cent listing fee paid in kUSD through k402 (the registry is a provider like any other).
#
#   uvicorn --app-dir examples kusd_registry:app --host 0.0.0.0 --port 8791
#
# Env (besides kusd_common's): REGISTRY_DB, LISTING_FEE_CENTS (default 10), ORACLE_PRICE (1e-4 USD/KAS, for
# ranking KAS beside kUSD volume; default 268 = $0.0268, the TN10 oracle's figure at deploy), CORS_ORIGINS
# (comma-separated browser origins allowed to read the registry, e.g. the Kaspa Lab market page).
import os

from k402.cors import origins_from_env
from k402.registry_server import create_registry_app

from kusd_common import NETWORK, backend, k402_for, wallet

FEE_CENTS = int(os.environ.get("LISTING_FEE_CENTS", "10"))
DB = os.path.expanduser(os.environ.get("REGISTRY_DB", "~/.kusd-market/registry.db"))

w = wallet()
be = backend()
k402 = k402_for(w, be, kusd_price_cents=FEE_CENTS)
app = create_registry_app(be, network=NETWORK, db_path=DB, k402=k402, kusd=w, kusd_listing_fee_cents=FEE_CENTS,
                          oracle_price=int(os.environ.get("ORACLE_PRICE", "268")),
                          cors_origins=origins_from_env(os.environ.get("CORS_ORIGINS")))
k402.install(app)
