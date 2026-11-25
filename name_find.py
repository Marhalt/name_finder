#!/usr/bin/env python3
"""
name_find.py - match cleaned story files to their original (uncleaned) versions.

For every .txt file in the CLEAN directory, find the corresponding file in the
OLD directory by normalized title, sanity-check the match by word count, and
write a CSV manifest.

Usage:
    python3 name_find.py                       # prompts for everything
    python3 name_find.py CLEAN_DIR OLD_DIR [MANIFEST.csv]
"""

import csv
import difflib
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------- settings ---

# Clean word count / old word count must fall in this range to pass the size
# check. Clean is expected to be shorter (metadata removed) but not by much.
MIN_RATIO = 0.80
MAX_RATIO = 1.00

# Small files: also pass if the clean file is at most this many words shorter
# than the old one, whatever the ratio. A 600-word story with a 200-word header
# fails the ratio (0.75) but is clearly the same story.
MAX_WORD_DIFF = 300

# How similar a normalized name must be (0-1) to count as a fuzzy match.
FUZZY_CUTOFF = 0.85

# Source tags to strip from OLD filenames before matching (clean files don't
# have them). Matched case-insensitively against the start / end of the name
# (without its extension), exactly as written - so add each spelling variant
# you find, e.g. both "B_library_" and "B library ". Only one prefix and one
# suffix are stripped per file; the longest matching entry wins.
OLD_PREFIXES = [
    "BDSM Library - ","BDSM Library "
]
OLD_SUFFIXES = [
    "_ ChestTalk.com", "ChestTalk.com", "Chest Talk"
]

# Extensions treated as real file extensions in OLD. Anything else after the
# last dot is kept as part of the name, so "story_ctalk.com" or "Mr. Smith"
# aren't chopped at the dot.
KNOWN_EXTENSIONS = {".txt", ".pdf"}

# Matches a translation suffix at the end of a filename stem: "_e", " e", "-E"...
TRANSLATED_SUFFIX = re.compile(r"[\s_\-]+e$", re.IGNORECASE)

CSV_COLUMNS = [
    "clean_name", "old_name", "status",
    "clean_words", "old_words", "size_ratio",
]


# ------------------------------------------------------------- path input ---

def clean_path_input(raw: str) -> Path:
    """Turn whatever was typed, pasted, or dragged into a terminal into a Path.

    Handles: surrounding whitespace, matching single/double quotes (Linux
    terminals, "Copy as Pathname"), backslash-escaped characters (macOS
    Terminal / iTerm drag-and-drop), file:// URIs, and ~.
    """
    s = raw.strip()

    # Strip one layer of matching quotes.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    elif "\\" in s:
        # Unescape "\ " "\(" "\'" etc. Only done for unquoted input, since
        # drag-and-drop either quotes OR escapes, never both.
        s = re.sub(r"\\(.)", r"\1", s)

    if s.startswith("file://"):
        s = unquote(urlparse(s).path)

    return Path(s).expanduser()


def ask_for_dir(prompt: str) -> Path:
    while True:
        p = clean_path_input(input(prompt))
        if p.is_dir():
            return p.resolve()
        print(f"  Not a directory: {p}  - try again.")


# ---------------------------------------------------------- normalization ---

def normalize(text: str) -> str:
    """Reduce a title to lowercase letters and digits only.

    'A Story By Fred', 'a_story_by_fred', 'astorybyfred' -> 'astorybyfred'.
    Accents are folded (café -> cafe); non-Latin letters are kept as-is.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c for c in text.casefold() if c.isalnum())


def old_title(path: Path) -> str:
    """The title part of an OLD filename: known extension removed, then any
    source prefix/suffix from OLD_PREFIXES / OLD_SUFFIXES removed."""
    name = path.name
    if path.suffix.lower() in KNOWN_EXTENSIONS:
        name = name[: -len(path.suffix)]

    for prefix in sorted(OLD_PREFIXES, key=len, reverse=True):
        if len(name) > len(prefix) and name[: len(prefix)].lower() == prefix.lower():
            name = name[len(prefix):]
            break

    for suffix in sorted(OLD_SUFFIXES, key=len, reverse=True):
        if len(name) > len(suffix) and name[-len(suffix):].lower() == suffix.lower():
            name = name[: -len(suffix)]
            break

    return name


def split_translated(stem: str):
    """Return (stem_without_suffix, True) if the stem ends in a translation
    suffix like '_e', else (stem, False)."""
    m = TRANSLATED_SUFFIX.search(stem)
    if m and m.start() > 0:
        return stem[:m.start()], True
    return stem, False


# ------------------------------------------------------------ word counts ---

def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")  # never fails


def word_count(path: Path):
    """Word count of a text file, or None for PDFs / unreadable files."""
    if path.suffix.lower() == ".pdf":
        return None
    try:
        return len(read_text(path).split())
    except OSError:
        return None


# --------------------------------------------------------------- matching ---

def is_hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def build_old_index(old_dir: Path) -> dict:
    """normalized name -> list of old file Paths (recursive)."""
    index = {}
    for p in old_dir.rglob("*"):
        if p.is_file() and not is_hidden(p, old_dir):
            index.setdefault(normalize(old_title(p)), []).append(p)
    return index


def size_ratio(clean_words, old_words):
    if clean_words is None or not old_words:
        return None
    return clean_words / old_words


def size_ok(clean_words, old_words) -> bool:
    """Clean is no longer than old, and either within the ratio band or
    within MAX_WORD_DIFF words of it."""
    ratio = size_ratio(clean_words, old_words)
    if ratio is None or ratio > MAX_RATIO:
        return False
    return ratio >= MIN_RATIO or old_words - clean_words <= MAX_WORD_DIFF


def match_one(clean_file: Path, index: dict, old_keys: list):
    """Return (status, old_path_or_list, translated, clean_words, old_words, ratio)."""
    stem, translated = split_translated(clean_file.stem)
    clean_words = word_count(clean_file)

    # Exact lookup. For "_e" files try the stripped name first, then the full
    # name (in case the title genuinely ends in " e").
    keys = [normalize(stem)]
    if translated:
        keys.append(normalize(clean_file.stem))

    candidates, fuzzy = [], False
    for k in keys:
        if k in index:
            candidates = index[k]
            break

    # Fuzzy fallback.
    if not candidates:
        close = difflib.get_close_matches(keys[0], old_keys, n=1, cutoff=FUZZY_CUTOFF)
        if close:
            candidates, fuzzy = index[close[0]], True

    if not candidates:
        return "not_found", None, translated, clean_words, None, None

    # Several old files share this normalized name: let the size check pick.
    if len(candidates) > 1:
        sized = [(c, word_count(c)) for c in candidates]
        passing = [(c, w) for c, w in sized if size_ok(clean_words, w)]
        if len(passing) == 1 and not translated:
            candidates = [passing[0][0]]
        else:
            return "ambiguous", candidates, translated, clean_words, None, None

    old = candidates[0]
    old_words = word_count(old)
    ratio = size_ratio(clean_words, old_words)

    if fuzzy:
        status = "fuzzy"
    elif old.suffix.lower() == ".pdf":
        status = "found_pdf"
    elif translated or size_ok(clean_words, old_words):
        status = "found"            # translated files skip the size check
    else:
        status = "found_size_mismatch"

    return status, old, translated, clean_words, old_words, ratio


# ------------------------------------------------------------------- main ---

def main():
    args = sys.argv[1:]
    if len(args) >= 2:
        clean_dir = clean_path_input(args[0]).resolve()
        old_dir = clean_path_input(args[1]).resolve()
        for d in (clean_dir, old_dir):
            if not d.is_dir():
                sys.exit(f"Not a directory: {d}")
    else:
        clean_dir = ask_for_dir("Clean directory: ")
        old_dir = ask_for_dir("Old directory:   ")

    if len(args) >= 3:
        manifest = clean_path_input(args[2])
    else:
        default = Path.cwd() / f"manifest_{clean_dir.name}.csv"
        raw = input(f"Manifest file [{default}]: ").strip()
        manifest = clean_path_input(raw) if raw else default

    print(f"\nIndexing {old_dir} ...")
    index = build_old_index(old_dir)
    old_keys = list(index)
    print(f"  {sum(len(v) for v in index.values())} old files, {len(index)} distinct names")

    clean_files = sorted(
        p for p in clean_dir.rglob("*.txt")
        if p.is_file() and not is_hidden(p, clean_dir)
    )
    print(f"Matching {len(clean_files)} clean files ...")

    counts = {}
    with open(manifest, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)

        for i, cf in enumerate(clean_files, 1):
            status, old, translated, cw, ow, ratio = match_one(cf, index, old_keys)
            counts[status] = counts.get(status, 0) + 1

            if isinstance(old, list):        # ambiguous: list every candidate
                old_name = " | ".join(str(p.relative_to(old_dir)) for p in old)
            elif old is not None:
                old_name = str(old.relative_to(old_dir))
            else:
                old_name = "not_found"

            writer.writerow([
                str(cf.relative_to(clean_dir)),
                old_name,
                status,
                "" if cw is None else cw,
                "" if ow is None else ow,
                "" if ratio is None else f"{ratio:.3f}",
            ])

            if i % 500 == 0:
                print(f"  {i}/{len(clean_files)}")

    print(f"\nWrote {manifest}")
    for status in ("found", "found_pdf", "found_size_mismatch", "fuzzy", "ambiguous", "not_found"):
        if status in counts:
            print(f"  {status:<20} {counts[status]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
