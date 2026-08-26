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

## Bumping the Quetoo version

Edit the `Quetoo` module in `org.quetoo.Quetoo.yaml`, setting `tag` and `commit`
to the new release, then add a matching `<release>` entry at the top of
`org.quetoo.Quetoo.metainfo.xml`. Every module carries `x-checker-data`, so
Flathub's external data checker can propose these bumps automatically.
