#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
while IFS= read -r -d '' file; do
    bash -n "$file"
    shellcheck -x "$file"
done < <(find . -name '*.sh' -not -path './.git/*' -not -path './.venv/*' -print0)
python3 - <<'PY'
import pathlib,py_compile
for root in ('scripts','watcher','claim','tests'):
 for path in pathlib.Path(root).rglob('*.py'): py_compile.compile(str(path),doraise=True)
print('bash -n, shellcheck, and py_compile passed')
PY
