# Model marketplace

Five merchandising categories: open text models, Claude, OpenAI, image, video.
Writing, coding and analysis are scenario filters, not additional products.

`gateway/storefront.py` is the explicit public merchandising allowlist. Only
allowlisted routes present in the deployment are returned. Do not infer licensing,
capabilities or availability from arbitrary upstream aliases. Review metadata when
changing upstream model mappings. Currently only `semifly-27b` is listed; the
hosted `deepseek-chat` and `qwen-plus` aliases await model identity/category review.

The client performs local keyword/tag matching, with at most three suggestions.
Ambiguous input asks a follow-up choice; no match never invents a model. This is
not an LLM advisor, quality ranking or latency guarantee. Prices come from the same
server products used by checkout. Empty categories and unpriced models do not
offer purchase. No prompt text is transmitted for scenario matching.

This release does not enable category-wide billing, a USD wallet, image/video
transport, or new suppliers. Existing model-specific entitlements and keys remain
unchanged. The five cards organize discovery; actual purchases still specify a
model and token package. Account data remains authenticated.
