# nightly-builds-conda

Nightly builds of the Cyclus ecosystem as conda packages

The `main` branches of [cyclus](https://github.com/cyclus/cyclus), [cycamore](https://github.com/cyclus/cycamore), and [cymetric](https://github.com/cyclus/cymetric) are built and tested as conda packages against the [conda-forge global pinning](https://github.com/conda-forge/conda-forge-pinning-feedstock), the same compilers, C standard library, and library pins that their conda-forge feedstocks build with. cycamore is built against the cyclus nightly, and cymetric against the cyclus and cycamore nightlies. The packages are published to the [`cyclus/nightly`](https://prefix.dev/channels/@cyclus/nightly) channel on prefix.dev.

New commits to the `main` branches are built within the hour, and changes of the global pinning every night.

cyclus and cycamore are built for `linux-64`, `linux-aarch64`, and `osx-arm64`, for the oldest and the newest Python that conda-forge supports. cymetric is `noarch: python`.

## Installing

### With [Pixi](https://pixi.sh/)

List the nightly channel before `conda-forge` so that the nightlies are preferred over the stable releases:

```toml
[workspace]
channels = ["https://prefix.dev/cyclus/nightly", "conda-forge"]
```

which can be done through the Pixi CLI with

```console
pixi workspace channel add --prepend https://prefix.dev/cyclus/nightly
```

and then add `cyclus`, `cycamore`, or `cymetric` as workspace dependencies as usual

```console
pixi add cyclus cycamore cymetric
```

### With conda

```console
conda create --name cyclus-nightly --channel https://prefix.dev/cyclus/nightly --channel conda-forge --strict-channel-priority cyclus cycamore cymetric
```

### Versions

The nightlies are versioned `<last release>.post<commits since it>` (e.g. `1.6.0.post814`), which sorts after the release they follow and before the next one. cycamore and cymetric require exactly the cyclus (and cymetric the cycamore) nightly they were built and tested with.

## How it works

- `recipes/<package>/` holds a rattler-build recipe for each package, based on the recipe of its conda-forge feedstock, but building a git revision of `main` given by the environment.
- `scripts/resolve_source.sh <package>` resolves the head of `main` of `https://github.com/cyclus/<package>` and prints the `<PACKAGE>_GIT_REV` and `<PACKAGE>_VERSION` the recipe reads.
- The global pinning is the `conda_build_config.yaml` of the `conda-forge-pinning` package, at the version in `pixi.lock`. `nightly_variants.yaml` overrides only the keys the nightlies build differently (the Python versions, the migrations the feedstocks are in the middle of, and the channels), so everything else follows conda-forge.
- The `build` task of `pixi.toml` passes the versions of the cyclus and cycamore nightlies as the variant keys `cyclus` and `cycamore`, which the downstream recipes depend on (e.g. `cyclus ==${{ cyclus }}`).

### What is built when

The file name of a nightly, e.g. `cycamore-1.6.0.post206-py315h55b1f84_0.conda`, only changes when one of its inputs does:

| Input | Changes the |
| --- | --- |
| A new commit to `main` of the package | version |
| A pin of the global pinning that the recipe uses (e.g. `hdf5`, `libboost_devel`, the compilers) | hash |
| A new nightly of an upstream package (through the `cyclus` and `cycamore` variant keys) | hash |
| A change to the recipe in this repository | build number, which has to be bumped by hand |

`scripts/plan_builds.py` renders all recipes for all platforms and plans a build job for each package whose file name is not in the channel yet. A new cycamore commit thus rebuilds cycamore and cymetric, but not cyclus, and a new cyclus commit rebuilds all three.

A change of the global pinning that rebuilds a commit already in the channel adds a package with the same version and build number but a new hash (e.g. `cyclus-x.y.z.post123-py315h<new hash>_0.conda` next to `cyclus-x.y.z.post123-py315h<old hash>_0.conda`), so the build number is deliberately not bumped for it. The solvers still prefer the newer package: when version and build number are equal, both rattler (Pixi) and the libsolv of conda-forge (conda, mamba) prefer the package whose dependencies allow the higher versions, and then the one with the newer `timestamp`. The older packages stay in the channel for environments whose other requirements only fit the older pins. Bumping the build number with every pinning update would rebuild every package including those whose pins did not change.

`.github/workflows/nightly.yml` runs

- **hourly**, with the pinning in `pixi.lock` on `main`, so that only new commits are built. Jobs that failed before are left out until the next nightly run, so that a broken `main` is not rebuilt every hour.
- **nightly** (06:00 UTC) and on manual runs, which first update the global pinning and commit `pixi.lock` to `main` if it changed, and retry the jobs that failed before.
- on **pull requests**, which build and test everything, but upload nothing.

The build jobs run in stages in dependency order (cyclus, cycamore, cymetric), one job per platform and Python variant as in the build matrices of conda-forge feedstocks, each a call of `.github/workflows/build-stage.yml`. A stage builds against the upstream packages built earlier in the run, or, if the run did not rebuild them, those in the channel. A job whose upstream packages are in neither, because the jobs building them failed or were cancelled, fails before building, and is planned again by the next run once they are built. Finally, the packages that passed their tests are uploaded to prefix.dev with [Repository Access](https://prefix.dev/docs/prefix/channels/access) (trusted publishing).

## Building locally

```console
for p in cyclus cycamore cymetric; do eval "$(pixi run resolve $p | sed 's/^/export /')"; done
pixi run build --recipe-dir recipes
```

The packages are written to `output/`. `rattler-build` runs as many jobs as there are CPUs unless `CPU_COUNT` is set.

To build a single recipe and variant, as each CI job does, pass the recipe and the overrides of a job from `pixi run plan --all`, e.g.

```console
pixi run build --recipe recipes/cycamore --variant 'python=3.15.* *_cp315' --variant is_python_min=false
```

`pixi run plan` prints the jobs that the workflow would run now.

## Adding a package

To add e.g. a package `cyfoo` that depends on cyclus:

1. Copy its feedstock recipe to `recipes/cyfoo/`, take the version and git revision from `CYFOO_VERSION` and `CYFOO_GIT_REV` as the other recipes do, and depend on the upstream nightlies through their variant keys as `recipes/cycamore/recipe.yaml` does.
2. Add it to `PACKAGES` in `scripts/plan_builds.py`, and as a stage to `.github/workflows/nightly.yml` and to the `needs` of its `upload` job.
3. If other packages are to build against it, pass its version as a variant key in the `build` task of `pixi.toml`.

## Setting up the channel

Uploading needs a one-time setup on prefix.dev: in the settings of the `cyclus/nightly` channel, add [Repository Access](https://prefix.dev/docs/prefix/channels/access) for GitHub with the organization `cyclus`, the repository `nightly-builds-conda`, the workflow `nightly.yml`, no GitHub Environment, and Read/write access. The upload job then authenticates with GitHub's OIDC token, so no prefix.dev API key is stored in the repository.

The nightly run commits the updated `pixi.lock` to `main` with the `GITHUB_TOKEN` of the workflow, which needs `main` to accept pushes from GitHub Actions.
