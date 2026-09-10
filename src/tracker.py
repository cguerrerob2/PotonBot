import time


class BuyTracker:
    """
    Groups buys by (chain, token). When >= threshold distinct wallets
    buy the same token within the time window, it fires the alert.
    Re-alerts: only if `realert_extra` new wallets join and the cooldown passed.
    """

    def __init__(self, threshold: int, window_sec: int, cooldown_sec: int, realert_extra: int, on_alert):
        self.threshold = threshold
        self.window_sec = window_sec
        self.cooldown_sec = cooldown_sec
        self.realert_extra = realert_extra
        self.on_alert = on_alert  # coroutine: (chain, token, count, buys)
        # key -> {"buys": {wallet: buy_info}, "alerted_at": float, "last_alert_count": int}
        self.tokens = {}

    async def add_buy(self, chain: str, token: str, wallet: str, buy_info: dict) -> int:
        key = f"{chain}:{token.lower()}"
        now = time.time()

        entry = self.tokens.setdefault(key, {"buys": {}, "alerted_at": 0.0, "last_alert_count": 0})

        # Purge buys outside the window
        entry["buys"] = {w: b for w, b in entry["buys"].items() if now - b["time"] <= self.window_sec}

        is_new_wallet = wallet not in entry["buys"]
        entry["buys"][wallet] = {**buy_info, "time": now}
        count = len(entry["buys"])

        # Drop tokens with no buys left
        if not entry["buys"]:
            self.tokens.pop(key, None)
            return 0

        if count >= self.threshold:
            first_alert = entry["last_alert_count"] == 0
            grew_enough = count >= entry["last_alert_count"] + self.realert_extra
            cooldown_ok = (now - entry["alerted_at"]) >= self.cooldown_sec
            if first_alert or (grew_enough and cooldown_ok):
                entry["alerted_at"] = now
                entry["last_alert_count"] = count
                buys_sorted = sorted(entry["buys"].values(), key=lambda b: b["time"])
                await self.on_alert(chain, token, count, buys_sorted)

        return count if is_new_wallet else -count
