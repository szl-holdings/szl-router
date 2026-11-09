# Truth Router — frontier monetization (x402)

## The paid surface
`POST /route` is the frontier's monetizable endpoint: a routing decision
justified by measured honesty, energy, and price — the exact artifact the
agentic-commerce wave pays for.

## Honest gate (always-receipts rule)
- Catalog entry: `truth-router /route` joins the x402 paid catalog as a
  **paid API route** (same class as /v1/chat in the a-11-oy.com catalog).
- Status: **UNSETTLED_UNTIL_PROVEN** — no revenue claim until one verified
  x402 settlement receipt exists and is read back from the gateway.
- Free routes (proof surface): `/healthz`, `/registry` — the leaderboard
  stays free; the *decision* is the product.

## Wiring order (source chain)
1. GitHub: this repo (source authority) -> branch vibe/truth-router-a8a5eb
2. Hugging Face: SZLHOLDINGS/truth-router Space (deployed from this source)
3. Cloudflare: CNAME router.szholdings.si -> szlholdings-truth-router.hf.space
   (proxied, Full strict) + the x402 payment rule on POST /route
