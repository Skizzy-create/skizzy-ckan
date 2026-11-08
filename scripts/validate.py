#!/usr/bin/env python3
"""Validate the .ckan files in this repository.

  python scripts/validate.py                                   # all files, vendored schema
  python scripts/validate.py --schema https://raw.githubusercontent.com/KSP-CKAN/CKAN/master/CKAN.schema
  python scripts/validate.py --check-downloads [FILE ...]      # also download and hash

Checks: JSON syntax, CKAN.schema, <identifier>/<identifier>-<version>.ckan naming, unique
identifier+version pairs, spec_version vs features used, download/hash formats, and that every
relationship (and replaced_by) to one of our own identifiers (SkizzyNaval*, BDArmory-Skizzy,
BDArmory-Overdrive, SubTrim) resolves to a file in this repository. With --check-downloads,
each download is fetched and its size, SHA-1 and SHA-256 compared with the metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWN = re.compile(r"^(SkizzyNaval.*|BDArmory-Skizzy|BDArmory-Overdrive|SubTrim)$")
REL_KEYS = ("depends", "recommends", "suggests", "supports", "conflicts")
HEX40 = re.compile(r"^[0-9A-F]{40}$")
HEX64 = re.compile(r"^[0-9A-F]{64}$")
UA = "skizzy-ckan-validator/1.0 (+https://github.com/Skizzy-create/skizzy-ckan)"


def spec_tuple(spec) -> tuple[int, int]:
    if spec in (1, "1"):
        return (1, 0)
    m = re.match(r"^v(\d+)\.(\d+)$", str(spec))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def walk_rels(rels):
    for rel in rels or []:
        yield rel
        if "any_of" in rel:
            yield from walk_rels(rel["any_of"])


def load_schema(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def fetch_and_hash(url: str, attempts: int = 5) -> tuple[int, str, str]:
    last = None
    for i in range(attempts):
        try:
            sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as resp:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    size += len(chunk)
                    sha1.update(chunk)
                    sha256.update(chunk)
            return size, sha1.hexdigest().upper(), sha256.hexdigest().upper()
        except Exception as exc:  # noqa: BLE001 - retry any network error
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"download failed: {last}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help=".ckan files (default: every .ckan in the repo)")
    ap.add_argument("--schema", default=str(ROOT / "schema" / "CKAN.schema"))
    ap.add_argument("--check-downloads", action="store_true")
    args = ap.parse_args(argv)

    try:
        import jsonschema
    except ImportError:
        print("error: pip install -r scripts/requirements.txt", file=sys.stderr)
        return 2

    all_files = sorted(p for p in ROOT.rglob("*.ckan") if ".git" not in p.parts)
    targets = [Path(f).resolve() for f in args.files] if args.files else all_files
    validator = jsonschema.Draft4Validator(load_schema(args.schema))
    problems: list[str] = []

    docs: dict[Path, dict] = {}
    for path in all_files:
        try:
            docs[path] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
    known = {d.get("identifier") for d in docs.values()}
    seen: dict[tuple, Path] = {}

    for path in targets:
        if path not in docs:
            if path.exists() and path.suffix == ".ckan" and path not in all_files:
                problems.append(f"{path}: not inside this repository")
            continue
        doc, rel = docs[path], path.relative_to(ROOT).as_posix()
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            problems.append(f"{rel}: schema: {loc}: {err.message}")
        ident, version = doc.get("identifier", "?"), str(doc.get("version", "?"))
        expected = f"{ident}/{ident}-{version.replace(':', '-')}.ckan"
        if rel != expected:
            problems.append(f"{rel}: should be at {expected}")
        key = (ident, version)
        if key in seen:
            problems.append(f"{rel}: duplicate of {seen[key].relative_to(ROOT)}")
        seen[key] = path
        spec = spec_tuple(doc.get("spec_version"))
        rels = [r for k in REL_KEYS for r in walk_rels(doc.get(k))]
        if any("any_of" in r for r in rels) and spec < (1, 26):
            problems.append(f"{rel}: any_of needs spec_version v1.26+")
        if any("as" in d for d in doc.get("install", [])) and spec < (1, 18):
            problems.append(f"{rel}: install 'as' needs spec_version v1.18+")
        if "replaced_by" in doc:
            if spec < (1, 26):
                problems.append(f"{rel}: replaced_by needs spec_version v1.26+")
            rels = rels + [doc["replaced_by"]]
        for r in rels:
            name = r.get("name")
            if name and OWN.match(name) and name not in known:
                problems.append(f"{rel}: relationship to {name}, which has no .ckan here")
        if doc.get("kind") == "metapackage":
            if "download" in doc or "install" in doc:
                problems.append(f"{rel}: metapackages must not have download or install")
            continue
        h = doc.get("download_hash", {})
        if not HEX40.match(h.get("sha1", "")) or not HEX64.match(h.get("sha256", "")):
            problems.append(f"{rel}: download_hash needs upper-case sha1 and sha256")
        if not isinstance(doc.get("download_size"), int) or doc["download_size"] <= 0:
            problems.append(f"{rel}: download_size must be a positive integer")
        url = doc.get("download")
        url = url[0] if isinstance(url, list) else url
        if not str(url).startswith("https://"):
            problems.append(f"{rel}: download must be an https URL")
        elif args.check_downloads:
            try:
                got = fetch_and_hash(url)
            except RuntimeError as exc:
                problems.append(f"{rel}: {url}: {exc}")
                continue
            want = (doc.get("download_size"), h.get("sha1"), h.get("sha256"))
            if got != want:
                problems.append(f"{rel}: download is size={got[0]} sha1={got[1]} sha256={got[2]}, "
                                f"metadata says size={want[0]} sha1={want[1]} sha256={want[2]}")
            else:
                print(f"ok  {rel}: {got[0]:,} bytes, sha256 {got[2]}")

    for p in problems:
        print(f"problem: {p}")
    print(f"checked {len(targets)} of {len(all_files)} .ckan files against {args.schema}: "
          f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
