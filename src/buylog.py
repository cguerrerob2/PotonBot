import json
import os
import tempfile
import time

MAX_AGE_SEC = 7 * 24 * 3600  # una semana de historial


class BuyLog:
    """Historial persistente de buys detectados (para cruzar con calls de Discord)."""

    def __init__(self, path: str):
        self.path = path
        self.entries = []
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.entries = data
            except Exception:
                pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.entries, f)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[buylog] Error saving: {e}")

    def add(self, chain: str, token: str, wallet: str, name: str, emoji: str,
            usd: float, tx_hash: str, score: float):
        now = time.time()
        self.entries.append({
            "chain": chain, "token": token.lower(), "wallet": wallet,
            "name": name, "emoji": emoji, "usd": round(float(usd), 2),
            "tx": tx_hash, "score": score, "ts": now,
        })
        # prune viejos
        cutoff = now - MAX_AGE_SEC
        self.entries = [e for e in self.entries if e["ts"] >= cutoff]
        self.save()

    def buyers_of(self, chain: str, token: str, before_ts: float) -> dict:
        """
        {wallet: {"name","emoji","score","usd_total","txs","first_ts"}}
        Solo buys ANTERIORES a before_ts.
        """
        out = {}
        tok = token.lower()
        for e in self.entries:
            if e["chain"] != chain or e["token"] != tok or e["ts"] >= before_ts:
                continue
            b = out.setdefault(e["wallet"], {
                "name": e["name"], "emoji": e["emoji"], "score": e.get("score", 0.3),
                "usd_total": 0.0, "txs": [], "first_ts": e["ts"],
            })
            b["usd_total"] += e["usd"]
            if e.get("tx"):
                b["txs"].append(e["tx"])
            b["first_ts"] = min(b["first_ts"], e["ts"])
        return out
