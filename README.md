# org.quetoo.Quetoo

Flatpak build for [Quetoo](https://quetoo.org), built from the WickedOldGames
sources.

The published Flathub package lives at
[flathub/org.quetoo.Quetoo](https://github.com/flathub/org.quetoo.Quetoo) and is
maintained separately. This repository builds the same application for
development, testing and direct `.flatpak` bundle downloads from the
[releases page](https://github.com/WickedOldGames/org.quetoo.Quetoo/releases).

## Prerequisites

```bash
flatpak install flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
```

## Build

`shared-modules` is a submodule, so clone recursively:

```bash
git clone --recurse-submodules https://github.com/WickedOldGames/org.quetoo.Quetoo.git
cd org.quetoo.Quetoo
flatpak-builder --force-clean --user --install-deps-from=flathub \
  --repo=quetoo-repo build org.quetoo.Quetoo.yaml
```

## Install and test

```bash
flatpak --user remote-add --no-gpg-verify --if-not-exists quetoo-repo quetoo-repo
flatpak --user install quetoo-repo org.quetoo.Quetoo
flatpak run org.quetoo.Quetoo
```

Or install a bundle straight from a release:

```bash
flatpak install --user org.quetoo.Quetoo-x86_64.flatpak
```

## Releasing

Tag the repository and CI does the rest:

```bash
git tag v1.0.82
git push origin v1.0.82
```

`.github/workflows/release.yml` builds `x86_64` and `aarch64` bundles and
attaches them to a GitHub release. `.github/workflows/build.yml` builds both
architectures on every push and pull request.

## Where the sources come from

Every module builds from the WickedOldGames organization. Those repositories
track [jdolan](https://github.com/jdolan), which is still where day-to-day
commits land, so they need syncing before a version bump:

```bash
gh repo sync WickedOldGames/quetoo --source jdolan/quetoo
git clone --bare https://github.com/jdolan/quetoo.git && cd quetoo.git
git push --tags https://github.com/WickedOldGames/quetoo.git
```

The tag push matters. `gh repo sync` and the GitHub merge-upstream API move
branches only, so a fork can sit on the right commit while the release tag the
manifest names is still missing.

## Bumping the Quetoo version

Edit the `Quetoo` module in `org.quetoo.Quetoo.yaml`, setting `tag` and `commit`
to the new release, then add a matching `<release>` entry at the top of
`org.quetoo.Quetoo.metainfo.xml`. Every module carries `x-checker-data`, so
Flathub's external data checker can propose these bumps automatically.

Prefer `tag` plus `commit` over a bare commit. `ObjectivelyGPU` is the current
exception: Quetoo v1.0.82 calls `TransferBuffer::write`, which landed after
v0.10.0, so it is pinned to a commit until a tag ships containing it.

## Relationship to Flathub

The published package is [flathub/org.quetoo.Quetoo](https://github.com/flathub/org.quetoo.Quetoo),
a separate repository maintained by the Flathub app maintainer. This repository
does not publish there. To ship a new version on Flathub:

1. Flathub's external data checker opens a bump pull request on its own, usually
   within a day of an upstream release.
2. That pull request gets a test build, and the bot comments with a downloadable
   bundle.
3. A repository maintainer merges it. The official build follows and normally
   publishes within a couple of hours, unless a permission or AppStream change
   sends it to moderation.

Because the checker only pins to tags, an untagged upstream fix cannot reach
Flathub. Tag the dependency first, then let the checker regenerate.
