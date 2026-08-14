#!/usr/bin/env python3
"""
Fetch, checksum-verify, and cache the dev-toolkit jar this plugin depends on.

    python3 scripts/tools/fetch_jar.py [--release-file R] [--cache-dir D]

Reads ``{version, download_url, sha256}`` from ``toolkit-release.json``
(next to this script unless ``--release-file`` overrides it). If
``<cache-dir>/dev-toolkit-<version>.jar`` already exists and matches the
pinned sha256, prints its absolute path and exits 0 -- no network call. Cache
dir defaults to ``$CLAUDE_PLUGIN_DATA``, falling back to
``~/.claude/plugins/data/play-to-springboot`` when that variable is unset
(running outside a plugin-loaded session, e.g. local testing).

The jar's provenance used to be unverifiable -- a filename that was never
bumped, no checksum, no build link back to source. This script is what
replaces that: the jar this prints is always exactly the bytes the pinned
sha256 describes, or the script fails loudly instead of handing back
something unverified. Nothing downstream (``agents/dev.md``, ``gate.py``)
should ever resolve a dev-toolkit jar path any other way.

Exit codes: 0 OK (path printed to stdout), 1 error (message on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_RELEASE_FILE = Path(__file__).resolve().parent / "toolkit-release.json"
DEFAULT_CACHE_DIR = Path.home() / ".claude" / "plugins" / "data" / "play-to-springboot"
CHUNK_SIZE = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_release(release_file: Path) -> dict[str, str]:
    if not release_file.is_file():
        raise SystemExit(f"ERROR: no release pin at {release_file}")
    data = json.loads(release_file.read_text(encoding="utf-8"))
    missing = [k for k in ("version", "download_url", "sha256") if not data.get(k)]
    if missing:
        raise SystemExit(
            f"ERROR: {release_file} is missing required field(s): {', '.join(missing)}"
        )
    return data


def download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"ERROR: failed to download dev-toolkit jar from {url}: {e}")
    tmp.replace(dest)


def fetch(release_file: Path, cache_dir: Path) -> Path:
    release = load_release(release_file)
    version = release["version"]
    expected_sha256 = release["sha256"].lower()
    jar_path = cache_dir / f"dev-toolkit-{version}.jar"

    if jar_path.is_file():
        actual = sha256_of(jar_path)
        if actual == expected_sha256:
            return jar_path
        print(
            f"[warn] cached {jar_path} does not match pinned sha256 "
            f"(expected {expected_sha256}, got {actual}); re-downloading.",
            file=sys.stderr,
        )
        jar_path.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading dev-toolkit {version} from {release['download_url']}...", file=sys.stderr)
    download(release["download_url"], jar_path)

    actual = sha256_of(jar_path)
    if actual != expected_sha256:
        jar_path.unlink()
        raise SystemExit(
            f"ERROR: downloaded dev-toolkit-{version}.jar sha256 mismatch "
            f"(expected {expected_sha256}, got {actual}). Refusing to use an "
            f"unverified jar; deleted the download."
        )

    return jar_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-file", type=Path, default=DEFAULT_RELEASE_FILE,
        help="Path to toolkit-release.json (default: next to this script).",
    )
    parser.add_argument(
        "--cache-dir", type=Path,
        default=Path(os.environ.get("CLAUDE_PLUGIN_DATA", str(DEFAULT_CACHE_DIR))),
        help="Where the versioned jar is cached (default: $CLAUDE_PLUGIN_DATA).",
    )
    args = parser.parse_args()

    jar_path = fetch(args.release_file.expanduser().resolve(), args.cache_dir.expanduser().resolve())
    print(str(jar_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
