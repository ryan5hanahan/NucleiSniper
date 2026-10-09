#!/bin/bash
# Score a fixed template subset against a local test page with each Kev model (KEV_SPEC.md "Evaluation").
# Kev runs contact only 127.0.0.1: Kev on :8009, the test page on :8010. Nothing is scanned (--no-scan).
# A jev-latest run also sends the test page's profile and template metadata to hosted TypeSafe.
#   KEV_DIR=~/tools/kev TEMPLATES=~/tools/nuclei-templates-v10.5.0 PYTHON=python3 ./eval_kev.sh
#   ONLY="kev-0.8b kev-4b" BATCH_SIZE=16 SITE=site-joomla ./eval_kev.sh   # subset / batch size / test page under eval/
#   The summary covers every run in eval/ against the same page.
#   ONLY=jev-latest scores with hosted Jev instead (needs TYPESAFE_API_KEY; never run unless named in ONLY).
#   KEV_START_TIMEOUT sets the model startup deadline in seconds (default: 600).
set -euo pipefail
cd "$(dirname "$0")"
KEV_DIR=${KEV_DIR:-$HOME/tools/kev}
TEMPLATES=${TEMPLATES:-$HOME/tools/nuclei-templates-v10.5.0}
PYTHON=${PYTHON:-python3}
SUBSET_SIZE=${SUBSET_SIZE:-400}
BATCH_SIZE=${BATCH_SIZE:-50}
KEV_START_TIMEOUT=${KEV_START_TIMEOUT:-600}
SITE=${SITE:-site}
suffix=$([ "$SITE" = site ] || echo "-${SITE#site-}")
# Kev 1.0 weight commits (the Hub v1.0 tags add only a model card on top of these).
MODELS=(
  "kev-0.8b jaredpalmer/kev-0.8b@9a45d25eb2ab761841196625383fa1dff0e56c1e"
  "kev-4b jaredpalmer/kev-4b@139fdd94f1b6a6ad80cc15e08fcb99cac885a101"
  "kev-9b jaredpalmer/kev-9b@b5d8c18e44c60888d138b65cb6507ff0a5a448a0"
  "jev-latest hosted"
)
mkdir -p eval

# Fixed subset: SUBSET_SIZE http/ templates drawn with seed 0 from the sorted list.
subset=eval/templates
if [ ! -d "$subset" ]; then
  "$PYTHON" - "$TEMPLATES" "$subset" "$SUBSET_SIZE" <<'PY'
import random, shutil, sys
from pathlib import Path
src, dst, n = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
files = sorted(p.relative_to(src) for p in (src / "http").rglob("*.yaml"))
for rel in random.Random(0).sample(files, n):
    (dst / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / rel, dst / rel)
print(f"[eval] {n} of {len(files)} templates -> {dst}")
PY
fi

"$PYTHON" -m http.server 8010 --bind 127.0.0.1 --directory "eval/$SITE" >/dev/null 2>&1 &
site_pid=$!
kev_pid=
trap 'kill $site_pid ${kev_pid:-} 2>/dev/null || true' EXIT

for entry in "${MODELS[@]}"; do
  read -r name run <<<"$entry"
  [[ -z "${ONLY:-}" && $run != hosted || " ${ONLY:-} " == *" $name "* ]] || continue
  out=eval/$name-b$BATCH_SIZE$suffix
  echo "[eval] starting $name ($run), batch size $BATCH_SIZE"
  endpoint=()
  if [ "$run" != hosted ]; then
    (cd "$KEV_DIR" && exec uv run --extra serve python -m kev.serve --run "$run" --host 127.0.0.1 --port 8009) >"$out.server.log" 2>&1 &
    kev_pid=$!
    readiness_headers=()
    if [ -n "${KEV_API_KEY:-}" ]; then
      readiness_headers=(-H "Authorization: Bearer $KEV_API_KEY")
    fi
    deadline=$(( $(date +%s) + KEV_START_TIMEOUT ))
    until curl -sf --connect-timeout 2 --max-time 5 ${readiness_headers[@]+"${readiness_headers[@]}"} http://127.0.0.1:8009/v1/models >/dev/null; do
      kill -0 $kev_pid 2>/dev/null || { echo "[eval] $name server exited, see $out.server.log"; exit 1; }
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[eval] $name server was not ready within $KEV_START_TIMEOUT seconds, see $out.server.log"
        exit 1
      fi
      sleep 5
    done
    endpoint=(--endpoint http://127.0.0.1:8009/v1/systemone)
  fi
  started=$(date +%s)
  "$PYTHON" NucleiSniper.py http://127.0.0.1:8010/ -t "$subset" ${endpoint[@]+"${endpoint[@]}"} \
    --model "$name" --batch-size "$BATCH_SIZE" --no-scan --timings --skip-version-check -o "$out.json" >"$out.log" 2>&1 \
    || echo "[eval] $name: NucleiSniper exited $?"
  echo "$(( $(date +%s) - started ))" >"$out.wall"
  [ -z "$kev_pid" ] || { kill $kev_pid; wait $kev_pid 2>/dev/null || true; kev_pid=; }
done

"$PYTHON" - "$suffix" <<'PY'
import json, re, sys
from itertools import combinations
from pathlib import Path
names = sorted(p.stem for p in Path("eval").glob("*.json") if re.fullmatch(rf"[jk]ev-[\w.]+-b\d+{re.escape(sys.argv[1])}", p.stem))
top = {}
print(f"{'run':<20} {'scored':>6} {'failed':>6} {'batches':>7} {'wall s':>7} {'tokens/batch (min/median/max)':>30}")
for n in names:
    rep = json.loads(Path(f"eval/{n}.json").read_text())
    row = rep["results"][0]
    toks = sorted(row["batch_input_tokens"])
    span = f"{toks[0]}/{toks[len(toks) // 2]}/{toks[-1]}" if toks else "-"
    ranked = sorted(row["all_results"], key=lambda r: (r["score"], r["confidence"]), reverse=True)
    top[n] = {r["file_path"] for r in ranked[:10]}
    wall = Path(f"eval/{n}.wall").read_text().strip()
    print(f"{n:<20} {row['evaluated_count']:>6} {row['failed_batches']:>6} {row['batch_count']:>7} {wall:>7} {span:>30}")
print("\ntop-10 overlap")
for a, b in combinations(names, 2):
    print(f"  {a} vs {b}: {len(top[a] & top[b])}/10")
PY
