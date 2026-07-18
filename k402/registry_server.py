# The registry SERVICE: providers publish signed listings, agents discover them, and reputation is
# derived from on-chain channel closes. The registry never touches money — it verifies signatures
# and reads the chain, but settlement happens directly between payer and provider.
#
#   app = create_registry_app(backend=NodeBackend("ws://127.0.0.1:17110"), network="mainnet")
#   # uvicorn thismodule:app  ->  POST /registry/list, GET /registry/search, a web UI at /
#
# v1 scope: signed listings (proves payee-key control), optional on-chain stake existence check,
# reputation = verified settled-KAS volume from reported closes. Listing-fee gating and full
# registry federation are documented next steps (PLAN-service-exchange-v1.md).
# NOTE: no `from __future__ import annotations` here — FastAPI must see the real Request type on the
# route handlers (a stringified annotation makes it treat `request` as a query param).
import json
import sqlite3
import threading
import time
from typing import Optional

from .registry import Listing


def _payee_address(payee_pubkey: str, network: str) -> str:
    from kaspa import PublicKey
    # address prefix is by network TYPE (mainnet | testnet | …), not id: testnet-10/-11 both use
    # the 'kaspatest' prefix, so map any testnet-* id to 'testnet' for address derivation.
    addr_net = "testnet" if network.startswith("testnet") else network
    return str(PublicKey(payee_pubkey).to_address(addr_net))


async def _received_from_tx(backend, address: str, txid: str) -> int:
    """Sompi the address holds in a UTXO created by `txid` (how much a close paid the payee).
    Works while the output is unspent — providers report closes promptly, so this is reliable
    in practice; a fuller version would read tx history from an indexer."""
    txid = txid.lower()
    total = 0
    for e in await backend.utxos(address):
        op = e.get("outpoint", {})
        if str(op.get("transactionId", op.get("transaction_id", ""))).lower() == txid:
            ue = e.get("utxoEntry", e.get("utxo_entry", {}))
            total += int(ue.get("amount", 0))
    return total


async def _outpoint_exists(backend, address: str, outpoint: str) -> Optional[int]:
    """Value at `outpoint` (txid:index) if it exists at address, else None — for stake checks."""
    try:
        txid, idx = outpoint.split(":")
        idx = int(idx)
    except (ValueError, AttributeError):
        return None
    for e in await backend.utxos(address):
        op = e.get("outpoint", {})
        if str(op.get("transactionId", op.get("transaction_id", ""))).lower() == txid.lower() \
                and int(op.get("index", -1)) == idx:
            return int(e.get("utxoEntry", e.get("utxo_entry", {})).get("amount", 0))
    return None


def create_registry_app(backend, network: str = "mainnet", db_path: str = "registry.db",
                        listing_fee_sompi: int = 0, k402=None):
    """Build the registry FastAPI app. `backend` is a k402 ChainBackend on `network` (used to verify
    reputation closes + stake outpoints). `network` is the kaspa SDK id ('mainnet' | 'testnet-10')."""
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    _lock = threading.Lock()

    def db():
        c = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    with db() as _c:
        _c.execute("""CREATE TABLE IF NOT EXISTS listings (
            payee_pubkey TEXT NOT NULL, capability TEXT NOT NULL, endpoint TEXT NOT NULL,
            price_usd REAL NOT NULL, network TEXT NOT NULL, schemes TEXT NOT NULL,
            channel_terms TEXT NOT NULL, stake_outpoint TEXT, stake_sompi INTEGER DEFAULT 0,
            meta TEXT NOT NULL, listed_at INTEGER NOT NULL, sig TEXT NOT NULL,
            PRIMARY KEY (payee_pubkey, capability))""")
        _c.execute("""CREATE TABLE IF NOT EXISTS reputation (
            payee_pubkey TEXT PRIMARY KEY, settled_sompi INTEGER NOT NULL DEFAULT 0,
            close_count INTEGER NOT NULL DEFAULT 0, first_settled INTEGER, last_settled INTEGER)""")
        _c.execute("""CREATE TABLE IF NOT EXISTS settled_closes (
            close_txid TEXT PRIMARY KEY, payee_pubkey TEXT NOT NULL, sompi INTEGER NOT NULL, t INTEGER)""")

    app = FastAPI(title="k402 service registry")

    def _rep(payee_pubkey: str) -> dict:
        with db() as c:
            row = c.execute("SELECT settled_sompi, close_count, first_settled, last_settled "
                            "FROM reputation WHERE payee_pubkey=?", (payee_pubkey,)).fetchone()
        if not row:
            return {"settled_kas": 0.0, "closes": 0, "first_settled": None, "last_settled": None}
        return {"settled_kas": round(row[0] / 1e8, 4), "closes": row[1],
                "first_settled": row[2], "last_settled": row[3]}

    # ---------------------------------------------------------------- list / delist
    @app.post("/registry/list")
    async def register(request: Request):
        # optional listing-fee gate (dogfoods the payment rail; off by default in v1)
        if listing_fee_sompi > 0 and k402 is not None:
            hdr = request.headers.get("X-K402-Payment")
            if not hdr:
                from .server import PaymentRequired
                raise PaymentRequired([await k402.create_offer(listing_fee_sompi, "registry listing")],
                                      "listing fee required")
            await k402.verify(hdr, listing_fee_sompi, "registry listing")
        try:
            listing = Listing.from_dict(await request.json())
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse({"error": f"bad listing: {e}"}, status_code=400)
        if not listing.verify():
            return JSONResponse({"error": "signature invalid — sign the canonical listing with the "
                                          "payee key"}, status_code=400)
        if listing.network != network:
            return JSONResponse({"error": f"this registry serves '{network}', listing is '{listing.network}'"},
                                status_code=400)
        # optional stake: verify the outpoint exists on the payee's address (skin in the game)
        stake_sompi = 0
        if listing.stake_outpoint:
            addr = _payee_address(listing.payee_pubkey, network)
            v = await _outpoint_exists(backend, addr, listing.stake_outpoint)
            if v is None:
                return JSONResponse({"error": "stake_outpoint not found on the payee address"},
                                    status_code=400)
            stake_sompi = v
        with _lock, db() as c:
            c.execute("""INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (listing.payee_pubkey, listing.capability, listing.endpoint, listing.price_usd,
                       listing.network, json.dumps(listing.schemes), json.dumps(listing.channel_terms),
                       listing.stake_outpoint, stake_sompi, json.dumps(listing.meta),
                       listing.listed_at, listing.sig))
        return {"ok": True, "payee_pubkey": listing.payee_pubkey, "capability": listing.capability,
                "stake_sompi": stake_sompi}

    @app.delete("/registry/list")
    async def delist(request: Request):
        """Signed delist: body is a listing (same payee_pubkey+capability) with a valid signature."""
        try:
            listing = Listing.from_dict(await request.json())
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        if not listing.verify():
            return JSONResponse({"error": "signature invalid"}, status_code=400)
        with _lock, db() as c:
            c.execute("DELETE FROM listings WHERE payee_pubkey=? AND capability=?",
                      (listing.payee_pubkey, listing.capability))
        return {"ok": True, "delisted": listing.capability}

    # ---------------------------------------------------------------- search / provider
    @app.get("/registry/search")
    def search(capability: str = "", max_price_usd: float = 0, min_reputation_kas: float = 0,
               network_filter: str = "", limit: int = 20):
        q = "SELECT * FROM listings WHERE 1=1"
        args: list = []
        if capability:
            q += " AND capability=?"; args.append(capability)
        if max_price_usd:
            q += " AND price_usd<=?"; args.append(max_price_usd)
        with db() as c:
            cols = [d[0] for d in c.execute("SELECT * FROM listings LIMIT 0").description]
            rows = [dict(zip(cols, r)) for r in c.execute(q, args).fetchall()]
        out = []
        for r in rows:
            rep = _rep(r["payee_pubkey"])
            if rep["settled_kas"] < min_reputation_kas:
                continue
            out.append({
                "capability": r["capability"], "endpoint": r["endpoint"],
                "payee_pubkey": r["payee_pubkey"], "price_usd": r["price_usd"],
                "network": r["network"], "schemes": json.loads(r["schemes"]),
                "channel_terms": json.loads(r["channel_terms"]),
                "stake_kas": round((r["stake_sompi"] or 0) / 1e8, 4),
                "meta": json.loads(r["meta"]), "listed_at": r["listed_at"], "reputation": rep})
        # rank: reputation (settled volume) desc, then price asc, then oldest listing (proven longer)
        out.sort(key=lambda x: (-x["reputation"]["settled_kas"], x["price_usd"], x["listed_at"]))
        return {"providers": out[:limit], "count": len(out)}

    @app.get("/registry/provider/{payee_pubkey}")
    def provider(payee_pubkey: str):
        with db() as c:
            cols = [d[0] for d in c.execute("SELECT * FROM listings LIMIT 0").description]
            rows = [dict(zip(cols, r)) for r in
                    c.execute("SELECT * FROM listings WHERE payee_pubkey=?", (payee_pubkey,)).fetchall()]
        if not rows:
            return JSONResponse({"error": "unknown provider"}, status_code=404)
        return {"payee_pubkey": payee_pubkey, "reputation": _rep(payee_pubkey),
                "address": _payee_address(payee_pubkey, network),
                "listings": [{"capability": r["capability"], "endpoint": r["endpoint"],
                              "price_usd": r["price_usd"], "meta": json.loads(r["meta"]),
                              "listed_at": r["listed_at"]} for r in rows]}

    # ---------------------------------------------------------------- reputation (verified closes)
    @app.post("/registry/settled")
    async def settled(req: dict):
        """Report a channel close; the registry VERIFIES on-chain that it paid the payee, then
        credits settled volume. Providers can't inflate (we check the tx); omitting closes only
        hurts their own reputation."""
        payee_pubkey = str(req.get("payee_pubkey", "")).lower()
        close_txid = str(req.get("close_txid", "")).lower()
        if len(bytes.fromhex(payee_pubkey or "x")) != 32 or not close_txid:
            return JSONResponse({"error": "payee_pubkey (32-byte hex) and close_txid required"},
                                status_code=400)
        with db() as c:
            if c.execute("SELECT 1 FROM settled_closes WHERE close_txid=?", (close_txid,)).fetchone():
                return {"ok": True, "already_counted": True}
        addr = _payee_address(payee_pubkey, network)
        try:
            sompi = await _received_from_tx(backend, addr, close_txid)
        except Exception as e:
            return JSONResponse({"error": f"chain unreachable: {type(e).__name__}"}, status_code=502)
        if sompi <= 0:
            return JSONResponse({"error": "close_txid does not pay this payee (unverified or "
                                          "already swept)"}, status_code=400)
        now = int(time.time())
        with _lock, db() as c:
            c.execute("INSERT OR IGNORE INTO settled_closes VALUES (?,?,?,?)",
                      (close_txid, payee_pubkey, sompi, now))
            c.execute("""INSERT INTO reputation (payee_pubkey, settled_sompi, close_count, first_settled, last_settled)
                         VALUES (?,?,1,?,?)
                         ON CONFLICT(payee_pubkey) DO UPDATE SET
                           settled_sompi=settled_sompi+excluded.settled_sompi,
                           close_count=close_count+1, last_settled=excluded.last_settled""",
                      (payee_pubkey, sompi, now, now))
        return {"ok": True, "credited_sompi": sompi, "reputation": _rep(payee_pubkey)}

    @app.get("/healthz")
    def healthz():
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        return {"ok": True, "network": network, "listings": n}

    @app.get("/", response_class=HTMLResponse)
    def ui():
        return _UI

    return app


# ---------------------------------------------------------------- self-contained web UI
_UI = """<!doctype html><html><head><meta charset=utf-8><title>k402 service exchange</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
:root{color-scheme:light dark;--bg:#0e1116;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;--acc:#3fb950}
@media (prefers-color-scheme:light){:root{--bg:#f6f8fa;--card:#fff;--bd:#d0d7de;--fg:#1f2328;--mut:#656d76;--acc:#1a7f37}}
*{box-sizing:border-box}body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
.wrap{max-width:1000px;margin:0 auto;padding:24px}h1{font-size:22px;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
input,select{background:var(--card);border:1px solid var(--bd);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px 16px;margin-bottom:10px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.cap{font-weight:600}.mut{color:var(--mut);font-size:13px}
.pill{display:inline-block;background:var(--bg);border:1px solid var(--bd);border-radius:20px;padding:1px 10px;font-size:12px;margin-right:4px}
.rep{text-align:right;white-space:nowrap}.rep .n{font-size:18px;font-weight:600;color:var(--acc)}
.price{font-size:18px;font-weight:600}
a{color:var(--acc)}.empty{color:var(--mut);padding:40px;text-align:center}
</style></head><body><div class=wrap>
<h1>k402 service exchange</h1>
<div class=sub id=meta>an open marketplace of agent-payable services — settled trustlessly over Kaspa payment channels</div>
<div class=bar>
 <input id=cap placeholder="capability (e.g. summarize, zk-prove)" style=flex:1;min-width:180px>
 <input id=price type=number step=0.001 placeholder="max $/call">
 <input id=rep type=number step=0.1 placeholder="min reputation (KAS)">
 <button onclick=load() style="background:var(--acc);color:#fff;border:none;border-radius:8px;padding:8px 16px;cursor:pointer">Search</button>
</div>
<div id=list></div>
</div><script>
function ago(ts){if(!ts)return'';const s=Date.now()/1000-ts;if(s<3600)return(s/60|0)+'m';if(s<86400)return(s/3600|0)+'h';return(s/86400|0)+'d ago'}
async function load(){
 const p=new URLSearchParams();
 const c=cap.value.trim(),pr=price.value,r=rep.value;
 if(c)p.set('capability',c);if(pr)p.set('max_price_usd',pr);if(r)p.set('min_reputation_kas',r);
 const d=await(await fetch('registry/search?'+p)).json();
 meta.textContent=`${d.count} service(s) listed · settled trustlessly over Kaspa payment channels`;
 list.innerHTML=d.providers.length?d.providers.map(x=>`<div class=card>
  <div><div class=cap>${x.capability} <span class=mut>· $${x.price_usd}/call</span></div>
   <div class=mut style=margin:4px_0>${x.meta.model?('model '+x.meta.model+' · '):''}${x.endpoint}</div>
   <div>${(x.schemes||[]).map(s=>`<span class=pill>${s}</span>`).join('')}
    ${x.stake_kas?`<span class=pill>staked ${x.stake_kas} KAS</span>`:''}</div>
   <div class=mut style=margin-top:6px>payee ${x.payee_pubkey.slice(0,16)}… · listed ${ago(x.listed_at)}</div></div>
  <div class=rep><div class=n>${x.reputation.settled_kas} KAS</div>
   <div class=mut>${x.reputation.closes} settlement(s)</div></div></div>`).join(''):
  '<div class=empty>No services match. The marketplace is new — <b>list yours</b> with the k402 client.</div>';
}
load();
</script></body></html>"""
