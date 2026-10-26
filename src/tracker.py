import re
import time


def trader_group(name: str) -> str:
    """
    Normalize a wallet label to its trader group:
    'Cupsey 2', 'Cupsey B1', 'Cupsey Main' -> 'cupsey'
    'Rowdy5', 'Rowdy 19', 'Rowdy Fomo' -> 'rowdy'
    'Waddles18' -> 'waddles' (but 'four3two1' stays intact)
    """
    g = (name or "").strip().lower()
    # word suffixes with separator: main/fomo/team/sender/side
    g = re.sub(r"[\s\?\-\.]+(main|fomo|team|sender|side)\s*\??\s*$", "", g)
    # 'b1'/'b2' style suffixes with separator
    g = re.sub(r"[\s\?\-\.]+b\d+\s*\??\s*$", "", g)
    # trailing number with separator: 'duval 16', 'derp 10?'
    g = re.sub(r"[\s\?\-]+\d+\s*\??\s*$", "", g)
    # trailing glued number ONLY if the base has no digits: 'rowdy5'->'rowdy', 'waddles18'->'waddles'
    m = re.match(r"^([^\d]+?)\d+\s*\??\s*$", g)
    if m:
        g = m.group(1).strip()
    g = re.sub(r"[\s\?]+$", "", g).strip()
    return g or (name or "").strip().lower()


class BuyTracker:
    """
    Groups buys by (chain, token), counting TRADERS (name groups), not wallets:
    all of a trader's wallets ('Cupsey 1/2/3...') count as ONE with their best score.
    Fires the alert when EITHER:
      - >= threshold distinct traders buy (broad consensus rule), OR
      - >= min_wallets_score traders AND combined score >= score_threshold
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
        # key -> {"buys": {group: info}, "alerted_at": float, "last_alert_count": int, "last_alert_score": float}
        self.tokens = {}

    async def add_buy(self, chain: str, token: str, wallet: str, buy_info: dict) -> tuple[int, float]:
        key = f"{chain}:{token.lower()}"
        now = time.time()
        # trader_name merges numbered aliases ('Cupsey 2/3'); trader_group also strips 'Main'/'Fomo'...
        group = trader_group(buy_info.get("trader_name") or buy_info.get("name") or wallet)

        entry = self.tokens.setdefault(
            key, {"buys": {}, "alerted_at": 0.0, "last_alert_count": 0, "last_alert_score": 0.0})

        # Purge buys outside the window
        entry["buys"] = {g: b for g, b in entry["buys"].items() if now - b["time"] <= self.window_sec}

        # Merge into the trader group
        if group in entry["buys"]:
            g = entry["buys"][group]
            g["wallets"].add(wallet)
            g["wallets_count"] = len(g["wallets"])
            g["amount"] = (g.get("amount") or 0) + (buy_info.get("amount") or 0)
            g["sol_spent"] = (g.get("sol_spent") or 0) + (buy_info.get("sol_spent") or 0)
            if buy_info.get("usd") is not None:
                g["usd"] = (g.get("usd") or 0) + float(buy_info["usd"])
            g["score"] = max(float(g.get("score", 0.3)), float(buy_info.get("score", 0.3)))
            g["time"] = now
            if not g.get("emoji") and buy_info.get("emoji"):
                g["emoji"] = buy_info["emoji"]
            if not g.get("tx_hash") and buy_info.get("tx_hash"):
                g["tx_hash"] = buy_info["tx_hash"]
            if len(buy_info.get("name") or "") < len(g.get("name") or " " * 99):
                g["name"] = buy_info["name"]  # keep the shortest label ('Cupsey' over 'Cupsey 2')
        else:
            entry["buys"][group] = {
                "name": buy_info.get("trader_name") or buy_info.get("name") or wallet[:8],
                "emoji": buy_info.get("emoji", ""),
                "wallet": wallet,
                "wallets": {wallet},
                "amount": buy_info.get("amount") or 0,
                "sol_spent": buy_info.get("sol_spent") or 0,
                "usd": float(buy_info["usd"]) if buy_info.get("usd") is not None else None,
                "symbol": buy_info.get("symbol", ""),
                "tx_hash": buy_info.get("tx_hash", ""),
                "score": float(buy_info.get("score", 0.3)),
                "time": now,
            }

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

        return count, score_sum
