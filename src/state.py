import json
import os
import tempfile


class State:
    """Simple on-disk persistence so we don't re-alert after restarts."""

    def __init__(self, path: str):
        self.path = path
        self.data = {"solana": {}, "evm": {}}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data["solana"] = loaded.get("solana", {})
                    self.data["evm"] = loaded.get("evm", {})
            except Exception:
                pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[state] Error saving state: {e}")

    # --- Solana: latest signature seen per wallet ---
    def get_sol_last_sig(self, address: str):
        return self.data["solana"].get(address)

    def set_sol_last_sig(self, address: str, sig: str):
        self.data["solana"][address] = sig

    # --- EVM: latest token-tx timestamp seen per wallet+chain ---
    def get_evm_last_ts(self, address: str, chain_id: str) -> int:
        return int(self.data["evm"].get(f"{chain_id}:{address.lower()}", 0))

    def set_evm_last_ts(self, address: str, chain_id: str, ts: int):
        self.data["evm"][f"{chain_id}:{address.lower()}"] = int(ts)

    # --- EVM RPC mode (HOOD): latest scanned block per wallet+chain ---
    def get_evm_last_block(self, address: str, chain_id: str) -> int:
        return int(self.data["evm"].get(f"block:{chain_id}:{address.lower()}", 0))

    def set_evm_last_block(self, address: str, chain_id: str, block: int):
        self.data["evm"][f"block:{chain_id}:{address.lower()}"] = int(block)
