#!/usr/bin/env python3
"""Track upstream Quetoo releases and refresh the Flatpak manifest.

Mirrors jdolan's branches and tags into the WickedOldGames forks, then repins
every module in the manifest and records the new release in the AppStream
metadata. Intended to run from CI, but it works standalone given GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

API: Final = "https://api.github.com"
UPSTREAM: Final = "jdolan"
FORK: Final = "WickedOldGames"

# Manifest module name -> repository name. Order matches the manifest.
MODULES: Final[dict[str, str]] = {
    "Objectively": "Objectively",
    "ObjectivelyGPU": "ObjectivelyGPU",
    "ObjectivelyMVC": "ObjectivelyMVC",
    "Quetoo-Data": "quetoo-data",
    "Quetoo": "quetoo",
}
MAIN_MODULE: Final = "Quetoo"
TAG_RE: Final = re.compile(r"^v(\d+(?:\.\d+)*)$")
WORKFLOWS: Final = ".github/workflows"
CHANGELOG_HEADING: Final = re.compile(r"^##\s+Changelog\s*$", re.I)
CHANGELOG_ITEM: Final = re.compile(r"^- (?:\[[^\]]+\]\([^)]+\) )?(.+)$")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Pin:
    """Where a module should build from."""

    commit: str
    tag: str | None  # None means the needed code is not in any release yet.


def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise GitHubError("GITHUB_TOKEN is not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp) if resp.status != 204 else None
    except urllib.error.HTTPError as exc:
        raise GitHubError(f"{method} {path} -> {exc.code}: {exc.read()[:300]!r}", exc.code) from exc


def peel(repo: str, ref: dict[str, Any]) -> str:
    """Resolve a ref to a commit, following annotated tag objects."""
    if ref["object"]["type"] != "tag":
        return str(ref["object"]["sha"])
    return str(request("GET", f"/repos/{repo}/git/tags/{ref['object']['sha']}")["object"]["sha"])


def is_workflow_permission_error(exc: GitHubError) -> bool:
    """True when GitHub refused a ref update that would rewrite workflow files."""
    if exc.status != 422:
        return False
    text = str(exc).lower()
    return "workflow" in text and ("scope" in text or "permission" in text)


def swap_tree_entry(entries: list[dict[str, Any]], path: str, sha: str) -> list[dict[str, Any]]:
    """Copy a Git tree list, pointing `path` at `sha`. Other entries are unchanged."""
    out: list[dict[str, Any]] = []
    found = False
    for entry in entries:
        item = {key: entry[key] for key in ("path", "mode", "type", "sha")}
        if item["path"] == path:
            item["sha"] = sha
            found = True
        out.append(item)
    if not found:
        raise GitHubError(f"{path} is not in this tree")
    return out


def tree_sha_at(repo: str, root: str, path: str) -> str | None:
    """SHA of the tree or blob at `path` under `root`, or None if a component is missing."""
    sha = root
    for part in path.split("/"):
        data = request("GET", f"/repos/{repo}/git/trees/{sha}")
        if data.get("truncated"):
            raise GitHubError(f"tree {sha} in {repo} is truncated")
        entry = next((item for item in data["tree"] if item["path"] == part), None)
        if entry is None:
            return None
        sha = str(entry["sha"])
    return sha


def replace_workflows(repo: str, upstream_root: str, workflows_sha: str) -> str:
    """Upstream root tree, with `.github/workflows` replaced by `workflows_sha`."""
    github = tree_sha_at(repo, upstream_root, ".github")
    if github is None:
        raise GitHubError(f"{repo} upstream tree has no .github")
    new_github = str(
        request(
            "POST",
            f"/repos/{repo}/git/trees",
            {
                "base_tree": github,
                "tree": [{"path": "workflows", "mode": "040000", "type": "tree", "sha": workflows_sha}],
            },
        )["sha"]
    )
    return str(
        request(
            "POST",
            f"/repos/{repo}/git/trees",
            {
                "base_tree": upstream_root,
                "tree": [{"path": ".github", "mode": "040000", "type": "tree", "sha": new_github}],
            },
        )["sha"]
    )


def merge_keeping_workflows(name: str, branch: str) -> None:
    """Merge upstream into the fork without rewriting `.github/workflows`.

    merge-upstream applies every upstream commit, so a Contents-only token is
    refused when any of those commits touch workflow files. A merge commit whose
    tree is upstream plus the fork's existing workflows has a tip-to-tip diff
    that does not change those files, which is enough for Contents write.
    Forks share Git objects with upstream, so the Git Data API on the fork can
    read the upstream commit without cloning. quetoo-data is multiple gigabytes;
    cloning it in CI is not an option.
    """
    fork = f"{FORK}/{name}"
    fork_head = str(request("GET", f"/repos/{fork}/commits/{branch}")["sha"])
    up_head = str(request("GET", f"/repos/{UPSTREAM}/{name}/commits/{branch}")["sha"])
    if fork_head == up_head:
        print(f"  {name}: branch {branch} already current")
        return

    fork_commit = request("GET", f"/repos/{fork}/git/commits/{fork_head}")
    # Read the upstream commit through the fork: the fork network already has
    # the object, and posting trees has to happen on the fork anyway.
    up_commit = request("GET", f"/repos/{fork}/git/commits/{up_head}")
    fork_tree = str(fork_commit["tree"]["sha"])
    up_tree = str(up_commit["tree"]["sha"])
    workflows = tree_sha_at(fork, fork_tree, WORKFLOWS)
    if workflows is None:
        raise GitHubError(
            f"{fork} has no {WORKFLOWS}; cannot sync past a workflow-file "
            "change without Workflows write on SYNC_TOKEN"
        )
    tree = replace_workflows(fork, up_tree, workflows)
    commit = request(
        "POST",
        f"/repos/{fork}/git/commits",
        {
            "message": (
                f"Merge {UPSTREAM}/{name} {branch} into {branch}, "
                "keeping this fork's GitHub Actions workflows"
            ),
            "tree": tree,
            "parents": [fork_head, up_head],
        },
    )
    request("PATCH", f"/repos/{fork}/git/refs/heads/{branch}", {"sha": commit["sha"]})
    print(f"  {name}: branch {branch} merged (workflows kept)")


def not_after(when: str, before: str | None) -> bool:
    """True if `when` is at or before `before`. GitHub timestamps compare as text."""
    return before is None or when <= before


def tag_time(repo: str, tag: dict[str, Any]) -> str:
    """Release published_at if GitHub has one, otherwise the tagged commit's date."""
    try:
        return str(request("GET", f"/repos/{repo}/releases/tags/{tag['name']}")["published_at"])
    except GitHubError as exc:
        if exc.status != 404:
            raise
    return str(request("GET", f"/repos/{repo}/commits/{tag['commit']['sha']}")["commit"]["committer"]["date"])


def newest_tag(repo: str, before: str | None = None) -> tuple[str, str]:
    """Highest version-sorted tag in repo, and the commit it points at.

    `before` is an ISO timestamp. Dependency pins use the Quetoo release time so
    a newer library tag cannot break an older game tag. ObjectivelyMVC 2.3.0
    added a data argument to View::updateBindings; Quetoo 1.0.86 still calls
    the one-argument form.
    """
    tags = request("GET", f"/repos/{repo}/tags?per_page=100")
    versioned = [t for t in tags if TAG_RE.match(t["name"])]
    if before:
        versioned = [t for t in versioned if not_after(tag_time(repo, t), before)]
    if not versioned:
        raise GitHubError(
            f"{repo} has no v-prefixed tags" + (f" at or before {before}" if before else "")
        )

    def key(tag: dict[str, Any]) -> list[int]:
        match = TAG_RE.match(tag["name"])
        assert match is not None
        return [int(part) for part in match.group(1).split(".")]

    best = max(versioned, key=key)
    return str(best["name"]), str(best["commit"]["sha"])


def sync_fork(name: str) -> None:
    """Fast-forward the fork's default branch, then mirror any missing tags.

    merge-upstream moves branches only, so tags are copied separately. Tags are
    created through the refs API rather than a clone: quetoo-data carries the
    game assets and cloning it in CI is gratuitous.
    """
    fork, upstream = f"{FORK}/{name}", f"{UPSTREAM}/{name}"
    branch = request("GET", f"/repos/{fork}")["default_branch"]
    try:
        result = request("POST", f"/repos/{fork}/merge-upstream", {"branch": branch})
        print(f"  {name}: branch {branch} {result['merge_type']}")
    except GitHubError as exc:
        # 409: already current. 422 with a workflow-scope message: upstream
        # changed `.github/workflows` and this token is Contents-only, so replay
        # the merge without those files. Anything else, 403 above all, means
        # the token cannot do its job: fail loudly rather than reporting a
        # no-op run as a success.
        if exc.status == 409:
            print(f"  {name}: branch {branch} already current")
        elif is_workflow_permission_error(exc):
            merge_keeping_workflows(name, branch)
        else:
            raise

    # Mirror only the newest release tag. The manifest never references older
    # ones, and creating a ref at an ancient commit whose .github/workflows
    # content differs from the branch needs Workflows write, a permission this
    # job has no business holding.
    have = {t["name"] for t in request("GET", f"/repos/{fork}/tags?per_page=100")}
    upstream_tags = [
        t for t in request("GET", f"/repos/{upstream}/tags?per_page=100") if TAG_RE.match(t["name"])
    ]
    if not upstream_tags:
        raise GitHubError(f"{upstream} has no v-prefixed tags")

    def version(tag: dict[str, Any]) -> list[int]:
        match = TAG_RE.match(tag["name"])
        assert match is not None
        return [int(part) for part in match.group(1).split(".")]

    newest = max(upstream_tags, key=version)
    if newest["name"] in have:
        print(f"  {name}: {newest['name']} already present")
    else:
        try:
            request(
                "POST",
                f"/repos/{fork}/git/refs",
                {"ref": f"refs/tags/{newest['name']}", "sha": newest["commit"]["sha"]},
            )
            print(f"  {name}: mirrored {newest['name']}")
        except GitHubError as exc:
            if not is_workflow_permission_error(exc):
                raise
            print(
                f"  {name}: {newest['name']} not mirrored "
                "(tag commit has workflow files this token cannot write)"
            )

    skipped = sorted(t["name"] for t in upstream_tags if t["name"] not in have | {newest["name"]})
    if skipped:
        print(f"  {name}: not mirrored, not needed: {', '.join(skipped)}")


def commit_exists(repo: str, sha: str) -> bool:
    try:
        request("GET", f"/repos/{repo}/git/commits/{sha}")
        return True
    except GitHubError as exc:
        if exc.status == 404:
            return False
        raise


def resolve_pin(name: str, prefer_head: bool, before: str | None = None) -> Pin:
    """Pick the commit to build. Tags are preferred; HEAD is the escape hatch."""
    fork = f"{FORK}/{name}"
    # Prefer the newest upstream release. After a workflow-preserving merge the
    # commit is on the fork even when the tag ref could not be created.
    tag, tag_commit = newest_tag(f"{UPSTREAM}/{name}", before=None if prefer_head else before)
    fork_tags = {t["name"] for t in request("GET", f"/repos/{fork}/tags?per_page=100")}
    if tag not in fork_tags:
        if commit_exists(fork, tag_commit):
            tag = None
        else:
            tag, tag_commit = newest_tag(fork, before=None if prefer_head else before)
    if not prefer_head:
        return Pin(commit=tag_commit, tag=tag)

    branch = request("GET", f"/repos/{fork}")["default_branch"]
    head = str(request("GET", f"/repos/{fork}/commits/{branch}")["sha"])
    # Only drop the tag when HEAD genuinely carries newer code.
    return Pin(commit=head, tag=None) if head != tag_commit else Pin(tag_commit, tag)


def current_pins(manifest: str) -> dict[str, str]:
    """Commit each module is pinned to right now."""
    found: dict[str, str] = {}
    for module, repo in MODULES.items():
        match = re.search(
            rf"  - name: {re.escape(module)}\n(?:.*\n)*?"
            rf"        url: https://github\.com/{FORK}/{re.escape(repo)}\.git\n"
            rf"(?:        tag: \S+\n)?        commit: (\S+)\n",
            manifest,
        )
        if not match:
            raise SystemExit(f"could not read the current pin for {module}")
        found[module] = match.group(1)
    return found


def is_ahead(repo: str, base: str, head: str) -> bool:
    """True when head contains commits base does not."""
    if base == head:
        return False
    status = request("GET", f"/repos/{repo}/compare/{base}...{head}")["status"]
    return bool(status == "ahead")


def apply_pins(manifest: str, pins: dict[str, Pin]) -> str:
    """Rewrite each module's tag and commit, leaving comments and order intact."""
    for module, repo in MODULES.items():
        pin = pins[module]
        pattern = re.compile(
            rf"(  - name: {re.escape(module)}\n(?:.*\n)*?"
            rf"        url: https://github\.com/{FORK}/{re.escape(repo)}\.git\n)"
            rf"((?:        (?:tag|commit): \S+\n)+)"
        )
        replacement = f"        commit: {pin.commit}\n"
        if pin.tag:
            replacement = f"        tag: {pin.tag}\n" + replacement

        manifest, count = pattern.subn(lambda m: m.group(1) + replacement, manifest, count=1)
        if count != 1:
            raise SystemExit(f"could not locate module {module} in the manifest")
    return manifest


def release_summary(body: str, version: str) -> str:
    """Plain-text AppStream blurb. GitHub notes open with download badges."""
    bullets: list[str] = []
    in_changelog = False
    for raw in (body or "").splitlines():
        line = raw.strip()
        if CHANGELOG_HEADING.match(line):
            in_changelog = True
            continue
        if not in_changelog:
            continue
        if line.startswith("## "):
            break
        match = CHANGELOG_ITEM.match(line)
        if match:
            bullets.append(match.group(1).strip().rstrip("."))
        if len(bullets) == 2:
            break
    return f"{'; '.join(bullets)}." if bullets else f"Quetoo {version}."


def add_release(metainfo: str, version: str, date: str, notes: str) -> str:
    """Prepend a release entry, unless this version is already recorded."""
    if f'<release version="{version}"' in metainfo:
        return metainfo
    safe = html.escape(notes, quote=False)
    entry = (
        f'    <release version="{version}" date="{date}">\n'
        f'      <url type="details">https://github.com/{FORK}/quetoo/releases/tag/v{version}</url>\n'
        f"      <description>\n"
        f"        <p>{safe}</p>\n"
        f"      </description>\n"
        f"    </release>\n"
    )
    return metainfo.replace("  <releases>\n", f"  <releases>\n{entry}", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="org.quetoo.Quetoo.yaml")
    parser.add_argument("--metainfo", default="org.quetoo.Quetoo.metainfo.xml")
    parser.add_argument(
        "--deps",
        choices=("tags", "head"),
        default="tags",
        help="pin dependencies to their newest release, or to branch HEAD when a "
        "needed fix has not been tagged yet",
    )
    parser.add_argument("--no-sync", action="store_true", help="skip mirroring the forks")
    args = parser.parse_args()

    if not args.no_sync:
        print("Syncing forks from upstream:")
        for repo in MODULES.values():
            sync_fork(repo)

    manifest_path, metainfo_path = args.manifest, args.metainfo
    with open(manifest_path) as handle:
        manifest = handle.read()
    existing = current_pins(manifest)

    pins: dict[str, Pin] = {}
    main_repo = MODULES[MAIN_MODULE]
    main_candidate = resolve_pin(main_repo, prefer_head=False)
    cutoff = None
    if main_candidate.tag:
        cutoff = str(
            request("GET", f"/repos/{UPSTREAM}/{main_repo}/releases/tags/{main_candidate.tag}")[
                "published_at"
            ]
        )

    for module, repo in MODULES.items():
        # The game itself always tracks a tagged release; only its dependencies
        # ever need the HEAD escape hatch.
        prefer_head = args.deps == "head" and module != MAIN_MODULE
        if module == MAIN_MODULE:
            candidate = main_candidate
        else:
            candidate = resolve_pin(repo, prefer_head, before=cutoff)
        held = existing[module]
        # Keep an untagged pin that is still ahead of the chosen tag, because a
        # needed fix is not in that tag yet. A newer tag than the cutoff, left
        # by a previous overshoot, must move back.
        if is_ahead(f"{FORK}/{repo}", candidate.commit, held) and (
            prefer_head or candidate.tag is None
        ):
            print(f"  {module}: keeping {held[:10]}, ahead of {candidate.tag or 'HEAD'}")
            pins[module] = Pin(commit=held, tag=None)
            continue
        pins[module] = candidate
        print(f"  {module}: {candidate.tag or 'untagged'} @ {candidate.commit[:10]}")

    manifest = apply_pins(manifest, pins)
    with open(manifest_path, "w") as handle:
        handle.write(manifest)

    main_pin = pins[MAIN_MODULE]
    release_tag = main_pin.tag or newest_tag(f"{UPSTREAM}/quetoo")[0]
    version = release_tag.lstrip("v")
    release = request("GET", f"/repos/{UPSTREAM}/quetoo/releases/tags/{release_tag}")
    date = str(release["published_at"])[:10]
    summary = release_summary(str(release.get("body") or ""), version)
    # Read fully before opening for write: nesting the two truncates the file.
    with open(metainfo_path) as handle:
        metainfo = add_release(handle.read(), version, date, summary)
    with open(metainfo_path, "w") as handle:
        handle.write(metainfo)

    print(f"\nManifest now builds Quetoo {version} ({date}).")
    # Consumed by the workflow to title the pull request.
    if step_output := os.environ.get("GITHUB_OUTPUT"):
        with open(step_output, "a") as handle:
            handle.write(f"version={version}\n")
    return 0


def _selfcheck() -> None:
    """Exercise the two pure rewrites, which are the only tricky logic here."""

    def toy() -> str:
        out = "modules:\n"
        for module, repo in MODULES.items():
            out += (
                f"  - name: {module}\n"
                "    sources:\n"
                "      - type: git\n"
                f"        url: https://github.com/{FORK}/{repo}.git\n"
                "        tag: v1.0.0\n"
                "        commit: aaa\n"
                "        x-checker-data:\n"
                "          type: git\n"
            )
        return out

    base = {m: Pin("aaa", "v1.0.0") for m in MODULES}

    out = apply_pins(toy(), base | {"Objectively": Pin("bbb", "v2.0.0")})
    assert "        tag: v2.0.0\n        commit: bbb\n" in out, out
    assert out.count("x-checker-data") == len(MODULES), "trailing keys must survive"

    # An untagged pin must drop the tag line entirely, not keep a stale one.
    out = apply_pins(toy(), base | {"ObjectivelyGPU": Pin("ccc", None)})
    gpu = out.split("ObjectivelyGPU.git")[1].split("x-checker-data")[0]
    assert "tag:" not in gpu and "commit: ccc" in gpu, gpu

    # Every module must still be pinned exactly once.
    assert out.count("commit: ") == len(MODULES), out

    # The parser must read back exactly what apply_pins wrote.
    round_trip = current_pins(apply_pins(toy(), base | {"ObjectivelyGPU": Pin("ccc", None)}))
    assert round_trip["ObjectivelyGPU"] == "ccc", round_trip
    assert round_trip["Quetoo"] == "aaa", round_trip

    meta = '  <releases>\n    <release version="1.0.67" date="2026-07-27"/>\n  </releases>\n'
    added = add_release(meta, "1.0.82", "2026-08-25", "Renderer fixes.")
    assert added.index("1.0.82") < added.index("1.0.67"), "newest release goes first"
    assert add_release(added, "1.0.82", "2026-08-25", "x") == added, "must be idempotent"
    escaped = add_release(meta, "1.0.86", "2026-09-03", "Pin SDL3 < 3.5 & ship it.")
    assert "<p>Pin SDL3 &lt; 3.5 &amp; ship it.</p>" in escaped, escaped

    badge = "## ![Windows](https://img.shields.io/badge/-%20-0078D6?style=flat-square&logo=x) Windows"
    changelog = (
        f"{badge}\n\n## Changelog\n"
        "- [`abc`](http://x) Pin SDL3 < 3.5\n"
        "- [`def`](http://x) Apple Silicon only\n"
        "- [`ghi`](http://x) ignored third\n"
    )
    assert release_summary(changelog, "1.0.86") == "Pin SDL3 < 3.5; Apple Silicon only."
    assert release_summary(badge, "1.0.86") == "Quetoo 1.0.86."
    assert release_summary("", "1.0.1") == "Quetoo 1.0.1."

    assert not_after("2026-09-03T03:13:17Z", "2026-09-03T22:03:48Z")
    assert not not_after("2026-09-04T18:00:47Z", "2026-09-03T22:03:48Z")
    assert not_after("2026-09-07T00:00:00Z", None)

    workflow_422 = GitHubError(
        'POST /repos/WickedOldGames/quetoo/merge-upstream -> 422: b\'{"message":'
        '"refusing to allow a Personal Access Token to create or update workflow '
        '`.github/workflows/build.yml` without `workflow` scope"}\'',
        422,
    )
    assert is_workflow_permission_error(workflow_422)
    assert not is_workflow_permission_error(GitHubError("already merged", 409))
    assert not is_workflow_permission_error(GitHubError("Merge conflict", 422))

    swapped = swap_tree_entry(
        [
            {"path": ".github", "mode": "040000", "type": "tree", "sha": "aaa", "size": 0},
            {"path": "src", "mode": "040000", "type": "tree", "sha": "bbb"},
        ],
        ".github",
        "ccc",
    )
    assert swapped == [
        {"path": ".github", "mode": "040000", "type": "tree", "sha": "ccc"},
        {"path": "src", "mode": "040000", "type": "tree", "sha": "bbb"},
    ]
    try:
        swap_tree_entry(swapped, "missing", "ddd")
    except GitHubError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("swap_tree_entry must reject a path that is not present")
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
