#!/usr/bin/env python3
"""Track upstream Quetoo releases and refresh the Flatpak manifest.

Mirrors jdolan's branches and tags into the WickedOldGames forks, then repins
every module in the manifest and records the new release in the AppStream
metadata. Intended to run from CI, but it works standalone given GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
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


def newest_tag(repo: str) -> tuple[str, str]:
    """Highest version-sorted tag in repo, and the commit it points at."""
    tags = request("GET", f"/repos/{repo}/tags?per_page=100")
    versioned = [t for t in tags if TAG_RE.match(t["name"])]
    if not versioned:
        raise GitHubError(f"{repo} has no v-prefixed tags")

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
        # A fork already level with upstream reports 409, which is fine. Anything
        # else, and 403 above all, means the token cannot do its job: fail loudly
        # rather than reporting a no-op run as a success.
        if exc.status != 409:
            raise
        print(f"  {name}: branch {branch} already current")

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
        request(
            "POST",
            f"/repos/{fork}/git/refs",
            {"ref": f"refs/tags/{newest['name']}", "sha": newest["commit"]["sha"]},
        )
        print(f"  {name}: mirrored {newest['name']}")

    skipped = sorted(t["name"] for t in upstream_tags if t["name"] not in have | {newest["name"]})
    if skipped:
        print(f"  {name}: not mirrored, not needed: {', '.join(skipped)}")


def resolve_pin(name: str, prefer_head: bool) -> Pin:
    """Pick the commit to build. Tags are preferred; HEAD is the escape hatch."""
    fork = f"{FORK}/{name}"
    tag, tag_commit = newest_tag(fork)
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


def add_release(metainfo: str, version: str, date: str, notes: str) -> str:
    """Prepend a release entry, unless this version is already recorded."""
    if f'<release version="{version}"' in metainfo:
        return metainfo
    entry = (
        f'    <release version="{version}" date="{date}">\n'
        f'      <url type="details">https://github.com/{FORK}/quetoo/releases/tag/v{version}</url>\n'
        f"      <description>\n"
        f"        <p>{notes}</p>\n"
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
    for module, repo in MODULES.items():
        # The game itself always tracks a tagged release; only its dependencies
        # ever need the HEAD escape hatch.
        prefer_head = args.deps == "head" and module != MAIN_MODULE
        candidate = resolve_pin(repo, prefer_head)
        held = existing[module]
        # Never move a pin backwards. A module deliberately pinned ahead of its
        # newest tag, because a needed fix is not released yet, must survive
        # until a tag actually overtakes it.
        if is_ahead(f"{FORK}/{repo}", candidate.commit, held):
            print(f"  {module}: keeping {held[:10]}, ahead of {candidate.tag or 'HEAD'}")
            pins[module] = Pin(commit=held, tag=None)
            continue
        pins[module] = candidate
        print(f"  {module}: {candidate.tag or 'untagged'} @ {candidate.commit[:10]}")

    manifest = apply_pins(manifest, pins)
    with open(manifest_path, "w") as handle:
        handle.write(manifest)

    main_pin = pins[MAIN_MODULE]
    assert main_pin.tag is not None
    version = main_pin.tag.lstrip("v")
    release = request("GET", f"/repos/{UPSTREAM}/quetoo/releases/tags/{main_pin.tag}")
    date = str(release["published_at"])[:10]
    notes = (release.get("body") or "").strip().splitlines()
    summary = notes[0].strip() if notes else f"Quetoo {version}."
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
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
