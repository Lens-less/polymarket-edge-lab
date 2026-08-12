# Phase 2 official semantics audit

Checked: 2026-07-24  
Scope: Polymarket official documentation, official Polymarket SDK/source repositories, the public first-party Data API, and the current V2 Polygon contracts. Polygon and Ethereum documentation is used only for standard public JSON-RPC/log retrieval and finality semantics. Chainlink documentation was considered only for a possible RTDS-to-Data-Streams timestamp mapping. No blog, forum, third-party client, or search-result summary is used as evidence.

## Executive implementation decisions

| Question | Evidence status | Safe implementation decision |
|---|---|---|
| Does Market WebSocket `last_trade_price.side` mean taker/aggressor direction? | **Still not explicitly defined.** Official examples strongly imply it, but neither the public event reference nor official SDK types say so directly. | Preserve the field with `side_semantics_verified=false`. Do not use it as the replay's authoritative direction source; public Data API or finalized V2 logs can replace that dependency. |
| Does public Data API `/trades?takerOnly=true` expose taker direction? | **Yes.** The official Rust SDK says this filter returns only taker trades, and its response type defines the wallet as the trader and `side` as the trade side. A public API row was also reconciled field-for-field to the same V2 `OrdersMatched` taker order. | It is valid independent taker-direction evidence. Capture it explicitly with `takerOnly=true`, raw response, receipt time, and `transactionHash`; do not assume undocumented completeness or finality. |
| Do current V2 logs expose taker/aggressor direction? | **Yes.** `OrdersMatched.side` is emitted from the active `takerOrder.side`. An arbitrary `OrderFilled.side` is not necessarily taker side because the same event is emitted for passive maker orders. | For historical replay, use finalized `OrdersMatched` plus sibling `OrderFilled` logs from both official V2 exchanges. This removes the dependency on unverified WebSocket side and correctly covers complementary, MINT, MERGE, and mixed-maker matches. |
| What is `fee_rate_bps`? | **Officially defined as a fee rate in basis points**, and the V2 migration guide says the `last_trade_price` value reflects the fee actually charged on the trade. Platform fees are applied at match time and are currently taker-only. | Do not treat it as a fee amount or a maker fee. Use decision-time market fee parameters for simulation; retain the WebSocket field as trade evidence/reconciliation. |
| Can CLOB Market subscriptions change without reconnecting? | **Officially explicit.** | Use `operation: "subscribe"` / `"unsubscribe"` with `assets_ids`; keep this protocol separate from RTDS `action` messages. |
| What do `fd`, `mos`, and `mts` mean? | **Officially explicit.** | Parse `fd.r/e/to`, `mos`, `mts`, plus `mbf`/`tbf`; persist the raw response and the decision-time interpreted values. |
| What is the Chainlink RTDS price timestamp/value schema? | **Officially explicit at RTDS level; not mapped to a Chainlink Data Streams report field.** | Treat `payload.timestamp` as the price-recorded timestamp in Unix ms and outer `timestamp` as message-send time. Preserve raw `value` as decimal text. Do not rename the inner timestamp to `observationsTimestamp`. |

## 1. Market WebSocket `last_trade_price.side`

### Officially explicit

The official Market Channel reference says `last_trade_price` is emitted when a maker and taker order are matched. Its public payload contains `asset_id`, `market`, `price`, `size`, `fee_rate_bps`, `side`, `timestamp`, and sometimes `transaction_hash`. The reference constrains `side` to `BUY` or `SELL`, but gives no prose definition of whose side it is:

- [Market Channel event reference](https://docs.polymarket.com/market-data/websocket/market-channel#last-trade-price)
- [API-reference form of the Market Channel](https://docs.polymarket.com/api-reference/wss/market)
- [Official agent-skills event table, pinned](https://github.com/Polymarket/agent-skills/blob/91ee44ae113e958affd20cd505c6e9d9d6100e0b/websocket.md#L29-L41)

The official authenticated User Channel example shows a matched trade with:

- top-level trade `side: "BUY"`;
- a participating `maker_orders[]` entry with `side: "SELL"`;
- top-level `trader_side: "TAKER"`.

Evidence: [official User Channel example](https://docs.polymarket.com/api-reference/wss/user).

### Inference from official sources

The authenticated example is consistent with top-level trade `side` being the taker/aggressor order direction, because the top-level side is opposite the maker order side and the example says `trader_side: "TAKER"`. The public `last_trade_price` event is also a trade-match event and exposes the same trade-shaped fields.

This is a **high-confidence inference from official material**, not an official field definition. No inspected official source directly states:

> `last_trade_price.side` is the taker/aggressor side.

### Unverified and implementation consequence

Do not encode the field as an incontrovertible aggressor flag. For a maker replay:

1. persist the raw `side`;
2. persist `side_semantics = "inferred_taker_direction"`;
3. persist `side_semantics_verified = false`;
4. do not use it as the authoritative capacity source when public Data API or finalized V2 logs are available;
5. if a diagnostic replay nevertheless uses `SELL` WebSocket events as capacity against a resting maker BUY, make that a named experimental assumption and keep the result outside any production-edge gate.

A book delta can corroborate the direction, but it does not turn the missing official definition into proof: cancellations, simultaneous order changes, aggregation, and message ordering can produce the same visible delta.

## 2. `fee_rate_bps`, maker/taker context, and fee curves

### Officially explicit

The official API documentation describes trade `fee_rate_bps` as “the fee rate in basis points.” The V2 migration guide adds that the `last_trade_price.fee_rate_bps` field “continues to reflect the fee actually charged on the trade”:

- [L2 trade fields](https://docs.polymarket.com/trading/clients/l2#gettrades)
- [CLOB V2 migration FAQ](https://docs.polymarket.com/v2-migration#faq)

Current platform-fee semantics are also explicit:

- fees are applied at match time;
- the current platform fee is paid by takers;
- makers are not charged the platform fee;
- the documented fee is `C × feeRate × p × (1 - p)` for the current fee tables;
- fee rounding is to five decimals and amounts below `0.00001` USDC round to zero.

Evidence: [official Fees documentation](https://docs.polymarket.com/trading/fees).

The official V2 SDK uses the general fee curve

```text
platformFeeRate = fd.r × (p × (1 - p)) ** fd.e
```

and separates platform fee from builder taker fee. Evidence: [official V2 SDK fee calculation, pinned](https://github.com/Polymarket/clob-client-v2/blob/f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69/src/fees/index.ts#L15-L30).

### Fields that must not be conflated

| Field | Unit/meaning |
|---|---|
| `last_trade_price.fee_rate_bps` | Trade fee rate in basis points; trade-time evidence, not a fee amount. |
| `fd.r` | Platform fee-curve rate parameter, represented as a decimal rate such as `0.07`, not the same wire unit as a bps integer. |
| `fd.e` | Fee-curve exponent. |
| `fd.to` | Taker-only flag; omitted when false in the official SDK type. |
| `mbf` | Maker base fee in basis points. |
| `tbf` | Taker base fee in basis points. |
| builder maker/taker fees | Separate builder charges; not established by `last_trade_price.fee_rate_bps`. |

### Safe replay use

- For a ghost maker order, record the decision-time `fd`, `mbf`, and `tbf` response.
- If `fd.to=true`, platform maker fee is zero under the current official model. Do not charge the observed taker trade rate to the ghost maker.
- Do not infer a monetary fee merely by dividing `fee_rate_bps` by 10,000. The platform fee curve depends on price, size, and `fd.e`, and builder fees are separate.
- Keep the WebSocket field as independent trade evidence. A null/missing field is not permission to substitute a current endpoint value retrospectively.

## 3. Dynamic subscribe/unsubscribe

### CLOB Market channel: officially supported format

Initial public Market subscription:

```json
{
  "assets_ids": ["TOKEN_ID_1", "TOKEN_ID_2"],
  "type": "market",
  "custom_feature_enabled": true
}
```

Dynamic changes on the same connection:

```json
{"assets_ids": ["NEW_TOKEN_ID"], "operation": "subscribe", "custom_feature_enabled": true}
{"assets_ids": ["OLD_TOKEN_ID"], "operation": "unsubscribe"}
```

Official evidence:

- [Market channel subscription reference](https://docs.polymarket.com/market-data/websocket/market-channel)
- [Official agent-skills dynamic examples, pinned](https://github.com/Polymarket/agent-skills/blob/91ee44ae113e958affd20cd505c6e9d9d6100e0b/websocket.md#L149-L162)

The official examples say subscriptions can be modified without reconnecting. They use token/asset IDs for the Market channel, not condition IDs.

### RTDS is a different protocol

RTDS uses:

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": "{\"symbol\":\"eth/usd\"}"
    }
  ]
}
```

Unsubscribe uses the same `subscriptions` structure with `"action": "unsubscribe"`. Official implementations:

- [Official RTDS documentation](https://docs.polymarket.com/market-data/realtime-data#rtds-streams)
- [Official TypeScript client subscribe/unsubscribe, pinned](https://github.com/Polymarket/real-time-data-client/blob/c937d9c11cdd2b771aa4818392a1b6dda65c25de/src/client.ts#L181-L211)
- [Official Rust request types, pinned](https://github.com/Polymarket/rs-clob-client/blob/dc8f1e58eb1e504dcb3d45a94a647dd2da89912b/src/rtds/types/request.rs#L9-L48)

Chainlink RTDS filters are JSON encoded **inside a string** and use slash-separated symbols. The official Rust SDK has an explicit special case for this wire format: [pinned source](https://github.com/Polymarket/rs-clob-client/blob/dc8f1e58eb1e504dcb3d45a94a647dd2da89912b/src/rtds/types/request.rs#L85-L94), [serialization rule](https://github.com/Polymarket/rs-clob-client/blob/dc8f1e58eb1e504dcb3d45a94a647dd2da89912b/src/rtds/types/request.rs#L138-L151).

### Limits not found in official material

The inspected official references do **not** specify:

- maximum asset IDs per dynamic CLOB update;
- maximum update rate;
- an acknowledgement/revision message for successful membership change;
- atomicity when a multi-asset update partially fails;
- duplicate subscribe/unsubscribe semantics;
- how changing `custom_feature_enabled` interacts with already subscribed assets.

Therefore the collector should:

- maintain its own desired/observed membership set;
- treat sends as requests, not acknowledgements;
- require a fresh `book` snapshot before a newly added asset is replay-eligible;
- use bounded batches and reconnect/resubscribe on ambiguity;
- retain connection/session IDs so data from a superseded connection cannot silently satisfy liveness.

## 4. `/clob-markets/{condition_id}` compact fields

The official endpoint returns all CLOB-level parameters for a market. The current docs define:

| Compact field | Official meaning |
|---|---|
| `c` | Condition ID. |
| `t` | Market tokens; each entry has `t` = token ID and `o` = outcome. |
| `mts` | Minimum tick size / price increment. |
| `mos` | Minimum order size. |
| `fd` | Platform fee details. |
| `fd.r` | Fee rate. |
| `fd.e` | Fee exponent. |
| `fd.to` | Taker only; omitted when false in the SDK type. |
| `mbf` | Maker base fee in basis points. |
| `tbf` | Taker base fee in basis points. |
| `nr` | Negative-risk flag; omitted when false in the SDK type. |
| `r` | Rewards configuration or null. |
| `ao` | Accepting orders. |
| `itode` | Taker-order delay enabled; the endpoint docs describe a 250 ms delay when true. |
| `oas` | Minimum order age in seconds. |

Evidence:

- [Get CLOB market info endpoint](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [Official V2 SDK `FeeDetails` and `MarketDetails`, pinned](https://github.com/Polymarket/clob-client-v2/blob/f3e1a05f868a1fd0c34ef85dfc45c6ce78f5bb69/src/types/clob.ts#L293-L329)

Implementation requirements:

- bind a replay decision to the exact raw endpoint record and its receipt time;
- parse decimal values without binary-float round trips;
- reject missing `mts` or `mos` for a proposed order;
- preserve unknown fields for forward compatibility;
- do not substitute a later market-info response for decision-time fee/tick/minimum-size evidence.

## 5. Chainlink RTDS schema and timestamps

### Officially explicit schema

Raw RTDS Chainlink messages use this envelope:

```json
{
  "topic": "crypto_prices_chainlink",
  "type": "update",
  "timestamp": 1782753357257,
  "payload": {
    "symbol": "eth/usd",
    "timestamp": 1782753357213,
    "value": 3420.15
  }
}
```

The official semantics available are:

- outer `timestamp`: Unix milliseconds when the message was sent;
- `payload.symbol`: slash-separated pair such as `btc/usd` or `eth/usd`;
- `payload.timestamp`: when the price was recorded, in Unix milliseconds;
- `payload.value`: current price in the quote currency;
- the official Rust SDK deserializes `value` as a decimal, not binary floating point.

Evidence:

- [Official RTDS examples and supported symbols](https://docs.polymarket.com/market-data/realtime-data#crypto-prices)
- [Official TypeScript envelope comment, pinned](https://github.com/Polymarket/real-time-data-client/blob/c937d9c11cdd2b771aa4818392a1b6dda65c25de/src/model.ts#L45-L63)
- [Official Rust `ChainlinkPrice`, pinned](https://github.com/Polymarket/rs-clob-client/blob/dc8f1e58eb1e504dcb3d45a94a647dd2da89912b/src/rtds/types/response.rs#L69-L79)

### Not established by official material

No inspected official Polymarket or Chainlink source maps RTDS `payload.timestamp` to a Chainlink Data Streams report field such as `observationsTimestamp`. The public RTDS payload does not expose a report ID, feed ID, signatures, valid-from time, expiry, or verified report bytes.

Therefore:

- call the inner field `price_timestamp_ms` or `recorded_timestamp_ms`;
- do **not** rename it to `observations_timestamp`;
- retain outer sent time and local receive time separately;
- do not claim that RTDS alone proves an onchain-verified Data Streams report.

The current official crypto schema defines `value`, but does not define a crypto `full_accuracy_value`. If an observed frame contains an extra precision field, preserve it verbatim as an undocumented extension and keep `schema_semantics_verified=false` for that field. Do not infer its scale from digit count.

### Boundary extraction consequence

For strict price-boundary work:

1. select on `payload.timestamp`, not the outer message-send time or local receipt time;
2. preserve the raw JSON scalar/text and parse through `Decimal`;
3. retain symbol, inner timestamp, outer timestamp, connection ID, session ID, local receipt time, raw record ID, manifest hash, and raw-file hash;
4. fail closed on duplicate same-timestamp values that disagree;
5. do not silently assume monotonic delivery, fixed cadence, no duplicates, or an exact tick at every wall-clock boundary—the official references make none of those guarantees.

## 6. Public, unauthenticated trade-direction evidence

### 6.1 Data API `/trades`: officially taker-side when requested that way

The first-party endpoint is public:

```text
GET https://data-api.polymarket.com/trades
```

Its official reference exposes `takerOnly` with default `true`, returns `proxyWallet`, `side`, `asset`, execution `size`/`price`, and `transactionHash`, and requires no authentication headers:

- [Get trades for a user or markets](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets)
- [Official Rust SDK Data API overview](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/README.md#L452-L474)

The official Rust SDK removes the remaining semantic ambiguity:

- `taker_only=true` means “only return taker trades”;
- each response is an executed trade where outcome tokens were bought or sold;
- `proxy_wallet` is “the trader's proxy wallet address”;
- `side` is the trade side, `BUY` or `SELL`.

Evidence:

- [official `TradesRequest`, pinned](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/src/data/types/request.rs#L112-L164)
- [official `Trade` response, pinned](https://github.com/Polymarket/rs-clob-client-v2/blob/222143d321eba97d5711a848265eb9aab3bc7ff4/src/data/types/response.rs#L172-L229)

Therefore, when the request explicitly contains `takerOnly=true`, its `side` is officially supported taker/aggressor direction. This conclusion does **not** redefine the separate Market WebSocket field.

#### Reproducible first-party/onchain cross-check

At 2026-07-24, this bounded public query:

```text
https://data-api.polymarket.com/trades?market=0x0ce24aa05b0f167611f82fa1a50b87b5a5e9f349a9715ea548e59a43895fbb3d&takerOnly=true&start=1784908414&end=1784908414&limit=500
```

returned transaction
`0x2c91c2ec4cb24f43a94fddf84979aa6c86be90d5a08f8c37b4aad44674c25261`
with:

```text
proxyWallet = 0xec62d23eff1a957d02293c5475d4b7a52f8d9191
side        = BUY
asset       = 31988071992387175316542693935425969010742574905109814924416067706681872704770
size        = 6.97
price       = 0.43
```

The same transaction's V2 `OrdersMatched` log has:

```text
takerOrderMaker  = 0xec62d23eff1a957d02293c5475d4b7a52f8d9191
side             = 0 (BUY)
tokenId          = 31988071992387175316542693935425969010742574905109814924416067706681872704770
makerAmountFilled = 2,997,100
takerAmountFilled = 6,970,000
```

Thus the public API row and the onchain active order agree on trader, BUY direction, token, 6.97-token quantity, and 0.43 execution price. This is an observed first-party cross-check, while the pinned SDK definitions above are the semantic authority.

When the same bounded query is made with `takerOnly=false`, it also returns a passive maker row:

```text
proxyWallet = 0x1bbd4be39595b8a2ba03895f38c3f471b4673bed
side        = BUY
asset       = 114128626005444826307900448598991163696598115079038846124094965053259387128928
size        = 6.97
price       = 0.57
```

This is a concrete V2 MINT match: active BUY on one outcome matched passive BUY on the complementary outcome. It proves that a maker-BUY replay which consumes only same-token `SELL` trades is structurally incomplete.

#### Data API boundary

The inspected official references do not specify:

- indexing latency or a maximum publication delay;
- a completeness SLA;
- pagination stability while new trades arrive;
- reorg/finality behavior;
- whether all passive maker legs are always decomposed in a way suitable for queue replay.

Consequently Data API `takerOnly=true` is sufficient to replace the unverified WebSocket field for **direction**, but not by itself for an exhaustive, finality-aware maker-capacity ledger. Always set the flag explicitly, capture immutable raw pages, paginate bounded time intervals, and deduplicate by a recorded composite such as `(transactionHash, proxyWallet, asset, side, size, price, timestamp)` before reconciling to finalized V2 logs.

### 6.2 Current V2 onchain events

The official contract-address page calls itself the single source of truth and lists Polygon mainnet chain ID 137:

| Contract | Address |
|---|---|
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg Risk CTF Exchange V2 | `0xe2222d279d744050d28e00520010520000310F59` |

Evidence: [official contract addresses](https://docs.polymarket.com/resources/contracts), [pinned V2 repository deployment table](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/README.md#L5-L19).

The source audit is pinned to V2 repository commit
[`ccc0596074f4dfd62c944fbca4de252893b82b4b`](https://github.com/Polymarket/ctf-exchange-v2/commit/ccc0596074f4dfd62c944fbca4de252893b82b4b).

#### Exact event ABIs

```solidity
event OrderFilled(
    bytes32 indexed orderHash,
    address indexed maker,
    address indexed taker,
    Side side,
    uint256 tokenId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled,
    uint256 fee,
    bytes32 builder,
    bytes32 metadata
);

event OrdersMatched(
    bytes32 indexed takerOrderHash,
    address indexed takerOrderMaker,
    Side side,
    uint256 tokenId,
    uint256 makerAmountFilled,
    uint256 takerAmountFilled
);
```

Evidence: [official `ITrading` event declarations, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/interfaces/ITrading.sol#L18-L40), [canonical topic strings and assembly encoding, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Events.sol#L12-L75).

Canonical topic zero values observed from the deployed V2 contract and fixed by those signatures are:

```text
OrderFilled  0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee
OrdersMatched 0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c
```

`OrdersMatched` is the decisive aggressor event:

- the external entry point defines `takerOrder` as the active order and `makerOrders` as orders matched against it;
- `_emitTakerFilledEvents` emits both the active order's `OrderFilled` and `OrdersMatched` from the same parameter object;
- every settlement path populates that object from `takerOrder.side` and `takerOrder.tokenId`.

Evidence:

- [active versus passive entry-point semantics, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/CTFExchange.sol#L39-L65)
- [paired active events, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Events.sol#L63-L75)
- [taker event population across settlement paths, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L65-L147)
- [complementary and BUY/mixed settlement paths, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L256-L348)

An arbitrary `OrderFilled.side` must **not** be labeled aggressor direction. Passive maker legs also emit `OrderFilled` with `side: makerOrder.side`. The active leg can be recognized by the sibling `OrdersMatched.takerOrderHash`, and its current implementation also uses `taker=address(this)`, but the explicit sibling event is the safer anchor:

- [passive complementary maker emission, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L201-L253)
- [passive batched-maker emission, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L478-L537)

#### `side`, asset, amount, and fee semantics

`Side.BUY=0` and `Side.SELL=1`. The event does not carry V1-style `makerAssetId` and `takerAssetId`; V2 derives them as:

```text
makerAssetId = side * tokenId
takerAssetId = tokenId - makerAssetId

BUY  -> maker asset = 0 (collateral), taker asset = tokenId
SELL -> maker asset = tokenId, taker asset = 0 (collateral)
```

Evidence: [official `Side` and order field semantics, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/libraries/Structs.sol#L30-L46), [asset derivation, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L610-L619).

The two fill amounts are relative to the order represented by that event:

| Order side | `makerAmountFilled` | `takerAmountFilled` |
|---|---|---|
| BUY | collateral actually paid | outcome tokens actually received |
| SELL | outcome tokens actually sold | collateral actually received |

`OrderFilled.fee` is the actual fee amount charged to that represented order, not a rate. BUY fees are collected in addition to collateral paid; SELL fees are deducted from collateral proceeds. `OrdersMatched` has no fee field, so link it by `takerOrderHash` to the active `OrderFilled` when fee evidence is needed. Evidence: [active fee and settlement path, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L351-L383), [maker fee and proceeds path, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L446-L475).

### 6.3 Read-only Polygon RPC/log retrieval

Polygon's official endpoint page lists chain ID 137 and a public, no-credential mainnet endpoint, `https://polygon.drpc.org`, while warning that public RPCs may be rate limited. Polygon endpoints follow the JSON-RPC standard:

- [Polygon PoS RPC endpoints](https://docs.polygon.technology/pos/reference/rpc-endpoints)
- [Ethereum JSON-RPC `eth_getLogs` filter and log-object semantics](https://ethereum.org/developers/docs/apis/json-rpc/#eth_getlogs)
- [Polygon finalized-block query](https://docs.polygon.technology/pos/concepts/finality/finality)

Read only the two official addresses, both event topic-zero values, and a bounded block range:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "eth_getLogs",
  "params": [{
    "fromBlock": "0x...",
    "toBlock": "0x...",
    "address": [
      "0xE111180000d2663C0091e4f400237545B87B996B",
      "0xe2222d279d744050d28e00520010520000310F59"
    ],
    "topics": [[
      "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",
      "0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c"
    ]]
  }]
}
```

This exact filter shape was live-checked on finalized block `0x5698866`. For the cross-check transaction above it returned passive `OrderFilled` at `logIndex=0x3b7`, active `OrderFilled` at `0x3bb`, and `OrdersMatched` at `0x3bc`, all from the official Neg Risk V2 exchange.

First obtain the latest irreversible boundary with `eth_getBlockByNumber(["finalized", false])`; then use that returned block number as `toBlock`. Query bounded chunks because the official public-RPC page does not promise an unlimited log range. Persist the raw JSON-RPC request and response plus `blockNumber`, `blockHash`, `transactionHash`, `transactionIndex`, `logIndex`, `address`, `topics`, `data`, and `removed`. Reject `removed=true`, verify the block hash again before sealing a manifest, and never silently backfill a failed range.

### 6.4 Decision for pessimistic maker-BUY replay

**The authoritative no-WebSocket-side path is finalized V2 `OrdersMatched` plus every sibling `OrderFilled` in its exchange-event segment.** It is sufficient to remove the unverified `last_trade_price.side` dependency from historical replay:

1. `OrdersMatched.side/tokenId` gives the active taker/aggressor order;
2. the active `OrderFilled` gives its actual fee;
3. passive `OrderFilled` legs give the exact represented maker side, token, and executed maker/taker amounts;
4. grouping all legs covers complementary BUY-vs-SELL, MINT both-BUY, MERGE both-SELL, and mixed-maker transactions.

Reconstruct segments in `logIndex` order separately for each `(transactionHash, exchangeAddress)`: maker `OrderFilled` events are emitted while settling the maker array, then `_emitTakerFilledEvents` emits the active `OrderFilled` followed immediately by `OrdersMatched`. Require the final active `OrderFilled.orderHash` to equal `OrdersMatched.takerOrderHash`; close the segment at that `OrdersMatched`, and fail closed on missing, duplicated, reordered, or undecodable legs. This avoids incorrectly grouping multiple `matchOrders` calls that share one transaction.

The official match types explicitly include complementary, MINT, and MERGE, and non-complementary paths use different token IDs:

- [official match-type enum, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/libraries/Structs.sol#L77-L83)
- [token compatibility checks, pinned](https://github.com/Polymarket/ctf-exchange-v2/blob/ccc0596074f4dfd62c944fbca4de252893b82b4b/src/exchange/mixins/Trading.sol#L637-L673)

For a ghost maker BUY on token X, a valid depletion event is not limited to an active SELL of X. An active BUY of the complementary token Y can fill passive BUY(X) through MINT. Therefore:

- **blocked:** same-token `last_trade_price.side == SELL` as the sole capacity rule;
- **blocked:** Data API `takerOnly=true` for the same token only;
- **allowed for research replay:** finalized V2 event-group decomposition across both condition tokens, with Data API as an independent reconciliation source;
- **still not production fill proof:** logs prove actual exchange executions, not the ghost order's queue position, order-age priority, hidden cancellations, or counterfactual matching outcome.

The pessimistic queue model must retain the decision-time L2 snapshot, give no cancellation credit unless independently proven, consume only eligible post-decision passive execution capacity at the ghost price or better, and keep all production/real-money promotion gates closed until queue/fill validity is separately established.

## Searched but insufficient to prove

- Official Market Channel documentation and official SDK event types list `last_trade_price.side` but do not define whose side it is.
- Official User Channel material supports a taker-side inference, but it is not a direct definition of the public event.
- Public Data API taker-side semantics are now explicit, but its official references do not provide indexing-latency, completeness, pagination-stability, or finality guarantees.
- V2 `OrdersMatched` proves active/taker direction, but `OrderFilled` must be classified as active or passive; arbitrary `OrderFilled.side` is not an aggressor flag.
- Official CLOB materials do not document a positive acknowledgement or capacity limit for dynamic subscription updates.
- Official RTDS material defines inner and outer timestamps at the service level, but does not map the inner timestamp to Chainlink Data Streams `observationsTimestamp`.
- Current official crypto-price schema does not document `full_accuracy_value` for `crypto_prices_chainlink`.

## Bottom line for Phase 2

The fee, market-info, subscription, RTDS timestamp/value, public Data API taker direction, and V2 onchain taker direction are sufficiently documented for a fail-closed public-data collector and historical research replay. `last_trade_price.side` itself remains undefined, but it is no longer a required semantic dependency: finalized `OrdersMatched` plus sibling `OrderFilled` logs can replace it and correctly handle same-side MINT as well as complementary matches.

This closes the **direction-source** blocker, not the profitability or real-fill gate. Neither Data API nor chain logs prove where a hypothetical order sat in queue or that it would have filled. Until that separate queue/counterfactual evidence exists, replay output may support falsifiable research but must remain `insufficient_data` for production-edge or real-money promotion.
