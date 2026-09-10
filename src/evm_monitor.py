import asyncio

import aiohttp

from .logutil import log as _log

# Etherscan V2: one API key works for every chain (chainid param)
API_URL = "https://api.etherscan.io/v2/api"

CHAINS = {
    "1": {"name": "ETH", "explorer": "https://etherscan.io/tx/"},
    "8453": {"name": "BASE", "explorer": "https://basescan.org/tx/"},
    "56": {"name": "BSC", "explorer": "https://bscscan.com/tx/"},
}

# Stables/wrapped excluded per chain (lowercase)
EXCLUDED_TOKENS = {
    "1": {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    },
    "8453": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        "0x4200000000000000000000000000000000000006",  # WETH
    },
    "56": {
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    },
}


def log(msg: str):
    _log("EVM", msg)


class EvmMonitor:
    """Monitor for EVM wallets (0x...) on ETH, BASE and BSC via Etherscan V2."""

    def __init__(self, api_key: str, wallets: list, tracker, state, cfg: dict):
        self.api_key = api_key
        self.wallets = wallets
        self.tracker = tracker
        self.state = state
        self.interval = cfg.get("poll_interval_sec", 15)
        self.per_cycle = cfg.get("wallets_per_cycle", 5)
        self.txs_limit = cfg.get("txs_limit", 10)
        self.offset = 0
        self.session: aiohttp.ClientSession | None = None

    async def run(self):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            await self._init_state()
            log(f"Monitor started: {len(self.wallets)} wallets x {len(CHAINS)} chains")
            while True:
                try:
                    await self._cycle()
                except Exception as e:
                    log(f"Cycle error: {e}")
                await asyncio.sleep(self.interval)

    async def _fetch_tokentx(self, chain_id: str, address: str) -> list:
        params = {
            "chainid": chain_id,
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": self.txs_limit,
            "sort": "desc",
            "apikey": self.api_key,
        }
        for attempt in range(3):
            try:
                async with self.session.get(API_URL, params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("status") == "1" and isinstance(data.get("result"), list):
                        return data["result"]
                    return []  # status 0 = no transactions (or error message)
            except Exception as e:
                log(f"API error chain {chain_id} {address[:8]}...: {e}")
                await asyncio.sleep(2 + attempt * 3)
        return []

    async def _init_state(self):
        log("Initializing state (no alerts)...")
        for w in self.wallets:
            addr = w["address"].lower()
            for chain_id in CHAINS:
                if self.state.get_evm_last_ts(addr, chain_id) > 0:
                    continue
                txs = await self._fetch_tokentx(chain_id, addr)
                if txs:
                    self.state.set_evm_last_ts(addr, chain_id, int(txs[0]["timeStamp"]))
                await asyncio.sleep(0.3)  # free tier: ~5 req/s
        self.state.save()
        log("Initial state ready.")

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
        for w in chunk:
            addr = w["address"].lower()
            for chain_id, chain in CHAINS.items():
                txs = await self._fetch_tokentx(chain_id, addr)
                if not txs:
                    await asyncio.sleep(0.3)
                    continue
                last_ts = self.state.get_evm_last_ts(addr, chain_id)
                new_txs = [t for t in txs if int(t.get("timeStamp", 0)) > last_ts]
                if new_txs:
                    self.state.set_evm_last_ts(addr, chain_id, int(txs[0]["timeStamp"]))
                    self.state.save()
                    # Oldest first
                    for t in reversed(new_txs):
                        await self._process_tx(chain_id, chain, w, t)
                await asyncio.sleep(0.3)

    async def _process_tx(self, chain_id: str, chain: dict, wallet: dict, t: dict):
        addr = wallet["address"].lower()
        token = (t.get("contractAddress") or "").lower()
        # Only tokens flowing INTO the wallet (buy/receive), not out
        if (t.get("to") or "").lower() != addr:
            return
        if (t.get("from") or "").lower() == addr:
            return
        if token in EXCLUDED_TOKENS.get(chain_id, set()):
            return
        try:
            amount = float(t.get("value", 0)) / (10 ** int(t.get("tokenDecimal", 18)))
        except (TypeError, ValueError, ZeroDivisionError):
            amount = 0.0
        count = await self.tracker.add_buy(chain["name"], token, wallet["address"], {
            "name": wallet["rename"],
            "emoji": wallet.get("emoji", ""),
            "wallet": wallet["address"],
            "amount": amount,
            "symbol": t.get("tokenSymbol") or "",
            "tx_hash": t.get("hash") or "",
        })
        n = abs(count)
        log(f"[{chain['name']}] {wallet.get('emoji','')} {wallet['rename']} received {t.get('tokenSymbol','?')} ({n}/{self.tracker.threshold} wallets in window)")
