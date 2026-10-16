#!/usr/bin/env bash
set -euo pipefail
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO"
PYTHON=${HARNESS_PYTHON:-python3}
OUTPUT_ROOT=${HARNESS_ACCEPT_ROOT:-"$REPO/../acceptance"}
mkdir -p "$OUTPUT_ROOT"
OUTPUT=$(mktemp -d "$OUTPUT_ROOT/run-XXXXXX")
mkdir -p "$OUTPUT/tmp" "$OUTPUT/base"
export TMPDIR="$OUTPUT/tmp"
export PYTHONDONTWRITEBYTECODE=1
export MSWEA_SILENT_STARTUP=1
export PYTEST_ADDOPTS=--capture=sys
export PYTHONPATH="$REPO/src:$REPO"
BASE=1f17d23e7df65e50a005542682f51d69853b5760
git rev-parse HEAD > "$OUTPUT/code-version.txt"
"$PYTHON" - > "$OUTPUT/dependencies.txt" <<'PYDEPS'
from importlib.metadata import distributions
print("\n".join(sorted(d.metadata["Name"] + "==" + d.version for d in distributions())))
PYDEPS
git archive "$BASE" src | tar -x -C "$OUTPUT/base"
if PYTHONPATH="$OUTPUT/base/src:$REPO" "$PYTHON" -m pytest tests/harness/test_boundaries.py tests/harness/test_workspace_binding.py -q \
    -k 'successful_batch or expired_pending or expired_mid or resume_binds_actual_workspace' --basetemp="$OUTPUT/before-temp" \
    --junitxml="$OUTPUT/before-fix.xml" > "$OUTPUT/before-fix.txt" 2>&1; then
    echo 'ERROR: original code unexpectedly passed the failure reproductions'; exit 1
else
    STATUS=$?
    test "$STATUS" -eq 1
fi
"$PYTHON" - "$OUTPUT/before-fix.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
assert len(root.findall('.//testcase/failure')) == 5
assert len(root.findall('.//testcase/error')) == 0
print('Verified: original commit fails exactly five boundary reproductions; absolute-cwd control passes.')
PY
"$PYTHON" -m pytest tests/harness tests/agents/test_default.py tests/environments/test_local.py -q \
    --basetemp="$OUTPUT/pytest-temp" --junitxml="$OUTPUT/regression.xml" 2>&1 | tee "$OUTPUT/regression.txt"
"$PYTHON" -m tests.harness.evaluate --output "$OUTPUT/original-seven.json" 2>&1 | tee "$OUTPUT/original-seven.txt"
"$PYTHON" -m tests.harness.coding_campaign --output "$OUTPUT/coding" 2>&1 | tee "$OUTPUT/coding.txt"
"$PYTHON" -m tests.harness.workspace_campaign --output "$OUTPUT/workspace" 2>&1 | tee "$OUTPUT/workspace.txt"
if [[ "${1:-}" == '--full' ]]; then
    "$PYTHON" -m tests.harness.benchmark --repeats 25 --workers 4 --output "$OUTPUT/original-repeated.json" \
        2>&1 | tee "$OUTPUT/original-repeated.txt"
fi
echo "Acceptance passed. Raw output: $OUTPUT"
