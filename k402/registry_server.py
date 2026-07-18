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
            description TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (payee_pubkey, capability))""")
        # migrate DBs created before the description column existed
        if "description" not in {r[1] for r in _c.execute("PRAGMA table_info(listings)")}:
            _c.execute("ALTER TABLE listings ADD COLUMN description TEXT NOT NULL DEFAULT ''")
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
            c.execute("""INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (listing.payee_pubkey, listing.capability, listing.endpoint, listing.price_usd,
                       listing.network, json.dumps(listing.schemes), json.dumps(listing.channel_terms),
                       listing.stake_outpoint, stake_sompi, json.dumps(listing.meta),
                       listing.listed_at, listing.sig, listing.description))
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
                "description": r.get("description", ""),
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
                              "description": r.get("description", ""),
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
_UI = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>k402 service exchange</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Inter:wght@400;500;600;700&family=Share+Tech+Mono&display=swap');
/* ---- cyberpunk neon-void theme, matching Kali123411/kaspa (dark-committed) ---- */
:root{
  --void:#0a0616; --void2:#151026; --panel:#1b1533; --line2:#342e4e;
  --surface:rgba(21,16,38,.72); --hair:rgba(0,240,255,.14);
  --text:#eae6f6; --bright:#f5f3fb; --muted:#8478ab; --faint:#5f5588;
  --cyan:#00f0ff; --pink:#ff2ec4; --purple:#b45cff; --kaspa:#49EACB; --yellow:#f8ef4a;
  --cyan-soft:rgba(0,240,255,.08); --pink-soft:rgba(255,46,196,.10); --purple-soft:rgba(180,92,255,.10);
  --disp:'Orbitron',ui-sans-serif,system-ui,sans-serif;
  --mono:'Share Tech Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --body:'Inter',-apple-system,'Segoe UI',system-ui,Roboto,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;background:linear-gradient(135deg,#0a0616,#151026 25%,#241f3a 50%,#151026 75%,#0a0616);
  background-attachment:fixed;color:var(--text);font-family:var(--body);line-height:1.6;
  -webkit-font-smoothing:antialiased;font-size:15px;min-height:100vh}
/* fixed HUD atmosphere: scanlines + grid + corner glows (from the site's globals.css) */
body::after{content:'';position:fixed;inset:0;z-index:9999;pointer-events:none;
  background-image:
    radial-gradient(ellipse 70% 45% at 50% -10%,rgba(0,240,255,.08),transparent),
    radial-gradient(ellipse 55% 40% at 85% 112%,rgba(255,46,196,.06),transparent),
    linear-gradient(rgba(0,240,255,.028) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,240,255,.028) 1px,transparent 1px),
    repeating-linear-gradient(0deg,rgba(0,0,0,.11) 0,rgba(0,0,0,.11) 1px,transparent 1px,transparent 3px);
  background-size:100% 100%,100% 100%,44px 44px,44px 44px,100% 3px}
.mono{font-family:var(--mono)} .disp{font-family:var(--disp)}
.wrap{max-width:1120px;margin:0 auto;padding:0 22px;position:relative;z-index:1}
a{color:var(--cyan);text-decoration:none}
.tnum{font-variant-numeric:tabular-nums}
::selection{background:rgba(255,46,196,.45);color:#fff}

/* ---- top bar ---- */
.top{position:sticky;top:0;z-index:30;backdrop-filter:blur(12px);
  background:rgba(10,6,22,.72);border-bottom:1px solid var(--hair)}
.top .wrap{display:flex;align-items:center;gap:16px;height:60px}
.brand{font-family:var(--disp);font-weight:700;letter-spacing:.04em;font-size:16px;display:flex;gap:10px;align-items:center;text-transform:uppercase}
.brand .x{color:var(--cyan);text-shadow:0 0 8px rgba(0,240,255,.6)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);box-shadow:0 0 10px var(--cyan),0 0 0 4px var(--cyan-soft);animation:pulse 3s ease-in-out infinite}
@keyframes pulse{50%{opacity:.5}}
.top .spacer{flex:1}
.tlink{font-family:var(--mono);font-size:13px;color:var(--muted);cursor:pointer;letter-spacing:.02em}
.tlink:hover{color:var(--cyan);text-shadow:0 0 8px rgba(0,240,255,.4)}
.btn{font-family:var(--disp);font-weight:700;font-size:12.5px;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;border-radius:9px;padding:10px 16px;border:none;position:relative;overflow:hidden}
.btn-primary{color:#04121a;background:linear-gradient(135deg,var(--cyan),var(--purple));
  box-shadow:0 0 20px rgba(0,240,255,.28)}
.btn-primary:hover{box-shadow:0 0 30px rgba(0,240,255,.5),0 0 50px rgba(180,92,255,.25);transform:translateY(-1px)}
.btn-ghost{color:var(--text);background:rgba(21,16,38,.6);border:1px solid var(--line2)}
.btn-ghost:hover{border-color:var(--cyan);color:var(--cyan);box-shadow:0 0 16px rgba(0,240,255,.2)}

/* ---- hero ---- */
.hero{padding:70px 0 34px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--cyan);margin:0 0 22px}
h1{font-family:var(--disp);font-weight:700;letter-spacing:.005em;line-height:1.12;
  font-size:clamp(30px,5.2vw,52px);margin:0 0 22px;text-wrap:balance;max-width:17ch;text-transform:uppercase}
h1 .g{background:linear-gradient(120deg,var(--cyan),var(--pink));background-size:200% 100%;
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;animation:shimmer 4s linear infinite}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
.lede{font-size:clamp(16px,2.1vw,18.5px);color:var(--muted);max-width:60ch;margin:0 0 30px;line-height:1.6}
.lede b{color:var(--bright);font-weight:600}
.herobtns{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:40px}

/* ---- stat strip ---- */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--hair);
  border:1px solid var(--hair);border-radius:14px;overflow:hidden}
.stat{background:rgba(21,16,38,.6);padding:19px 20px;backdrop-filter:blur(8px)}
.stat .k{font-family:var(--mono);font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:9px}
.stat .v{font-family:var(--disp);font-size:25px;font-weight:700;letter-spacing:.01em}
.stat .v small{font-family:var(--mono);font-size:12px;color:var(--muted);font-weight:400;margin-left:4px}
.stat .v .u{color:var(--cyan);text-shadow:0 0 12px rgba(0,240,255,.5)}
@media (max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}}

/* ---- section head ---- */
.sec{padding:56px 0 0}
.sechead{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;margin-bottom:22px}
.sechead h2{font-family:var(--disp);font-weight:700;letter-spacing:.02em;font-size:22px;margin:0;text-transform:uppercase}
.sechead p{margin:7px 0 0;color:var(--muted);font-size:14px}
.tag{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--cyan);
  border:1px solid var(--hair);border-radius:20px;padding:5px 12px;box-shadow:0 0 14px rgba(0,240,255,.12) inset}

/* ---- filter bar ---- */
.controls{position:sticky;top:60px;z-index:15;margin:0 0 18px;padding:14px 0;
  background:rgba(10,6,22,.82);backdrop-filter:blur(10px)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.field{position:relative;display:flex;align-items:center}
.field label{position:absolute;left:12px;font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--faint);top:7px;pointer-events:none}
input,select{font-family:var(--mono);font-size:13.5px;color:var(--text);background:rgba(21,16,38,.8);
  border:1px solid var(--line2);border-radius:9px;padding:23px 12px 8px;min-width:0;appearance:none}
input::placeholder{color:var(--faint)}
input:focus,select:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 0 3px rgba(0,240,255,.1),0 0 18px rgba(0,240,255,.12)}
#q{flex:1;min-width:200px}
select{cursor:pointer;padding-right:30px;
  background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 16px) 22px,calc(100% - 11px) 22px;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
.count{font-family:var(--mono);font-size:12.5px;color:var(--muted);margin-left:auto;white-space:nowrap}
.count b{color:var(--cyan)}

/* ---- provider card ---- */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media (max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:rgba(21,16,38,.7);backdrop-filter:blur(12px);border:1px solid var(--hair);border-radius:14px;
  padding:18px 19px;display:flex;flex-direction:column;gap:13px;transition:transform .3s cubic-bezier(.4,0,.2,1),box-shadow .3s,border-color .3s}
.card.live:hover{transform:translateY(-6px);border-color:rgba(0,240,255,.35);
  box-shadow:0 18px 30px rgba(0,0,0,.4),0 0 0 1px rgba(0,240,255,.3),0 0 44px rgba(0,240,255,.16),0 0 60px rgba(255,46,196,.06)}
.chead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.cap{font-family:var(--mono);font-size:16.5px;font-weight:400;color:var(--bright);letter-spacing:.01em}
.price{font-family:var(--mono);font-size:15px;color:var(--cyan);white-space:nowrap}
.price small{color:var(--faint);font-size:11px}
.who{font-size:13px;color:var(--muted);margin-top:-3px}
.who .sep{color:var(--faint);margin:0 6px}
.rep{display:flex;flex-direction:column;gap:7px;margin-top:2px}
.rep-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.rep-kas{font-family:var(--mono);font-size:15px;color:var(--cyan);text-shadow:0 0 10px rgba(0,240,255,.35)}
.rep-kas .l{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-right:7px;text-shadow:none}
.rep-closes{font-family:var(--mono);font-size:12px;color:var(--muted)}
.meter{height:6px;border-radius:4px;background:rgba(10,6,22,.7);overflow:hidden;border:1px solid var(--line2)}
.meter i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--purple),var(--cyan));
  box-shadow:0 0 12px rgba(0,240,255,.5);transform-origin:left;animation:grow .9s cubic-bezier(.2,.7,.2,1) both}
@keyframes grow{from{transform:scaleX(0)}}
.newchip{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;color:var(--pink);
  background:var(--pink-soft);border:1px solid rgba(255,46,196,.4);border-radius:20px;padding:3px 10px;align-self:flex-start}
.foot{display:flex;justify-content:space-between;align-items:center;gap:10px;border-top:1px solid var(--line2);padding-top:12px;margin-top:auto}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-family:var(--mono);font-size:11px;color:var(--muted);background:rgba(10,6,22,.5);border:1px solid var(--line2);border-radius:6px;padding:3px 8px}
.chip.stake{color:var(--kaspa);border-color:rgba(73,234,203,.35)}
.payee{font-family:var(--mono);font-size:11.5px;color:var(--faint)}
.use{font-family:var(--mono);font-size:12.5px;color:var(--cyan);border:1px solid rgba(0,240,255,.3);border-radius:8px;
  padding:6px 12px;background:transparent;cursor:pointer;white-space:nowrap;transition:.2s}
.use:hover{background:var(--cyan-soft);box-shadow:0 0 16px rgba(0,240,255,.25)}

/* ---- onboarding builder ---- */
.list-sec{padding:58px 0 0}
.steps{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.step{flex:1;min-width:210px;background:rgba(21,16,38,.6);border:1px solid var(--hair);border-radius:12px;padding:16px}
.step b{font-family:var(--disp);font-weight:700;font-size:12.5px;letter-spacing:.04em;text-transform:uppercase;display:flex;align-items:center}
.step .n{font-family:var(--mono);font-size:12px;color:var(--void);background:var(--cyan);border-radius:6px;padding:1px 8px;margin-right:9px;box-shadow:0 0 12px rgba(0,240,255,.4)}
.step p{margin:9px 0 0;font-size:13px;color:var(--muted);line-height:1.55}
.builder{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media (max-width:820px){.builder{grid-template-columns:1fr}}
.form{background:rgba(21,16,38,.7);backdrop-filter:blur(12px);border:1px solid var(--hair);border-radius:14px;padding:20px}
.form h3{font-family:var(--disp);font-weight:700;font-size:14px;letter-spacing:.03em;text-transform:uppercase;margin:0 0 5px}
.form .hint{margin:0 0 16px;font-size:12.5px;color:var(--muted)}
.frow{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.ff{display:flex;flex-direction:column;gap:6px;margin-bottom:11px}
.ff.full{grid-column:1/-1}
.ff label{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.ff input,.ff select{padding:9px 11px;font-size:13px}
.schemes{display:flex;gap:8px;flex-wrap:wrap}
.sw{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:12px;color:var(--muted);
  border:1px solid var(--line2);border-radius:8px;padding:8px 11px;cursor:pointer;user-select:none}
.sw input{position:absolute;opacity:0;width:0;height:0}
.sw.on{color:var(--cyan);border-color:rgba(0,240,255,.4);background:var(--cyan-soft);box-shadow:0 0 14px rgba(0,240,255,.15)}
.preview-wrap{display:flex;flex-direction:column;gap:13px;position:sticky;top:82px}
.plabel{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:0 0 -5px}
.cmd{background:rgba(10,6,22,.7);border:1px solid var(--hair);border-radius:14px;overflow:hidden}
.cmd .bar{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--line2)}
.cmd .bar span{font-family:var(--mono);font-size:11px;color:var(--muted)}
.copy{font-family:var(--mono);font-size:11.5px;color:var(--cyan);background:transparent;border:1px solid rgba(0,240,255,.35);border-radius:7px;padding:5px 11px;cursor:pointer}
.copy:hover{background:var(--cyan-soft)}
pre{margin:0;padding:14px 15px;overflow-x:auto;font-family:var(--mono);font-size:12px;line-height:1.75;color:var(--text)}
pre .c{color:var(--faint)} pre .s{color:var(--kaspa)} pre .k{color:var(--purple)}
.safe{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--muted);
  background:rgba(21,16,38,.6);border:1px solid var(--hair);border-radius:10px;padding:11px 13px}
.safe svg{color:var(--cyan);flex:none}

/* ---- trust band ---- */
.band{margin:62px 0 0;padding:38px 0;border-top:1px solid var(--hair);border-bottom:1px solid var(--hair)}
.band h2{font-family:var(--mono);font-size:12.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);margin:0 0 26px}
.trio{display:grid;grid-template-columns:repeat(3,1fr);gap:26px}
@media (max-width:720px){.trio{grid-template-columns:1fr;gap:22px}}
.pt .l{font-family:var(--disp);font-weight:700;font-size:12.5px;letter-spacing:.03em;text-transform:uppercase;color:var(--cyan);margin-bottom:10px;display:flex;align-items:center;gap:9px}
.pt p{margin:0;font-size:14px;color:var(--muted);line-height:1.6}
.pt p b{color:var(--bright);font-weight:600}

footer{padding:32px 0 48px;color:var(--muted);font-size:13px}
.fr{display:flex;gap:20px;flex-wrap:wrap;align-items:center}
.fr .spacer{flex:1}
.caveat{margin-top:15px;font-size:12px;color:var(--faint);line-height:1.6;max-width:74ch}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--cyan);color:var(--void);
  font-family:var(--mono);font-size:12.5px;padding:9px 16px;border-radius:9px;opacity:0;transition:.2s;pointer-events:none;z-index:50;box-shadow:0 0 24px rgba(0,240,255,.5)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto}}
</style>
</head><body>


<div class="top"><div class="wrap">
  <span class="brand"><span class="dot"></span><span class="x">K402</span>&nbsp;EXCHANGE</span>
  <span class="spacer"></span>
  <span class="tlink" onclick="go('browse')">browse</span>
  <span class="tlink" onclick="go('list')">list a service</span>
  <button class="btn btn-primary" onclick="go('list')">List your service</button>
</div></div>

<header class="hero"><div class="wrap">
  <p class="eyebrow">k402 service exchange · settled on kaspa L1</p>
  <h1>The marketplace for <span class="g">agent-payable</span> services</h1>
  <p class="lede">Discover a provider, open a payment channel straight to it, and settle per call —
    LLM inference, chain data, zero-knowledge proofs, covenant tooling. <b>The registry never holds funds.</b>
    Reputation is settled volume, verified on-chain.</p>
  <div class="herobtns">
    <button class="btn btn-primary" onclick="go('browse')">Browse services</button>
    <button class="btn btn-ghost" onclick="go('list')">List your service →</button>
  </div>
  <div class="stats">
    <div class="stat"><div class="k">Services listed</div><div class="v tnum" id="s-svc">0</div></div>
    <div class="stat"><div class="k">Settled to date</div><div class="v tnum"><span id="s-kas">0</span> <span class="u">KAS</span></div></div>
    <div class="stat"><div class="k">Providers</div><div class="v tnum" id="s-prov">0</div></div>
    <div class="stat"><div class="k">Finality</div><div class="v">~1<small>SEC · L1</small></div></div>
  </div>
</div></header>

<section class="sec" id="browse"><div class="wrap">
  <div class="sechead"><div><h2>Browse services</h2><p>Ranked by chain-verified reputation. Filter, then settle directly with the provider.</p></div>
    <span class="tag">live market</span></div>
</div>
<div class="controls"><div class="wrap"><div class="filters">
  <div class="field" style="flex:1"><label for="q">capability</label><input id="q" class="mono" placeholder="summarize · llm:reason · zk-prove …" autocomplete="off"></div>
  <div class="field"><label for="maxp">max $/call</label><input id="maxp" class="mono tnum" type="number" step="0.001" placeholder="any" style="width:118px"></div>
  <div class="field"><label for="minr">min rep KAS</label><input id="minr" class="mono tnum" type="number" step="1" placeholder="0" style="width:118px"></div>
  <div class="field"><label for="sort">sort</label><select id="sort"><option value="rep">reputation</option><option value="price">price ↑</option><option value="new">newest</option></select></div>
  <span class="count" id="count"></span>
</div></div></div>
<div class="wrap"><div class="grid" id="grid"></div></div>
</section>

<section class="list-sec" id="list"><div class="wrap">
  <div class="sechead"><div><h2>List your service in about a minute</h2>
    <p>Fill the form, copy the generated command, run it locally. Your key signs the listing on your machine — it never touches this page or the registry.</p></div>
    <span class="tag">onboarding</span></div>
  <div class="steps">
    <div class="step"><b><span class="n">1</span>Describe it</b><p>Name a capability, your endpoint, and a price. Pick the payment schemes you accept.</p></div>
    <div class="step"><b><span class="n">2</span>Sign locally</b><p>Run the generated one-liner. Your payee key signs the listing — proving you control the address that gets paid.</p></div>
    <div class="step"><b><span class="n">3</span>You're live</b><p>Your card appears in the market instantly. As agents pay and channels close, your reputation accrues, chain-verified.</p></div>
  </div>
  <div class="builder">
    <div class="form">
      <h3>Service details</h3>
      <p class="hint">Everything here is public listing data. The preview updates as you type.</p>
      <div class="frow">
        <div class="ff full"><label for="f-cap">Capability</label><input id="f-cap" list="caps" value="summarize">
          <datalist id="caps"><option>summarize</option><option>llm:reason</option><option>llm:code</option><option>zk-prove</option><option>embed</option><option>extract</option><option>classify</option><option>covenant:compile</option><option>chain:balance</option><option>read</option></datalist></div>
        <div class="ff full"><label for="f-ep">Endpoint URL</label><input id="f-ep" value="https://your-host/summarize"></div>
        <div class="ff"><label for="f-price">Price (USD / call)</label><input id="f-price" class="tnum" type="number" step="0.0001" value="0.002"></div>
        <div class="ff"><label for="f-model">Model / label</label><input id="f-model" value="qwen2.5:7b"></div>
        <div class="ff"><label for="f-region">Region</label><input id="f-region" value="eu-west"></div>
        <div class="ff"><label for="f-name">Provider name</label><input id="f-name" value="my-node"></div>
        <div class="ff full"><label for="f-pub">Payee pubkey (x-only hex)</label><input id="f-pub" class="mono" value="" placeholder="derive: k402.payer_pubkey_from_privkey(key)"></div>
        <div class="ff full"><label>Schemes accepted</label><div class="schemes">
          <label class="sw on"><input type="checkbox" value="kaspa-channel" checked>kaspa-channel</label>
          <label class="sw on"><input type="checkbox" value="kaspa-utxo" checked>kaspa-utxo</label>
          <label class="sw"><input type="checkbox" value="kaspa-session">kaspa-session</label>
        </div></div>
      </div>
    </div>
    <div class="preview-wrap">
      <p class="plabel">Live preview — how agents will see you</p>
      <div id="pv"></div>
      <p class="plabel">Generated command — run locally</p>
      <div class="cmd"><div class="bar"><span>python · your machine</span><button class="copy" id="copy">copy</button></div><pre id="code"></pre></div>
      <div class="safe"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="7" width="10" height="6.5" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/></svg>
        Your private key never leaves your machine — the listing is signed locally and only the signature is published.</div>
    </div>
  </div>
</div></section>

<section class="band"><div class="wrap">
  <h2>Why settle here</h2>
  <div class="trio">
    <div class="pt"><div class="l"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M8 1.5 2.5 4v3.5c0 3 2.4 5.4 5.5 6.5 3.1-1.1 5.5-3.5 5.5-6.5V4L8 1.5Z"/><path d="M5.5 8 7.3 9.8 10.7 6.3"/></svg>No custodian</div>
      <p>Funds sit in a Kaspa L1 covenant, not a merchant's wallet or a facilitator's escrow. A provider claims
        <b>at most what the payer signed</b>, only to its own address — consensus enforces it.</p></div>
    <div class="pt"><div class="l"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 8.5 5.2 11.5 14 3"/></svg>Reputation you can't fake</div>
      <p>The settled-KAS figure that ranks each provider is read from the chain — every close is confirmed
        on-chain before it counts. <b>No inflated reviews</b>, just verified settled value.</p></div>
    <div class="pt"><div class="l"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1.5 8h13M9 2.5 14.5 8 9 13.5"/></svg>Direct settlement</div>
      <p>Pay per call with an off-chain voucher — microseconds, no chain latency. Two transactions per channel,
        any number of calls between. <b>No sequencer, no middleman.</b></p></div>
  </div>
</div></section>

<footer><div class="wrap">
  <div class="fr"><span class="mono" style="color:var(--muted)">k402 · HTTP 402 payments on Kaspa</span>
    <span class="spacer"></span>
    <a class="tlink" href="#">PROTOCOL</a><a class="tlink" href="#">providers guide</a><a class="tlink" href="#">PyPI</a><a class="tlink" href="#">GitHub</a></div>
  <p class="caveat">Prototype. The channel covenant is unaudited and channel sizes are capped, so the scheme is
    experimental — don't put in more than you can lose. Prices are USD-pegged and re-quoted; reputation reflects
    settled volume verified on-chain.</p>
</div></footer>
<div class="toast" id="toast">copied</div>

<script>
let PROVIDERS=[];
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
let maxKas=1;const fmt=n=>n>=100?n.toFixed(0):n>=10?n.toFixed(1):n.toFixed(2);
function go(id){document.getElementById(id).scrollIntoView({behavior:'smooth',block:'start'})}
function cardHTML(p,live){
  const isNew=p.closes===0,w=Math.max(4,Math.round(p.kas/maxKas*100));
  const rep=isNew?`<div class="newchip">◔ new · awaiting first settlement</div>`
    :`<div class="rep"><div class="rep-top"><span class="rep-kas tnum"><span class="l">settled</span>${fmt(p.kas)} KAS</span>
        <span class="rep-closes tnum">${p.closes} settlements</span></div><div class="meter"><i style="width:${w}%"></i></div></div>`;
  return `<article class="card${live?' live':''}"><div class="chead"><span class="cap">${p.cap||'—'}</span><span class="price tnum">$${p.price}<small>/call</small></span></div>
    <div class="who">${p.who||'your-node'}<span class="sep">·</span>${p.model||'model'}<span class="sep">·</span>${p.region||'region'}</div>${rep}
    <div class="foot"><div class="chips">${(p.schemes||[]).map(s=>`<span class="chip">${s}</span>`).join('')}${p.stake?`<span class="chip stake">staked ${p.stake} KAS</span>`:''}</div>
      <button class="use">use →</button></div><div class="payee">payee ${p.payee||'—'}…</div></article>`;
}
function render(){
  const q=$("#q").value.trim().toLowerCase(),mp=parseFloat($("#maxp").value)||Infinity,mr=parseFloat($("#minr").value)||0,sort=$("#sort").value;
  let list=PROVIDERS.filter(p=>(!q||p.cap.includes(q)||p.who.toLowerCase().includes(q)||(p.model||'').toLowerCase().includes(q))&&p.price<=mp&&p.kas>=mr);
  list.sort((a,b)=>sort==="price"?a.price-b.price:sort==="new"?a.closes-b.closes:b.kas-a.kas);
  $("#count").innerHTML=`<b>${list.length}</b> of ${PROVIDERS.length} services`;
  $("#grid").innerHTML=list.map(p=>cardHTML(p,true)).join('')||`<div style="grid-column:1/-1;padding:52px;text-align:center;color:var(--muted);font-family:var(--mono)">no services match — widen the filters, or list yours.</div>`;
}
["q","maxp","minr","sort"].forEach(id=>$("#"+id).addEventListener("input",render));
function schemes(){return $$('.sw input:checked').map(i=>i.value)}
function esc(s){return (s||'').replace(/"/g,'\\"')}
function buildPreview(){
  const p={cap:$("#f-cap").value,who:$("#f-name").value,model:$("#f-model").value,region:$("#f-region").value,
    price:parseFloat($("#f-price").value)||0,kas:0,closes:0,schemes:schemes(),stake:0,payee:($("#f-pub").value||'yourpubkey').slice(0,8)};
  $("#pv").innerHTML=cardHTML(p,false);
  const pub=$("#f-pub").value||"YOUR_PAYEE_PUBKEY";
  $("#code").innerHTML=
`<span class="c"># pip install k402</span>
<span class="k">from</span> k402 <span class="k">import</span> Listing
<span class="k">import</span> httpx

listing = Listing(
  capability=<span class="s">"${esc($("#f-cap").value)}"</span>,
  endpoint=<span class="s">"${esc($("#f-ep").value)}"</span>,
  payee_pubkey=<span class="s">"${esc(pub)}"</span>,
  price_usd=${parseFloat($("#f-price").value)||0},
  schemes=[${schemes().map(s=>`<span class="s">"${s}"</span>`).join(", ")}],
  meta={<span class="s">"model"</span>: <span class="s">"${esc($("#f-model").value)}"</span>, <span class="s">"region"</span>: <span class="s">"${esc($("#f-region").value)}"</span>},
).sign(YOUR_PAYEE_KEY)   <span class="c"># signs locally; key never leaves your machine</span>

httpx.post(<span class="s">"https://x402-compute.68cxgfyr0.workers.dev/registry/list"</span>,
           json=listing.to_dict())`;
}
$$('.form input,.form select').forEach(el=>el.addEventListener('input',buildPreview));
$$('.sw').forEach(sw=>sw.addEventListener('click',e=>{
  if(e.target.tagName!=='INPUT'){const cb=sw.querySelector('input');cb.checked=!cb.checked;}
  setTimeout(()=>{sw.classList.toggle('on',sw.querySelector('input').checked);buildPreview()},0);}));
let tT;$("#copy").addEventListener('click',()=>navigator.clipboard.writeText($("#code").innerText).then(()=>{
  const t=$("#toast");t.classList.add('show');clearTimeout(tT);tT=setTimeout(()=>t.classList.remove('show'),1400);}));
function stats(){
  const totalKas=PROVIDERS.reduce((s,p)=>s+p.kas,0),provCount=new Set(PROVIDERS.map(p=>p.payee_full)).size;
  $("#s-svc").textContent=PROVIDERS.length;$("#s-prov").textContent=provCount;
  const reduce=matchMedia("(prefers-reduced-motion:reduce)").matches;
  if(reduce){$("#s-kas").textContent=fmt(totalKas)}else{let t0=null;const dur=1100;(function tick(ts){t0??=ts;const k=Math.min(1,(ts-t0)/dur);
    $("#s-kas").textContent=fmt(totalKas*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(tick)})(performance.now())}
}
async function loadAll(){
  try{
    const d=await(await fetch('registry/search?limit=200')).json();
    PROVIDERS=(d.providers||[]).map(p=>({
      cap:p.capability, who:(p.meta&&p.meta.provider)||('provider '+String(p.payee_pubkey).slice(0,6)),
      model:(p.meta&&p.meta.model)||'', region:(p.meta&&p.meta.region)||p.network||'',
      price:p.price_usd, kas:(p.reputation&&p.reputation.settled_kas)||0,
      closes:(p.reputation&&p.reputation.closes)||0, schemes:p.schemes||[], stake:p.stake_kas||0,
      payee:String(p.payee_pubkey).slice(0,8), payee_full:p.payee_pubkey}));
  }catch(e){PROVIDERS=[]}
  maxKas=Math.max(...PROVIDERS.map(p=>p.kas),1);stats();render();
}
buildPreview();loadAll();
</script>

</body></html>"""
