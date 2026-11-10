"""Aave v3 pre-SVR baseline: liquidations, debt repaid and liquidator bonus on one deployment.

Reproduces the table in the forum post "Independent pre-SVR baseline for Avalanche, Polygon and BNB Chain V3".
Read-only, public RPC, standard library only.

    python aave_baseline.py avalanche
    python aave_baseline.py polygon
    python aave_baseline.py polygon --blocks 91685546 96869546 --rpc URL --span N

Method (same definition as the Aave ARFC: liquidation bonus less the protocol liquidation fee)
- Every LiquidationCall event from the Pool in the block window.
- Debt repaid D = debtToCover x Aave oracle price.
- Bonus = D x (LB - 1) x (1 - liquidation protocol fee), with LB and the fee from the collateral reserve's
  configuration. If the borrower is in an E-Mode category that contains the collateral, the category's LB is used.
  E-Mode is read as of today.
- Prices: the Aave oracle today, except debt in the chain's native token, which is priced in the block from the
  Chainlink native/USD feed where one exists (Polygon). USD amounts drift a little with prices; counts do not.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chain import Chain, addr, words, decode_str, a32, u32, price_at, note, RpcError

T_LIQ = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"  # LiquidationCall
DEPLOYMENTS = {
    # name: pool, default RPC, max log span, default blocks (9 Aug - 7 Nov 2026), native symbols, native/USD feed, ARFC 365-day bonus
    "avalanche": dict(pool="0x794a61358d6845594f94dc1db02a252b5b4814ad", rpc="https://avalanche.gateway.tenderly.co",
                      span=50_000, blocks=(92360145, 99628082), native={"WAVAX"}, feed=None, arfc=2_270_000),
    "polygon": dict(pool="0x794a61358d6845594f94dc1db02a252b5b4814ad", rpc="https://polygon.gateway.tenderly.co",
                    span=50_000, blocks=(91685546, 96869546), native={"WPOL", "WMATIC", "POL"},
                    feed="0xab594600376ec9fd91f8e885dadf0ce036862de0", arfc=1_220_000),
}


def reserves(ch, pool):
    prov = "0x" + ch.call(pool, "0x0542975c")[-40:]                 # ADDRESSES_PROVIDER()
    oracle = "0x" + ch.call(prov, "0xfca513a8")[-40:]               # getPriceOracle()
    unit = int(ch.call(oracle, "0x8c89b64f"), 16)                   # BASE_CURRENCY_UNIT()
    lw = words(ch.call(pool, "0xd1946dbc"))                         # getReservesList()
    lst = [addr(x) for x in lw[2:2 + lw[1]]]
    q = []
    for a in lst:
        q += [("eth_call", [{"to": pool, "data": "0xc44b11f7" + a32(a)}, "latest"]),     # getConfiguration
              ("eth_call", [{"to": pool, "data": "0x35ea6a75" + a32(a)}, "latest"]),     # getReserveData
              ("eth_call", [{"to": a, "data": "0x95d89b41"}, "latest"]),                 # symbol()
              ("eth_call", [{"to": a, "data": "0x313ce567"}, "latest"]),                 # decimals()
              ("eth_call", [{"to": oracle, "data": "0xb3596f07" + a32(a)}, "latest"])]   # getAssetPrice
    r = ch.batch(q)
    out = {}
    for i, a in enumerate(lst):
        cfg, rd, sym, dec, price = r[5 * i:5 * i + 5]
        if not cfg or not dec or not price:
            continue
        c = int(cfg, 16)
        out[a] = dict(sym=decode_str(sym).upper(), dec=int(dec, 16), price=int(price, 16) / unit,
                      lb=((c >> 32) & 0xFFFF) / 1e4, fee=((c >> 152) & 0xFFFF) / 1e4, emode_bits=(c >> 168) & 0xFF,
                      id=words(rd)[7] if rd else None)
    return out


def emode_lb(ch, pool, users):
    um = ch.batch([("eth_call", [{"to": pool, "data": "0xeddf1b79" + a32(u)}, "latest"]) for u in users])  # getUserEMode
    ucat = {u: (int(x, 16) if x else 0) for u, x in zip(users, um)}
    cats = {}
    for c in sorted(set(ucat.values()) - {0}):
        try:
            w = words(ch.call(pool, "0xb286f467" + u32(c)))        # getEModeCategoryCollateralConfig(uint8)
            cats[c] = (w[2] / 1e4, int(ch.call(pool, "0xb0771dba" + u32(c)), 16))   # (LB, collateral bitmap)
        except RpcError:
            try:
                cats[c] = (words(ch.call(pool, "0x6c6f6ae1" + u32(c)))[3] / 1e4, None)  # getEModeCategoryData (older)
            except RpcError:
                pass
    return ucat, cats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("deployment", choices=sorted(DEPLOYMENTS))
    ap.add_argument("--blocks", nargs=2, type=int, metavar=("FROM", "TO"))
    ap.add_argument("--rpc")
    ap.add_argument("--span", type=int)
    a = ap.parse_args()
    D = DEPLOYMENTS[a.deployment]
    ch = Chain(a.rpc or D["rpc"], a.span or D["span"])
    b0, b1 = a.blocks or D["blocks"]
    R = reserves(ch, D["pool"])
    note(f"{len(R)} reserves; reading LiquidationCall in blocks {b0}-{b1}")
    evs = []
    for l in ch.logs([T_LIQ], b0, b1, address=D["pool"], label=a.deployment):
        w = words(l["data"])
        e = dict(block=int(l["blockNumber"], 16), tx=l["transactionHash"], coll="0x" + l["topics"][1][-40:],
                 debt="0x" + l["topics"][2][-40:], user="0x" + l["topics"][3][-40:], amt=w[0])
        if e["amt"] > 0 and e["coll"] in R and e["debt"] in R:
            evs.append(e)
    ucat, cats = emode_lb(ch, D["pool"], sorted({e["user"] for e in evs}))
    native_now = next((r["price"] for r in R.values() if r["sym"] in D["native"]), None)
    series = ch.price_series(D["feed"], b0, b1) if D["feed"] and native_now else []
    debt = bonus = 0.0
    for e in evs:
        rc, rd = R[e["coll"]], R[e["debt"]]
        lb, cn = rc["lb"], ucat.get(e["user"], 0)
        if cn in cats and cats[cn][0] > 1:
            bm = cats[cn][1]
            inside = (bm >> rc["id"]) & 1 if bm is not None and rc["id"] is not None else rc["emode_bits"] == cn
            if inside:
                lb = cats[cn][0]
        d = e["amt"] / 10 ** rd["dec"] * rd["price"]
        if rd["sym"] in D["native"] and series:
            d *= price_at(series, e["block"], native_now) / native_now
        debt += d
        bonus += d * (lb - 1) * (1 - rc["fee"])
    days = (ch.timestamp(b1) - ch.timestamp(b0)) / 86400
    ann = bonus * 365 / days if days else 0
    print(f"\nAave v3 {a.deployment}: blocks {b0}-{b1} ({days:.1f} days)")
    print(f"  liquidations         {len(evs):>10,}   ({len({e['tx'] for e in evs}):,} transactions)")
    print(f"  debt repaid          ${debt:>10,.0f}")
    print(f"  liquidator bonus     ${bonus:>10,.0f}   (after the protocol liquidation fee)")
    print(f"  annualised           ${ann:>10,.0f}   = {ann / D['arfc']:.0%} of the ARFC's 365-day figure (${D['arfc'] / 1e6:.2f} M)")
    print("\nPrices: Aave oracle now" + (", native-token debt priced in the block." if series else "."))


if __name__ == "__main__":
    sys.exit(main())
