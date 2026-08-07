# Union Release Guide — `release_union.py`

This document explains how unfoldingWord cuts a **union release** of the seven English
resource repos, and how the `release_union.py` script automates it.

- **Script:** `release_union.py` (lives in this directory, next to the repos and `.env`)
- **Repos released together:** `en_ult`, `en_ust`, `en_tn`, `en_tq`, `en_twl`, `en_ta`, `en_tw`
- **What you provide:** the **new** Bible book(s) being released this round (e.g. `PSA HAB LAM`)
- **What the script produces:** an incremented version, updated manifests, per-book release
  branches, refreshed book files, pushes, and a prerelease on the DCS (Gitea) for each repo.
- **Two ways to run it:** all the way to a prerelease (the default), or
  `--release_branch_only` — stage and push just the `release_v<new>` branches, with no tag
  and no release, and re-run it as often as you like while the release is being prepared
  (see §6).

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
   **cloned automatically from the target host** (see §8). The clone is shallow
   (`--depth 1 --no-single-branch`) — it pulls `master` and every `release_v<version>` branch
   tip without full history, which is all a release needs. This means you can run in an empty
   directory, or delete and re-clone repos freely. (A directory that exists but isn't a git
   repo is an error.) Pushing from a shallow clone normally works fine; if your DCS server
   ever rejects it, add `--unshallow` to fetch full history first.
2. **No uncommitted changes to tracked files** in any repo. (Untracked scratch files are
   fine — they're never touched.) Commit, stash, or discard tracked changes first.
3. **`master` must be in sync with the remote** — no local commits on `master` that haven't
   been pushed. Such commits would be swept into the release push (or copied onto the
   release branch) and mean the repo isn't in the known-published state a release assumes.
   Push or discard them first.
   *(This check is skipped in `--resume` mode, because the bump commit from the interrupted
   run is legitimately an un-pushed commit at that point. Under `--release_branch_only` it
   applies to the five book repos only — `en_ta`/`en_tw` aren't touched in that mode.)*
4. **`GITEA_TOKEN` set in `.env`** (same directory). Format: `GITEA_TOKEN=<token>`.
   Only needed when actually creating releases — **not** for `--dry-run` and **not** for
   `--release_branch_only`, neither of which calls the release API.
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

# Stage + push ONLY the release_v<new> branches — no tag, no release (see §6)
./release_union.py PSA HAB LAM --release_branch_only

# Skip the "are you sure?" confirmation before pushing
./release_union.py PSA HAB LAM --yes

# Target a different DCS instance (QA testing) — see §8
./release_union.py PSA HAB LAM --host qa.door43.org

# Finish a run that already pushed but didn't complete (see §7)
./release_union.py PSA HAB LAM --resume
```

### Arguments & flags

| Item | Meaning |
|------|---------|
| `BOOK ...` | One or more **new** book codes to release this round (e.g. `PSA`, `HAB`, `2KI`). Case-insensitive. These must be books that exist in `master` but have **not** been released before. Optional once a `release_v<new>` branch is already staged — see §6. |
| `--release_branch_only` | Create (if needed) and push **only** the `release_v<new>` branches, refreshed from `master`. Never touches `master`, creates no tag and no release, needs no `GITEA_TOKEN`. Idempotent — re-run it any time to re-sync. See §6. |
| `--host HOST` | DCS hostname for both push and release API. Default `git.door43.org`. Must be a **full** hostname (e.g. `qa.door43.org`, not `qa`). |
| `--resume` | Idempotently finish an interrupted release (no rollback). |
| `--unshallow` | Fetch full history for any shallow (freshly cloned) repo before doing work. Use if your DCS server ever rejects pushes from a shallow clone. No-op for repos that already have full history. |
| `--dry-run` | Do all local work, show diffs + notes, then roll everything back. Never pushes, never calls the API. Combines with `--release_branch_only`. |
| `--yes` | Don't prompt for confirmation before the push/release phase. |

The book codes are the **newly added** books only. Updates to already-published books happen
automatically (every published book file is refreshed from `master`) — you do not list them.

---

## 4. What the script does, step by step

It works in a planning step plus three phases, and is **atomic across all seven repos**: all
local work happens first; nothing is pushed unless every repo prepared cleanly.

### Planning (read-only — nothing is changed)

For every repo the script checks out `master`, fast-forwards it from the remote, then works
out the versions **from the target host** rather than from guesswork:

- **old version** = the highest `v<int>` **tag on the remote** (asked via `git ls-remote`, so
  it's whatever that host has actually released; local tags are ignored because they're a mix
  of whichever host was fetched last). Tags like `v85.1` and `ver6` never count.
- **new version** = old + 1, always.
- If `master` is already bumped to old+1, and/or a `release_v<old+1>` branch already exists,
  a release is **already in progress**: the script says so and picks it up instead of
  starting a new version. (This is the normal state after a `--release_branch_only` run.)
- Anything **further ahead** than old+1 (say a stray `release_v92` when v89 is the newest
  release) is an error you have to sort out by hand.
- All seven repos must land on the **same** new version — they release in lockstep — or the
  run stops.
- The book codes are validated here: each must exist in `master`'s `projects` and must **not**
  already be in the previous release (`release_v<old>`).

### Phase A — Local preparation (reversible)

For **every** repo, on `master` — **skipped entirely under `--release_branch_only`**:
- Set `dublin_core.modified` and `dublin_core.issued` to **today** (`YYYY-MM-DD`).
- Set the repo's own `source` entry version (e.g. the `ult` entry for `en_ult`) to the *old*
  version. External sources (`uhb`, `ugnt`, `asv`) are left untouched.
- Set `dublin_core.version` to the new version (e.g. `'89'` → `'90'`).
- Commit `manifest.yaml` — but only if that actually changed something:
  - Book repos: `Update manifest.yaml for release v<new>`
  - `en_ta`/`en_tw`: `Preparing for v<new> release`
  - Re-running on a later day, when `master` was already bumped by an earlier run, only
    moves the dates: `Refresh manifest.yaml dates for v<new> release`.

For **book repos** additionally:
- Create `release_v<new>` off `release_v<old>` (fast-forwarded from the remote), **or** check
  out the `release_v<new>` that already exists (staged by an earlier `--release_branch_only`
  run, or left by an interrupted release) and update it in place.
- **The branch's `manifest.yaml` becomes master's `manifest.yaml`.** `master` is the single
  source of truth for the whole file: all of `dublin_core.*` (identifier, title, creator,
  contributor, publisher, relation, the full `source` array, rights, subject, type, format,
  language, …) and all of `checking.*` are copied verbatim, so anything edited on `master`
  between releases always lands in the release. Exactly two things differ:
  1. the four **release-owned** `dublin_core` fields — `version` (= new), the repo's own
     `source` entry `version` (= old), `issued` and `modified` (= today);
  2. `projects`, which is trimmed (below).
- `projects` = the books released in the previous version **+** whatever the branch already
  had staged **+** the new books from the arguments, each entry **copied from master's
  `projects`** (so master owns every book's `title`, `sort`, `path`, `categories` and
  `versification`), listed in **master's order** — master's order *is* sort order, so new
  books slot into their canonical Bible position.
- Refresh **every** book file listed in that `projects` list from `master` (this both updates
  already-published books and adds the new ones).
- Commit — again only if something actually changed:
  - a newly created branch: `Releasing <CODES>` (e.g. `Releasing PSA HAB LAM`)
  - an existing branch being re-synced: `Updating release_v<new> from master`

Because the branch's manifest is derived from master's plus fixed release values, staging the
branch early and publishing later converge on **exactly** the same content — the re-sync
during a publish run is a no-op when nothing on `master` moved.

### Phase A review

The script prints:
- The generated **release notes** (see §5) — marked *PREVIEW ONLY* under
  `--release_branch_only`, where nothing gets published.
- Per repo, a commit/diff **stat** of every branch it actually changed, or
  `(nothing changed — already up to date with master)`.

Then, unless `--yes` was given, it asks for confirmation. Declining rolls everything back.

### Phase B — Push (point of no return)

Pushes `master` for every repo, plus `release_v<new>` for the book repos.

Under `--release_branch_only` it pushes **only** the `release_v<new>` branches (`master` was
never touched, so there is nothing to push there), and if those branches already match the
host it says so and does nothing. Afterwards every repo is left checked out on `master`.

### Phase C — Create releases

POSTs a **prerelease** to the DCS for each repo (see §5 for the exact payload). Release
creation is idempotent: a tag that already exists is skipped rather than re-created.

**Skipped entirely under `--release_branch_only`** — that mode stops after Phase B.

### Failure handling

- **Any failure during Phase A** → the script rolls back **all** repos and pushes nothing:
  release branches it created are deleted, a release branch that already existed is reset to
  the commit it was on, and `master` is reset to its prior commit.
- **Failure during Phase B or C** (after something was pushed) → the script does **not** roll
  back (that would undo remote state). It tells you to re-run with `--resume` to finish (or,
  under `--release_branch_only`, just to re-run the same command).

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
- **"What's New"** lists the books this release adds over the previous one, in canonical
  order, grammatically joined (`A`, `A and B`, or `A, B, and C`). It is worked out by
  comparing the release branch against `release_v<old>` rather than read off the command
  line, so it stays correct across `--release_branch_only` re-runs and when books were staged
  by an earlier run.
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

## 6. Staging the release branch first (`--release_branch_only`)

Use this when the release branch needs to exist and be worked with **before** you're ready to
publish — the usual case being "cut `release_v<new>` now, keep pulling `master`'s fixes into
it for a few weeks, then release".

```bash
# Create release_v<new> for all five book repos, add ISA + JER, push the branches. No release.
./release_union.py ISA JER --release_branch_only

# Days later: pull master's latest into the staged branches again (same books, or none at all)
./release_union.py ISA JER --release_branch_only
./release_union.py --release_branch_only

# Add another book to the branch that's already staged
./release_union.py ISA JER EZK --release_branch_only

# When it's ready — same command, no flag: publishes v<new> exactly as always
./release_union.py ISA JER
```

What it does and doesn't do:

| | `--release_branch_only` | normal run |
|---|---|---|
| Create/refresh `release_v<new>` from `master` | ✅ | ✅ |
| Push `release_v<new>` | ✅ | ✅ |
| Touch/bump/push `master` | ❌ **never** | ✅ |
| Touch `en_ta` / `en_tw` | ❌ (they release off `master`) | ✅ |
| Create the tag + prerelease | ❌ | ✅ |
| Needs `GITEA_TOKEN` | ❌ | ✅ |

Details worth knowing:

- **It picks up where it left off.** The next run reads the newest release tag on the host,
  sees the `release_v<old+1>` branch you already pushed, and checks that branch out and
  updates it rather than making a new version. Same for the eventual publish run.
- **It's idempotent.** Re-running with nothing changed on `master` makes no commit and prints
  `release_v<new> on <host> already matches your local branches — nothing to push.`
- **Book codes are optional after the first run.** `--release_branch_only` on its own just
  re-syncs whatever the branch already stages. Books you pass again are simply already there;
  passing a book that's in the *previous* release is still an error.
- **`master` is left alone completely** — no bump, no dates, no push. That means `master`'s
  `dublin_core.version` keeps reading as the last released version until you actually publish,
  and `issued`/`modified` get today's date on the day of the *release*, not the day you staged
  the branch. The version bump on `master` happens only on a real release run.
- **The staged branch is not a promise.** Nothing is tagged and nothing appears in the DCS
  catalog, so a staged `release_v<new>` can be re-synced, added to, or abandoned freely.
- Every repo is left checked out on `master` afterwards, ready for you to keep working.

Preview it first if you like — `--dry-run` combines with it and rolls everything back:

```bash
./release_union.py ISA JER --release_branch_only --dry-run
```

---

## 7. Resuming an interrupted release (`--resume`)

Use `--resume` when a run got past the push step (Phase B/C) but didn't finish — for example,
a network hiccup while creating releases, or one repo's release API call failed. Re-run with
the **same book arguments** (and same `--host`):

```bash
./release_union.py PSA HAB LAM --resume
```

In `--resume` mode the script:
- **Skips the "master is in sync with the remote" check** — the bump commit from the
  interrupted run is legitimately un-pushed at that point.
- **Does not roll back** (the remote already has state from the earlier run), so a failure
  leaves your local branches alone for another attempt.
- **Creates only the releases that don't already exist** (it checks each tag first).

Everything else `--resume` used to be needed for now happens on any run: because versions come
from the host's release tags (§4), an already-bumped `master` and an existing
`release_v<new>` branch are recognised as *a release in progress* and picked up — the script
re-bumps nothing, re-uses the branch (checking it out from the remote if your local copy is
gone), re-syncs it from `master`, and re-pushes idempotently. Double-bumping can't happen: the
new version is always `<newest released tag> + 1`.

---

## 8. Releasing to a different DCS (`--host`, e.g. QA)

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

## 9. Quick reference: full release procedure

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

### …or, staging the branch first (§6)

1. `./release_union.py <BOOKS> --release_branch_only` — pushes the `release_v<new>` branches
   and nothing else.
2. Work on `master` as usual. Re-run the same command whenever you want the staged branches
   refreshed from `master` (or to add another book).
3. When it's ready, run steps 3–7 above (`./release_union.py <BOOKS>`); it picks up the
   branches you already staged.

---

## 10. Version mechanics (reference)

- Versions are simple incrementing integers stored as strings (e.g. `'88'` → `'89'`).
- The **old** version is the highest `v<int>` tag **on the target host**, and the new version
  is always old + 1 (§4). `master`'s manifest version and any existing `release_v<n>` branch
  are used to detect an in-progress release, not to invent the number.
- On `master` (real releases only — never under `--release_branch_only`):
  `dublin_core.version` becomes the new version; the repo's own `source` entry version becomes
  the old version; `modified`/`issued` become today. Nothing else in the file is touched.
- On the `release_v<new>` branch, `master`'s `manifest.yaml` is the source of truth for
  **everything** — all of `dublin_core.*` and `checking.*` verbatim, and each book's `projects`
  entry copied from master's — except the four release-owned `dublin_core` fields (`version`,
  own `source` version, `issued`, `modified`) and *which* projects are listed.
- `release_v<old>` is the previous version's branch and is the base for the new one. All seven
  repos move in lockstep, so the "old version" is the same number across them — the script
  stops if they disagree.
