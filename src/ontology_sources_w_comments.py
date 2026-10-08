# ##########################################################################
# PLAIN-LANGUAGE GUIDE (comments added for explanation; code is unchanged)
# ##########################################################################
#
# WHAT THIS FILE DOES, IN ONE SENTENCE
#   It decides which copy of HPO, UBERON and CL a run uses, downloads new
#   releases when asked, and writes down exactly what was used.
#
# WHY IT EXISTS
#   HPO, UBERON and CL are updated every few weeks. Results built from the
#   March HPO can differ from results built from the September HPO. To be
#   able to say later "this table was built from HPO 2026-09-01", every run
#   must record the versions it used. This file takes care of that.
#
# IT IS USED IN TWO WAYS
#   1. On its own, just to download:
#        python src/ontology_sources.py --latest
#   2. By the extractor and propagation scripts, which call its functions
#      at the start (pick files, make run folder, write metadata) and at the
#      end (point "latest" at the finished run).
#
# WHERE THINGS END UP
#   data/hpo/releases/2026-09-01/hp.owl          downloaded releases,
#   data/uberon/releases/2026-10-01/uberon.owl   one folder per release date
#   data/cl/releases/2026-06-08/cl.owl
#   data/outputs/<script>/<date_time>/           one folder per run, with
#       run_metadata.json                        the record of what was used
#   data/outputs/<script>/latest                 shortcut to the newest run
# ##########################################################################

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

Download only, without running anything else:
    python ontology_sources.py --latest

HPO, UBERON and CL publish on separate schedules, so each has its own option.

Downloads are kept in a cache, one folder per release:
    ./data/hpo/releases/2026-09-01/hp.owl
A release that is already in the cache is not downloaded again. For "latest",
only the first part of the remote file is read to learn its release date; the
full download happens only if that release is not cached yet.

Every run gets its own output folder, <out>/<YYYY-MM-DD_HHMMSS>/, and
<out>/latest always points to the newest one. Each run folder holds the CSVs
and run_metadata.json: the settings used and, for each
ontology: where it came from, its version IRI / release date, file size and a
SHA-256 checksum (a fingerprint that proves which exact file was used).

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

# The three ontologies this project uses. For each: the file name, its short
# OBO name (used in the official web address) and its GitHub repository
# (used as a backup download address).
ONTOLOGIES = {
    "hpo":    {"file": "hp.owl",     "obo_id": "hp",     "repo": "obophenotype/human-phenotype-ontology"},
    "uberon": {"file": "uberon.owl", "obo_id": "uberon", "repo": "obophenotype/uberon"},
    "cl":     {"file": "cl.owl",     "obo_id": "cl",     "repo": "obophenotype/cell-ontology"},
}

# A release is named by its date, e.g. 2026-09-01. This pattern checks
# that a text looks like such a date.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Only the first ~300 KB of a file are read to find its version, because the
# version is written at the very top. This avoids reading a 300 MB file just
# to learn its date.
HEADER_BYTES = 300_000          # version info sits at the top of the file
USER_AGENT = "hpo-crosslink/1.0 (ontology version check)"

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
# Every ontology file starts with a short "about this file" section. It
# contains a versionIRI, a web address that includes the release date, e.g.
#   http://purl.obolibrary.org/obo/hp/releases/2026-09-01/hp.owl
# and sometimes a versionInfo (often just the date). This function finds
# those and extracts the release date from them.
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


# Same, for a file already on disk: read its first part, find the version.
def file_version(path: Path) -> dict:
    with open(path, "rb") as f:
        return parse_version(f.read(HEADER_BYTES).decode("utf-8", "replace"))


# Computes a "fingerprint" of the whole file. Two files with the same
# fingerprint are byte-for-byte identical; change one character and the
# fingerprint changes completely. Recording it proves exactly which file
# was used, even if it was renamed or the version text was missing.
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -------------------- DOWNLOADING --------------------
# The web addresses to try, in order:
#   1. the official OBO address (purl.obolibrary.org), which redirects to
#      the current download location
#   2. the GitHub release page of the ontology, as a backup
# "latest" and a dated release (e.g. 2026-09-01) have different addresses.
def candidate_urls(name: str, release: str) -> list[str]:
    o = ONTOLOGIES[name]
    if release == "latest":
        return [f"https://purl.obolibrary.org/obo/{o['file']}",
                f"https://github.com/{o['repo']}/releases/latest/download/{o['file']}"]
    return [f"https://purl.obolibrary.org/obo/{o['obo_id']}/releases/{release}/{o['file']}",
            f"https://github.com/{o['repo']}/releases/download/v{release}/{o['file']}"]


# Opens a web address. It identifies itself politely (User-Agent) and gives
# up after 120 seconds without a response.
def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=120)


# Asks "what is the newest release?" WITHOUT downloading the whole file:
# it reads only the top of the online file, finds the release date, and
# stops. If the first address fails, it tries the next one. If all fail,
# it stops with an error listing what went wrong for each address
# (this is the error you saw when Python could not reach the internet).
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


# Downloads a whole file safely:
#   - it writes to "hp.owl.part" first and renames it to "hp.owl" only
#     when complete, so a half-finished download is never mistaken for a
#     real file;
#   - if the connection drops, it waits a few seconds and tries again
#     (up to 3 times);
#   - if the server says the file does not exist (e.g. a wrong date gives
#     "404 Not Found"), it stops at once, since retrying would not help.
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
    print(f"  saved {dest} ({dest.stat().st_size / 1e6:,.1f} MB)")


# -------------------- CHOOSING THE FILE --------------------
# Looks in data/<ontology>/releases/ and returns the newest date folder
# that really contains the file. Dates sort correctly as text
# (2026-10-01 comes after 2026-09-01), so the last one is the newest.
def newest_downloaded(name: str, cache_dir: str | Path) -> str | None:
    """Release date of the newest release already in the cache, or None."""
    d = Path(cache_dir) / name / "releases"
    if not d.is_dir():
        return None
    dates = sorted(p.name for p in d.iterdir()
                   if DATE_RE.match(p.name) and (p / ONTOLOGIES[name]["file"]).exists())
    return dates[-1] if dates else None


# THE CORE OF THIS FILE: decides which file to use for ONE ontology.
# It goes through four cases, in this order:
#   1. you named a local file (--hpo path/to/hp.owl)       -> use that file
#   2. you asked for nothing special (the normal case)     -> use the newest
#      downloaded release; if none is downloaded yet, treat it as "latest"
#   3. you asked for a date (--hpo-release 2026-09-01)     -> use it from the
#      cache, or download it if it is not there
#   4. you asked for "latest" (--latest / --hpo-release latest)
#                                                          -> check online
#      which date the newest release has; download only if that date is
#      not already in the cache
# It returns the file to use plus a short note on where it came from
# ("source"), which ends up in run_metadata.json.
def resolve(name: str, local_path: str | None, release: str | None,
            cache_dir: str | Path) -> tuple[Path | None, dict | None]:
    """Return (path to use, provenance record) for one ontology."""
    o = ONTOLOGIES[name]

    # 1. A local file given explicitly with --hpo / --uberon / --cl
    if local_path and not release:
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"{name}: local file not found: {path}")
        return path, {"source": "local file", "download_url": None}

    # 2. Default: newest release already downloaded, else fetch the latest
    if not release:
        newest = newest_downloaded(name, cache_dir)
        if newest:
            path = Path(cache_dir) / name / "releases" / newest / o["file"]
            print(f"{name}: using newest downloaded release {newest}")
            return path, {"source": f"newest downloaded release {newest}",
                          "download_url": None}
        print(f"{name}: no release downloaded yet, fetching the latest")
        release = "latest"

    # A release must be "latest" or a real-looking date, otherwise stop with
    # a clear message (e.g. a typo like 2026-9-1).
    if release != "latest" and not DATE_RE.match(release):
        raise ValueError(f"{name}: release must be 'latest' or YYYY-MM-DD, got {release!r}")

    cache = Path(cache_dir) / name / "releases"

    # 3. A specific release: use the cache if present, else download it
    if release != "latest":
        path = cache / release / o["file"]
        if path.exists():
            print(f"{name}: release {release} found in cache")
            return path, {"source": f"release {release} (cached)", "download_url": None}
        last_err = None
        for url in candidate_urls(name, release):
            try:
                download(url, path)
                return path, {"source": f"release {release}", "download_url": url}
            except Exception as e:
                last_err = e
        raise RuntimeError(f"{name}: could not download release {release}: {last_err}")

    # 4. Latest: learn its date first, download only if not cached yet
    version, url = peek_remote_version(name, "latest")
    date = version["release_date"]
    print(f"{name}: latest release is {date or 'unknown'}")
    if date:
        path = cache / date / o["file"]
        if path.exists():
            print(f"{name}: {date} already in cache, no download needed")
            return path, {"source": f"latest = {date} (cached)", "download_url": None}
    # (Rare) the file did not say which release it is: keep it in a folder
    # named "unknown-<today>" so it is not confused with a dated release.
    else:  # no date in the header: store under today's date
        path = cache / ("unknown-" + dt.date.today().isoformat()) / o["file"]
    download(url, path)
    return path, {"source": f"latest = {date}", "download_url": url}


# -------------------- COMMAND-LINE OPTIONS --------------------
# The command-line options for choosing files, shared by all scripts so
# that they behave the same way:
#   --hpo / --uberon / --cl        use a specific local file
#   --hpo-release etc.             "latest" or a date, per ontology
#                                  (they are released on different days, so
#                                  each has its own option)
#   --latest                       "latest" for all three at once
#   --cache-dir                    base folder for downloads (default ./data)
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
                   help="check online for the latest release of all three")
    g.add_argument("--cache-dir", default="./data",
                   help="downloads go to <cache-dir>/<ontology>/releases/<date>/")


# Runs resolve() for HPO, UBERON and CL, using the options given.
def resolve_all(args) -> dict[str, tuple[Path | None, dict | None]]:
    out = {}
    for name in ONTOLOGIES:
        release = getattr(args, f"{name}_release") or ("latest" if args.latest else None)
        out[name] = resolve(name, getattr(args, name), release, args.cache_dir)
    return out


# -------------------- ONE FOLDER PER RUN --------------------
# The command-line options for where results go:
#   --out            the base folder for this script's results
#   --no-timestamp   write directly into --out, overwriting (old behaviour)
def add_output_args(ap, default_out: str) -> None:
    g = ap.add_argument_group("output")
    g.add_argument("--out", default=default_out,
                   help="base folder; each run goes into <out>/<timestamp>/")
    g.add_argument("--no-timestamp", action="store_true",
                   help="write straight into --out (old behaviour, overwrites)")


# Creates a new folder for this run, named by date and time, e.g.
#   data/outputs/ancestor_propagation/2026-10-05_171742/
# so a new run never overwrites an earlier one.
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


# Called as the LAST step of a successful run. It points the shortcut
#   data/outputs/<script>/latest
# at this run's folder. Because this happens only at the very end, a run
# that crashed halfway keeps its own (incomplete) folder but never becomes
# "latest". Other code can always read from ".../latest/" and get the most
# recent complete results. (On Windows, where such shortcuts may not be
# allowed, it writes the folder name into LATEST.txt instead.)
def finish_run(run: Path) -> None:
    """Call at the very end of a successful run: point <base>/latest at it.
    A run that crashes never becomes 'latest'."""
    if not DATE_RE.match(run.name[:10]):
        return  # --no-timestamp: nothing to point at
    base, latest = run.parent, run.parent / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run.name, target_is_directory=True)
    except OSError:  # e.g. Windows without symlink rights: write a note instead
        (base / "LATEST.txt").write_text(run.name + "\n")
    print(f"Done. {latest} -> {run.name}")


# -------------------- RECORDING WHAT WAS USED --------------------
# Looks up the installed version of a Python package (e.g. rdflib), so it
# can be recorded. Different library versions can, rarely, give different
# results.
def _package_version(name: str) -> str | None:
    try:
        return __import__(name).__version__
    except Exception:
        return None


# Writes run_metadata.json into the run folder. It contains:
#   created     when the run started (UTC)
#   script      which script produced the results
#   settings    the options that change the RESULTS (e.g. use_adjectives)
#   command     the exact command that was typed
#   arguments   every option, including defaults that were not typed
#   software    Python, rdflib and pandas versions
#   ontologies  for HPO, UBERON and CL: where the file came from, its path,
#               release date, version address, size and fingerprint (sha256)
# It also prints a short summary to the screen, and warns if a file does
# not state its version.
def write_run_metadata(out_dir: str | Path, resolved: dict, script: str,
                       settings: dict | None = None, args=None) -> Path:
    """Write run_metadata.json: the method settings that shape the results,
    every command-line argument, software versions and the ontologies used."""

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
    }
    out = Path(out_dir) / "run_metadata.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, indent=2))
    print(f"  -> {out}")
    return out


# -------------------- DOWNLOAD ONLY --------------------
# python ontology_sources.py --latest   -> download the latest HPO, UBERON, CL
# Running this file directly only downloads/selects the files and records
# them in data/run_metadata.json; nothing else is computed.
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Download ontology releases and show their versions")
    add_ontology_args(ap)
    args = ap.parse_args()
    files = resolve_all(args)
    write_run_metadata(args.cache_dir, files, script=Path(__file__).name, args=args)