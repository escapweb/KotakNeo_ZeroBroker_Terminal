# Kotak Neo Option-Chain Terminal — Phase 7

A local, single-user Flask terminal for NIFTY and SENSEX. It loads REST option-chain snapshots, streams option quotes and the underlying index through Kotak SFeed, shows the chain in ascending strike order with ITM/ATM/OTM coloring, and opens a movable ticket when an LTP is clicked. The chain-size selector offers 10, 20, 30, or 40 strikes per side (approximately twice that many rows overall). Option Chain and Order Book are separate top-level tabs, alongside Positions.

## Order ticket and order management

- Buy/Sell sends one order request directly from the ticket button. The button is disabled immediately to guard against duplicate clicks. The ticket remembers the last selected Buy/Sell side in browser storage.
- Quantity is entered in units and must be a positive multiple of the market lot. Exchange is derived from the selected contract.
- Limit, Stop-loss Limit, and Stop-loss Market use their corresponding ticket fields.
- Choosing Market sends a Kotak LIMIT order priced at a fresh SFeed LTP (quote received within the last 30 seconds); otherwise it falls back to that contract's chain-snapshot LTP. The server never maps this choice to Kotak `MKT`.
- AMO is not configurable in the UI and the broker request always uses `NO`.
- The Order Book tab shows active/open, pending, trigger-pending, and partially filled orders. Limit and stop orders can be modified (price, quantity, trigger where applicable); active orders can be cancelled. Kotak remains the authority on whether an action is accepted.
- Order, modify, and cancel outcomes appear in a top-right toast with a close button and an 8-second auto-dismiss. No native JavaScript alerts are used; Cancel sends on its button click without an extra confirmation. An acknowledgement is not a fill. If a request result is `UNKNOWN` or the connection times out, refresh/check the order status before trying again; the broker may have received the request.
- The chain displays time value for each call and put: option LTP minus intrinsic value, using live SFeed option/spot prices when fresh and chain snapshot values otherwise. The highest displayed time value is highlighted.
- The ticket saves its last dragged screen position in browser storage.

## Positions

- The Positions tab lists only open NIFTY/SENSEX option positions (CE/PE), excluding futures, equities, and flat positions.
- Add More opens the existing editable ticket in the position's direction, defaulting to one lot. Exit opens it on the opposite side, defaulting to the full net quantity; quantity, price/order type, and validity can be reviewed or edited before the single submit click.
- Position tickets retain the position's exchange segment and product. The server refreshes positions before preview/submission, enforces lot multiples, accounts for active exit orders, and prevents an exit quantity larger than the available net position. Position contracts are also subscribed to SFeed; Market selection remains a LIMIT at the freshest LTP, with the broker position report's LTP as fallback when available.
- No position action is sent by clicking a table row/button; the user submits from the ticket.

## Install / run

Use Windows, Python 3.13 and the project virtual environment. If needed:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Start the app with:

```powershell
python app.py
```

If you keep a local `credentials.py` beside `app.py`, startup reads `CONSUMER_KEY`, `MOBILE_NUMBER`, `UCC`, `TOTP`, and `MPIN` from it. Without that file, the app falls back to terminal prompts. The Kotak Neo SDK's current TOTP login does not use `PASSWORD`; that field can remain in your file but is ignored. Keep `credentials.py` local: it is excluded by `.gitignore` and must not be included in a ZIP or shared copy. Open `http://127.0.0.1:5000`.

The app binds to `127.0.0.1` only. Run it from the whitelisted machine/network and do not expose this development server to your LAN or the internet.

## Files

- `app.py` — local Flask routes for market data, positions, order send/modify/cancel, and active-order reads
- `broker.py` — SDK login, chain normalization/sorting, moneyness, and order API adapter
- `market_feed.py` — async SFeed bridge and option/index quote cache
- `order_service.py` — server-side order validation and broker-field mapping
- `templates/index.html` — option chain, time value, top-level tabs, and movable order ticket
