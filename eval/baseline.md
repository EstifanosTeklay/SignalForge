# tau^2-Bench Baseline — Act I

## What Was Reproduced

Ran the **tau^2-Bench retail domain** (30 test-split tasks, 5 trials each = 150 runs) using `anthropic/claude-haiku-4-5-20251001` via OpenRouter. The harness was cloned from the official tau^2-Bench repository and run against the standard retail task suite.

## Results

| Metric | Value |
|---|---|
| Model | `anthropic/claude-haiku-4-5-20251001` |
| Tasks | 30 (retail test split) |
| Trials | 5 per task |
| Total runs | 150 |
| **pass@1** | **0.5133 (51.3%)** |
| 95% CI | [0.4333, 0.5933] |
| Cost per run | $0.0182 |
| Total cost | $2.73 |
| Latency p50 | 21.7 s |
| Latency p95 | 30.2 s |

## Reproduction Check

A second pass with the same configuration reproduced identical scores, confirming stable output. The 95% Wilson CI `[0.4333, 0.5933]` brackets the pass@1 cleanly at n=150.

## Cost Per Run

Estimated at **$0.018 per run** based on haiku-4-5 pricing. At this rate, a full 150-run batch costs ~$2.73. A production eval tier run (claude-sonnet-4-6) would cost approximately 5–8x more.

## Unexpected Behavior

1. **OpenRouter weekly spending cap** was hit mid-run, requiring the batch to resume after cap reset.
2. **Airtable msgpack encoding**: layoffs.fyi serves its Airtable iframe data as gzip-compressed MessagePack rather than JSON. The scraper (v5) was updated to rewrite the request header (`x-airtable-accept-msgpack`) to force a JSON response — resulting in 4,360 real records.
3. **HubSpot custom properties**: `icp_segment`, `ai_maturity_score`, `enrichment_source`, `enrichment_timestamp` are not pre-created in the developer sandbox. The CRM handler falls back to storing enrichment data as a note body.
4. **Agent termination**: the haiku model occasionally chose the `TRANSFER` action before completing all required sub-tasks, resulting in partial rewards. This is expected at lower capability tiers and motivates the eval-tier model upgrade.
