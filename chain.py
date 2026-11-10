"""Small read-only JSON-RPC helpers used by the check scripts.

Standard library only. No keys, no signing, no transactions: the scripts only call
eth_blockNumber, eth_getBlockByNumber, eth_getLogs, eth_call and eth_getTransactionReceipt.
"""
import json
import sys
import time
import urllib.error
import urllib.request

HEADERS = {"Content-Type": "application/json", "User-Agent": "oevscope-checks/1.0"}

# Chainlink AnswerUpdated(int256 current, uint256 roundId, uint256 updatedAt)
T_ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"


class RpcError(Exception):
    pass


def words(h):
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def addr(word):
    return "0x" + hex(word)[2:].rjust(40, "0")


def a32(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def u32(n):
    return hex(n)[2:].rjust(64, "0")


def decode_str(h):
    """ABI string or bytes32 symbol."""
    if not h or h == "0x":
        return "?"
    b = bytes.fromhex(h[2:])
    try:
        if len(b) >= 64:
            off = int.from_bytes(b[:32], "big")
            n = int.from_bytes(b[off:off + 32], "big")
            return b[off + 32:off + 32 + n].decode("utf-8", "replace")
        return b.rstrip(b"\0").decode("utf-8", "replace")
    except Exception:
        return "?"


def note(msg):
    print(f"  {msg}", file=sys.stderr, flush=True)


class Chain:
    def __init__(self, url, max_span, sleep=0.15, batch_size=5):
        self.url, self.max_span, self.sleep, self.batch_size = url, max_span, sleep, batch_size

    def post(self, payload, tries=8):
        data = json.dumps(payload).encode()
        for i in range(tries):
            try:
                req = urllib.request.Request(self.url, data=data, headers=HEADERS)
                return json.loads(urllib.request.urlopen(req, timeout=90).read())
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if i == tries - 1:
                    raise RpcError(f"{self.url}: {e}")
                time.sleep(min(60, 2 * 2 ** i))

    def rpc(self, method, params):
        r = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RpcError(r["error"])
        return r["result"]

    def call(self, to, data, block="latest"):
        return self.rpc("eth_call", [{"to": to, "data": data}, block])

    def try_call(self, to, data, block="latest"):
        try:
            return self.call(to, data, block)
        except RpcError:
            return None

    def batch(self, calls):
        """[(method, params)] -> results in the same order (None where a call failed)."""
        out = []
        for i in range(0, len(calls), self.batch_size):
            grp = calls[i:i + self.batch_size]
            payload = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(grp)]
            r = self.post(payload)
            if isinstance(r, dict):   # endpoint without batch support: one by one
                res = []
                for m, p in grp:
                    try:
                        res.append(self.rpc(m, p))
                    except RpcError:
                        res.append(None)
                out += res
            else:
                by_id = {x.get("id"): x for x in r}
                out += [by_id.get(j, {}).get("result") for j in range(len(grp))]
            time.sleep(self.sleep)
        return out

    def head(self):
        return int(self.rpc("eth_blockNumber", []), 16)

    def timestamp(self, block):
        return int(self.rpc("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)

    def logs(self, topics, start, end, address=None, label=""):
        """eth_getLogs over [start, end] in chunks of at most max_span blocks; halves the chunk on errors."""
        out, cur, span, fails, calls = [], start, self.max_span, 0, 0
        while cur <= end:
            hi = min(end, cur + span - 1)
            flt = {"topics": topics, "fromBlock": hex(cur), "toBlock": hex(hi)}
            if address:
                flt["address"] = address
            try:
                r = self.post({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [flt]})
            except RpcError:
                r = {"error": "transport"}
            if "error" in r:
                fails += 1
                if fails > 40 or span == 1:
                    raise RpcError(r["error"])
                msg = str(r["error"]).lower()
                if "limit" in msg and "block" not in msg and "range" not in msg:
                    time.sleep(5)          # rate limit: wait, keep the chunk
                else:
                    span = max(1, span // 2)
                continue
            out.extend(l for l in r["result"] if not l.get("removed"))
            cur, fails, span = hi + 1, 0, min(self.max_span, span * 2)
            calls += 1
            if calls % 200 == 0:
                note(f"{label} {100 * (cur - start) / max(1, end - start + 1):.0f} % ({len(out)} events)")
            time.sleep(self.sleep)
        return out

    def price_series(self, proxy, start, end):
        """[(block, price)] from the Chainlink aggregator behind `proxy` (AnswerUpdated logs; no archive node needed)."""
        agg = "0x" + self.call(proxy, "0x245a7bfc")[-40:]            # aggregator()
        dec = int(self.call(proxy, "0x313ce567"), 16)                # decimals()
        lg = self.logs([T_ANSWER_UPDATED], max(1, start - 200_000), end, address=agg, label="price feed")
        return sorted((int(l["blockNumber"], 16), int(l["topics"][1], 16) / 10 ** dec) for l in lg)


def price_at(series, block, fallback):
    """Last feed answer at or before `block`."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= block:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else (series[0][1] if series else fallback)
