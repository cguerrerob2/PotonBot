import asyncio

import aiohttp

from .logutil import log as _log

WSOL = "So11111111111111111111111111111111111111112"

# Stables / wrapped tokens we don't count as "apes"
EXCLUDED_MINTS = {
    WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

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
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",  # Phoenix
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",  # Moonshot
}


def log(msg: str):
    _log("SOL", msg)


class SolanaMonitor:
    def __init__(self, rpc_url: str, wallets: list, tracker, state, cfg: dict):
        self.rpc_url = rpc_url
        self.wallets = wallets
        self.tracker = tracker
        self.state = state
        self.interval = cfg.get("poll_interval_sec", 8)
        self.per_cycle = cfg.get("wallets_per_cycle", 10)
        self.sig_limit = cfg.get("signatures_limit", 5)
        self.max_txs = cfg.get("max_txs_per_wallet_cycle", 3)
        self.offset = 0
        self.session: aiohttp.ClientSession | None = None

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            await self._init_state()
            log(f"Monitor started: {len(self.wallets)} wallets. RPC: {self.rpc_url}")
            while True:
                try:
                    await self._cycle()
                except Exception as e:
                    log(f"Cycle error: {e}")
                await asyncio.sleep(self.interval)

    # ---------------- RPC ----------------

    async def _rpc_batch(self, calls: list) -> dict:
        """calls = [(method, params), ...] -> {id: result_item} with retries/backoff."""
        if not calls:
            return {}
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, (method, params) in enumerate(calls)
        ]
        delay = 3
        for attempt in range(4):
            try:
                async with self.session.post(self.rpc_url, json=payload) as resp:
                    if resp.status == 429:
                        log(f"Rate limit (429). Backoff {delay}s")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 45)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    items = data if isinstance(data, list) else [data]
                    return {item.get("id"): item for item in items}
            except Exception as e:
                log(f"RPC error ({type(e).__name__}): {e}. Retry in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 45)
        return {}

    # ---------------- Init ----------------

    async def _init_state(self):
        """First pass: store the latest signature of each wallet without alerting."""
        log("Initializing state (no alerts)...")
        for i in range(0, len(self.wallets), 20):
            chunk = self.wallets[i:i + 20]
            calls = [
                ("getSignaturesForAddress", [w["address"], {"limit": 1}])
                for w in chunk
            ]
            results = await self._rpc_batch(calls)
            for idx, w in enumerate(chunk):
                item = results.get(idx)
                sigs = (item or {}).get("result") or []
                if sigs and self.state.get_sol_last_sig(w["address"]) is None:
                    self.state.set_sol_last_sig(w["address"], sigs[0]["signature"])
            self.state.save()
            await asyncio.sleep(1)
        log("Initial state ready.")

    # ---------------- Main cycle ----------------

    def _next_chunk(self) -> list:
        n = len(self.wallets)
        if n == 0:
            return []
        chunk = [self.wallets[(self.offset + j) % n] for j in range(min(self.per_cycle, n))]
        self.offset = (self.offset + self.per_cycle) % n
        return chunk

    async def _cycle(self):
        chunk = self._next_chunk()
        if not chunk:
            return

        # 1) Recent signatures for this chunk of wallets
        calls = [
            ("getSignaturesForAddress", [w["address"], {"limit": self.sig_limit}])
            for w in chunk
        ]
        results = await self._rpc_batch(calls)

        to_fetch = []  # (wallet, signature)
        for idx, w in enumerate(chunk):
            item = results.get(idx)
            if item is None:
                continue
            if "error" in item:
                continue
            sigs = item.get("result") or []
            if not sigs:
                continue
            last_seen = self.state.get_sol_last_sig(w["address"])
            new_sigs = []
            for s in sigs:  # newest first
                if s["signature"] == last_seen:
                    break
                if not s.get("err"):
                    new_sigs.append(s["signature"])
            # Always move the marker to the newest signature
            self.state.set_sol_last_sig(w["address"], sigs[0]["signature"])
            for sig in reversed(new_sigs[: self.max_txs]):  # oldest first
                to_fetch.append((w, sig))

        self.state.save()
        if not to_fetch:
            return

        # 2) Fetch transactions in batch
        tx_calls = [
            ("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
            for _, sig in to_fetch
        ]
        tx_results = await self._rpc_batch(tx_calls)

        # 3) Parse and register buys
        for idx, (w, sig) in enumerate(to_fetch):
            item = tx_results.get(idx)
            tx = (item or {}).get("result")
            if not tx:
                continue
            for mint, amount in self._extract_buys(tx, w["address"]).items():
                count = await self.tracker.add_buy("SOL", mint, w["address"], {
                    "name": w["rename"],
                    "emoji": w.get("emoji", ""),
                    "wallet": w["address"],
                    "amount": amount,
                    "symbol": "",
                    "tx_hash": sig,
                })
                n = abs(count)
                log(f"{w.get('emoji','')} {w['rename']} bought {mint[:8]}... ({n}/{self.tracker.threshold} wallets in window)")

    # ---------------- Parsing ----------------

    def _extract_buys(self, tx: dict, wallet: str) -> dict:
        """Returns {mint: amount_gained} if the tx is a DEX swap where the wallet gains tokens."""
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return {}

        # The tx must involve a known DEX
        programs = set()
        message = (tx.get("transaction") or {}).get("message") or {}
        for key in message.get("accountKeys") or []:
            programs.add(key.get("pubkey") if isinstance(key, dict) else key)
        for ix in message.get("instructions") or []:
            if ix.get("programId"):
                programs.add(ix["programId"])
        for inner in meta.get("innerInstructions") or []:
            for ix in inner.get("instructions") or []:
                if ix.get("programId"):
                    programs.add(ix["programId"])
        if not (programs & DEX_PROGRAMS):
            return {}

        def balances(entries):
            out = {}
            for b in entries or []:
                if b.get("owner") != wallet:
                    continue
                mint = b.get("mint")
                try:
                    amt = float((b.get("uiTokenAmount") or {}).get("uiAmountString") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                out[mint] = out.get(mint, 0.0) + amt
            return out

        pre = balances(meta.get("preTokenBalances"))
        post = balances(meta.get("postTokenBalances"))

        gains = {}
        for mint, post_amt in post.items():
            if mint in EXCLUDED_MINTS:
                continue
            delta = post_amt - pre.get(mint, 0.0)
            if delta > 0:
                gains[mint] = delta
        return gains
