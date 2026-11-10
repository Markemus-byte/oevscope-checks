# OEV Scope checks

Scripts that reproduce the headline numbers [OEV Scope](https://oevscope.com) publishes about liquidation bonus and OEV recapture, straight from public blockchain data.

- **Read-only.** The scripts only call `eth_getLogs`, `eth_call`, `eth_getBlockByNumber`, `eth_blockNumber` and `eth_getTransactionReceipt`. No keys, no signing, no transactions.
- **No install.** Python 3.10 or newer, standard library only.
- **Free public RPCs by default.** Pass `--rpc` to use your own endpoint. Wider log ranges are much faster.
- **Aggregates only.** The scripts never print borrower or liquidator addresses.

## What each script reproduces

| Script | Claim | Published | What you should get |
|---|---|---|---|
| `moonwell_x72.py` | Moonwell Core on Base: share of liquidator bonus that went through the OEV wrapper, before and after MIP-X72 | 4.3 % before (8 Aug – 25 Oct 2026), 85.7 % after (26 Oct – 6 Nov); OEV fee 69–70 % of the bonus in those transactions | Same liquidation counts (2,179 and 432) and wrapper shares (4.3 % and 85.7 %). OEV fee 69 % and 70 % of the bonus in wrapper transactions. Bonus within a few % (we got $139.4k and $3.2k on 10 Nov) |
| `aave_baseline.py avalanche` | Aave v3 Avalanche, pre-SVR baseline, 9 Aug – 7 Nov 2026 | 156 liquidations, ≈ $472k debt repaid, ≈ $33k bonus, 6 % of the ARFC's 365-day figure | Same counts. USD within about 1 % (we got $471k and $32.9k on 10 Nov) |
| `aave_baseline.py polygon` | Aave v3 Polygon, same window | 396 liquidations, ≈ $666k debt repaid, ≈ $31k bonus, 10 % of the ARFC figure | Same counts. USD within about 1 % (we got $663k and $30.8k on 10 Nov) |
| `svr_refund.py <tx>` | The $23.4M Aave Ethereum Core liquidation on 4 Nov 2026: bonus and SVR refund | Bonus ≈ 400 ETH; 210.19 ETH (52.5 %) refunded through SVR; validator 0.20 ETH | Identical (prices are read in the block) |

USD figures move a little with prices, because most assets are priced at today's oracle price (see Method). Counts and shares do not.

`svr_refund.py` takes the liquidation's transaction hash. We do not publish transaction hashes or addresses in our posts. The 4 Nov case is the largest Ethereum Core liquidation that day, and it is listed on [LlamaRisk's SVR dashboard](https://dashboard.llamarisk.com/protocols/aave/svr/v3), which reports the same refund. The script works for any Aave v3 Ethereum liquidation.

## Run

```bash
python svr_refund.py 0x<liquidation tx hash>
python aave_baseline.py avalanche          # about 2 minutes
python aave_baseline.py polygon            # about 3 minutes
python moonwell_x72.py --after-only        # about 15 minutes
python moonwell_x72.py                     # about 1 hour on the free Tenderly gateway (1,000 blocks per call)
```

## Method

**Aave v3** (`aave_baseline.py`, `svr_refund.py`)
- `LiquidationCall` events from the Pool.
- Bonus = debt repaid × (liquidation bonus − 1) × (1 − liquidation protocol fee). This matches the ARFC's definition. LB and the fee come from the collateral reserve's configuration, or from the borrower's E-Mode category when it covers the collateral. E-Mode is read as of today.
- `svr_refund.py` instead uses collateral received × price − debt repaid × price in that block. Builders pay refunds through a small forwarder contract whose bytecode sends the value to the address in `calldata[4:24]`, so the real recipient can be read from the transaction input.

**Moonwell** (`moonwell_x72.py`)
- `LiquidateBorrow` events from every Moonwell Core market (from the Comptroller's `getAllMarkets`).
- Bonus = debt repaid × (liquidation incentive × (1 − protocol seize share) − 1).
- A liquidation counts as "through the OEV wrapper" when the same transaction emits `PriceUpdatedEarlyAndLiquidated`.

**Prices.** Most assets are priced with the protocol's own oracle as of today. Debt in the chain's native token, plus WETH and ETH derivatives on Base, is priced in the block, using the Chainlink feed's `AnswerUpdated` logs, so no archive node is needed.

**Differences from our published figures.** For Moonwell, our own pipeline estimated the wrapper share from a weighted sample of up to 800 transactions. `moonwell_x72.py` checks every transaction, so the shares can differ by a fraction of a percentage point.

## What is not here

These scripts cover the numbers in our public posts. OEV Scope's monthly measurement covers Aave-code markets, Compound III, Moonwell and Venus on twelve chains. It also estimates builder payments, winning bids and liquidator concentration. That pipeline and its raw data are part of the paid service.

## Disclosure and licence

OEV Scope is a paid measurement service of Brafaring Thevik (sole proprietorship, Norway, org. no. 938 405 565). These scripts are free under the MIT licence. They provide information about protocols and markets, not investment advice.

Questions or corrections: research@oevscope.com, or open an issue.
