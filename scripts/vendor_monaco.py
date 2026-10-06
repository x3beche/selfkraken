#!/usr/bin/env python3.12
"""Vendor the Monaco editor (VS Code's editor) into the GUI package, offline-safe.

Why a script and a commit instead of a download at runtime: the desktop app must
never touch the network.  The editor is fetched **once, here, by the maintainer**,
verified against npm's published integrity hash, and the result is committed under
``selfkraken/gui/monaco/`` so the wheel works offline and is byte-for-byte
auditable.

What lands in the package:

* ``package/min/vs/**``   -> ``selfkraken/gui/monaco/vs/**``
* ``package/LICENSE`` and ``package/ThirdPartyNotices.txt`` -> next to it, because
  Monaco is MIT and its own third-party notices must travel with it
* a ``VERSION`` marker carrying the pinned version, the integrity string and every
  pruning decision, so ``--check`` can prove what is on disk

What is deliberately **not** written (size, and none of it is reachable at runtime):

* every ``*.map`` — source maps are tens of MB the editor never asks for
* ``vs/language/**`` — the pre-0.5x AMD worker/contribution duplicates; 0.57's
  ``vs/editor/editor.main.js`` imports the hashed root wrappers instead
  (``../ts.worker-BLYAhMkX``), so the legacy tree is dead weight
* ``vs/nls/lang/**`` — localisation bundles; the app speaks English and never sets
  a Monaco locale, so no code path fetches them
* basic-language chunks for the languages :data:`KEEP_LANGUAGES` does not list —
  which language id maps to which hashed chunk is read out of
  ``vs/basic-languages/monaco.contribution.js`` itself, not guessed

Usage::

    python3.12 scripts/vendor_monaco.py            # download + extract + prune
    python3.12 scripts/vendor_monaco.py --check    # verify what is on disk
    python3.12 scripts/vendor_monaco.py --offline monaco-editor-0.57.0.tgz

Exit code 0 on success; non-zero with a plain-text reason on failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import socket
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# the pin
# --------------------------------------------------------------------------- #
MONACO_VERSION = "0.57.0"
TARBALL_URL = (
    f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{MONACO_VERSION}.tgz"
)
#: npm's ``dist.integrity`` for this exact tarball (base64 of the sha512 digest).
INTEGRITY_SRI = (
    "sha512-5BkI9KGoqrNvBGUe15/QlZq3OooZ8WLg1AxTpaqHRCP3HNpzPPZKE2EDz8M7c+"
    "VRmCeUw1Brp4cx/PWm3kI/5A=="
)

#: where the vendored editor lives, relative to the repo root
VENDOR_ROOT = Path("selfkraken") / "gui" / "monaco"

#: the two license files the tarball carries at ``package/`` level
LICENSE_FILES = ("LICENSE", "ThirdPartyNotices.txt")

_MIN_PREFIX = "package/min/vs/"
_ROOT_PREFIX = "package/"

#: language ids the editor must be able to highlight.  Repo code, patches, JSON,
#: YAML/TOML-ish config, markdown, shell, SQL and the usual compiled languages.
KEEP_LANGUAGES: tuple[str, ...] = (
    "python", "javascript", "typescript", "json", "html", "css", "markdown",
    "yaml", "shell", "sql", "ini", "xml", "go", "rust", "java", "c", "cpp",
    "csharp", "ruby", "php", "dockerfile",
)

#: root directories under ``vs/`` that are dropped entirely (see module docstring)
PRUNE_VS_DIRS: tuple[str, ...] = ("language", "nls/lang")

#: files whose presence makes the vendored tree usable (``--check``).  Patterns,
#: because the hashed names (``ts.worker-BLYAhMkX.js``) change with every release.
REQUIRED_PATTERNS: tuple[str, ...] = (
    "vs/loader.js",
    "vs/editor/editor.main.js",
    "vs/editor/editor.main.css",
    "vs/editor/editor.worker.js",
    "vs/nls.messages-loader.js",
    "vs/basic-languages/monaco.contribution.js",
    "vs/assets/*worker*.js",
    "LICENSE",
    "ThirdPartyNotices.txt",
    "VERSION",
)

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # the tarball is ~30 MB; a hard ceiling

#: ``i({id:"python",…,loader:(e,s)=>a(["../python-CasA8BlK"],e,s)})``
_BASIC_LANG_RE = re.compile(
    r'id:"(?P<lang>[^"]+)"(?P<body>(?:(?!id:").)*?a\(\["\.\./(?P<chunk>[^"]+)"\])',
    re.DOTALL,
)


def _integrity_base64() -> str:
    """The bare base64 sha512 hidden inside the SRI string."""
    algorithm, _, value = INTEGRITY_SRI.partition("-")
    if algorithm != "sha512":  # pragma: no cover - guard against an edited pin
        raise SystemExit(f"unsupported integrity algorithm {algorithm!r}")
    return value


def sha512_b64(payload: bytes) -> str:
    """Base64 of the sha512 digest of *payload* — npm's ``dist.integrity`` body."""
    return base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")


def download(url: str, timeout: int = 120) -> bytes:
    """Fetch *url* fully into memory with a size ceiling (no streaming to disk)."""
    request = urllib.request.Request(  # noqa: S310 - fixed https pin, no redirect surprises
        url, headers={"User-Agent": "selfkraken-vendor-monaco/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:  # pragma: no cover - absurd server response
        raise SystemExit(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes — refusing")
    return body


def verify(payload: bytes) -> None:
    """Fail loudly unless *payload* hashes to the pinned integrity."""
    expected = _integrity_base64()
    actual = sha512_b64(payload)
    if actual != expected:
        raise SystemExit(
            "integrity check FAILED — the tarball is not the one we pinned\n"
            f"  expected {INTEGRITY_SRI}\n"
            f"  actual   sha512-{actual}\n"
            "Refusing to vendor it. If the pin was updated on purpose, edit "
            "INTEGRITY_SRI in this script."
        )


# --------------------------------------------------------------------------- #
# planning what to write
# --------------------------------------------------------------------------- #
def basic_language_chunks(contribution_text: str) -> dict[str, str]:
    """``language id -> hashed chunk file name`` read out of Monaco's own registry.

    ``vs/basic-languages/monaco.contribution.js`` registers every language with a
    lazy ``require(["../<chunk>"])`` loader.  Parsing that file means the prune
    never has to guess a hashed name, and a future Monaco release cannot make the
    list silently wrong: a language that is missing from the parse keeps its chunk.
    """
    chunks: dict[str, str] = {}
    for match in _BASIC_LANG_RE.finditer(contribution_text):
        lang = match.group("lang").strip()
        chunk = match.group("chunk").strip()
        if lang and chunk:
            chunks[lang] = chunk
    return chunks


def _pruned_vs_path(rel: str) -> bool:
    """True when *rel* (a path under ``vs/``) is in a dropped tree."""
    inside = rel[len("vs/") :] if rel.startswith("vs/") else rel
    return any(
        inside == prefix or inside.startswith(prefix + "/")
        for prefix in PRUNE_VS_DIRS
    )


def plan_files(
    payload: bytes, keep_languages: tuple[str, ...] | None
) -> tuple[dict[str, str], dict[str, int]]:
    """Decide what the tarball contributes: ``{member: target_rel_path}`` + stats.

    ``target_rel_path`` is relative to the vendor root, and ``keep_languages=None``
    keeps every basic language.  Nothing is written here, so the decision can be
    inspected (and unit-tested) without touching a disk.
    """
    keep = set(keep_languages) if keep_languages is not None else None
    wanted: dict[str, str] = {}
    dropped = {"maps": 0, "legacy_language_or_nls": 0, "other_languages": 0,
               "unwanted_top_level": 0}

    # first pass: find the basic-language registry so the chunk map is known
    chunks: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.name == f"{_ROOT_PREFIX}min/vs/basic-languages/monaco.contribution.js":
                handle = archive.extractfile(member)
                if handle is not None:
                    chunks = basic_language_chunks(
                        handle.read().decode("utf-8", "replace")
                    )

    # every chunk the kept languages reference, as ``vs/<name>.js``
    keep_chunks = {f"vs/{chunks[lang]}.js" for lang in (keep or set()) if lang in chunks}
    all_basic_chunks = {f"vs/{name}.js" for name in chunks.values()}

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name
            if name.startswith(_MIN_PREFIX):
                rel = "vs/" + name[len(_MIN_PREFIX) :]
                if rel.endswith(".map"):
                    dropped["maps"] += 1
                    continue
                if _pruned_vs_path(rel):
                    dropped["legacy_language_or_nls"] += 1
                    continue
                if (
                    keep is not None
                    and rel in all_basic_chunks
                    and rel not in keep_chunks
                ):
                    dropped["other_languages"] += 1
                    continue
                wanted[name] = rel
            elif name.startswith(_ROOT_PREFIX):
                plain = name[len(_ROOT_PREFIX) :]
                if plain in LICENSE_FILES:
                    wanted[name] = plain
                else:
                    dropped["unwanted_top_level"] += 1
    stats = dict(dropped)
    stats["chunks_known"] = len(chunks)
    stats["chunks_kept"] = len(keep_chunks) if keep is not None else len(all_basic_chunks)
    return wanted, stats


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def write_files(payload: bytes, destination: Path, wanted: dict[str, str]) -> None:
    """Write the planned members of the npm tarball into *destination*.

    ``filter='data'`` semantics are honoured by hand: only regular files whose
    planned path stays inside the vendor root are written, so a hostile tarball
    cannot put bytes anywhere else.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            rel = wanted.get(member.name)
            if rel is None:
                continue
            handle = archive.extractfile(member)
            if handle is None:  # pragma: no cover - isfile members always open
                continue
            target = (destination / rel).resolve()
            if destination.resolve() not in target.parents:  # pragma: no cover
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())


def write_marker(
    destination: Path, stats: dict[str, int], keep_languages: tuple[str, ...] | None
) -> None:
    """Record what is on disk so ``--check`` and the app can verify the pin."""
    marker = {
        "name": "monaco-editor",
        "version": MONACO_VERSION,
        "integrity": INTEGRITY_SRI,
        "tarball": TARBALL_URL,
        "license": "MIT",
        "vendored_by": "scripts/vendor_monaco.py",
        "pruned": {
            "source_maps": True,
            "vs_dirs": list(PRUNE_VS_DIRS),
            "languages_kept": (
                sorted(keep_languages) if keep_languages is not None else "all"
            ),
            "files": stats,
        },
        "note": (
            "min/vs with *.map, the legacy vs/language/** tree, vs/nls/lang/** "
            "localisation bundles and non-kept basic-language chunks removed; "
            "Monaco is MIT, see LICENSE and ThirdPartyNotices.txt beside this file"
        ),
    }
    (destination / "VERSION").write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )


def read_marker(destination: Path) -> dict:
    try:
        return json.loads((destination / "VERSION").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# checking an existing tree
# --------------------------------------------------------------------------- #
def check(destination: Path) -> list[str]:
    """Problems with an already-vendored tree; empty list means it is usable."""
    problems: list[str] = []
    for pattern in REQUIRED_PATTERNS:
        if "*" in pattern:
            if not list(destination.glob(pattern)):
                problems.append(f"missing {pattern}")
        elif not (destination / pattern).is_file():
            problems.append(f"missing {pattern}")
    if problems:
        return problems

    marker = read_marker(destination)
    if marker.get("integrity") != INTEGRITY_SRI:
        problems.append("VERSION marker does not carry the pinned integrity")
    if marker.get("version") != MONACO_VERSION:
        problems.append(f"VERSION marker says {marker.get('version')!r}")
    for stray, label in (
        (list(destination.rglob("*.map")), "*.map"),
        (list((destination / "vs" / "language").rglob("*.js")), "vs/language/**"),
        (list((destination / "vs" / "nls" / "lang").rglob("*.js")), "vs/nls/lang/**"),
    ):
        if stray:
            problems.append(f"{len(stray)} {label} file(s) should have been pruned")
    if not problems:
        languages = marker.get("pruned", {}).get("languages_kept", [])
        if not languages:
            problems.append("VERSION marker does not record the kept languages")
    return problems


def tree_size(destination: Path) -> tuple[int, int]:
    """``(bytes, file_count)`` of a vendored tree."""
    files = [p for p in destination.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def clean(destination: Path) -> None:
    """Remove a previous vendoring so a re-run cannot leave stale files."""
    if destination.is_dir():
        shutil.rmtree(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the folder this script is in)",
    )
    parser.add_argument(
        "--offline",
        type=Path,
        metavar="TGZ",
        help="use this tarball instead of downloading (still hash-verified)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the vendored tree already on disk and exit",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="do not delete the tree first (a re-run overwrites in place)",
    )
    parser.add_argument(
        "--keep-all-languages",
        action="store_true",
        help="vendor every basic language (bigger tree, for debugging the prune)",
    )
    args = parser.parse_args(argv)

    destination = args.root / VENDOR_ROOT
    if args.check:
        problems = check(destination)
        if problems:
            print("Monaco vendor check FAILED:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        size, count = tree_size(destination)
        print(
            f"Monaco {MONACO_VERSION} vendored and verified at {destination} "
            f"({count} files, {size / 1024 / 1024:.1f} MiB)"
        )
        return 0

    if args.offline is not None:
        payload = Path(args.offline).read_bytes()
        source = str(args.offline)
    else:
        if not socket.getdefaulttimeout():
            socket.setdefaulttimeout(120)
        try:
            payload = download(TARBALL_URL)
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"could not download {TARBALL_URL}: {exc}\n"
                "Re-run with network access, or with --offline FILE when you "
                "have the tarball.",
                file=sys.stderr,
            )
            return 1
        source = TARBALL_URL

    verify(payload)
    print(f"integrity verified: {INTEGRITY_SRI}")

    keep = None if args.keep_all_languages else KEEP_LANGUAGES
    wanted, stats = plan_files(payload, keep)
    baseline_bytes, baseline_files = min_vs_total(payload)
    after_bytes = unpacked_bytes(payload, wanted)
    keeping = ", ".join(sorted(keep)) if keep else "every language (nothing pruned)"
    print(
        f"basic-language registry: {stats['chunks_known']} languages parsed, "
        f"keeping {stats['chunks_kept']} -> {keeping}"
    )
    print(
        f"before prune (min/vs minus *.map): {baseline_files} files, "
        f"{baseline_bytes / 1024 / 1024:.1f} MiB"
    )
    print(f"after prune:  {len(wanted)} files, {after_bytes / 1024 / 1024:.1f} MiB")
    if keep and not stats["chunks_known"]:  # pragma: no cover - format change
        print(
            "WARNING: the basic-language registry did not parse, so no language "
            "chunk was pruned (the tree is simply bigger than intended).",
            file=sys.stderr,
        )

    if not args.keep_existing:
        clean(destination)
    write_files(payload, destination, wanted)
    stats["written"] = len(wanted)
    write_marker(destination, stats, keep)

    problems = check(destination)
    if problems:
        print("vendoring incomplete:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    size, count = tree_size(destination)
    print(f"source: {source}")
    print(
        "wrote {written} files; dropped {maps} source maps, "
        "{legacy_language_or_nls} legacy/nls files, {other_languages} other "
        "languages, {unwanted_top_level} non-license top-level files".format(**stats)
    )
    print(f"vendored on disk: {count} files, {size / 1024 / 1024:.1f} MiB")
    print("Now commit it: git add selfkraken/gui/monaco && git commit")
    return 0


def unpacked_bytes(payload: bytes, wanted: dict[str, str]) -> int:
    """Sum of the uncompressed sizes of the planned members."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.name in wanted:
                total += member.size
    return total


def min_vs_total(payload: bytes) -> tuple[int, int]:
    """``(bytes, files)`` of the whole ``min/vs`` tree minus ``*.map`` — the
    un-pruned baseline the report compares against."""
    total = count = 0
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if (
                member.isfile()
                and member.name.startswith(_MIN_PREFIX)
                and not member.name.endswith(".map")
            ):
                total += member.size
                count += 1
    return total, count


if __name__ == "__main__":
    raise SystemExit(main())
