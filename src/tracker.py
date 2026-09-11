import time


class BuyTracker:
    """
    Groups buys by (chain, token). Fires the alert when EITHER:
      - >= threshold distinct wallets buy (broad consensus rule), OR
      - >= min_wallets_score wallets AND their combined PnL score >= score_threshold
        (smart-money rule, e.g. 2 top traders = 0.9 + 0.9 = 1.8 >= 1.76)
    Re-alerts if the group grows enough after the cooldown.
    """

    def __init__(self, threshold: int, window_sec: int, cooldown_sec: int, realert_extra: int,
                 min_wallets_score: int, score_threshold: float, on_alert):
        self.threshold = threshold
        self.window_sec = window_sec
        self.cooldown_sec = cooldown_sec
        self.realert_extra = realert_extra
        self.min_wallets_score = min_wallets_score
        self.score_threshold = score_threshold
        self.on_alert = on_alert  # coroutine: (chain, token, count, score_sum, buys)
        # key -> {"buys": {wallet: info}, "alerted_at": float, "last_alert_count": int, "last_alert_score": float}
        self.tokens = {}

    async def add_buy(self, chain: str, token: str, wallet: str, buy_info: dict) -> tuple[int, float]:
        key = f"{chain}:{token.lower()}"
        now = time.time()

        entry = self.tokens.setdefault(
            key, {"buys": {}, "alerted_at": 0.0, "last_alert_count": 0, "last_alert_score": 0.0})

        # Purge buys outside the window
        entry["buys"] = {w: b for w, b in entry["buys"].items() if now - b["time"] <= self.window_sec}

        is_new_wallet = wallet not in entry["buys"]
        entry["buys"][wallet] = {**buy_info, "time": now}
        count = len(entry["buys"])
        score_sum = sum(float(b.get("score", 0.3)) for b in entry["buys"].values())

        if not entry["buys"]:
            self.tokens.pop(key, None)
            return 0, 0.0

        consensus_hit = count >= self.threshold
        smart_hit = count >= self.min_wallets_score and score_sum >= self.score_threshold

        if consensus_hit or smart_hit:
            first_alert = entry["last_alert_count"] == 0
            grew = (count >= entry["last_alert_count"] + self.realert_extra
                    or score_sum >= entry["last_alert_score"] + 0.6)
            cooldown_ok = (now - entry["alerted_at"]) >= self.cooldown_sec
            if first_alert or (grew and cooldown_ok):
                entry["alerted_at"] = now
                entry["last_alert_count"] = count
                entry["last_alert_score"] = score_sum
                buys_sorted = sorted(entry["buys"].values(), key=lambda b: b["time"])
                await self.on_alert(chain, token, count, score_sum, buys_sorted)

        return (count, score_sum) if is_new_wallet else (-count, score_sum)
