# Alpaca paper-trading pilot

This service scans SPY, QQQ, IWM, SMH, and XLE once per minute during regular U.S. market hours. It is intentionally hard-coded to Alpaca's paper endpoint and cannot place live orders.

The pilot is designed around a simulated $406 account even though Alpaca displays $100,000 of paper buying power. It trades at most one position at a time, caps each entry at $350 notional while keeping the planned loss at no more than $2.03, stops at six entries per day, and will not enter when any unrelated position or order exists.

## Signal

A paper long entry requires all of the following:

- At least 30 current-session one-minute bars.
- Four of the five tracked ETFs above session VWAP.
- Candidate above VWAP with 9 EMA above 20 EMA.
- Break above the prior 15-minute high without being more than 0.20% extended.
- Latest one-minute volume at least 1.5 times the median of the previous 20 bars.

The order is a day limit bracket with a 0.55% stop and 1.00% target (about 1.8:1 planned reward/risk). No new entry is allowed after 3:00 PM ET; tagged positions are flattened from 3:45 PM ET.

## Required Render secrets

Enter these directly in Render. Never send them in chat or commit them to Git:

- `APCA_API_KEY_ID`
- `APCA_API_SECRET_KEY`

Use paper credentials only. If credentials are missing, the service remains fail-closed and reports `paper_credentials_missing` at `/status`.

## Local test

```bash
python -m pip install -r requirements.txt pytest
pytest -q
uvicorn app:app --reload
```

This is an execution and monitoring scaffold, not a promise of profit. Paper fills omit important live-market effects, and any move to live trading should require a separate review of at least 50 paper trades.

## September 13 reliability update

- Collect all market-data pages; reject missing/stale data for any tracked symbol and exclude the current, unfinished minute.
- Reconstruct cooldown from broker history after restart and use a deterministic entry ID to prevent duplicate submissions within the cooldown bucket.
- Recheck open positions/orders and the session clock immediately before entry. Skip slow scans and entries that exceed the remaining planned daily loss allowance.
- Use the broker's session close to stop entries one hour before an early close and start exits 15 minutes before it.
- Confirm cancellations and reload fills/positions before flattening. Include partial fills and standalone exits in daily P&L.
- Emit a scan heartbeat to Render logs and expose the deployed revision at `/status`.

The configured Free Render service sleeps without inbound traffic. The paper loop runs only while the process is awake; this configuration does **not** provide continuous unattended execution. Upgrade the existing service to always-on paid compute before relying on scheduled scans/exits. Do not run a second copy against the same account. Paper credentials remain in Render, and every broker request is hard-coded to the paper endpoint.

Validation: `python -m pytest -q` checks order caps, pagination, stale/incomplete data, duplicate IDs, early close handling, partial-fill P&L, and exit cancellation races. These tests check execution behavior; they do not validate strategy profitability. Neither historical research rule tested separately on September 13 was approved for live trading, and neither is enabled here.

To pause new paper entries, set `PAPER_ORDERS_ENABLED=false` and redeploy. Existing tagged positions still receive exit management while the process is running. A stop order does not guarantee the configured loss cap during a gap or outage. This pilot does not reconcile old overnight positions automatically; any existing position blocks new entries and needs review if it is from a prior session.
