# Union Release Guide — `release_union.py`

This document explains how unfoldingWord cuts a **union release** of the seven English
resource repos, and how the `release_union.py` script automates it.

- **Script:** `release_union.py` (lives in this directory, next to the repos and `.env`)
- **Repos released together:** `en_ult`, `en_ust`, `en_tn`, `en_tq`, `en_twl`, `en_ta`, `en_tw`
- **What you provide:** the **new** Bible book(s) being released this round (e.g. `PSA HAB LAM`)
- **What the script produces:** an incremented version, updated manifests, per-book release
  branches, refreshed book files, pushes, and a prerelease on the DCS (Gitea) for each repo.

---

## 1. The two kinds of repos

| Repos | Kind | What gets released | Release is cut from |
|-------|------|--------------------|---------------------|
| `en_ult`, `en_ust`, `en_tn`, `en_tq`, `en_twl` | **Book repos** | Individual Bible books (one file per book, e.g. `19-PSA.usfm`, `tn_PSA.tsv`) | `release_v<new>` branch |
| `en_ta`, `en_tw` | **Whole repos** | The entire repo | `master` |

Book repos publish a curated subset of books on a dedicated `release_v<version>` branch.
`en_ta` and `en_tw` are released wholesale straight from `master`.

`en_ult`/`en_ust` also carry a **Front Matter** project (`A0-FRT.usfm`, identifier `frt`);
it is always part of the release but is **not** counted as an Old or New Testament book.

---

## 2. Prerequisites (read before every run)

The script enforces these and will stop before changing anything if they aren't met:

1. **Repos are present, or cloneable.** Any of the seven repos missing from this directory is
   **cloned automatically from the target host** (see §7). The clone is shallow
   (`--depth 1 --no-single-branch`) — it pulls `master` and every `release_v<version>` branch
   tip without full history, which is all a release needs. This means you can run in an empty
   directory, or delete and re-clone repos freely. (A directory that exists but isn't a git
   repo is an error.) Pushing from a shallow clone normally works fine; if your DCS server
   ever rejects it, add `--unshallow` to fetch full history first.
2. **No uncommitted changes to tracked files** in any repo. (Untracked scratch files are
   fine — they're never touched.) Commit, stash, or discard tracked changes first.
3. **`master` must be in sync with the remote** — no local commits on `master` that haven't
   been pushed. Such commits would be swept into the release push and mean the repo isn't in
   the known-published state a release assumes. Push or discard them first.
   *(This check is skipped in `--resume` mode, because the bump commit from the interrupted
   run is legitimately an un-pushed commit at that point.)*
4. **`GITEA_TOKEN` set in `.env`** (same directory). Format: `GITEA_TOKEN=<token>`.
   Only needed when actually creating releases (not for `--dry-run`).
5. **Push access** to the target host over SSH (the repos' `origin` uses
   `git@git.door43.org:...`).
6. **Python 3** — the script auto-creates a local `.release-venv` and installs `ruamel.yaml`
   on first run (used to keep manifest diffs minimal). The release API uses the standard
   library, so no other dependency is required.

If any precondition fails, the script exits with a clear message and makes **no** changes.

---

## 3. How to run it

```bash
# Standard release of one or more NEW books (to production, git.door43.org)
./release_union.py PSA HAB LAM

# Preview everything locally — does all the work, prints diffs + the release notes,
# then rolls it ALL back and pushes nothing. Use this first, every time.
./release_union.py PSA HAB LAM --dry-run

# Skip the "are you sure?" confirmation before pushing
./release_union.py PSA HAB LAM --yes

# Target a different DCS instance (QA testing) — see §7
./release_union.py PSA HAB LAM --host qa.door43.org

# Finish a run that already pushed but didn't complete (see §6)
./release_union.py PSA HAB LAM --resume
```

### Arguments & flags

| Item | Meaning |
|------|---------|
| `BOOK ...` | One or more **new** book codes to release this round (e.g. `PSA`, `HAB`, `2KI`). Case-insensitive. These must be books that exist in `master` but have **not** been released before. |
| `--host HOST` | DCS hostname for both push and release API. Default `git.door43.org`. Must be a **full** hostname (e.g. `qa.door43.org`, not `qa`). |
| `--resume` | Idempotently finish an interrupted release (no rollback). |
| `--unshallow` | Fetch full history for any shallow (freshly cloned) repo before doing work. Use if your DCS server ever rejects pushes from a shallow clone. No-op for repos that already have full history. |
| `--dry-run` | Do all local work, show diffs + notes, then roll everything back. Never pushes, never calls the API. |
| `--yes` | Don't prompt for confirmation before the push/release phase. |

The book codes are the **newly added** books only. Updates to already-published books happen
automatically (every published book file is refreshed from `master`) — you do not list them.

---

## 4. What the script does, step by step

It works in three phases and is **atomic across all seven repos**: all local work happens
first; nothing is pushed unless every repo prepared cleanly.

### Phase A — Local preparation (reversible)

For **every** repo, on `master`:
- `fetch`, checkout `master`, fast-forward from the remote.
- Read `dublin_core` from `manifest.yaml`; remember the current `version` (the *old* version).
- Set `dublin_core.modified` and `dublin_core.issued` to **today** (`YYYY-MM-DD`).
- Set the repo's own `source` entry version (e.g. the `ult` entry for `en_ult`) to the *old*
  version. External sources (`uhb`, `ugnt`, `asv`) are left untouched.
- Increment `dublin_core.version` by one (e.g. `88` → `89`).
- Commit `manifest.yaml`:
  - Book repos: `Update manifest.yaml for release v<new>`
  - `en_ta`/`en_tw`: `Preparing for v<new> release`

For **book repos** additionally:
- Branch `release_v<new>` off `release_v<old>` (fast-forwarded from the remote).
- Replace that branch's `dublin_core` so it is **identical to master's** (full `source`
  array, dates, and the bumped version).
- Rebuild the `projects` list to contain **only** the books released in the previous version
  **plus** the new books from the arguments. Books are placed in **master's order** (master's
  `sort` values are preserved, so new books slot into their canonical Bible position).
- Refresh **every** book file listed in that `projects` list from `master` (this both updates
  already-published books and adds the new ones), then add the new book files.
- Commit everything: `Releasing <CODES>` (e.g. `Releasing PSA HAB LAM`).

### Phase A review

The script prints:
- The generated **release notes** (see §5).
- A per-repo commit/diff **stat** of what will be released.

Then, unless `--yes` was given, it asks for confirmation. Declining rolls everything back.

### Phase B — Push (point of no return)

Pushes `master` for every repo, plus `release_v<new>` for the book repos.

### Phase C — Create releases

POSTs a **prerelease** to the DCS for each repo (see §5 for the exact payload). Release
creation is idempotent: a tag that already exists is skipped rather than re-created.

### Failure handling

- **Any failure during Phase A** → the script rolls back **all** repos (deletes the new
  release branches, resets `master` to its prior commit) and pushes nothing.
- **Failure during Phase B or C** (after something was pushed) → the script does **not** roll
  back (that would undo remote state). It tells you to re-run with `--resume` to finish.

---

## 5. The release (the prerelease created on DCS)

For each repo the script POSTs to:

```
POST https://<host>/api/v1/repos/unfoldingWord/<repo>/releases
Authorization: token <GITEA_TOKEN>
Content-Type: application/json
```

with this body:

```json
{
  "body": "<release notes markdown>",
  "draft": false,
  "name": "v<version>",
  "prerelease": true,
  "tag_name": "v<version>",
  "target_commitish": "release_v<version>"
}
```

- `target_commitish` is `release_v<version>` for **book repos** and `master` for
  **`en_ta`/`en_tw`**.
- `body` for book repos is the full Book-Package release notes (below). For `en_ta`/`en_tw`
  it is a simple header, `# v<version> Release`, followed by the "Changes Since" block.

> **⚠️ Every release is created as a PRERELEASE** (`"prerelease": true`). The script does
> **not** publish a production release. After the run, someone must go to **each of the seven
> repos** on the DCS server and edit that release to **uncheck "This is a pre-release"** (i.e.
> promote it to a production/latest release). Until that's done, the v`<version>` release stays
> marked as a prerelease on DCS. This manual promotion step is intentional — it's the final
> human gate before the release goes live.

### Release notes (`body`) — book packages

The notes are generated from the release-branch `projects` (titles, codes, OT/NT category,
and order). They are **identical across all five book repos** (they release the same book
set in union). A testament header shows `[ALL]` when every book in that testament is present
(39 OT, 27 NT). Example for a v89 release that newly adds Psalms, Lamentations, and Habakkuk:

```markdown
# v89 Release of unfoldingWord Book Packages

## What's New in this Release

- This release is the first release of Psalms (PSA), Lamentations (LAM), and Habakkuk (HAB).

## All Book Packages in this Release

The following books have undergone a Book Package consistency check and are included in this release:

### Old Testament Books (27):

- Genesis (GEN)
- Exodus (EXO)
- Leviticus (LEV)
- Deuteronomy (DEU)
- Joshua (JOS)
- Judges (JDG)
- Ruth (RUT)
- 1 Samuel (1SA)
- 2 Samuel (2SA)
- 1 Kings (1KI)
- 2 Kings (2KI)
- Ezra (EZR)
- Nehemiah (NEH)
- Esther (EST)
- Job (JOB)
- Psalms (PSA)
- Proverbs (PRO)
- Song of Songs (SNG)
- Lamentations (LAM)
- Joel (JOL)
- Obadiah (OBA)
- Jonah (JON)
- Nahum (NAM)
- Habakkuk (HAB)
- Zephaniah (ZEP)
- Haggai (HAG)
- Malachi (MAL)

### New Testament Books (27 [ALL]):

- Matthew (MAT)
- Mark (MRK)
- Luke (LUK)
- John (JHN)
- Acts (ACT)
- Romans (ROM)
- 1 Corinthians (1CO)
- 2 Corinthians (2CO)
- Galatians (GAL)
- Ephesians (EPH)
- Philippians (PHP)
- Colossians (COL)
- 1 Thessalonians (1TH)
- 2 Thessalonians (2TH)
- 1 Timothy (1TI)
- 2 Timothy (2TI)
- Titus (TIT)
- Philemon (PHM)
- Hebrews (HEB)
- James (JAS)
- 1 Peter (1PE)
- 2 Peter (2PE)
- 1 John (1JN)
- 2 John (2JN)
- 3 John (3JN)
- Jude (JUD)
- Revelation (REV)

## Changes Since the Previous Release (v88)

- [See a detailed, line-by-line list of everything that changed in version 89](/compare/v88...v89).
```

Notes:
- **"What's New"** lists exactly the book codes you passed as arguments (the new books), in
  canonical order, grammatically joined (`A`, `A and B`, or `A, B, and C`).
- The book counts and lists in **"All Book Packages"** come from the release branch's
  `projects`, so they always reflect what is actually in the release.
- **"Changes Since the Previous Release"** is appended to **every** release body (all seven
  repos, including `en_ta`/`en_tw`). The link is **repo-relative** — `/compare/v<old>...v<new>`
  (e.g. `/compare/v88...v89`). Gitea resolves release-body links against the repo's own path,
  so this correctly opens that repo's compare page on whichever DCS instance the release was
  created on (production or QA). (An absolute `/unfoldingWord/<repo>/...` link is wrong here:
  Gitea prepends the repo path again, doubling it.) For `en_ta`/`en_tw` the body is
  `# v<version> Release` followed by this same block.

---

## 6. Resuming an interrupted release (`--resume`)

Use `--resume` when a run got past the push step (Phase B/C) but didn't finish — for example,
a network hiccup while creating releases, or one repo's release API call failed. Re-run with
the **same book arguments** (and same `--host`):

```bash
./release_union.py PSA HAB LAM --resume
```

In `--resume` mode the script:
- **Detects** that `master` is already bumped (by recognizing the bump commit) and **skips**
  re-bumping it.
- **Reuses** the existing `release_v<new>` branch (checking it out from the remote if your
  local copy is gone) instead of erroring that it already exists.
- **Re-pushes** idempotently (already-current branches are no-ops).
- **Creates only the releases that don't already exist** (it checks each tag first).
- **Does not roll back** (the remote already has state from the earlier run).

Note: a normal (non-`--resume`) run will **refuse** to start if it detects `master` is already
bumped, and tell you to use `--resume`. This prevents accidentally double-bumping.

---

## 7. Releasing to a different DCS (`--host`, e.g. QA)

`--host qa.door43.org` points the **entire** release at another DCS instance — both the git
push and the release API:

```bash
./release_union.py PSA HAB LAM --host qa.door43.org --dry-run   # preview against QA
./release_union.py PSA HAB LAM --host qa.door43.org             # release to QA
```

**Cloning + isolation from production:** missing repos are cloned from the **target host**
(`git@<host>:unfoldingWord/<repo>.git`), so a fresh QA workflow looks like:

```bash
mkdir qa-release && cd qa-release      # empty dir + a .env with a QA-valid GITEA_TOKEN
cp /path/to/.env .                      # GITEA_TOKEN for qa.door43.org
/path/to/release_union.py PSA HAB LAM --host qa.door43.org --dry-run
```

Everything (clone, fetch, push, releases) then targets QA — production is never contacted.
You can delete the whole directory and start over at any time.

How the git remote is handled:

- For the **default** host (`git.door43.org`) the script uses the existing `origin` remote.
- For any **other** host it uses a remote **named after the host's first label** (so
  `qa.door43.org` → a remote named `qa`). **You do not need to create these remotes
  yourself** — the script ensures the remote exists in each repo automatically:
  - If the repo already has a `qa` remote, it's reused (and its URL is corrected if needed).
  - If not, the script **adds** one, deriving the URL from that repo's `origin` by swapping
    the hostname (`git@git.door43.org:unfoldingWord/en_ult.git` →
    `git@qa.door43.org:unfoldingWord/en_ult.git`).

So a `--host qa.door43.org` run reads/refreshes book files from **QA's** `master` and pushes
branches + creates releases on **QA**. The `GITEA_TOKEN` in `.env` must be valid for the host
you target.

> The host must be a full hostname. `--host qa` is rejected; use `--host qa.door43.org`.

---

## 8. Quick reference: full release procedure

1. Make sure every repo is on `master`, clean, and fully pushed (see §2).
2. Confirm the book code(s) you're adding this round.
3. **Dry run** and review the diffs and release notes:
   `./release_union.py <BOOKS> --dry-run`
4. If it looks right, run for real: `./release_union.py <BOOKS>`
   - Review the printed notes/diffs, then confirm at the prompt.
5. If anything fails after pushing, re-run with `--resume` (same args) to finish.
6. Verify the prereleases on the DCS for all seven repos.
7. **Promote each release from prerelease to production:** open every one of the seven repos
   on the DCS, edit the new `v<version>` release, and **uncheck "This is a pre-release"**. The
   script never does this — releases are created as prereleases by design (see §5).

---

## 9. Version mechanics (reference)

- Versions are simple incrementing integers stored as strings (e.g. `'88'` → `'89'`).
- On `master`: `dublin_core.version` becomes the new version; the repo's own `source` entry
  version becomes the old version; `modified`/`issued` become today.
- The `release_v<new>` branch's `dublin_core` is made **identical to master's**; only its
  `projects` list differs (trimmed to the released subset).
- `release_v<old>` is the previous version's branch and is the base for the new one. All seven
  repos move in lockstep, so the "old version" is the same number across them.
