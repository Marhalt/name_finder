#!/usr/bin/env python3
"""
author_find.py - recover the author of each story using a name_find.py manifest.

For every row in the manifest, read the first HEAD_WORDS and last TAIL_WORDS
words of the matched OLD file (some sites put the byline at the end), ask the LLM served by LM Studio for the author, and write
story, author, note to an authors CSV (and to the console).

Rows already in the authors CSV are skipped, so an interrupted run can be
restarted and several manifests can be pointed at the same authors file.

If nothing is found in the old file or in a "..._by_xxx" clean filename, the
first CLEAN_LINES_TO_SEND lines of the clean story are sent as a last check,
because some stories had "by authorname" added by hand during cleaning.

Usage:
    python3 author_find.py                          # prompts for everything
    python3 author_find.py MANIFEST.csv OLD_DIR CLEAN_DIR [AUTHORS.csv]

Note values (several can be joined with "; "):
    verified       the name the LLM gave appears in the text it was sent
    unverified     the name does NOT appear in the text - possibly made up
    llm_unknown    the LLM could not find an author
    from_filename  author taken from a "..._by_xxx" clean filename
    from_cleaned_story  author taken from the first lines of the clean story
    no_old_file    no old file was matched (or it is missing on disk)
    check_pdf      old file is a PDF - not sent, check by hand
    ambiguous      several old files matched; all were queried
    disagree       ambiguous candidates gave different authors ("A | B")
    fuzzy          old file was a fuzzy name match - may be the wrong file
    size_mismatch  old file failed the size check - may be the wrong file
    llm_error      the request to LM Studio failed
"""

import csv
import re
import sys
from pathlib import Path

import requests

from name_find import clean_path_input, ask_for_dir, normalize, read_text, split_translated

# ---------------------------------------------------------------- settings ---

LMSTUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
HEAD_WORDS = 700           # words sent from the start of the old file
TAIL_WORDS = 200           # words sent from the end (some bylines are there)
OMITTED_MARKER = "\n\n[... middle of story omitted ...]\n\n"
CLEAN_LINES_TO_SEND = 6     # non-blank lines from the start of the clean story
TEMPERATURE = 0.0
TIMEOUT = 300   # seconds; generous because reasoning models think first

SYSTEM_PROMPT = (
    "You identify the author of a story from its text file. You are given the "
    "beginning of the file and, for longer files, its end, with the middle "
    "omitted. The text may include a header or footer from the website it "
    "came from, a title, and a byline such as 'by John Smith', "
    "'Author: jsmith', or 'Copyright 2004 Jane Doe'. The byline may be at "
    "the start or at the end. Reply with the author's name or username "
    "exactly as written in the text, and nothing else: no explanation, no "
    "punctuation, no 'by'. Do not guess. If no author is named, reply with "
    "exactly: unknown"
)

# Clean filename of the form "title_by_author" (also "title by author",
# "title-by-author"). Matched against the stem after any "_e" suffix is removed.
BY_IN_FILENAME = re.compile(r"[\s_\-]by[\s_\-]+(.+)$", re.IGNORECASE)

# Replies meaning "no author found".
UNKNOWN_REPLIES = re.compile(
    r"^(unknown|none|n/?a|not (found|specified|stated|mentioned|given)|"
    r"no author.*|anonymous|there is no.*|i (cannot|can't|could not).*)$",
    re.IGNORECASE,
)

# Words after "by" in a filename that are part of the title, not an author
# ("Stand by me", "Taken by surprise").
NOT_AUTHORS = {"me", "you", "us", "them", "him", "her", "it", "my", "the", "a",
               "surprise", "force", "night", "day", "chance", "accident"}

OUT_COLUMNS = ["story", "author", "note", "clean_words"]


# ------------------------------------------------------------------- text ---

def head_and_tail(text: str, head: int = HEAD_WORDS, tail: int = TAIL_WORDS) -> str:
    """The first `head` and last `tail` words of the text, joined by
    OMITTED_MARKER, keeping the original line breaks so the LLM still sees
    the header and footer layout. Short texts are returned whole."""
    words = list(re.finditer(r"\S+", text))
    if len(words) <= head + tail:
        return text
    return text[: words[head - 1].end()] + OMITTED_MARKER + text[words[-tail].start():]


def first_lines(text: str, n: int) -> str:
    """The first n non-blank lines of the text."""
    return "\n".join([l for l in text.splitlines() if l.strip()][:n])


def author_from_filename(clean_name: str):
    """'Some story_by_Fred Bloggs.txt' -> 'Fred Bloggs', else None."""
    stem, _ = split_translated(Path(clean_name).stem)
    m = BY_IN_FILENAME.search(stem)
    if not m or m.start() == 0:
        return None
    name = re.sub(r"[_\s]+", " ", m.group(1)).strip(" -")
    if not name or name.split()[0].lower() in NOT_AUTHORS:
        return None
    return name


# -------------------------------------------------------------------- LLM ---

def clean_reply(content: str) -> str:
    """Reduce the LLM reply to a bare name, or 'unknown'."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    if "<think>" in content:                           # thinking never finished
        return "unknown"
    content = content.split("</think>")[-1]          # closing tag only
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if not lines:
        return "unknown"
    name = lines[0]
    name = re.sub(r"^\**(author|by|written by)\s*[:\-]?\s*\**", "", name, flags=re.IGNORECASE)
    name = name.strip(" \t\"'`*.,;:")
    if not name or UNKNOWN_REPLIES.match(name) or len(name) > 60 or len(name.split()) > 6:
        return "unknown"
    return name


def ask_llm(text: str) -> str:
    """Return the author the LLM found, or 'unknown'. Raises on request errors."""
    data = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": TEMPERATURE,
    }
    response = requests.post(LMSTUDIO_URL, json=data, timeout=TIMEOUT)
    response.raise_for_status()
    return clean_reply(response.json()["choices"][0]["message"]["content"])


def check_server():
    models_url = LMSTUDIO_URL.rsplit("/chat/", 1)[0] + "/models"
    try:
        requests.get(models_url, timeout=3).raise_for_status()
    except requests.exceptions.RequestException as e:
        sys.exit(f"Could not reach LM Studio at {models_url}: {e}\nIs the server running?")


# ---------------------------------------------------------------- per row ---

def author_of_file(old_file: Path):
    """(author, notes) for one old text file."""
    try:
        text = head_and_tail(read_text(old_file))
    except OSError:
        return "unknown", ["no_old_file"]
    return author_of_text(text)


def author_of_text(text: str):
    """(author, notes) for a piece of text sent to the LLM."""
    try:
        author = ask_llm(text)
    except Exception as e:
        print(f"    LLM error: {e}")
        return "unknown", ["llm_error"]
    if author == "unknown":
        return author, ["llm_unknown"]
    if normalize(author) and normalize(author) in normalize(text):
        return author, ["verified"]
    return author, ["unverified"]


def process_row(row: dict, old_dir: Path, clean_dir: Path):
    """(author, note) for one manifest row."""
    status = row["status"]
    notes = []
    if status == "fuzzy":
        notes.append("fuzzy")
    elif status == "found_size_mismatch":
        notes.append("size_mismatch")

    if status == "not_found":
        author, notes = "unknown", ["no_old_file"]

    elif status == "found_pdf":
        author, notes = "unknown", ["check_pdf"]

    elif status == "ambiguous":
        notes.append("ambiguous")
        found = {}                     # normalized name -> (name, notes)
        for rel in row["old_name"].split(" | "):
            path = old_dir / rel
            if path.suffix.lower() == ".pdf":
                notes.append("check_pdf")
                continue
            if not path.is_file():
                continue
            a, n = author_of_file(path)
            if a != "unknown":
                found.setdefault(normalize(a), (a, n))
        if not found:
            author = "unknown"
            notes.append("llm_unknown")
        elif len(found) == 1:
            author, n = next(iter(found.values()))
            notes += n
        else:
            author = " | ".join(a for a, _ in found.values())
            notes.append("disagree")
            if any("unverified" in n for _, n in found.values()):
                notes.append("unverified")

    else:   # found, found_size_mismatch, fuzzy
        path = old_dir / row["old_name"]
        if path.is_file():
            author, n = author_of_file(path)
            notes += n
        else:
            author = "unknown"
            notes.append("no_old_file")

    # Nothing from the old file: fall back to "..._by_xxx" in the clean name.
    if author == "unknown":
        from_name = author_from_filename(row["clean_name"])
        if from_name:
            author = from_name
            notes.append("from_filename")

    # Last check: "by authorname" added by hand at the top of the clean story.
    if author == "unknown":
        clean_file = clean_dir / row["clean_name"]
        try:
            text = first_lines(read_text(clean_file), CLEAN_LINES_TO_SEND)
        except OSError:
            text = ""
        if text:
            a, n = author_of_text(text)
            if a != "unknown":
                author = a
                notes += ["from_cleaned_story"] + n
            else:
                notes += [x for x in n if x != "llm_unknown"]   # keep llm_error

    return author, "; ".join(dict.fromkeys(notes))   # dedupe, keep order


# ------------------------------------------------------------------- main ---

def ask_for_file(prompt: str) -> Path:
    while True:
        p = clean_path_input(input(prompt))
        if p.is_file():
            return p.resolve()
        print(f"  Not a file: {p}  - try again.")


def main():
    args = sys.argv[1:]
    if len(args) >= 3:
        manifest = clean_path_input(args[0]).resolve()
        old_dir = clean_path_input(args[1]).resolve()
        clean_dir = clean_path_input(args[2]).resolve()
        if not manifest.is_file():
            sys.exit(f"Not a file: {manifest}")
        for d in (old_dir, clean_dir):
            if not d.is_dir():
                sys.exit(f"Not a directory: {d}")
    else:
        manifest = ask_for_file("Manifest file:   ")
        old_dir = ask_for_dir("Old directory:   ")
        clean_dir = ask_for_dir("Clean directory: ")

    if len(args) >= 4:
        out_path = clean_path_input(args[3])
    else:
        name = manifest.stem.removeprefix("manifest_")
        default = manifest.parent / f"authors_{name}.csv"
        raw = input(f"Authors file [{default}]: ").strip()
        out_path = clean_path_input(raw) if raw else default

    check_server()

    with open(manifest, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    clean_words = {r["clean_name"]: r.get("clean_words", "") for r in rows}

    # Resume: skip stories already in the authors file.
    done = set()
    if out_path.is_file():
        with open(out_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            existing = list(reader)
            columns = reader.fieldnames
        done = {r["story"] for r in existing}
        if columns != OUT_COLUMNS:
            # Older authors file: rewrite it with the current columns,
            # filling clean_words from the manifest.
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=OUT_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for r in existing:
                    r.setdefault("clean_words", clean_words.get(r["story"], ""))
                    writer.writerow(r)
            print(f"Updated {out_path.name} to columns: {', '.join(OUT_COLUMNS)}")
    else:
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:   # BOM for Excel
            csv.writer(f).writerow(OUT_COLUMNS)

    todo = [r for r in rows if r["clean_name"] not in done]
    print(f"\n{len(rows)} stories in manifest, {len(rows) - len(todo)} already done, "
          f"{len(todo)} to do.\n")

    counts = {}
    authors_found = 0
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for i, row in enumerate(todo, 1):
            author, note = process_row(row, old_dir, clean_dir)
            if author != "unknown":
                authors_found += 1
            writer.writerow([row["clean_name"], author, note, row.get("clean_words", "")])
            f.flush()
            for n in note.split("; "):
                counts[n] = counts.get(n, 0) + 1
            print(f"[{i}/{len(todo)}] {row['clean_name']}  ->  {author}   ({note})")

    print(f"\nWrote {out_path}")
    print(f"  {len(todo)} files, {authors_found} authors found")
    for n, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:<20} {c}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled. Rerun to continue where it stopped.")
