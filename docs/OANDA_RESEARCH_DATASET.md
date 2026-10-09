# Reproducible OANDA practice research dataset

## Why this exists

The repository's tracked Git tree did not contain the historical 1,799-row raw dataset during the recovery check. The root `.gitignore` also excludes `data/`, so a local file stored only there would not appear in normal Git history. Do not infer that a file never existed from those facts alone.

The existing `app.data.fetch_forex_oanda()` helper requests a candle **count** and does not pin a fixed `from`/`to` window. Re-running that helper later can return a different period. It is therefore unsuitable by itself for exact historical dataset reproduction.

## Capture the same dataset

1. Ensure the GitHub repository has an Actions secret named `OANDA_API_TOKEN` containing an OANDA **practice** token. Never commit the token or put it in workflow inputs.
2. Open **Actions → OANDA Practice Dataset Capture → Run workflow**.
3. Supply the original instrument, granularity, start timestamp, and end timestamp. Use the original values from the research notes or replay configuration; do not guess them.
4. Keep the expected completed-row count at `1799` for the recovery attempt. A mismatch intentionally fails the workflow after writing the capture files, so the artifact can be inspected without claiming it is the original.
5. Download the workflow artifact. It contains:
   - `oanda_raw_response.json`: exact HTTP response body returned by the practice endpoint;
   - `oanda_completed_mid_candles.csv`: completed mid candles with provider timestamp text and OHLC decimal strings preserved;
   - `manifest.json`: fixed request parameters, UTC timestamp range, row count, retrieval time, and SHA-256 hashes.

This endpoint is read-only and the capture script never places an order. It always uses `https://api-fxpractice.oanda.com`.

## Permanent archive and replay gate

GitHub Actions artifacts expire (this workflow retains them for 90 days). For durable research provenance, archive the three files under a tracked path such as `research_data/oanda/<capture-id>/` in a reviewed commit or an approved versioned object store; do not store the archive only under ignored `data/`. Record the archive commit or object version alongside the manifest.

Before any replay:
- run `python scripts/verify_oanda_research_dataset.py --dataset-dir <capture-directory>` and require a successful result;
- verify the raw-response and normalized-CSV SHA-256 values against the manifest;
- verify completed-row count, duplicate-free ascending timestamps (including sub-second precision), instrument, granularity, and fixed request window;
- compare the request window and instrument with the original experiment record;
- fix and test the known `train_model()` label-remapping issue before evaluating model results.

A new capture is a new dataset unless its provenance and exact content match the original. Matching 1,799 rows alone does not prove equivalence. Do not run performance calculations or report replay metrics until this gate passes.
