"""How much of one Aave v3 Ethereum liquidation bonus came back through Chainlink SVR (MEV-Share refund)?

Reproduces the "recent large case on Ethereum Core" in the Aave forum post. Read-only; needs an Ethereum RPC
with historical state (the default, MEV Blocker's public endpoint, has it). Prints no addresses.

    python svr_refund.py <liquidation tx hash>

How it works
- Bonus = collateral to the liquidator x price - debt repaid x price, with Aave oracle prices in that block.
  The LiquidationCall collateral amount is already net of the protocol fee.
- Block builders (e.g. beaverbuild, BuilderNet) pay refunds through a small forwarder contract
  (0xfeeeeee4...). Its bytecode checks that calldata[0:4] equals the block timestamp and sends the value to
  calldata[4:24], so the real recipient can be read from the transaction input.
- SVR refund = the builder's payments in the same block to the SVR refund recipient (0x149b41...14e5).
  The validator payment is the builder's last transaction in the block.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chain import Chain, a32

RPC = "https://rpc.mevblocker.io"
ORACLE = "0x54586be62e3c3580375ae3723c145253060ca0c2"         # AaveOracle, Ethereum Core
T_LIQ = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
FORWARDER = "0xfeeeeee44046c3f61a8cc081e0918ef0de0a7ffc"
SVR_RECIPIENT = "0x149b41b1e4c00b5f9aa34b14fd9f84cfd2f014e5"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def main(h):
    ch = Chain(RPC, 20_000)
    rc = ch.rpc("eth_getTransactionReceipt", [h])
    b = int(rc["blockNumber"], 16)
    price = lambda asset: int(ch.call(ORACLE, "0xb3596f07" + a32(asset), hex(b)), 16) / 1e8   # getAssetPrice
    dec = lambda asset: int(ch.call(asset, "0x313ce567"), 16)
    liqs = [l for l in rc["logs"] if l["topics"] and l["topics"][0] == T_LIQ]
    bonus_usd = 0.0
    for l in liqs:
        coll, debt = "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:]
        d = l["data"][2:]
        debt_amt = int(d[0:64], 16) / 10 ** dec(debt)
        coll_amt = int(d[64:128], 16) / 10 ** dec(coll)
        bonus_usd += coll_amt * price(coll) - debt_amt * price(debt)
    eth = price(WETH)
    blk = ch.rpc("eth_getBlockByNumber", [hex(b), True])
    miner, txs = blk["miner"].lower(), blk["transactions"]
    refund = proposer = 0.0
    for i, t in enumerate(txs):
        if t["from"].lower() != miner or int(t["value"], 16) == 0:
            continue
        v = int(t["value"], 16) / 1e18
        to = (t["to"] or "").lower()
        real = "0x" + t["input"][10:50] if to == FORWARDER and len(t["input"]) >= 50 else to
        if real == SVR_RECIPIENT:
            refund += v
        elif i == len(txs) - 1:
            proposer += v
    bonus_eth = bonus_usd / eth
    print(f"block {b}: {len(liqs)} liquidation(s), bonus {bonus_eth:,.1f} ETH (${bonus_usd:,.0f} at ETH ${eth:,.2f})")
    print(f"SVR refund in the block: {refund:,.4f} ETH = {refund / bonus_eth:.1%} of the bonus")
    print(f"  implied winning bid if the builder keeps 10 %: {refund / 0.9:,.1f} ETH = {refund / 0.9 / bonus_eth:.1%}")
    print(f"validator payment (last builder tx): {proposer:,.4f} ETH")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
