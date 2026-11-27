Recorded provider responses (public market data, no secrets).
- dexscreener_tokens_wsol.json: live `GET /tokens/v1/solana/{WSOL}` (images/socials stripped).
- jupiter_search_wsol.json: live `GET /tokens/v2/search?query={WSOL}` (icon stripped).
- birdeye_ohlcv.json: hand-written from the documented response example; NOT a live capture
  (Birdeye requires an API key, which was unavailable).
- jupiter_quote_sell.json: live `GET /swap/v1/quote` selling a token for wSOL (report fields stripped).
