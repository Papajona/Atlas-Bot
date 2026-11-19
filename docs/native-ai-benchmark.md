# Atlas Native AI benchmark

## What this benchmark measures

Run the evaluator against labelled strategy-request prompts. It reports:

- exact-case accuracy (all supplied labels for a case must match);
- expectation accuracy, including required and forbidden warning markers;
- per-field accuracy for asset, instrument, strategy family, direction, timeframe, risk, stop/target and sessions;
- strategy-output contract validity, including supported enums, non-empty rules, mandatory backtest/walk-forward flags and paper-shadow-only execution;
- optional paired scores for another system on the *same exact dataset*.

The benchmark is deterministic and makes no network calls. Reports contain case IDs and aggregate/mismatch details, never prompt text.

## Built-in corpus

The repository's `tests/fixtures/native_ai_prompt_corpus.json` is a self-authored synthetic regression fixture. It is useful for detecting parser regressions. It is not a representative sample of Atlas customers, a real-world benchmark, or a comparison against another AI system. Its scores must not be described as any of those.

Run it with:

```bash
python scripts/eval_native_ai.py --json --output native-ai-regression-report.json
```

## Supplying a real-user or external corpus

Do not enable background/raw-prompt collection to manufacture benchmark data. Prepare an offline JSON file containing only prompts for which the submitter has permission to use the text for evaluation. Remove names, contact details, account IDs, API keys, portfolio identifiers and other identifying material before running the evaluator. Attach a stable, non-identifying case ID and a human-reviewed expected label for each measured field.

For a real-user set, use this wrapper shape (replace the example case with consented, anonymised and labelled examples):

```json
{
  "schema_version": 1,
  "metadata": {
    "dataset_type": "real_user_consented",
    "source": "opt-in, anonymised strategy-lab evaluation sample",
    "consent_confirmed": true,
    "anonymized": true
  },
  "cases": [
    {
      "id": "case-001",
      "prompt": "BTC breakout on 1h, risk 0.25%, target 3R",
      "expect": {
        "asset": "crypto",
        "symbol": "BTC/USDT",
        "strategy_family": "BREAKOUT",
        "timeframes": ["1h"],
        "risk_per_trade": 0.0025,
        "take_profit_rr": 3.0
      }
    }
  ]
}
```

The consent and anonymisation flags are submitter attestations; the evaluator cannot independently verify consent, identity removal or dataset representativeness. A plain JSON array is accepted as `user_supplied_unverified`, not as a real-user benchmark. The evaluator validates labels and case IDs and writes no prompts to its report.

Run the custom corpus locally:

```bash
python scripts/eval_native_ai.py \
  --dataset /secure/local/path/atlas-prompts.json \
  --output /secure/local/path/native-ai-real-user-report.json \
  --json
```

Do not commit private prompt corpora, prompt exports, or reports containing sensitive annotations to the public repository.

## Paired system comparison

For a fair comparison, run the reference system against the same labelled cases and normalise its structured output to the Atlas strategy-spec field names. Save a separate file containing one prediction for every case:

```json
{
  "schema_version": 1,
  "system": "Reference system and version",
  "dataset_sha256": "SHA256_OF_THE_EXACT_DATASET_FILE",
  "predictions": [
    {
      "id": "case-001",
      "output": {
        "version": "atlas-strategy-v1",
        "title": "Reference draft",
        "summary": "A reference strategy draft for evaluation.",
        "asset": "crypto",
        "symbols": ["BTC/USDT"],
        "timeframes": ["1h"],
        "direction": "LONG",
        "strategy_family": "BREAKOUT",
        "entry_conditions": [{"type": "BREAKOUT", "rule": "Completed close above prior range"}],
        "exit_conditions": [{"type": "EXIT", "rule": "Protective stop or target"}],
        "risk_per_trade": 0.0025,
        "requires_backtest": true,
        "requires_walk_forward": true,
        "execution_mode": "PAPER_SHADOW_ONLY"
      }
    }
  ]
}
```

Compute the SHA-256 over the exact dataset bytes, not a reconstructed or reformatted copy. The evaluator rejects mismatched dataset hashes, duplicate IDs, missing IDs and extra IDs so the systems cannot be scored on different prompt sets.

```bash
python scripts/eval_native_ai.py \
  --dataset /secure/local/path/atlas-prompts.json \
  --comparison-results /secure/local/path/reference-results.json \
  --output /secure/local/path/paired-benchmark-report.json \
  --json
```

A paired score is only as reliable as the human labels and the comparator outputs supplied. Record the reference model/version, configuration and generation date with the benchmark run. Do not compare a real-user score to the built-in synthetic regression score as if they were the same test population.

## Metric interpretation

- **Expectation accuracy** is the fraction of explicitly labelled output fields and warning assertions that match.
- **Field accuracy** breaks that score down by label type. Warning markers are counted separately as a field.
- **Case exact accuracy** is the fraction of prompts for which every expectation matches and the strategy output satisfies the paper-only contract.
- **Output contract rate** is the fraction of responses satisfying the schema-critical safety constraints checked by this evaluator. It is not a backtest, trading-performance or execution-safety certification.
- **Provenance status** distinguishes repository fixtures, unverified user files and submitter-attested real/public datasets. Submitter attestation is not independent verification.

A real-world quality benchmark is only complete after a consented, anonymised prompt sample has been labelled and evaluated. A competitor comparison is only complete after paired predictions from a named and versioned reference system have been supplied and scored against that same sample.
