# name_finder

## Project context

- **Corpus:** about 15,000 stories gathered from the internet over several years.
- **`/old`:** the raw files. They have metadata, mixed encodings, and a few are in other languages. These are the only files that still contain the author.
- **`/cleaned`:** the same stories after months of cleaning (metadata removed, UTF-8, English only), split into about 14 subdirectories. The author was lost during cleaning.

## Pipeline for recovering authors

1. **Step 1, `name_find.py` (current):** match each cleaned file to its original in `/old` and write a CSV manifest. It is being tested on progressively larger cleaned subdirectories, and each one serves as a test bed.
2. **Step 2, `author_find.py`:** for each manifest row, send part of the matched old file to a local LLM (LM Studio) and write story, author and note to an `authors_*.csv`. It resumes interrupted runs. If nothing is found, it falls back to a `_by_xxx` clean filename, then to the first lines of the clean story.
   - It sends the **first 700 words plus the last 200** (`HEAD_WORDS` / `TAIL_WORDS`), because some sites put the byline or copyright at the end (changed from the first 1200 words on 2026-11-25).
   - A `[... middle of story omitted ...]` marker separates the two parts, and the prompt says the byline may be at either end.
   - Files of 900 words or fewer are sent whole.

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
