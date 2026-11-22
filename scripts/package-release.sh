#!/usr/bin/env bash
set -euo pipefail

output_dir=${1:?Specify the release asset directory}
mkdir -p "$output_dir"
output_dir=$(cd -P -- "$output_dir" && pwd)
cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

npm --prefix frontend ci --ignore-scripts
npm --prefix frontend run build

# Keep the existing Cue install layout and only include files needed at runtime.
files=(
  app.py
  cue_tui.py
  assets/demo-flow.svg
  frontend/dist/index.html
  frontend/dist/assets/app.js
  frontend/dist/assets/app.css
  herdr-plugin/cue_plugin.py
  herdr-plugin/herdr-plugin.toml
)
test -x app.py
asset=decision-inbox.tar.gz
# Fixed metadata lets a retry compare the published asset against the same source.
python3 - "$output_dir/$asset" "${files[@]}" <<'PYTHON'
import gzip
from pathlib import Path
import sys
import tarfile

with open(sys.argv[1], "wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
    with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sys.argv[2:]:
            source = Path(name)
            if not source.is_file() or any(path.is_symlink() for path in (source, *source.parents)):
                raise SystemExit(f"Not a regular release file: {name}")
            info = tarfile.TarInfo(name)
            info.size = source.stat().st_size
            info.mode = 0o755 if name == "app.py" else 0o644
            with source.open("rb") as file:
                archive.addfile(info, file)
PYTHON
tar -tzf "$output_dir/$asset" | diff - <(printf '%s\n' "${files[@]}")
cd "$output_dir"
shasum -a 256 "$asset" >"$asset.sha256"
shasum -a 256 -c "$asset.sha256"
