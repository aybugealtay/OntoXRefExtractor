# ================= Ontology files: choose, download and record versions =================
"""
Decides which HPO / UBERON / CL file a run uses, downloads releases when asked,
and records exactly which versions were used.

Per ontology there are four choices:
    (default)                 use the newest release already downloaded;
                              if nothing is downloaded yet, download the latest
    --hpo-release latest      check online for a newer release (download if new)
    --hpo-release 2025-05-06  use / download that specific release
    --hpo ./my/hp.owl         use a local file you provide
(same for UBERON and CL; --latest = check all three online)

Combining options:
    --hpo FILE together with --hpo-release  -> error (contradictory)
    --hpo FILE together with --latest       -> the local file is used for HPO,
                                               --latest applies to the others

Download only, without running anything else:
    python ontology_sources.py --latest

HPO, UBERON and CL publish on separate schedules, so each has its own option.

Downloads are kept in a cache, one folder per release:
    ./data/hpo/releases/2026-09-01/hp.owl
The folder name is always the release date written INSIDE the file, never the
date that was asked for: GitHub tag dates can differ from it (UBERON tag
v2026-06-23 holds release 2026-06-19). When they differ, a warning is printed
and the mapping is remembered in releases/aliases.json, so asking for the tag
date again uses the cache. A file without a release date is stored as
releases/undated-<day>-<checksum>/ and identical files are stored only once.

A release that is already in the cache is not downloaded again. For "latest",
only the first part of the remote file is read to learn its release date; the
full download happens only if that release is not cached yet.

Every run gets its own output folder, <out>/<YYYY-MM-DD_HHMMSS>/, and
<out>/latest always points to the newest successful one. Each run folder holds
the CSVs and run_metadata.json: the settings used and, for each ontology: how
it was chosen, its version IRI / release date, file size and a SHA-256
checksum (a fingerprint that proves which exact file was used). If an ontology
differs from the previous run of the same script, a warning is printed and the
change is recorded in run_metadata.json.

Download addresses, tried in order:
    1. the official OBO PURL   (purl.obolibrary.org)
    2. the GitHub release asset (fallback if the PURL is unreachable)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ONTOLOGIES = {
    "hpo":    {"file": "hp.owl",     "obo_id": "hp",     "repo": "obophenotype/human-phenotype-ontology"},
    "uberon": {"file": "uberon.owl", "obo_id": "uberon", "repo": "obophenotype/uberon"},
    "cl":     {"file": "cl.owl",     "obo_id": "cl",     "repo": "obophenotype/cell-ontology"},
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADER_BYTES = 300_000          # version info sits at the top of the file
USER_AGENT = "hpo-crosslink/1.0 (ontology version check)"
INCOMING = "_incoming"          # downloads land here until their version is known
ALIASES = "aliases.json"        # requested date -> real release date

# Where the version is written in the file header (RDF/XML and Turtle forms)
VERSION_IRI_PATTERNS = [
    re.compile(r'owl:versionIRI\s+rdf:resource="([^"]+)"'),
    re.compile(r'owl:versionIRI\s+<([^>]+)>'),
]
VERSION_INFO_PATTERNS = [
    re.compile(r'owl:versionInfo[^>]*>([^<]+)<'),
    re.compile(r'owl:versionInfo\s+"([^"]+)"'),
]
RELEASE_DATE_IN_IRI = re.compile(r"/releases/(\d{4}-\d{2}-\d{2})/")


# -------------------- READING THE VERSION --------------------
def parse_version(header: str) -> dict:
    """Pull version IRI, version info and release date out of a file header."""
    iri = next((m.group(1) for p in VERSION_IRI_PATTERNS
                if (m := p.search(header))), None)
    info = next((m.group(1).strip() for p in VERSION_INFO_PATTERNS
                 if (m := p.search(header))), None)
    date = None
    if iri and (m := RELEASE_DATE_IN_IRI.search(iri)):
        date = m.group(1)
    elif info and DATE_RE.match(info):
        date = info
    return {"version_iri": iri, "version_info": info, "release_date": date}


def file_version(path: Path) -> dict:
    with open(path, "rb") as f:
        return parse_version(f.read(HEADER_BYTES).decode("utf-8", "replace"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -------------------- DOWNLOADING --------------------
def candidate_urls(name: str, release: str) -> list[str]:
    o = ONTOLOGIES[name]
    if release == "latest":
        return [f"https://purl.obolibrary.org/obo/{o['file']}",
                f"https://github.com/{o['repo']}/releases/latest/download/{o['file']}"]
    return [f"https://purl.obolibrary.org/obo/{o['obo_id']}/releases/{release}/{o['file']}",
            f"https://github.com/{o['repo']}/releases/download/v{release}/{o['file']}"]


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=120)


def peek_remote_version(name: str, release: str) -> tuple[dict, str]:
    """Read only the start of the remote file to learn its version.
    Returns (version dict, url that worked)."""
    errors = []
    for url in candidate_urls(name, release):
        try:
            with _open(url) as r:
                head = r.read(HEADER_BYTES).decode("utf-8", "replace")
            return parse_version(head), url
        except Exception as e:  # try the next address
            errors.append(f"{url}: {e}")
    raise RuntimeError(f"Could not reach any download address for {name} "
                       f"({release}):\n  " + "\n  ".join(errors))


def download(url: str, dest: Path, attempts: int = 3) -> None:
    """Download to a .part file, then rename; retry if the connection drops."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, attempts + 1):
        print(f"  downloading {url}" + (f" (attempt {attempt})" if attempt > 1 else ""))
        try:
            with _open(url) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, length=1 << 20)
            break
        except urllib.error.HTTPError:
            tmp.unlink(missing_ok=True)
            raise  # e.g. 404: retrying will not help
        except Exception as e:
            tmp.unlink(missing_ok=True)
            if attempt == attempts:
                raise
            print(f"  connection problem ({e}), retrying in {5 * attempt}s")
            time.sleep(5 * attempt)
    os.replace(tmp, dest)  # only appears under its final name once complete
    print(f"  downloaded {dest.stat().st_size / 1e6:,.1f} MB")


# -------------------- THE CACHE --------------------
def releases_dir(name: str, cache_dir: str | Path) -> Path:
    return Path(cache_dir) / name / "releases"


def load_aliases(name: str, cache_dir: str | Path) -> dict:
    p = releases_dir(name, cache_dir) / ALIASES
    return json.loads(p.read_text()) if p.exists() else {}


def save_alias(name: str, cache_dir: str | Path, requested: str, actual: str) -> None:
    aliases = load_aliases(name, cache_dir)
    aliases[requested] = actual
    p = releases_dir(name, cache_dir) / ALIASES
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(aliases, indent=2, sort_keys=True))


def store_download(name: str, incoming: Path, cache_dir: str | Path,
                   requested: str) -> tuple[Path, dict]:
    """Move a finished download into the cache under the release date written
    INSIDE the file. Returns (cached path, version dict)."""
    o = ONTOLOGIES[name]
    cache = releases_dir(name, cache_dir)
    v = file_version(incoming)
    date = v["release_date"]

    if date:
        dest = cache / date / o["file"]
    else:
        # No date inside the file: reuse an identical undated copy if there is one
        digest = sha256(incoming)
        for folder in sorted(cache.glob("undated-*")):
            f = folder / o["file"]
            if f.exists() and sha256(f) == digest:
                incoming.unlink()
                print(f"  WARNING: {name} file has no release date; identical to "
                      f"the cached copy in {folder.name}, keeping that one")
                return f, v
        dest = cache / f"undated-{dt.date.today().isoformat()}-{digest[:8]}" / o["file"]
        print(f"  WARNING: {name} file has no release date inside; "
              f"stored as {dest.parent.name}")

    if dest.exists():
        incoming.unlink()
        print(f"  {name}: release {date} was already in the cache, keeping the cached copy")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(incoming, dest)
        print(f"  saved {dest}")

    if requested != "latest" and date and date != requested:
        print(f"  WARNING: asked for {name} release {requested}, but the file is "
              f"release {date} (GitHub tag dates can differ from the release date).\n"
              f"  Stored under {date}; asking for {requested} again will use it.")
        save_alias(name, cache_dir, requested, date)
    return dest, v


def newest_downloaded(name: str, cache_dir: str | Path) -> str | None:
    """Folder name of the newest release in the cache, or None.
    Dated releases are preferred; undated ones are used only if there is no
    dated release, newest download first."""
    d = releases_dir(name, cache_dir)
    if not d.is_dir():
        return None
    f = ONTOLOGIES[name]["file"]
    dated = sorted(p.name for p in d.iterdir()
                   if p.is_dir() and DATE_RE.match(p.name) and (p / f).exists())
    if dated:
        return dated[-1]
    undated = sorted((p for p in d.glob("undated-*") if (p / f).exists()),
                     key=lambda p: (p / f).stat().st_mtime)
    return undated[-1].name if undated else None


# -------------------- CHOOSING THE FILE --------------------
def resolve(name: str, local_path: str | None, release: str | None,
            cache_dir: str | Path) -> tuple[Path, dict]:
    """Return (path to use, provenance record) for one ontology."""
    o = ONTOLOGIES[name]
    if local_path and release:
        raise ValueError(f"{name}: give either a local file or a release, not both")

    # 1. A local file given explicitly with --hpo / --uberon / --cl
    if local_path:
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"{name}: local file not found: {path}")
        print(f"{name}: using local file {path}")
        return path, {"source": "local file", "selected_by": f"--{name}",
                      "download_url": None}

    # 2. Default: newest release already downloaded, else fetch the latest
    if not release:
        newest = newest_downloaded(name, cache_dir)
        if newest:
            path = releases_dir(name, cache_dir) / newest / o["file"]
            print(f"{name}: no release given, using the newest downloaded release "
                  f"{newest} (pin it with --{name}-release {newest} for stable results)")
            return path, {"source": f"newest downloaded release {newest}",
                          "selected_by": "default (newest downloaded)",
                          "download_url": None}
        print(f"{name}: no release downloaded yet, fetching the latest")
        rec = _resolve_latest(name, cache_dir)
        rec[1]["selected_by"] = "default (nothing downloaded yet)"
        return rec

    if release == "latest":
        rec = _resolve_latest(name, cache_dir)
        rec[1]["selected_by"] = f"--{name}-release latest / --latest"
        return rec

    if not DATE_RE.match(release):
        raise ValueError(f"{name}: release must be 'latest' or YYYY-MM-DD, got {release!r}")

    # 3. A specific release: use the cache (also via a remembered alias), else download
    real = load_aliases(name, cache_dir).get(release, release)
    path = releases_dir(name, cache_dir) / real / o["file"]
    if path.exists():
        note = f" (= release {real})" if real != release else ""
        print(f"{name}: release {release}{note} found in cache")
        return path, {"source": f"release {release}{note} (cached)",
                      "selected_by": f"--{name}-release {release}", "download_url": None}
    incoming = releases_dir(name, cache_dir) / INCOMING / o["file"]
    errors = []
    for url in candidate_urls(name, release):
        try:
            download(url, incoming)
        except Exception as e:
            errors.append(f"{url}: {e}")
            continue
        path, v = store_download(name, incoming, cache_dir, requested=release)
        got = v["release_date"]
        note = f" (file says {got})" if got and got != release else ""
        return path, {"source": f"release {release}{note}",
                      "selected_by": f"--{name}-release {release}", "download_url": url}
    raise RuntimeError(
        f"{name}: could not download release {release}:\n  " + "\n  ".join(errors) +
        f"\n  Note: GitHub tag dates can differ from release dates; see "
        f"https://github.com/{o['repo']}/releases for the exact tag.")


def _resolve_latest(name: str, cache_dir: str | Path) -> tuple[Path, dict]:
    """Learn the latest release date first; download only if not cached yet."""
    o = ONTOLOGIES[name]
    version, url = peek_remote_version(name, "latest")
    date = version["release_date"]
    print(f"{name}: latest release is {date or 'undated'}")
    if date:
        path = releases_dir(name, cache_dir) / date / o["file"]
        if path.exists():
            print(f"{name}: {date} already in cache, no download needed")
            return path, {"source": f"latest = {date} (cached)", "download_url": None}
    incoming = releases_dir(name, cache_dir) / INCOMING / o["file"]
    download(url, incoming)
    path, v = store_download(name, incoming, cache_dir, requested="latest")
    got = v["release_date"]
    if date and got != date:  # a new release appeared between check and download
        print(f"  note: {name} changed during the download; using release {got}")
    return path, {"source": f"latest = {got or 'undated'}", "download_url": url}


# -------------------- COMMAND-LINE OPTIONS --------------------
def add_ontology_args(ap) -> None:
    """Add the file and release options to a script's argument parser."""
    g = ap.add_argument_group("ontology files")
    g.add_argument("--hpo", help="use this local HPO file instead of a downloaded release")
    g.add_argument("--uberon", help="use this local UBERON file")
    g.add_argument("--cl", help="use this local CL file")
    g.add_argument("--hpo-release", metavar="latest|YYYY-MM-DD",
                   help="use/download this HPO release (default: newest downloaded)")
    g.add_argument("--uberon-release", metavar="latest|YYYY-MM-DD")
    g.add_argument("--cl-release", metavar="latest|YYYY-MM-DD")
    g.add_argument("--latest", action="store_true",
                   help="check online for the latest release of all three "
                        "(a local file given with --hpo/--uberon/--cl still wins)")
    g.add_argument("--cache-dir", default="./data",
                   help="downloads go to <cache-dir>/<ontology>/releases/<date>/")


def resolve_all(args) -> dict[str, tuple[Path, dict]]:
    """Choose the file for every ontology. Contradictory options stop the run."""
    out = {}
    for name in ONTOLOGIES:
        local = getattr(args, name)
        release = getattr(args, f"{name}_release")
        if local and release:
            raise SystemExit(f"Error: --{name} {local} and --{name}-release {release} "
                             f"contradict each other; use only one of them.")
        if local and args.latest:
            print(f"{name}: --{name} given, so --latest does not apply to {name}")
        elif not release and args.latest:
            release = "latest"
        out[name] = resolve(name, local, release, args.cache_dir)
    return out


# -------------------- ONE FOLDER PER RUN --------------------
def add_output_args(ap, default_out: str) -> None:
    g = ap.add_argument_group("output")
    g.add_argument("--out", default=default_out,
                   help="base folder; each run goes into <out>/<timestamp>/")
    g.add_argument("--no-timestamp", action="store_true",
                   help="write straight into --out (old behaviour, overwrites)")


def make_run_dir(base: str | Path, timestamped: bool = True) -> Path:
    """Create <base>/<YYYY-MM-DD_HHMMSS>/ for this run."""
    base = Path(base)
    if not timestamped:
        base.mkdir(parents=True, exist_ok=True)
        return base
    run = base / dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run.mkdir(parents=True, exist_ok=False)
    print(f"Output folder: {run}")
    return run


def _is_run_dir(path: Path) -> bool:
    return bool(DATE_RE.match(path.name[:10]))


def finish_run(run: Path) -> None:
    """Call at the very end of a successful run: point <base>/latest at it.
    A run that crashes never becomes 'latest'."""
    if not _is_run_dir(run):
        return  # --no-timestamp: nothing to point at
    base, latest = run.parent, run.parent / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run.name, target_is_directory=True)
    except OSError:  # e.g. Windows without symlink rights: write a note instead
        (base / "LATEST.txt").write_text(run.name + "\n")
    print(f"Done. {latest} -> {run.name}")


def _previous_run_metadata(run: Path) -> dict | None:
    """run_metadata.json of the previous successful run in the same base folder."""
    if not _is_run_dir(run):
        return None
    base = run.parent
    prev = base / "latest"
    if not prev.exists() and (base / "LATEST.txt").exists():
        prev = base / (base / "LATEST.txt").read_text().strip()
    f = prev / "run_metadata.json"
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except (OSError, ValueError):
        return None


# -------------------- RECORDING WHAT WAS USED --------------------
def _package_version(name: str) -> str | None:
    try:
        return __import__(name).__version__
    except Exception:
        return None


def write_run_metadata(out_dir: str | Path, resolved: dict, script: str,
                       settings: dict | None = None, args=None,
                       filename: str = "run_metadata.json") -> Path:
    """Write the run's metadata: the method settings that shape the results,
    every command-line argument, software versions and the ontologies used.
    Warns if an ontology differs from the previous run of the same script."""

    entries = {}
    print("Ontology versions used:")
    for name, (path, rec) in resolved.items():
        if path is None:
            entries[name] = None
            print(f"  {name:7s} not used")
            continue
        v = file_version(path)
        entries[name] = {
            **rec,
            "path": str(path),
            "release_date": v["release_date"],
            "version_iri": v["version_iri"],
            "version_info": v["version_info"],
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "file_modified": dt.datetime.fromtimestamp(
                path.stat().st_mtime, dt.timezone.utc).isoformat(timespec="seconds"),
        }
        if not v["version_iri"] and not v["version_info"]:
            print(f"  WARNING: no version found in the header of {path}")
        print(f"  {name:7s} {v['release_date'] or v['version_info'] or 'unknown':12s} "
              f"{rec['source']}")

    # Compare with the previous run of this script (same base folder)
    changes = {}
    prev = _previous_run_metadata(Path(out_dir))
    if prev:
        for name, e in entries.items():
            p = (prev.get("ontologies") or {}).get(name)
            if e and p and p.get("sha256") != e["sha256"]:
                changes[name] = {"previous": p.get("release_date") or p.get("path"),
                                 "now": e["release_date"] or e["path"],
                                 "previous_run": prev.get("created")}
                print(f"  WARNING: {name} differs from the previous run "
                      f"({changes[name]['previous']} -> {changes[name]['now']}); "
                      f"results may change for that reason")

    if settings:
        print("Settings:")
        for k, v in settings.items():
            print(f"  {k}: {v}")

    meta = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "script": script,
        "settings": settings or {},          # options that change the results
        "command": " ".join(sys.argv),
        "arguments": ({k: (str(v) if isinstance(v, Path) else v)
                       for k, v in vars(args).items()} if args is not None else None),
        "software": {"python": platform.python_version(),
                     "rdflib": _package_version("rdflib"),
                     "pandas": _package_version("pandas")},
        "ontologies": entries,
        "changes_since_previous_run": changes if prev else None,
    }
    out = Path(out_dir) / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, indent=2))
    print(f"  -> {out}")
    return out


# -------------------- DOWNLOAD ONLY --------------------
# python ontology_sources.py --latest   -> download the latest HPO, UBERON, CL
# Each call writes its own log: <cache-dir>/download_log/<YYYY-MM-DD_HHMMSS>.json
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Download ontology releases and show their versions")
    add_ontology_args(ap)
    args = ap.parse_args()
    files = resolve_all(args)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    write_run_metadata(Path(args.cache_dir) / "download_log", files,
                       script=Path(__file__).name, args=args, filename=f"{stamp}.json")