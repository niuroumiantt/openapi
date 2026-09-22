# Customer workspace: shared prepaid USD balance

Status: interactive prototype; not production billing. Approved direction: shared
prepaid balance, visible model input/output prices, two main navigation entries.

## Customer journey

The model page combines balance, manual recharge, searchable provider/model
catalogue, model connection details and paginated request history. API Keys is
the second main page. The profile menu contains recharge/billing and account
security. A customer may use several models with one key or several keys.

Recharge adds USD credit, not tokens or model ownership. A model's input, output,
cache and other billable units have explicit prices. Requests show actual units
and money charged, with the exact model and key. Do not display an exact remaining
token count for money shared across differently priced models.

The prototype covers zero balance, manual recharge, key creation, multi-model
requests, insufficient balance, key replacement and retained history. Its rates,
model identifiers, payments and keys are fictional. Integer micro-USD accounting
keeps prototype balance and ledger totals consistent. The conversation host may
retain demo state; this is not production account persistence.

## Required production implementation

- A durable monetary ledger, separate from the existing model-token ledger.
  Payment events grant credit idempotently after server-side amount, currency,
  account and order verification. Client success pages never grant funds.
- Immutable price versions and per-request input/output/cache usage. Define
  rounding and precision explicitly before real charges.
- Atomically reserve maximum authorized cost before forwarding, settle actual
  reported usage and release the remainder. Explicitly handle unknown usage,
  interruption, retries and provider errors. Prevent concurrent overspending.
- Account-scoped order, credit, charge and refund records with reconciliation.
  Never silently convert existing model-specific balances into money. Preserve
  legacy entitlements until an explicit migration or coexistence rule is defined.
- Key grants and revocation stay independent of funds. Support model allowlists,
  spend caps, revoked-key history and atomic replacement. Reveal plaintext once.
- Model catalogue stores precise provider/model IDs, availability, protocols,
  capability and rate limits; only tested, supported routes are saleable.
- Real accounts persist settings, keys, transactions and history server-side.
  Add tested password recovery and verified account changes.
- Model-specific discounted packages may be added later, only with explicit
  expiry, charging priority, refund and migration rules. Not part of default flow.

## Acceptance scenarios

1. A $10 top-up and two differently priced model calls reconcile to one balance.
2. A duplicated payment event does not duplicate credit.
3. Insufficient balance and concurrent reservations never produce overspending.
4. Revocation blocks the old key; replacement retains funds and prior attribution.
5. A new session restores account data; large histories are filtered/paginated.
6. A price update does not rewrite already settled usage or receipts.
7. Legacy model-token balances remain intact and separately identifiable.

No production deployment or monetary migration is included in this design step.
