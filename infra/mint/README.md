# Village token mint

A Cashu mint ([cdk-mintd](https://github.com/cashubtc/cdk) 0.18.2, image `cashubtc/mintd`) for
Open Source Village's tokens. The tokens have **no monetary value**: the
payment backend is cdk's fake wallet, the unit is `osv`, and nothing is ever paid in or out.

| What | How |
| --- | --- |
| Issuance | At activation the onboarding service mints 50 tokens per pass day (`TOKENS_PER_DAY`; day pass 50, week 350, full access every event day from the start) with the mint's custom NUT-04 method `belfry` (the fake backend's `bolt11` cannot price a custom unit). The `cashuB` token is stored on the pass (`cashu_token`, admins only; `cashu_amount`); `reconcile` mints for active passes without tokens. |
| Delivery | The activation mail and the claim page link `pass.<domain>/tokens/<claim token>`: a button into the wallet (`wallet.<domain>/#token=…`), the token to copy for any other Cashu wallet, or "already in a wallet" once redeemed (NUT-07 `checkstate`). |
| Gate | Only the onboarding service mints, over the internal network (`MINT_URL=http://mint:8085`). The production proxy (`mint.<domain>`) answers 403 for `/v1/mint/*` and `/v1/melt/*`; swap, keys, info and checkstate are public, so participants pass tokens to each other. Locally the host port 3338 is the mint itself, minting included. |
| Config | `config.toml.tmpl`, rendered by `entrypoint.sh` and imported into the mint's database (`config init` on the first start, `config apply` afterwards; cdk-mintd 0.18 reads its settings from the database). |
| Secret | `infra/secrets/mint_seed` (64 hex characters, `gen-secrets.sh`): the keyset keys derive from it. Back it up with the `mint-data` volume; a lost seed makes every token unredeemable. |

Checks: onboarding `tests/test_tokens.py` (NUT-00 test vectors, v4 token round trip, minting against
a fake mint that signs like a real one), smoke step 8c (350 tokens on the smoke test's week pass,
mail link, redeem like a wallet, the bundle reads as spent, minting closed on a public deployment).
