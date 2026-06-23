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
    ./release_union.py PSA HAB LAM --yes      # skip the confirmation prompt before pushing
    ./release_union.py PSA HAB LAM --host qa.door43.org    # target a different DCS (push + releases)
    ./release_union.py PSA HAB LAM --resume   # finish a run that already pushed but didn't complete
    ./release_union.py PSA HAB LAM --unshallow  # fetch full history for shallow clones before pushing

What it does (see the README block the user approved):
  1. For every repo: checkout+update master, bump dublin_core (modified/issued=today,
     self source.version = old version, version = old+1), commit.
       - book repos:  "Update manifest.yaml for release v<new>"
       - ta/tw:       "Preparing for v<new> release"
  2. For book repos: branch release_v<new> off release_v<old>, set its dublin_core
     identical to master, trim projects to (previously-released books + the new arg
     books) in master's sort order, refresh every book file from master + add the new
     ones, commit "Releasing <CODES>".
  3. Two-phase & atomic: ALL local work first. Only if every repo prepped cleanly do we
     push, then create the Gitea releases. Any failure during prep rolls everything back
     and pushes nothing.
  4. Create a prerelease on the DCS host (default git.door43.org; override with --host)
     for each repo (book repos -> release_v<new>, ta/tw -> master) using GITEA_TOKEN.

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
wrong): it skips the master bump if already done, reuses an existing release_v<new> branch
(pulling it from the remote if needed), re-pushes (no-ops what's current), and creates only
the releases that don't already exist. Rollback is disabled in --resume mode.
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


def bump_dublin_core(data):
    """Mutate dublin_core in place; return (old_version, new_version)."""
    dc = data["dublin_core"]
    old_version = str(dc["version"])
    new_version = str(int(old_version) + 1)
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
    return old_version, new_version


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
    git(repo, "fetch", remote)
    if not remote_branch_exists(repo, "master", remote):
        return
    ahead = git(repo, "rev-list", f"{remote}/master..master", capture=True, check=False)
    if ahead:
        n = len(ahead.splitlines())
        raise ReleaseError(
            f"{repo}: local master has {n} commit(s) not pushed to '{remote}'. "
            f"Push or discard them before releasing (a release would publish them)."
        )


def bump_commit_message(repo, version):
    return (f"Update manifest.yaml for release v{version}"
            if repo in BOOK_REPOS else
            f"Preparing for v{version} release")


def master_versions(repo):
    """Inspect master to decide versions and whether it's already bumped.

    Returns (old_version, new_version, already_bumped). 'already_bumped' is true
    when master's HEAD is our bump commit (i.e. a prior run got at least this far)."""
    _, data = load_manifest(repo, ref="master")
    mv = str(data["dublin_core"]["version"])
    head_subj = git(repo, "log", "-1", "--format=%s", "master", capture=True)
    if head_subj == bump_commit_message(repo, mv):
        return str(int(mv) - 1), mv, True
    return mv, str(int(mv) + 1), False


def prep_master(repo, remote, resume):
    """Update master's manifest and commit. Returns (old, new, orig_master_sha).

    orig_master_sha is None when master was already bumped (nothing to roll back)."""
    git(repo, "fetch", remote)
    git(repo, "checkout", "master")
    if remote_branch_exists(repo, "master", remote):
        git(repo, "merge", "--ff-only", f"{remote}/master")

    old_version, new_version, already_bumped = master_versions(repo)
    if already_bumped:
        if not resume:
            raise ReleaseError(
                f"{repo}: master is already bumped to v{new_version} — a release "
                f"seems in progress. Re-run with --resume to finish it."
            )
        return old_version, new_version, None

    orig_sha = git(repo, "rev-parse", "HEAD", capture=True)
    y, data = load_manifest(repo)
    old_version, new_version = bump_dublin_core(data)
    dump_manifest(repo, y, data)
    git(repo, "add", "manifest.yaml")
    git(repo, "commit", "-m", bump_commit_message(repo, new_version))
    return old_version, new_version, orig_sha


def prep_release_branch(repo, old_version, new_version, new_codes, remote, resume):
    """Create release_v<new> off release_v<old> with trimmed projects + refreshed files.

    Returns (release_branch_name, release_projects, master_projects).
    """
    old_branch = f"release_v{old_version}"
    new_branch = f"release_v{new_version}"

    if not (branch_exists(repo, old_branch) or remote_branch_exists(repo, old_branch, remote)):
        raise ReleaseError(f"{repo}: base branch {old_branch} does not exist.")

    # If the new branch already exists (locally or on the remote), this is a resume:
    # reuse it as-is and just gather the data needed for the release notes.
    if branch_exists(repo, new_branch) or remote_branch_exists(repo, new_branch, remote):
        if not resume:
            raise ReleaseError(
                f"{repo}: {new_branch} already exists — delete it, or re-run with "
                f"--resume to continue an interrupted release."
            )
        if not branch_exists(repo, new_branch):
            git(repo, "checkout", "-b", new_branch, f"{remote}/{new_branch}")
        _, master_data = load_manifest(repo, ref="master")
        _, rel_data = load_manifest(repo, ref=new_branch)
        return new_branch, list(rel_data["projects"]), list(master_data["projects"])

    # Master's freshly committed manifest is the source of truth for dc + projects.
    _, master_data = load_manifest(repo, ref="master")
    master_dc = master_data["dublin_core"]
    master_projects = list(master_data["projects"])

    # Identifiers previously released = projects on the old release branch.
    _, old_rel_data = load_manifest(repo, ref=old_branch)
    released_ids = {str(p["identifier"]).lower() for p in old_rel_data["projects"]}

    # Validate the new book codes.
    master_ids = {str(p["identifier"]).lower() for p in master_projects}
    for code in new_codes:
        if code.lower() not in master_ids:
            raise ReleaseError(f"{repo}: book '{code}' not found in master projects.")
        if code.lower() in released_ids:
            raise ReleaseError(
                f"{repo}: book '{code}' is already published (it must be a NEW book)."
            )

    target_ids = released_ids | {c.lower() for c in new_codes}

    # Check out the old release branch, then branch the new one.
    git(repo, "checkout", old_branch)
    if remote_branch_exists(repo, old_branch, remote):
        git(repo, "merge", "--ff-only", f"{remote}/{old_branch}")
    git(repo, "checkout", "-b", new_branch)

    # Build the new manifest: dc identical to master, projects = target subset
    # in master's order (master order IS sort order).
    y, rel_data = load_manifest(repo)  # working tree (== old release content)
    rel_data["dublin_core"] = master_dc
    new_projects = CommentedSeq(
        p for p in master_projects if str(p["identifier"]).lower() in target_ids
    )
    rel_data["projects"] = new_projects
    dump_manifest(repo, y, rel_data)

    # Refresh every book file in the release from master (existing + new).
    for p in new_projects:
        relpath = str(p["path"]).lstrip("./")
        content = git(repo, "show", f"master:{relpath}", capture=True)
        (BASE / repo / relpath).write_text(content + ("\n" if not content.endswith("\n") else ""))
        git(repo, "add", relpath)

    git(repo, "add", "manifest.yaml")
    git(repo, "commit", "-m", f"Releasing {' '.join(new_codes)}")
    return new_branch, list(new_projects), master_projects


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def rollback(state):
    print("\n!! Rolling back all local changes (nothing was pushed) ...")
    for repo, info in state.items():
        try:
            if current_branch(repo) != "master":
                git(repo, "checkout", "master", check=False)
            nb = info.get("new_branch")
            if nb and branch_exists(repo, nb):
                git(repo, "branch", "-D", nb, check=False)
            orig = info.get("orig_sha")
            if orig:
                git(repo, "reset", "--hard", orig, check=False)
            print(f"   {repo}: reverted to {info.get('orig_sha','?')[:8]}")
        except Exception as e:  # noqa: BLE001
            print(f"   {repo}: rollback issue: {e}")


# ---------------------------------------------------------------------------
# Push + Gitea release
# ---------------------------------------------------------------------------
def push_repo(repo, info, remote):
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
def parse_args(argv):
    host = DEFAULT_HOST
    dry_run = assume_yes = resume = unshallow = False
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
        elif a.startswith("--"):
            sys.exit(f"Unknown option: {a}")
        else:
            codes.append(a.upper())
        i += 1
    return host, dry_run, assume_yes, resume, unshallow, codes


def main():
    host, dry_run, assume_yes, resume, unshallow, codes = parse_args(sys.argv[1:])

    if not codes:
        sys.exit("Usage: release_union.py BOOK [BOOK ...] "
                 "[--host HOST] [--resume] [--unshallow] [--dry-run] [--yes]")

    if "." not in host:
        sys.exit(f"--host must be a full hostname (e.g. qa.door43.org), not '{host}'.")

    print(f"Releasing new books: {' '.join(codes)}   (date {TODAY}, host {host}"
          f"{', resume' if resume else ''})")

    token = None
    if not dry_run:
        token = load_env_token()

    # Safety gate: clone any missing repos (from `host`), make sure each has its host
    # remote, is clean, and (on a fresh run) has master in sync — before any work.
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
        if not resume:
            ensure_master_synced(repo, remotes[repo])

    # In --resume mode a prior run already pushed, so rolling back would undo
    # remote state. Re-running is idempotent instead.
    allow_rollback = not resume
    state = {}  # repo -> info dict (for rollback + later phases)

    # ---- Phase A: all local work ------------------------------------------
    try:
        notes_body = None
        for repo in ALL_REPOS:
            print(f"\n== Preparing {repo} ==")
            old_v, new_v, orig_sha = prep_master(repo, remotes[repo], resume)
            info = {"old_version": old_v, "new_version": new_v,
                    "orig_sha": orig_sha, "new_branch": None, "notes": None}
            state[repo] = info
            print(f"   master: v{old_v} -> v{new_v}")

            if repo in BOOK_REPOS:
                nb, rel_projects, master_projects = prep_release_branch(
                    repo, old_v, new_v, codes, remotes[repo], resume
                )
                info["new_branch"] = nb
                # Generate the shared notes once (book sets are identical across repos).
                if notes_body is None:
                    notes_body = build_release_notes(
                        new_v, rel_projects, master_projects, codes
                    )
                info["notes"] = notes_body
                print(f"   branch {nb}: {len(rel_projects)} projects, files refreshed")
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR during preparation: {e}")
        if allow_rollback:
            rollback(state)
        else:
            print("(--resume: not rolling back; fix the issue and re-run.)")
        sys.exit(1)

    # ---- Review -----------------------------------------------------------
    any_new = state[BOOK_REPOS[0]]["new_version"]
    print("\n" + "=" * 70)
    print(f"PREPARED v{any_new} locally for all {len(ALL_REPOS)} repos.")
    print("=" * 70)
    print("\n--- Release notes (book packages) ---\n")
    ex = BOOK_REPOS[0]
    print(state[ex]["notes"])
    print(changelog_section(state[ex]["old_version"], state[ex]["new_version"]))
    print(f"\n(The 'Changes Since' link above is per-repo; each release gets its own. "
          f"en_ta/en_tw bodies are '# v{any_new} Release' + the same block.)")
    print("\n--- Per-repo manifest diff (stat) ---")
    for repo in ALL_REPOS:
        head = "release branch" if repo in BOOK_REPOS else "master"
        print(f"\n# {repo} (release target: {head})")
        ref = state[repo]["new_branch"] if repo in BOOK_REPOS else "master"
        print(git(repo, "show", "--stat", "--oneline", ref, capture=True))

    if dry_run:
        print("\n--dry-run: rolling everything back, nothing pushed.")
        if allow_rollback:
            rollback(state)
        else:
            print("(--resume: nothing to roll back.)")
        return

    if not assume_yes:
        ans = input(
            f"\nPush all branches and create v{any_new} prereleases on "
            f"{host}? [y/N] "
        ).strip().lower()
        if ans != "y":
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
        for repo in ALL_REPOS:
            push_repo(repo, state[repo], remotes[repo])
            pushed.append(repo)
            print(f"   pushed {repo}")
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR while pushing: {e}")
        print(f"Pushed so far: {', '.join(pushed) or 'none'}.")
        print("Local branches are intact. Re-run with --resume (same args) to "
              "finish pushing and create the releases. NOT rolling back master.")
        sys.exit(1)

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

    if failures:
        print(f"\nReleases failed for: {', '.join(failures)}. Branches are pushed; "
              "re-run with --resume (same args) to retry the releases.")
        sys.exit(1)
    print(f"\nDone. v{any_new} released for all repos on {host}.")
    print("NOTE: these are PRERELEASES. Promote each one to production on the DCS "
          "(edit the release in all 7 repos and uncheck 'This is a pre-release').")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
