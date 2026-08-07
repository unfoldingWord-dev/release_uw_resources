#!/usr/bin/env python3
"""
release_union.py — Cut a union release of the seven unfoldingWord English repos.

Per-book repos (release off a release_v<new> branch):
    en_ult en_ust en_tn en_tq en_twl
Whole-repo resources (release off master):
    en_ta en_tw

Usage:
    ./release_union.py PSA HAB LAM            # release these NEW books (+ refresh all published ones)
    ./release_union.py PSA HAB LAM --dry-run  # do all local work, show diffs + notes, then roll everything back
    ./release_union.py PSA HAB LAM --release_branch_only   # create/refresh + push release_v<new> only; no release
    ./release_union.py PSA HAB LAM --yes      # skip the confirmation prompt before pushing
    ./release_union.py PSA HAB LAM --host qa.door43.org    # target a different DCS (push + releases)
    ./release_union.py PSA HAB LAM --resume   # finish a run that already pushed but didn't complete
    ./release_union.py PSA HAB LAM --unshallow  # fetch full history for shallow clones before pushing

Versions come from the target host, not from guesswork: the newest `v<int>` tag on the
remote is the "old" (last released) version, and the new version is always old+1. If a
release_v<old+1> branch and/or an already-bumped master are found, that in-progress
version is picked up and worked with instead of starting a new one.

What it does (see the README block the user approved):
  1. For every repo: checkout+update master, set dublin_core for the release
     (modified/issued=today, self source.version = old version, version = old+1), commit.
       - book repos:  "Update manifest.yaml for release v<new>"
       - ta/tw:       "Preparing for v<new> release"
     (Skipped entirely under --release_branch_only — master is not touched in that mode.)
  2. For book repos: create release_v<new> off release_v<old> (or check out the existing
     release_v<new>), then make its manifest.yaml master's manifest.yaml — every
     dublin_core.* and checking.* field verbatim from master — with only two differences:
     `projects` is trimmed to (previously-released books + whatever the branch already had
     + the new arg books) in master's sort order, and the four release-owned dublin_core
     fields are set (version = new, own source.version = old, issued/modified = today).
     Then refresh every book file listed there from master, add the new ones, and commit.
  3. Two-phase & atomic: ALL local work first. Only if every repo prepped cleanly do we
     push, then create the Gitea releases. Any failure during prep rolls everything back
     and pushes nothing.
  4. Create a prerelease on the DCS host (default git.door43.org; override with --host)
     for each repo (book repos -> release_v<new>, ta/tw -> master) using GITEA_TOKEN.

--release_branch_only stops after step 2 + the push: it creates release_v<new> if needed,
(re)syncs it from master, pushes just that branch, and creates NO tag and NO release. It
never touches master and never calls the release API (so no GITEA_TOKEN is needed). Re-run
it as often as you like — it is idempotent — to keep the staged release branch up to date
with master while the release is being prepared. en_ta/en_tw have no release branch (they
release off master), so they are left alone in this mode. When you are ready to publish,
run the same command without the flag: it picks up the existing release_v<new> branches,
bumps master, re-syncs, pushes and creates the prereleases.

Any of the seven repos that is missing from this directory is cloned from `host` first
(shallow: --depth 1 --no-single-branch, so master and all release_v<version> branch tips
are present without full history). This lets you run in an empty directory, or delete and
re-clone repos at will. Because the clone uses `host`, a QA run never touches production.
--unshallow fetches full history for any shallow repo up front (use it if your DCS server
ever rejects pushes from a shallow clone).

--host targets a different DCS instance (e.g. qa.door43.org). It is applied consistently
to clone, git push AND the release API: for a non-default host a git remote named after the
host (e.g. "qa") is created from origin's URL with the host swapped, and pushes/fetches
use it.

--resume re-runs idempotently to finish a run that already pushed (so rollback would be
wrong): it skips the master-in-sync check (the earlier run's bump commit is legitimately
un-pushed at that point), re-pushes (no-ops what's current), and creates only the releases
that don't already exist. Rollback is disabled in --resume mode.
"""

import os
import sys
import json
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Bootstrap: make sure ruamel.yaml is importable (use a local venv if needed).
# ---------------------------------------------------------------------------
def _ensure_ruamel():
    try:
        import ruamel.yaml  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("RELEASE_UNION_BOOTSTRAPPED") == "1":
        sys.exit("ERROR: ruamel.yaml is still missing after venv bootstrap.")
    venv = BASE / ".release-venv"
    py = venv / "bin" / "python"
    if not py.exists():
        print("Creating venv and installing ruamel.yaml ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "--quiet", "ruamel.yaml"], check=True)
    env = dict(os.environ, RELEASE_UNION_BOOTSTRAPPED="1")
    os.execve(str(py), [str(py), str(Path(__file__).resolve())] + sys.argv[1:], env)


_ensure_ruamel()

import io  # noqa: E402
from ruamel.yaml import YAML  # noqa: E402
from ruamel.yaml.comments import CommentedSeq  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOOK_REPOS = ["en_ult", "en_ust", "en_tn", "en_tq", "en_twl"]
WHOLE_REPOS = ["en_ta", "en_tw"]
ALL_REPOS = BOOK_REPOS + WHOLE_REPOS

BRANCH_PREFIX = "release_v"
DEFAULT_HOST = "git.door43.org"
TODAY = date.today().isoformat()  # YYYY-MM-DD


def api_base(host):
    return f"https://{host}/api/v1/repos/unfoldingWord"


def make_yaml():
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    # ta/tw use explicit `null`; keep it explicit so untouched lines don't churn.
    y.representer.add_representer(
        type(None),
        lambda r, d: r.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    return y


def q(value):
    """Single-quote a scalar the way the manifests do (e.g. version: '88')."""
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    return SingleQuotedScalarString(str(value))


# ---------------------------------------------------------------------------
# Small shell / git helpers
# ---------------------------------------------------------------------------
class ReleaseError(Exception):
    pass


def run(args, cwd, capture=False, check=True):
    """Run a command; raise ReleaseError on failure."""
    res = subprocess.run(
        args, cwd=str(cwd),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if check and res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise ReleaseError(f"`{' '.join(args)}` failed in {cwd}:\n{err}")
    return (res.stdout or "").strip() if capture else ""


def git(repo, *args, capture=False, check=True):
    return run(["git", *args], BASE / repo, capture=capture, check=check)


def current_branch(repo):
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD", capture=True)


def branch_exists(repo, name):
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
               capture=True, check=False) != ""


def remote_branch_exists(repo, name, remote="origin"):
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{name}",
               capture=True, check=False) != ""


def branch_ref(repo, name, remote):
    """A readable ref for `name`: the local branch if we have it, else the remote one."""
    if branch_exists(repo, name):
        return name
    if remote_branch_exists(repo, name, remote):
        return f"{remote}/{name}"
    raise ReleaseError(f"{repo}: branch {name} does not exist locally or on '{remote}'.")


def checkout_branch(repo, name, remote):
    """Check out `name`, creating it from the remote if only the remote ref exists, and
    fast-forwarding from the remote when it has one. Raises if the branch is nowhere."""
    if branch_exists(repo, name):
        git(repo, "checkout", name)
        if remote_branch_exists(repo, name, remote):
            git(repo, "merge", "--ff-only", f"{remote}/{name}")
    elif remote_branch_exists(repo, name, remote):
        git(repo, "checkout", "-b", name, f"{remote}/{name}")
    else:
        raise ReleaseError(f"{repo}: branch {name} does not exist locally or on '{remote}'.")


_FETCHED = set()


def fetch_once(repo, remote):
    """`git fetch` a repo's remote at most once per run (several steps want it fresh)."""
    if (repo, remote) in _FETCHED:
        return
    git(repo, "fetch", remote)
    _FETCHED.add((repo, remote))


def _int_versions(names, prefix):
    """{5, 6} from names like ('release_v5', 'release_v6', 'release_v6.1') for the
    release_v prefix. Only plain integers count — v83.1/v8-/ver6 style tags are patch or
    junk refs and never take part in integer version math."""
    out = set()
    for name in names:
        if name.startswith(prefix):
            rest = name[len(prefix):]
            if rest.isdigit():
                out.add(int(rest))
    return out


def released_versions(repo, remote):
    """Integer versions released on `remote`, read from its v<int> tags.

    The remote is asked directly (`git ls-remote`) rather than trusting local tags: local
    tags are a mix of whatever host was fetched last, while a release on the target host
    always has a tag there. A tag with no release still counts as taken, which is the safe
    direction (we never reuse the number)."""
    out = git(repo, "ls-remote", "--tags", remote, "refs/tags/v*", capture=True)
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.endswith("^{}"):      # peeled annotated tag
            ref = ref[:-3]
        names.append(ref.rsplit("/", 1)[-1])
    return _int_versions(names, "v")


def release_branch_versions(repo, remote):
    """Integer versions that have a release_v<n> branch, local or on `remote`."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)",
              f"refs/heads/{BRANCH_PREFIX}*", f"refs/remotes/{remote}/{BRANCH_PREFIX}*",
              capture=True)
    names = [r.rsplit("/", 1)[-1] for r in out.splitlines() if r]
    return _int_versions(names, BRANCH_PREFIX)


def branch_needs_push(repo, name, remote):
    """True if `name` is missing from `remote` or differs from what's there."""
    if not remote_branch_exists(repo, name, remote):
        return True
    return (git(repo, "rev-parse", name, capture=True)
            != git(repo, "rev-parse", f"{remote}/{name}", capture=True))


def remote_name_for_host(host):
    """Remote name to use for a host: 'origin' for the default, else the host label."""
    return "origin" if host == DEFAULT_HOST else host.split(".")[0]


def ensure_remote(repo, host):
    """Make sure a git remote exists for `host`; return its name.

    For a non-default host the remote URL is origin's URL with the host swapped in
    (preserving SSH/HTTPS form, user, and org/path). The remote is added/updated in
    the repo's git config (idempotent)."""
    name = remote_name_for_host(host)
    if name == "origin":
        return name
    origin_url = git(repo, "remote", "get-url", "origin", capture=True)
    target_url = origin_url.replace(DEFAULT_HOST, host)
    existing = git(repo, "remote", "get-url", name, capture=True, check=False)
    if existing == "":
        git(repo, "remote", "add", name, target_url)
    elif existing != target_url:
        git(repo, "remote", "set-url", name, target_url)
    return name


def clone_url(repo, host):
    """SSH clone URL for a repo on a given DCS host (matches origin's form)."""
    return f"git@{host}:unfoldingWord/{repo}.git"


def ensure_repo_cloned(repo, host):
    """Clone `repo` from `host` if it isn't already present. Returns True if cloned.

    The clone is shallow (--depth 1) but fetches every branch tip
    (--no-single-branch), so master AND the release_v<version> branches are all
    available without pulling full history. Cloning from `host` keeps everything
    (e.g. a QA run) off the production server."""
    path = BASE / repo
    if (path / ".git").exists():
        return False
    if path.exists():
        raise ReleaseError(f"{path} exists but is not a git repository.")
    url = clone_url(repo, host)
    print(f"   cloning {repo} from {host} (shallow, all branches) ...")
    run(["git", "clone", "--depth", "1", "--no-single-branch", url, repo], BASE)
    return True


def is_shallow(repo):
    return git(repo, "rev-parse", "--is-shallow-repository", capture=True) == "true"


def unshallow_repo(repo, remote):
    """Convert a shallow repo to full history (safety before pushing). No-op if
    the repo already has full history."""
    if not is_shallow(repo):
        return False
    print(f"   unshallowing {repo} ...")
    git(repo, "fetch", "--unshallow", remote)
    return True


def show_file(repo, ref, relpath):
    """Return the contents of a file at a given ref."""
    return git(repo, "show", f"{ref}:{relpath}", capture=True)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def load_env_token():
    env_path = BASE / ".env"
    if not env_path.exists():
        raise ReleaseError(f"No .env file at {env_path}")
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "GITEA_TOKEN":
            return val.strip().strip('"').strip("'")
    raise ReleaseError("GITEA_TOKEN not found in .env")


# ---------------------------------------------------------------------------
# Manifest manipulation
# ---------------------------------------------------------------------------
def load_manifest(repo, ref=None):
    """Load manifest.yaml from disk (ref=None) or from a git ref."""
    y = make_yaml()
    if ref is None:
        text = (BASE / repo / "manifest.yaml").read_text()
    else:
        text = show_file(repo, ref, "manifest.yaml")
    return y, y.load(text)


def dump_manifest(repo, yaml_obj, data):
    buf = io.StringIO()
    yaml_obj.dump(data, buf)
    (BASE / repo / "manifest.yaml").write_text(buf.getvalue())


def set_dublin_core(dc, old_version, new_version):
    """Set a dublin_core mapping (in place) to the state a v<new_version> release wants.

    modified/issued -> today, the repo's own source entry version -> old_version,
    version -> new_version. Idempotent: re-applying it to an already-bumped manifest only
    ever moves the dates, so it is safe to call on every run."""
    dc["modified"] = q(TODAY)
    dc["issued"] = q(TODAY)
    self_id = str(dc["identifier"])
    bumped = False
    for src in dc.get("source", []):
        if str(src.get("identifier")) == self_id:
            src["version"] = q(old_version)
            bumped = True
    if not bumped:
        raise ReleaseError(
            f"No source entry with identifier '{self_id}' to bump (source.version)."
        )
    dc["version"] = q(new_version)


# ---------------------------------------------------------------------------
# Release-notes generation (shared by all book repos)
# ---------------------------------------------------------------------------
def book_code(project):
    return str(project["identifier"]).upper()


def in_category(project, cat):
    cats = project.get("categories") or []
    return cat in cats


def grammatical_join(items):
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def build_release_notes(new_version, release_projects, master_projects, new_codes):
    """Build the Markdown release-notes body for the book packages."""
    ot_total = sum(1 for p in master_projects if in_category(p, "bible-ot"))
    nt_total = sum(1 for p in master_projects if in_category(p, "bible-nt"))

    ot = [p for p in release_projects if in_category(p, "bible-ot")]
    nt = [p for p in release_projects if in_category(p, "bible-nt")]

    def hdr_count(n, total):
        return f"{n} [ALL]" if n == total else f"{n}"

    # "What's New": the newly released books, in canonical (sort) order.
    by_id = {str(p["identifier"]).lower(): p for p in master_projects}
    new_sorted = sorted(
        (by_id[c.lower()] for c in new_codes),
        key=lambda p: int(p["sort"]),
    )
    new_phrases = [f"{p['title']} ({book_code(p)})" for p in new_sorted]
    whats_new = (
        f"This release is the first release of {grammatical_join(new_phrases)}."
        if new_phrases else
        "This release contains updates to the previously published books."
    )

    lines = []
    lines.append(f"# v{new_version} Release of unfoldingWord Book Packages")
    lines.append("")
    lines.append("## What's New in this Release")
    lines.append("")
    lines.append(f"- {whats_new}")
    lines.append("")
    lines.append("## All Book Packages in this Release")
    lines.append("")
    lines.append(
        "The following books have undergone a Book Package consistency check "
        "and are included in this release:"
    )
    lines.append("")
    lines.append(f"### Old Testament Books ({hdr_count(len(ot), ot_total)}):")
    lines.append("")
    for p in ot:
        lines.append(f"- {p['title']} ({book_code(p)})")
    lines.append("")
    lines.append(f"### New Testament Books ({hdr_count(len(nt), nt_total)}):")
    lines.append("")
    for p in nt:
        lines.append(f"- {p['title']} ({book_code(p)})")
    return "\n".join(lines)


def changelog_section(old_version, new_version):
    """The 'Changes Since the Previous Release' block appended to each release body.

    The compare link is relative to the repo (Gitea resolves release-body links
    against the repo's own path), so it works on whatever DCS instance and repo the
    release was created on — production or QA."""
    return (
        f"\n\n## Changes Since the Previous Release (v{old_version})\n\n"
        f"- [See a detailed, line-by-line list of everything that changed in version "
        f"{new_version}](/compare/v{old_version}...v{new_version})."
    )


# ---------------------------------------------------------------------------
# Per-repo preparation (local only — no push, no API)
# ---------------------------------------------------------------------------
def ensure_clean(repo):
    # Only tracked (modified/staged) changes block us; untracked scratch files are
    # safe (checkout/reset never touch them).
    status = git(repo, "status", "--porcelain", "--untracked-files=no", capture=True)
    if status:
        raise ReleaseError(
            f"{repo} has uncommitted changes to tracked files — commit/stash them "
            f"first:\n{status}"
        )


def ensure_master_synced(repo, remote):
    """Fail if local master has commits not yet pushed to the remote.

    Such commits would be swept into the release push, and they signal the local
    repo isn't in the known-published state a release assumes."""
    fetch_once(repo, remote)
    if not remote_branch_exists(repo, "master", remote):
        return
    ahead = git(repo, "rev-list", f"{remote}/master..master", capture=True, check=False)
    if ahead:
        n = len(ahead.splitlines())
        raise ReleaseError(
            f"{repo}: local master has {n} commit(s) not pushed to '{remote}'. "
            f"Push or discard them before releasing (a release would publish them)."
        )


def bump_commit_message(repo, version, refresh=False):
    if refresh:
        return f"Refresh manifest.yaml dates for v{version} release"
    return (f"Update manifest.yaml for release v{version}"
            if repo in BOOK_REPOS else
            f"Preparing for v{version} release")


def sync_master(repo, remote):
    """Check out master and fast-forward it from the remote. No commits, no edits."""
    fetch_once(repo, remote)
    git(repo, "checkout", "master")
    if remote_branch_exists(repo, "master", remote):
        git(repo, "merge", "--ff-only", f"{remote}/master")


def plan_repo(repo, remote, codes):
    """Work out which version this repo is releasing, and validate the book codes.

    Read-only — call it after sync_master() so master's manifest is current. Returns a
    plan dict. The last released version is the newest v<int> tag on the remote, and the
    new version is always that + 1. Master already being bumped to old+1, or a
    release_v<old+1> branch already existing, means a release is in progress: we pick it
    up and keep working with it rather than starting a new version."""
    _, data = load_manifest(repo, ref="master")
    raw = str(data["dublin_core"]["version"])
    if not raw.isdigit():
        raise ReleaseError(
            f"{repo}: master's dublin_core.version is '{raw}', not an integer — this "
            f"script only handles integer versions."
        )
    manifest_version = int(raw)

    tagged = released_versions(repo, remote)
    branched = release_branch_versions(repo, remote) if repo in BOOK_REPOS else set()

    if tagged:
        old_version = max(tagged)
    else:
        old_version = manifest_version
        print(f"   {repo}: no v<n> tags on '{remote}' — treating master's manifest "
              f"version v{manifest_version} as the last release.")

    if manifest_version < old_version:
        raise ReleaseError(
            f"{repo}: master's manifest says v{manifest_version} but v{old_version} is "
            f"already released on '{remote}'. Fix master's manifest by hand first."
        )

    new_version = old_version + 1
    ahead = sorted(v for v in ({manifest_version} | branched) if v > new_version)
    if ahead:
        raise ReleaseError(
            f"{repo}: found v{'/v'.join(str(v) for v in ahead)} ahead of the next "
            f"release v{new_version} (newest release on '{remote}' is v{old_version}). "
            f"Sort that out by hand before releasing."
        )

    plan = {
        "old_version": str(old_version),
        "new_version": str(new_version),
        "master_bumped": manifest_version == new_version,
        "branch_in_progress": new_version in branched,
        "released_ids": set(),
    }

    if repo in BOOK_REPOS:
        master_projects = list(data["projects"])
        master_ids = {str(p["identifier"]).lower() for p in master_projects}
        # Previously released books = the projects on the old release branch. Validation
        # is against *that* branch, not the in-progress one, so re-passing the same book
        # codes on a --release_branch_only re-run is fine.
        old_branch = f"{BRANCH_PREFIX}{old_version}"
        _, old_rel = load_manifest(repo, ref=branch_ref(repo, old_branch, remote))
        plan["released_ids"] = {str(p["identifier"]).lower() for p in old_rel["projects"]}
        for code in codes:
            if code.lower() not in master_ids:
                raise ReleaseError(f"{repo}: book '{code}' not found in master projects.")
            if code.lower() in plan["released_ids"]:
                raise ReleaseError(
                    f"{repo}: book '{code}' is already published in v{old_version} "
                    f"(it must be a NEW book)."
                )
    return plan


def bump_master(repo, plan):
    """Set master's dublin_core for this release and commit if anything changed.

    Returns (orig_sha, committed); orig_sha is None when there was nothing to commit."""
    orig_sha = git(repo, "rev-parse", "HEAD", capture=True)
    y, data = load_manifest(repo)
    set_dublin_core(data["dublin_core"], plan["old_version"], plan["new_version"])
    dump_manifest(repo, y, data)
    if git(repo, "diff", "--name-only", capture=True) == "":
        return None, False
    git(repo, "add", "manifest.yaml")
    git(repo, "commit", "-m",
        bump_commit_message(repo, plan["new_version"], refresh=plan["master_bumped"]))
    return orig_sha, True


def sync_release_branch(repo, plan, codes, remote):
    """Create release_v<new> if needed, then (re)sync it from master.

    Creating it branches off release_v<old>; an existing release_v<new> (from an earlier
    --release_branch_only run, or an interrupted release) is checked out and updated in
    place. Either way the branch ends up with master's dublin_core (at the release
    version/dates), a projects list of (previously released + already staged + newly
    requested) books in master's order, and every one of those book files refreshed from
    master.

    Returns an info dict; safe to re-run (it only commits when something changed)."""
    new_branch = f"{BRANCH_PREFIX}{plan['new_version']}"
    created = not (branch_exists(repo, new_branch)
                   or remote_branch_exists(repo, new_branch, remote))
    if created:
        checkout_branch(repo, f"{BRANCH_PREFIX}{plan['old_version']}", remote)
        git(repo, "checkout", "-b", new_branch)
        branch_orig_sha = None
    else:
        checkout_branch(repo, new_branch, remote)
        branch_orig_sha = git(repo, "rev-parse", "HEAD", capture=True)

    # What the branch currently stages, read before we rebuild its manifest from master.
    _, branch_data = load_manifest(repo)  # working tree == this branch's content
    staged_ids = {str(p["identifier"]).lower() for p in branch_data["projects"]}

    # The release branch's manifest IS master's manifest: every dublin_core.* and
    # checking.* field (and anything else at the top level) comes straight from master, so
    # edits made on master between releases always land in the release. Only two things
    # differ: `projects` is trimmed to the released books, and the four release-owned
    # dublin_core fields (version, own source.version, issued, modified) are set below.
    y, rel_data = load_manifest(repo, ref="master")
    master_projects = list(rel_data["projects"])

    released_ids = plan["released_ids"]
    target_ids = released_ids | staged_ids | {c.lower() for c in codes}

    set_dublin_core(rel_data["dublin_core"], plan["old_version"], plan["new_version"])
    new_projects = CommentedSeq(
        p for p in master_projects if str(p["identifier"]).lower() in target_ids
    )
    rel_data["projects"] = new_projects
    dump_manifest(repo, y, rel_data)

    # Refresh every book file in the release from master (existing + new).
    for p in new_projects:
        relpath = str(p["path"]).lstrip("./")
        content = git(repo, "show", f"master:{relpath}", capture=True)
        dest = BASE / repo / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content + ("\n" if not content.endswith("\n") else ""))
        git(repo, "add", relpath)
    git(repo, "add", "manifest.yaml")

    # What this release adds over the last one, in master's (canonical) order. Derived
    # from the branch rather than from argv so it stays right across re-runs.
    added_codes = [book_code(p) for p in new_projects
                   if str(p["identifier"]).lower() not in released_ids]

    committed = git(repo, "diff", "--cached", "--name-only", capture=True) != ""
    if committed:
        if not created:
            message = f"Updating {new_branch} from master"
        elif added_codes:
            message = f"Releasing {' '.join(added_codes)}"
        else:
            message = f"Preparing {new_branch}"
        git(repo, "commit", "-m", message)
    return {
        "new_branch": new_branch,
        "branch_created": created,
        "branch_orig_sha": branch_orig_sha,
        "branch_commit": committed,
        "rel_projects": list(new_projects),
        "master_projects": master_projects,
        "added_codes": added_codes,
    }


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def rollback(state):
    print("\n!! Rolling back all local changes (nothing was pushed) ...")
    for repo, info in state.items():
        try:
            if current_branch(repo) != "master":
                git(repo, "checkout", "--force", "master", check=False)
            nb = info.get("new_branch")
            if nb and branch_exists(repo, nb):
                if info.get("branch_created"):
                    # We made this branch — remove it.
                    git(repo, "branch", "-D", nb, check=False)
                elif info.get("branch_orig_sha"):
                    # It predates this run — put it back where we found it.
                    git(repo, "branch", "-f", nb, info["branch_orig_sha"], check=False)
            orig = info.get("orig_sha")
            if orig:
                git(repo, "reset", "--hard", orig, check=False)
            print(f"   {repo}: reverted")
        except Exception as e:  # noqa: BLE001
            print(f"   {repo}: rollback issue: {e}")


# ---------------------------------------------------------------------------
# Push + Gitea release
# ---------------------------------------------------------------------------
def push_repo(repo, info, remote, branch_only):
    # --release_branch_only never commits to master, so there is nothing to push there.
    if not branch_only:
        git(repo, "push", remote, "master")
    if info.get("new_branch"):
        git(repo, "push", remote, info["new_branch"])


def release_exists(repo, tag, host, token):
    """True if a release already exists for `tag` on `host` (for idempotent resume)."""
    url = f"{api_base(host)}/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"token {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise ReleaseError(f"{repo}: release lookup {e.code}: {e.read().decode('utf-8','replace')}")


def create_release(repo, info, token, host):
    new_version = info["new_version"]
    tag = f"v{new_version}"
    if release_exists(repo, tag, host, token):
        return f"{tag} (already exists, skipped)"

    target = info["new_branch"] if repo in BOOK_REPOS else "master"
    base_body = info["notes"] if repo in BOOK_REPOS else f"# v{new_version} Release"
    body = base_body + changelog_section(info["old_version"], new_version)
    payload = {
        "body": body,
        "draft": False,
        "name": tag,
        "prerelease": True,
        "tag_name": tag,
        "target_commitish": target,
    }
    url = f"{api_base(host)}/{repo}/releases"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("html_url") or data.get("url") or tag
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise ReleaseError(f"{repo}: release API {e.code}: {detail}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
USAGE = ("Usage: release_union.py [BOOK ...] [--release_branch_only] [--host HOST] "
         "[--resume] [--unshallow] [--dry-run] [--yes]")


def parse_args(argv):
    host = DEFAULT_HOST
    dry_run = assume_yes = resume = unshallow = branch_only = False
    codes = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--host":
            i += 1
            if i >= len(argv):
                sys.exit("--host requires a value, e.g. --host qa.door43.org")
            host = argv[i]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a == "--dry-run":
            dry_run = True
        elif a == "--yes":
            assume_yes = True
        elif a == "--resume":
            resume = True
        elif a == "--unshallow":
            unshallow = True
        elif a in ("--release_branch_only", "--release-branch-only"):
            branch_only = True
        elif a.startswith("--"):
            sys.exit(f"Unknown option: {a}\n{USAGE}")
        else:
            codes.append(a.upper())
        i += 1
    return host, dry_run, assume_yes, resume, unshallow, branch_only, codes


def restore_master(repos):
    """Leave every repo checked out on master — the release branch is pushed, and nobody
    wants to come back later to a working copy sitting on a release branch."""
    for repo in repos:
        if current_branch(repo) != "master":
            git(repo, "checkout", "master", check=False)


def main():
    (host, dry_run, assume_yes, resume, unshallow,
     branch_only, codes) = parse_args(sys.argv[1:])

    if "." not in host:
        sys.exit(f"--host must be a full hostname (e.g. qa.door43.org), not '{host}'.")

    # Only the book repos have a release branch; en_ta/en_tw release off master, so
    # --release_branch_only has nothing to stage for them and leaves them alone.
    target_repos = BOOK_REPOS if branch_only else ALL_REPOS

    print(f"Mode: {'release branch only (no tag, no release)' if branch_only else 'full release'}"
          f"   (date {TODAY}, host {host}{', resume' if resume else ''})")
    print(f"New books: {' '.join(codes) if codes else '(none given — refresh only)'}")

    token = None
    if not dry_run and not branch_only:
        token = load_env_token()

    # Safety gate: clone any missing repos (from `host`), make sure each has its host
    # remote and is clean — before any work.
    print("\n== Checking repos ==")
    for repo in ALL_REPOS:
        if not ensure_repo_cloned(repo, host):
            print(f"   {repo}: present")
    remotes = {repo: ensure_remote(repo, host) for repo in ALL_REPOS}
    # --unshallow: fetch full history for any shallow repo so pushes never rely on
    # a shallow clone (use it if your DCS server rejects shallow pushes).
    if unshallow:
        for repo in ALL_REPOS:
            unshallow_repo(repo, remotes[repo])
    for repo in ALL_REPOS:
        ensure_clean(repo)

    # ---- Planning: read-only. Decide versions, validate the book codes. ----
    print("\n== Planning ==")
    plans = {}
    for repo in ALL_REPOS:
        sync_master(repo, remotes[repo])
        # Unpushed local master commits would leak unpublished content into the release.
        # Skipped in --resume (the interrupted run's bump commit is legitimately unpushed)
        # and, for --release_branch_only, for the repos we aren't touching.
        if not resume and repo in target_repos:
            ensure_master_synced(repo, remotes[repo])
        plans[repo] = plan_repo(repo, remotes[repo], codes)
        p = plans[repo]
        found = []
        if p["master_bumped"]:
            found.append("master already bumped")
        if p["branch_in_progress"]:
            found.append(f"{BRANCH_PREFIX}{p['new_version']} exists")
        print(f"   {repo}: v{p['old_version']} -> v{p['new_version']}"
              + (f"   (in progress: {', '.join(found)})" if found else ""))

    versions = sorted({p["new_version"] for p in plans.values()}, key=int)
    if len(versions) > 1:
        detail = ", ".join(f"{r}=v{plans[r]['new_version']}" for r in ALL_REPOS)
        sys.exit(f"\nERROR: the repos disagree on the next version ({detail}). All seven "
                 f"move in lockstep, so sort that out before releasing.")
    new_version = versions[0]
    in_progress = any(plans[r]["branch_in_progress"] for r in BOOK_REPOS)

    if not codes and not (branch_only or resume or in_progress):
        sys.exit(f"\nNo book codes given, and no release_v{new_version} branch exists yet. "
                 f"Pass the NEW book(s) to release (e.g. PSA HAB LAM), or use "
                 f"--release_branch_only to stage the branch.\n{USAGE}")

    # In --resume mode a prior run already pushed, so rolling back would undo
    # remote state. Re-running is idempotent instead.
    allow_rollback = not resume
    state = {}  # repo -> info dict (for rollback + later phases)

    # ---- Phase A: all local work ------------------------------------------
    try:
        notes_body = None
        for repo in target_repos:
            print(f"\n== Preparing {repo} ==")
            plan = plans[repo]
            info = {"old_version": plan["old_version"],
                    "new_version": plan["new_version"],
                    "orig_sha": None, "master_commit": False,
                    "new_branch": None, "branch_created": False,
                    "branch_orig_sha": None, "branch_commit": False,
                    "added_codes": [], "notes": None}
            state[repo] = info

            if branch_only:
                print("   master: untouched (--release_branch_only)")
            else:
                orig_sha, committed = bump_master(repo, plan)
                info["orig_sha"] = orig_sha
                info["master_commit"] = committed
                print(f"   master: v{plan['old_version']} -> v{plan['new_version']}"
                      + ("" if committed else " (already current, nothing to commit)"))

            if repo in BOOK_REPOS:
                res = sync_release_branch(repo, plan, codes, remotes[repo])
                for key in ("new_branch", "branch_created", "branch_orig_sha",
                            "branch_commit", "added_codes"):
                    info[key] = res[key]
                # Generate the shared notes once (book sets are identical across repos).
                if notes_body is None:
                    notes_body = build_release_notes(
                        plan["new_version"], res["rel_projects"],
                        res["master_projects"], res["added_codes"],
                    )
                info["notes"] = notes_body
                print(f"   branch {res['new_branch']}: "
                      f"{'created' if res['branch_created'] else 'existing'}, "
                      f"{len(res['rel_projects'])} projects, files refreshed from master"
                      + ("" if res["branch_commit"] else " (already current, nothing to commit)"))
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR during preparation: {e}")
        if allow_rollback:
            rollback(state)
        else:
            print("(--resume: not rolling back; fix the issue and re-run.)")
        sys.exit(1)

    # ---- Review -----------------------------------------------------------
    ex = BOOK_REPOS[0]
    print("\n" + "=" * 70)
    if branch_only:
        print(f"PREPARED {BRANCH_PREFIX}{new_version} locally for "
              f"{len(target_repos)} book repos. No release will be created.")
    else:
        print(f"PREPARED v{new_version} locally for all {len(ALL_REPOS)} repos.")
    print("=" * 70)
    notes_label = ("PREVIEW ONLY — nothing gets published in this mode"
                   if branch_only else "book packages")
    print(f"\n--- Release notes ({notes_label}) ---\n")
    print(state[ex]["notes"])
    print(changelog_section(state[ex]["old_version"], state[ex]["new_version"]))
    if not branch_only:
        print(f"\n(The 'Changes Since' link above is per-repo; each release gets its own. "
              f"en_ta/en_tw bodies are '# v{new_version} Release' + the same block.)")
    print("\n--- What changed locally ---")
    for repo in target_repos:
        info = state[repo]
        print(f"\n# {repo}")
        shown = False
        for label, ref in (("master", "master"), (info["new_branch"], info["new_branch"])):
            committed = info["master_commit"] if ref == "master" else info["branch_commit"]
            if ref and committed:
                print(f"   -- {label} --")
                print(git(repo, "show", "--stat", "--oneline", ref, capture=True))
                shown = True
        if not shown:
            print("   (nothing changed — already up to date with master)")

    if dry_run:
        print("\n--dry-run: rolling everything back, nothing pushed.")
        if allow_rollback:
            rollback(state)
        else:
            print("(--resume: nothing to roll back.)")
        return

    # In branch-only mode there is nothing to do if every branch already matches the host.
    if branch_only:
        pending = [r for r in target_repos
                   if branch_needs_push(r, state[r]["new_branch"], remotes[r])]
        if not pending:
            print(f"\n{BRANCH_PREFIX}{new_version} on {host} already matches your local "
                  f"branches — nothing to push.")
            restore_master(target_repos)
            return
        prompt = (f"\nPush {BRANCH_PREFIX}{new_version} for {', '.join(pending)} to "
                  f"{host}? No tag or release is created. [y/N] ")
    else:
        prompt = (f"\nPush all branches and create v{new_version} prereleases on "
                  f"{host}? [y/N] ")

    if not assume_yes:
        if input(prompt).strip().lower() != "y":
            if allow_rollback:
                rollback(state)
                print("Aborted by user; rolled back.")
            else:
                print("Aborted by user (--resume: nothing rolled back).")
            return

    # ---- Phase B: push (point of no return) -------------------------------
    print("\n== Pushing ==")
    pushed = []
    try:
        for repo in target_repos:
            push_repo(repo, state[repo], remotes[repo], branch_only)
            pushed.append(repo)
            print(f"   pushed {repo}")
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR while pushing: {e}")
        print(f"Pushed so far: {', '.join(pushed) or 'none'}.")
        if branch_only:
            print("Local branches are intact; just re-run the same command to finish.")
        else:
            print("Local branches are intact. Re-run with --resume (same args) to "
                  "finish pushing and create the releases. NOT rolling back master.")
        sys.exit(1)

    if branch_only:
        restore_master(target_repos)
        print(f"\nDone. {BRANCH_PREFIX}{new_version} is pushed to {host} for: "
              f"{', '.join(target_repos)}.")
        print(f"No tag and no release were created, master was not touched, and "
              f"{'/'.join(WHOLE_REPOS)} were left alone entirely.")
        print("Re-run the same command any time to re-sync the branch(es) from master.")
        publish = " ".join(["./release_union.py"] + codes
                           + ([f"--host {host}"] if host != DEFAULT_HOST else []))
        print(f"To publish v{new_version} when it's ready:  {publish}")
        return

    # ---- Phase C: Gitea releases ------------------------------------------
    print("\n== Creating releases ==")
    failures = []
    for repo in ALL_REPOS:
        try:
            url = create_release(repo, state[repo], token, host)
            print(f"   {repo}: {url}")
        except Exception as e:  # noqa: BLE001
            failures.append(repo)
            print(f"   {repo}: FAILED — {e}")

    restore_master(ALL_REPOS)
    if failures:
        print(f"\nReleases failed for: {', '.join(failures)}. Branches are pushed; "
              "re-run with --resume (same args) to retry the releases.")
        sys.exit(1)
    print(f"\nDone. v{new_version} released for all repos on {host}.")
    print("NOTE: these are PRERELEASES. Promote each one to production on the DCS "
          "(edit the release in all 7 repos and uncheck 'This is a pre-release').")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
