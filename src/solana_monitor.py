import asyncio
import json
import time
from collections import OrderedDict

import aiohttp

from .logutil import log as _log

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# What a buy is paid with (never counted as the coin being aped)
QUOTE_MINTS = {WSOL, USDC, USDT}
STABLE_MINTS = {USDC, USDT}

# Known DEX programs: the tx must touch one of these to count as a buy
DEX_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eAdduJ2Wv",  # Raydium LaunchLab
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2HXiPDDewW8G88TX2",  # Jupiter v4
    "JUP3c2Uh3WA4Ng34tw6kPd2G4C5BB21Xo36Je1s32Ph",  # Jupiter v3
    "JUP2jxvXaqu7NQY1GmNF4m1vodw12LVXYxbFL2uJvfo",  # Jupiter v2
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca v2
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora AMM
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora DBC (launchpads)
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",  # Phoenix
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",  # Moonshot
}

# accountNotification slot vs. the tx's slot: allow a few slots of slack
SLOT_SLACK = 4


def log(msg: str):
    _log("SOL", msg)


def ws_url_from_http(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def extract_buys(tx: dict, wallet: str) -> list:
    """
    Returns [(mint, amount_gained, sol_spent, usd_spent)] only for real buys:
    the wallet SIGNED a DEX tx, paid with SOL/WSOL/stables (or another token) and gained a coin.
    Sells, airdrops, transfers and liquidity removals return [].
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []

    programs = {k.get("pubkey") if isinstance(k, dict) else k for k in keys}
    for ix in message.get("instructions") or []:
        programs.add(ix.get("programId"))
    for inner in meta.get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            programs.add(ix.get("programId"))
    if not programs & DEX_PROGRAMS:
        return []

    wallet_idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet:
            wallet_idx = i
            signer = k.get("signer") if isinstance(k, dict) else (i == 0)
            if not signer:
                return []  # someone else's tx (airdrop / transfer to the wallet)
            break
    if wallet_idx is None:
        return []

    def balances(entries):
        out = {}
        for b in entries or []:
            if b.get("owner") != wallet:
                continue
            try:
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmountString") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            out[b.get("mint")] = out.get(b.get("mint"), 0.0) + amt
        return out

    pre = balances(meta.get("preTokenBalances"))
    post = balances(meta.get("postTokenBalances"))
    deltas = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}

    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    lamports = 0.0
    if wallet_idx < len(pre_bal) and wallet_idx < len(post_bal):
        lamports = (pre_bal[wallet_idx] - post_bal[wallet_idx]) / 1e9
    sol_spent = max(0.0, lamports - deltas.get(WSOL, 0.0))  # native + wrapped SOL
    usd_spent = sum(-deltas[m] for m in STABLE_MINTS if deltas.get(m, 0.0) < 0)
    paid_with_token = any(d < 0 for m, d in deltas.items() if m not in QUOTE_MINTS)

    # Fees + new token account rent are ~0.002-0.01 SOL: anything above that is a real payment
    if sol_spent < 0.01 and usd_spent <= 0 and not paid_with_token:
        return []

    return [
        (mint, d, sol_spent, usd_spent or None)
        for mint, d in deltas.items()
        if d > 0 and mint not in QUOTE_MINTS
    ]


class SolanaMonitor:
    """
    Real-time Solana buy detection over WebSocket (accountSubscribe on each wallet's SOL account).
    No polling: the RPC pushes within ~1s whenever a wallet's balance changes (any tx it pays for),
    then getSignaturesForAddress + getTransaction find and parse the new tx.
    accountSubscribe instead of logsSubscribe: popular wallets get hundreds of failed spam txs
    per second that merely *mention* them; those never change the wallet's balance.
    """

    def __init__(self, rpc_url: str, wallets: list, tracker, state, cfg: dict, ws_url: str | None = None):
        self.rpc_url = rpc_url
        self.ws_url = ws_url or ws_url_from_http(rpc_url)
        self.wallets = wallets
        self.by_addr = {w["address"]: w for w in wallets}
        self.tracker = tracker
        self.state = state
        self.subs_per_conn = max(1, int(cfg.get("subs_per_connection", 100)))
        self.min_gap = 1.0 / max(float(cfg.get("max_rpc_per_sec", 8)), 0.1)
        self.max_age = int(cfg.get("max_tx_age_sec", 300))
        self.catchup = bool(cfg.get("catchup_on_reconnect", True))
        self.sig_page = int(cfg.get("signatures_page", 100))
        self.session: aiohttp.ClientSession | None = None
        self._rpc_lock = asyncio.Lock()
        self._last_rpc = 0.0
        self._seen = OrderedDict()      # (sig, wallet) already handled
        self._tx_cache = OrderedDict()  # sig -> tx (several tracked wallets can share a tx)
        self._state_dirty = False
        self._tasks = set()
        self._scanning = set()  # wallets with a scan in flight
        self._rescan = {}       # wallet -> trigger slot that arrived during a scan
        self._reset_stats()

    def _reset_stats(self):
        self.stats = {"notifs": 0, "no_tx": 0, "tx_fetched": 0, "buys": 0,
                      "rpc_calls": 0, "rate_limits": 0, "reconnects": 0}
        self.stats_start = time.time()

    async def run(self):
        rpc_timeout = aiohttp.ClientTimeout(total=20)
        ws_timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        async with aiohttp.ClientSession(timeout=rpc_timeout) as session, \
                aiohttp.ClientSession(timeout=ws_timeout) as ws_session:
            self.session = session
            n = self.subs_per_conn
            chunks = [self.wallets[i:i + n] for i in range(0, len(self.wallets), n)]
            log(f"Realtime monitor: {len(self.wallets)} wallets over {len(chunks)} WebSocket(s) -> {self.ws_url[:45]}...")
            await asyncio.gather(
                *(self._ws_loop(ws_session, i, c) for i, c in enumerate(chunks)),
                self._housekeeping(),
            )

    # ---------------- RPC ----------------

    async def _rpc(self, method: str, params: list):
        """Single JSON-RPC call, globally rate-limited (free Helius = 10 req/s)."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        delay = 1.0
        for _ in range(4):
            async with self._rpc_lock:
                wait = self.min_gap - (time.time() - self._last_rpc)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_rpc = time.time()
            self.stats["rpc_calls"] += 1
            try:
                async with self.session.post(self.rpc_url, json=payload) as resp:
                    if resp.status == 429:
                        self.stats["rate_limits"] += 1
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 20)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("error"):
                        log(f"RPC {method} error: {data['error']}")
                        return None
                    return data.get("result")
            except Exception as e:
                log(f"RPC {method} failed ({type(e).__name__}): {e}. Retry in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20)
        return None

    async def _get_tx(self, sig: str):
        if sig in self._tx_cache:
            return self._tx_cache[sig]
        tx = None
        # Right after a 'confirmed' push the tx can still be missing on the RPC node: retry briefly
        for attempt in range(4):
            tx = await self._rpc("getTransaction", [sig, {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            if tx:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        self.stats["tx_fetched"] += 1
        if tx:
            self._tx_cache[sig] = tx
            while len(self._tx_cache) > 500:
                self._tx_cache.popitem(last=False)
        return tx

    # ---------------- WebSocket ----------------

    async def _ws_loop(self, ws_session: aiohttp.ClientSession, idx: int, wallets: list):
        tag = f"WS{idx + 1}"
        backoff = 1
        was_connected = False
        while True:
            started = time.time()
            try:
                async with ws_session.ws_connect(self.ws_url, heartbeat=30, max_msg_size=0) as ws:
                    for i, w in enumerate(wallets):
                        await ws.send_json({
                            "jsonrpc": "2.0", "id": i, "method": "accountSubscribe",
                            "params": [w["address"], {"encoding": "base64", "commitment": "confirmed"}],
                        })
                        await asyncio.sleep(0.01)
                    subs = {}  # subscription id -> wallet address
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                            continue
                        data = json.loads(msg.data)
                        if data.get("method") == "accountNotification":
                            params = data.get("params") or {}
                            addr = subs.get(params.get("subscription"))
                            slot = ((params.get("result") or {}).get("context") or {}).get("slot") or 0
                            if addr:
                                self.stats["notifs"] += 1
                                self._schedule_scan(addr, slot)
                            continue
                        req_id = data.get("id")
                        if isinstance(req_id, int) and 0 <= req_id < len(wallets):
                            if "result" in data:
                                subs[data["result"]] = wallets[req_id]["address"]
                                if len(subs) == len(wallets):
                                    log(f"{tag} live: {len(subs)} wallets subscribed")
                                    backoff = 1
                                    if was_connected and self.catchup:
                                        for w in wallets:  # pick up whatever happened while offline
                                            self._schedule_scan(w["address"], None)
                                    was_connected = True
                            else:
                                log(f"{tag} subscribe failed for {wallets[req_id]['rename']}: {data.get('error')}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"{tag} error ({type(e).__name__}): {e}")
            self.stats["reconnects"] += 1
            if time.time() - started > 60:
                backoff = 1
            log(f"{tag} disconnected, reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _remember(self, key) -> bool:
        """True if this (sig, wallet) is new."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    def _schedule_scan(self, addr: str, slot: int | None):
        """One scan per wallet at a time; pushes during a scan coalesce into one re-scan.
        slot=None means catch-up: take every successful tx newer than the last one seen."""
        if addr in self._scanning:
            self._rescan.setdefault(addr, set()).add(slot)
            return
        self._scanning.add(addr)
        self._spawn(self._scan_loop(addr, {slot}))

    async def _scan_loop(self, addr: str, slots: set):
        try:
            while True:
                found = await self._scan(addr, slots)
                if not found and None not in slots:
                    await asyncio.sleep(0.7)  # RPC signature index can trail the account push
                    if not await self._scan(addr, slots):
                        self.stats["no_tx"] += 1  # e.g. a failed tx it paid fees for
                if addr not in self._rescan:
                    break
                slots = self._rescan.pop(addr)
        except Exception as e:
            log(f"Scan error {addr[:8]}...: {type(e).__name__}: {e}")
        finally:
            self._scanning.discard(addr)

    async def _scan(self, addr: str, slots: set) -> bool:
        """
        Fetch signatures newer than the last one seen and queue the wallet's own txs.
        getSignaturesForAddress also returns every tx that merely mentions the wallet, so only
        txs in the slot(s) where its balance changed are fetched (pages back up to 3x to reach
        them under spam). Returns True if one was found.
        """
        catchup = None in slots
        oldest_slot = 0 if catchup else min(slots) - SLOT_SLACK
        last = self.state.get_sol_last_sig(addr)
        sigs, before = [], None
        for _ in range(3):
            opts = {"limit": self.sig_page, "commitment": "confirmed"}
            if last:
                opts["until"] = last
            if before:
                opts["before"] = before
            page = await self._rpc("getSignaturesForAddress", [addr, opts]) or []
            sigs.extend(page)
            if len(page) < self.sig_page or (page[-1].get("slot") or 0) < oldest_slot:
                break
            before = page[-1]["signature"]
        if not sigs:
            return False
        self.state.set_sol_last_sig(addr, sigs[0]["signature"])
        self._state_dirty = True
        found = False
        now = time.time()
        for s in reversed(sigs):  # oldest first
            if s.get("err"):
                continue
            slot = s.get("slot") or 0
            if not catchup and not any(t - SLOT_SLACK <= slot <= t for t in slots):
                continue  # someone else's tx that only mentions the wallet
            if s.get("blockTime") and now - s["blockTime"] > self.max_age:
                continue
            found = True
            if self._remember((s["signature"], addr)):
                self._spawn(self._handle(addr, s["signature"]))
        return found

    # ---------------- Buy handling ----------------

    async def _handle(self, addr: str, sig: str):
        try:
            tx = await self._get_tx(sig)
            if not tx:
                return
            block_time = tx.get("blockTime")
            if block_time and time.time() - block_time > self.max_age:
                return  # stale (never alert old buys as if they were fresh)
            w = self.by_addr[addr]
            for mint, amount, sol_spent, usd_spent in extract_buys(tx, addr):
                self.stats["buys"] += 1
                buy = {
                    "name": w["rename"],
                    "emoji": w.get("emoji", ""),
                    "wallet": addr,
                    "amount": amount,
                    "sol_spent": sol_spent,
                    "symbol": "",
                    "tx_hash": sig,
                    "score": w.get("score", 0.3),
                    "trader_key": w.get("trader_key"),
                    "trader_name": w.get("trader_name"),
                }
                if usd_spent:
                    buy["usd"] = usd_spent
                count, score_sum = await self.tracker.add_buy("SOL", mint, addr, buy)
                lag = f" | {time.time() - block_time:.1f}s after block" if block_time else ""
                log(f"{w.get('emoji', '')} {w['rename']} (score {w.get('score', 0.3):.2f}) bought {mint[:8]}... "
                    f"{sol_spent:.2f} SOL ({count} traders, score {score_sum:.2f}){lag}")
        except Exception as e:
            log(f"Error handling {sig[:12]}...: {type(e).__name__}: {e}")

    async def _housekeeping(self):
        """Flush state to disk every 30s; hourly health line with real RPC usage."""
        while True:
            await asyncio.sleep(30)
            if self._state_dirty:
                self.state.save()
                self._state_dirty = False
            elapsed = time.time() - self.stats_start
            if elapsed >= 3600:
                s = self.stats
                per_day = int(s["rpc_calls"] / elapsed * 86400)
                health = "OK" if s["rate_limits"] == 0 else f"RATE-LIMITED ({s['rate_limits']}x429)"
                log(f"HEALTH 1h: {s['notifs']} pushes ({s['no_tx']} without own tx), {s['tx_fetched']} txs fetched, {s['buys']} buys | {s['rpc_calls']} RPC calls "
                    f"(~{per_day:,}/day) | reconnects {s['reconnects']} | RPC {health}")
                self._reset_stats()
