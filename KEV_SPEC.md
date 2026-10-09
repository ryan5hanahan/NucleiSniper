# Local KEV backend — requirements & spec

[Kev](https://github.com/jaredpalmer/kev) is an open-weight, locally served model that answers
TypeSafe's System One API (`POST /v1/systemone`) with the same request/response shape as Jev.

## Requirements

- R1. NucleiSniper can score templates with a local Kev server instead of hosted Jev.
- R2. Jev stays the default; existing commands behave exactly as before.
- R3. No API key is needed for a local Kev server that runs open (its default).
  If the server sets `KEV_API_KEY`, NucleiSniper sends it.
- R4. An oversized request to Kev (HTTP 422, state > 65,536 tokens) splits the batch, just as a
  Jev `max_tokens_exceeded` 400 does.
- R5. Scores from different models never mix: `--resume` caches stay keyed by model name.
- R6. Verified against kev-0.8b, kev-4b and kev-9b running on this machine.

## Spec

### CLI
| Flag | Default | Meaning |
|---|---|---|
| `--endpoint URL` | `https://api.typesafe.ai/v1/systemone` | System One endpoint. Point at `http://127.0.0.1:8009/v1/systemone` for Kev. |
| `--model NAME` | `jev-latest` | Unchanged. Use `kev-0.8b`, `kev-4b`, `kev-9b` or `kev-latest` with Kev. |

No `--backend` flag: Kev *is* the same API, so the endpoint is the only thing that changes.

### API key
- Key = `TYPESAFE_API_KEY`, else `KEV_API_KEY`.
- A key is required only when `--endpoint` is the hosted TypeSafe default.
- With no key, no `Authorization` header is sent.

### Errors
- HTTP 422 whose body mentions tokens → `TokenLimitError` (batch is halved and retried).
- Connection refused on a local endpoint → existing retry, then the batch fails with the error
  message (unchanged behaviour).

### Batch size
Kev-0.8B/4B/9B are validated to ~8k tokens of state. Defaults stay unchanged; the eval reports
the token counts per batch so users can lower `--batch-size` if needed.

## Tests
- `test_kev.py` (stdlib `unittest`, no network): a fake System One server on localhost checks
  - requests go to `--endpoint`, with no `Authorization` header when no key is set;
  - a 422 token error splits the batch and every template still gets a score.

## Evaluation
- `eval_kev.sh` starts each Kev model in turn (port 8009) and runs NucleiSniper with
  `--no-scan` against a **local** test page on `127.0.0.1` with a fixed subset of
  nuclei-templates. No external hosts are scanned.
- Output per run: `eval/<model>-b<batch size>[-<page>].json` (scores, per-batch token usage, timings) and a
  summary table: scored templates, failed batches, wall time, tokens per batch, and top-10 overlap between runs
  on the same page. `ONLY`, `BATCH_SIZE` and `SITE` select models, batch size and test page (`eval/site`,
  `eval/site-joomla`).

### Results (2026-10-09, Apple Silicon, MLX)
Kev 1.0 weights, nuclei-templates v10.5.0, 400 sampled `http/` templates (seed 0); the prefilter kept 171
for the WordPress page and 113 for the Joomla page. Every run scored every template with no failed batches.

| Model | Batch | WordPress page: WP templates in top 10 / mean rank of 67 | Joomla page: ranks of the 4 Joomla templates | Wall s (WP / Joomla) |
|---|---|---|---|---|
| jev-latest (hosted) | 50 | 10 / 34 | 1, 2, 3, 4 | 0.4 / 0.3 |
| jev-latest (hosted) | 16 | 10 / 34 | 1, 2, 3, 4 | 0.6 / 0.4 |
| kev-0.8b | 16 | 8 / 63 | – | 4 / – |
| kev-4b | 12 | 10 / 40 | 1, 2, 4, 6 | 15 / 10 |
| kev-4b | 16 | 10 / 38 | 1, 2, 4, 5 | 14 / 9 |
| kev-4b | 50 | 10 / 43 | 1, 3, 4, 8 | 18 / 10 |
| kev-9b | 12 | 10 / 36 | 1, 2, 4, 5 | 28 / 17 |
| kev-9b | 16 | 10 / 35 | 1, 2, 3, 4 | 26 / 16 |
| kev-9b | 50 | 10 / 37 | 1, 2, 3, 6 | 28 / 17 |

Jev times are NucleiSniper's scoring time (`jev_wall`, hosted API). Kev times are the whole NucleiSniper run on this
Mac, of which indexing and profiling take under 0.1 s.

- Recommended: `--model kev-9b --batch-size 16 --threshold 1.0 --scan-min-score 1.0` (kev-4b if speed matters).
- kev-9b ranks the relevant templates about as well as Jev on both pages (mean WordPress rank 35 vs 34; Joomla
  templates 1–4 for both) but is ~50× slower on this Mac. Spearman correlation of all scores with Jev (batch 16):
  0.78 kev-9b, 0.74 kev-4b on the WordPress page; 0.37 and 0.48 on the Joomla page, where most of the order is among
  irrelevant templates.
- Jev separates more sharply: on the Joomla page exactly the 4 Joomla templates score ≥ 1.0 (max 1.66, next 0.75);
  kev-9b puts 12 templates over 1.0. Jev's mean confidence is 0.81–0.93 against kev-9b's 0.53–0.65.
- Scores are low for both backends on these thin pages: Jev scores 4 (WordPress) and 0 (Joomla) templates ≥ 2.0 at
  its default batch size, kev-9b 0 and 0 at batch 16. The defaults (`--threshold 2.5`, `--scan-min-score 2.0`)
  keep almost nothing with either; 1.0 keeps the relevant templates. Defaults stay unchanged (R2).
- Batch size barely matters for Jev (top-10 overlap 6–7/10 between 16 and 50, same target ranks).
- kev-0.8b scores everything about the same (sd 0.1, confidence ≤ 0.18): not useful for ranking.
- Batch size 50 sends 9–25k tokens per request, over the validated 8k; 16 keeps the median at 6.6k. 12 was no
  better than 16.
- Repeat runs: kev-4b is deterministic; kev-9b varies by ≤ 0.05 in score. Top-10 order still shifts between
  batch sizes because a template's score depends on the other templates in its batch; with many similar
  candidates (67 WordPress templates) ties reshuffle, while the 4 Joomla templates stay on top in every run.
