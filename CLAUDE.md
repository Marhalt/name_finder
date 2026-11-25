# name_finder

## Project context

- **Corpus:** about 15,000 stories gathered from the internet over several years.
- **`/old`:** the raw files. They have metadata, mixed encodings, and a few are in other languages. These are the only files that still contain the author.
- **`/cleaned`:** the same stories after months of cleaning (metadata removed, UTF-8, English only), split into about 14 subdirectories. The author was lost during cleaning.

## Pipeline for recovering authors

1. **Step 1, `name_find.py` (current):** match each cleaned file to its original in `/old` and write a CSV manifest. It is being tested on progressively larger cleaned subdirectories, and each one serves as a test bed.
2. **Step 2 (planned):** send the first ~1000 words of each matched old file to an LLM, ask it for the author, and add the answer to the manifest.

Judge changes to `name_find.py` by whether they give step 2 a reliable old-file path for every clean file.

## Decisions so far

- Size check: clean/old word ratio between 0.80 and 1.00, **or** clean at most `MAX_WORD_DIFF` (300) words shorter than old. The second rule lets small stories with large headers pass. The 300 is a guess and can be tuned.
- Translated files (`_e` suffix, about 100 of them) skip the size check. There is no `translated` column in the CSV because there are too few to be worth one.
- Matching works well enough on the directories tested so far (as of 2026-11-24).

## Ideas not yet done

- Write the absolute OLD path (or an `old_root` column) so each manifest is self-contained for step 2.
- Flag old files that more than one clean file matched, ideally across all manifests.
- Apply the size check to fuzzy matches too.
- PDFs (`found_pdf`) will need text extraction before step 2.
- For step 2, consider sending the first ~700 words plus the last ~300, because some sites put the byline at the end.
