import re
from collections import Counter


def _normalized(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().rstrip("?"))


def _numbered_candidate(name: str):
    clean = (name or "").strip().rstrip("?")
    separated = re.match(r"^(.*\S)\s+(?:[bBcC]\s*)?\d+$", clean)
    if separated:
        return separated.group(1).strip()
    attached = re.match(r"^(.*?[A-Za-z])(?:[bBcC])?\d+$", clean)
    if attached:
        return attached.group(1).strip()
    return None


def annotate_traders(wallets: list) -> list:
    """Add trader_key/name, grouping repeated numbered wallet aliases."""
    candidates = [_numbered_candidate(w.get("rename", "")) for w in wallets]
    candidate_counts = Counter(_normalized(c) for c in candidates if c)
    exact_names = {_normalized(w.get("rename", "")) for w in wallets}

    labels = {}
    for wallet, candidate in zip(wallets, candidates):
        original = (wallet.get("rename") or wallet["address"]).strip().rstrip("?")
        key = _normalized(original)
        if candidate:
            candidate_key = _normalized(candidate)
            if candidate_counts[candidate_key] >= 2 or candidate_key in exact_names:
                key = candidate_key
                labels.setdefault(key, candidate)
        labels.setdefault(key, original)
        wallet["trader_key"] = key

    for wallet in wallets:
        wallet["trader_name"] = labels[wallet["trader_key"]]
    return wallets
