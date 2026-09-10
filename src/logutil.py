import time


def log(tag: str, msg: str):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{tag}] {msg}", flush=True)
