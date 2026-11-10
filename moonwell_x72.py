"""Moonwell Core on Base: share of liquidator bonus that went through the OEV wrapper, before and after MIP-X72.

Reproduces the table in the forum post "Independent check: MIP-X72 moved Moonwell Base liquidations
into the OEV wrapper". Read-only, public RPC, standard library only.

    python moonwell_x72.py                      # default windows below, ~30-60 min on the free Tenderly gateway
    python moonwell_x72.py --after-only         # only the 12 days after the change, ~10 min
    python moonwell_x72.py --rpc URL --span N   # your own Base RPC (wider log ranges are much faster)

Method
- Every LiquidateBorrow event from a Moonwell Core market (all markets from the Comptroller).
- Debt repaid D = repayAmount x oracle price. Liquidator bonus = D x (LI x (1 - protocolSeizeShare) - 1).
- A liquidation counts as "through the OEV wrapper" when the same transaction emits
  PriceUpdatedEarlyAndLiquidated (Moonwell's ChainlinkOEVWrapper).
- OEV fee kept by the protocol = the fee in collateral mTokens from that event, valued at the liquidation's
  own rate in the same transaction (seized collateral = debt repaid x LI), so it does not drift with prices.
- Prices: the Moonwell oracle today, except debt in WETH and ETH derivatives, which is priced in the block
  from the Chainlink ETH/USD feed. USD amounts therefore drift a little with prices; counts and shares do not.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chain import Chain, addr, words, decode_str, a32, price_at, note, T_ANSWER_UPDATED

COMPTROLLER = "0xfbb21d0380bee3312b33c4353c8936a0f13ef26c"   # Moonwell Core, Base (Unitroller)
ETH_USD_PROXY = "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"  # Chainlink ETH / USD, Base
# LiquidateBorrow(address liquidator, address borrower, uint256 repayAmount, address mTokenCollateral, uint256 seizeTokens)
T_LB = "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52"
# PriceUpdatedEarlyAndLiquidated(address,uint256,address,address,uint256,uint256), first argument indexed
T_MW = "0xa08ed58aa51f2201266e88f22195bb2f6302eb0013fca2ac16e12151b356b35e"
ETH_LIKE = {"WETH", "ETH", "WSTETH", "STETH", "WEETH", "RETH", "CBETH", "EZETH", "RSETH", "WRSETH", "OSETH", "ETHX",
            "SUPEROETHB", "WSUPEROETHB", "WOETH"}
# Windows used in the post: MIP-X72 took effect on Base on 26 Oct 2026 (Moonwell's monthly report)
BEFORE = (49716374, 53091726)   # 8 Aug - 25 Oct 2026
AFTER = (53091727, 54037943)    # 26 Oct - 6 Nov 2026


def load_markets(ch):
    w = words(ch.call(COMPTROLLER, "0xb0772d0b"))                       # getAllMarkets()
    mk = [addr(x) for x in w[2:2 + w[1]]]
    oracle = "0x" + ch.call(COMPTROLLER, "0x7dc0d1d0")[-40:]              # oracle()
    li = int(ch.call(COMPTROLLER, "0x4ada90af"), 16) / 1e18              # liquidationIncentiveMantissa()
    q = []
    for m in mk:
        q += [("eth_call", [{"to": m, "data": "0x6f307dc3"}, "latest"]),                       # underlying()
              ("eth_call", [{"to": oracle, "data": "0xfc57d4df" + a32(m)}, "latest"]),         # getUnderlyingPrice
              ("eth_call", [{"to": m, "data": "0x6752e702"}, "latest"]),                       # protocolSeizeShareMantissa()
              ("eth_call", [{"to": m, "data": "0x182df0f5"}, "latest"]),                       # exchangeRateStored()
              ("eth_call", [{"to": m, "data": "0x95d89b41"}, "latest"])]                       # symbol()
    r = ch.batch(q)
    unders = ["0x" + x[-40:] if x and x != "0x" else None for x in r[0::5]]
    usyms = ch.batch([("eth_call", [{"to": u, "data": "0x95d89b41"}, "latest"]) for u in unders if u])
    it, out = iter(usyms), {}
    for i, m in enumerate(mk):
        _, price, pss, exch, sym = r[5 * i:5 * i + 5]
        usym = decode_str(next(it)) if unders[i] else decode_str(sym)[1:]
        out[m] = dict(usym=usym.upper(), price=int(price, 16) if price and price != "0x" else 0,
                      pss=int(pss, 16) / 1e18 if pss and pss != "0x" else 0.0,
                      exch=int(exch, 16) if exch and exch != "0x" else 0)
    return out, li


def scan(ch, markets, li, eth_now, b0, b1, label):
    t0 = time.time()
    feed_agg = "0x" + ch.call(ETH_USD_PROXY, "0x245a7bfc")[-40:]
    feed_dec = int(ch.call(ETH_USD_PROXY, "0x313ce567"), 16)
    lg = ch.logs([[T_LB, T_MW, T_ANSWER_UPDATED]], b0, b1, label=label)
    series = sorted((int(l["blockNumber"], 16), int(l["topics"][1], 16) / 10 ** feed_dec)
                    for l in lg if l["topics"][0] == T_ANSWER_UPDATED and l["address"].lower() == feed_agg)
    wrapper_tx, fee_mt = set(), {}   # fee_mt[(tx, collateral market)] = protocol fee in mTokens
    for l in lg:
        if l["topics"][0] == T_MW:
            w = words(l["data"])
            h = l["transactionHash"]
            wrapper_tx.add(h)
            k = (h, addr(w[1]))                  # w[1] = collateral market, w[3] = protocol fee in mTokens
            fee_mt[k] = fee_mt.get(k, 0) + w[3]
    seized = {}   # seized[(tx, collateral market)] = [debt x LI in USD, seized mTokens]
    n, txs, debt, bonus, bonus_mw = 0, set(), 0.0, 0.0, 0.0
    for l in lg:
        if l["topics"][0] != T_LB or l["address"].lower() not in markets:
            continue
        w = words(l["data"])
        repay, coll, seize = w[2], addr(w[3]), w[4]
        m, mc = markets[l["address"].lower()], markets.get(coll)
        if not mc or repay == 0 or not m["price"]:
            continue
        d = repay * m["price"] / 1e36
        if m["usym"] in ETH_LIKE and series:
            d *= price_at(series, int(l["blockNumber"], 16), eth_now) / eth_now
        b = d * (li * (1 - mc["pss"]) - 1)
        n, debt, bonus = n + 1, debt + d, bonus + b
        txs.add(l["transactionHash"])
        if l["transactionHash"] in wrapper_tx:
            bonus_mw += b
            s = seized.setdefault((l["transactionHash"], coll), [0.0, 0])
            s[0] += d * li
            s[1] += seize
    # The fee is valued at the liquidation's own exchange rate: seized collateral is worth debt x LI at the
    # moment of liquidation, so one collateral mToken is worth (debt x LI) / seized mTokens. No price drift.
    fee = sum(mt * seized[k][0] / seized[k][1] for k, mt in fee_mt.items() if k in seized and seized[k][1])
    days = (ch.timestamp(b1) - ch.timestamp(b0)) / 86400
    note(f"{label}: {len(lg)} events read in {time.time() - t0:.0f} s")
    return dict(label=label, blocks=(b0, b1), days=days, n=n, tx=len(txs), debt=debt, bonus=bonus,
                share=bonus_mw / bonus if bonus else 0.0, fee=fee, fee_share=fee / bonus_mw if bonus_mw else 0.0,
                no_fee=bonus - bonus_mw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rpc", default="https://base.gateway.tenderly.co")
    ap.add_argument("--span", type=int, default=1000, help="max blocks per eth_getLogs (Tenderly without key: 1000)")
    ap.add_argument("--after-only", action="store_true")
    a = ap.parse_args()
    ch = Chain(a.rpc, a.span)
    markets, li = load_markets(ch)
    weth = next((m for m in markets.values() if m["usym"] == "WETH"), None)
    eth_now = weth["price"] / 1e18 if weth else 0
    note(f"{len(markets)} Moonwell Core markets on Base, liquidation incentive {li:.3f}, ETH ${eth_now:,.2f}")
    res = [] if a.after_only else [scan(ch, markets, li, eth_now, *BEFORE, "before MIP-X72")]
    res.append(scan(ch, markets, li, eth_now, *AFTER, "after MIP-X72"))
    print("\nMoonwell Core on Base, liquidator bonus and the OEV wrapper")
    for r in res:
        print(f"\n{r['label']}: blocks {r['blocks'][0]}-{r['blocks'][1]} ({r['days']:.1f} days)")
        print(f"  liquidations                        {r['n']:>10,}   ({r['tx']:,} transactions)")
        print(f"  debt repaid                         ${r['debt']:>10,.0f}")
        print(f"  liquidator bonus                    ${r['bonus']:>10,.0f}")
        print(f"  share of bonus through OEV wrapper  {r['share']:>10.1%}")
        print(f"  OEV fee kept by the protocol        ${r['fee']:>10,.0f}   ({r['fee_share']:.0%} of the bonus in those transactions)")
        print(f"  bonus with no OEV fee               ${r['no_fee']:>10,.0f}")
    print(f"\nPrices: Moonwell oracle now (ETH ${eth_now:,.2f}); WETH and ETH-derivative debt priced in the block.")


if __name__ == "__main__":
    sys.exit(main())
