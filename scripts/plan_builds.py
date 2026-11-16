"""Plan the build jobs of a run of the nightly workflow.

Renders every recipe for every platform (with ``pixi run -e default build
--render-only``, so with exactly the variant configs and the pinning of the
builds), works out the file name of
every package that would be built, and prints for each package a JSON list of
the build jobs that are needed, as lines for $GITHUB_OUTPUT:

    cyclus=[{"package": "cyclus", "platform": "linux-64", "runner": "ubuntu-latest",
             "id": "py3.15", "overrides": ["python=3.15.* *_cp315", "is_python_min=false"],
             "subdir": "linux-64", "files": ["cyclus-1.6.0.post814-py315he068e02_0.conda"],
             "key": "cyclus-1.6.0.post814-py315he068e02_0.conda",
             "artifact": "packages-cyclus-linux-64-py3.15",
             "upstream": [], "upstream_artifacts": ""}, ...]
    cycamore=[...]
    cymetric=[...]
    any=true

The file name of a nightly only changes when one of its inputs does (see
recipes/cyclus/recipe.yaml), so a job is needed exactly when one of its files
is not in the nightly channel yet. With --all, every job is planned (for pull
requests). With --attempted, jobs whose key is listed in the given file are
left out, so that a build that failed is not retried until the file name
changes or a run without --attempted (the nightly one) retries it.

The "upstream" of a job are the files of the upstream packages that it builds
against and that are not in the channel, so that the run has to build them
first, and "upstream_artifacts" is the glob of the artifacts of the jobs
building them, which the job downloads. A job whose upstream packages are
neither in the channel nor planned (e.g. because their job failed before) is
left out, as it could not be built.

Each job builds one recipe for one Python variant: python and is_python_min
are the only keys with several values in nightly_variants.yaml, and are zipped
in the global pinning, so rattler-build only accepts overrides of both
together. If the global pinning gives several values to another key that the
recipes use (e.g. during a migration), a job builds all of them, which is still
correct, only less parallel.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

# The platforms to build on and their GitHub Actions runners
RUNNERS = {
    "linux-64": "ubuntu-latest",
    "linux-aarch64": "ubuntu-24.04-arm",
    "osx-arm64": "macos-latest",
}
# noarch packages are built once, on this platform
NOARCH_PLATFORM = "linux-64"
# In dependency order, as the build stages of .github/workflows/nightly.yml
PACKAGES = ["cyclus", "cycamore", "cymetric"]

NIGHTLY_VARIANTS = Path("nightly_variants.yaml")


def python_overrides(nightly_variants):
    """Map each Python variant to the --variant overrides that select it."""
    pythons = nightly_variants["python"]
    is_python_mins = nightly_variants["is_python_min"]
    if len(pythons) != len(is_python_mins):
        sys.exit(
            f"{NIGHTLY_VARIANTS}: python and is_python_min are zipped and must have the same length"
        )
    return {
        python: [f"python={python}", f"is_python_min={str(is_python_min).lower()}"]
        for python, is_python_min in zip(pythons, is_python_mins)
    }


def render(platform):
    """Render all recipes for a platform as rattler-build would build them."""
    result = subprocess.run(
        [
            *("pixi", "run", "--frozen", "--environment", "default", "build"),
            *("--recipe-dir", "recipes", "--render-only"),
            # Each platform builds natively on its own runner. With only
            # --target-platform, rattler-build renders a cross-compilation
            # from the platform this runs on, whose hashes differ.
            *("--target-platform", platform),
            *("--build-platform", platform),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(f"Rendering the recipes for {platform} failed")
    return json.loads(result.stdout)


def published_files(channel, subdirs):
    """The file names of the packages in the given subdirs of a channel."""
    files = set()
    for subdir in subdirs:
        # prefix.dev answers the default Python-urllib user agent with 403
        request = urllib.request.Request(
            f"{channel}/{subdir}/repodata.json",
            headers={"User-Agent": "nightly-builds-conda"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                repodata = json.load(response)
        except urllib.error.HTTPError as error:
            # A subdir that nothing was uploaded to yet
            if error.code == 404:
                continue
            raise
        files.update(repodata.get("packages", {}))
        files.update(repodata.get("packages.conda", {}))
    return files


def plan_jobs(overrides):
    """All build jobs, keyed by package, platform, and Python variant."""
    jobs = {}
    for platform in RUNNERS:
        for output in render(platform):
            recipe = output["recipe"]
            name = recipe["package"]["name"]
            if name not in PACKAGES:
                sys.exit(f"recipes/{name} has no build stage in {PACKAGES}")
            filename = "{}-{}-{}.conda".format(
                name, recipe["package"]["version"], recipe["build"]["string"]
            )
            # "noarch" for noarch packages, the platform otherwise
            subdir = output["build_configuration"]["target_platform"]
            if subdir == "noarch":
                job_platform, job_id, job_overrides = NOARCH_PLATFORM, "noarch", []
            else:
                python = output["build_configuration"]["variant"].get("python")
                if python not in overrides:
                    sys.exit(
                        f"{name} has the Python variant {python}, which is not in {NIGHTLY_VARIANTS}"
                    )
                # e.g. "3.15.* *_cp315" -> "py3.15"
                job_platform, job_id = platform, "py" + python.split(".*")[0]
                job_overrides = overrides[python]
            job = jobs.setdefault(
                (name, job_platform, job_id),
                {
                    "package": name,
                    "platform": job_platform,
                    "runner": RUNNERS[job_platform],
                    "id": job_id,
                    "overrides": job_overrides,
                    "subdir": subdir,
                    "files": [],
                },
            )
            # noarch packages render the same on every platform
            if filename not in job["files"]:
                job["files"].append(filename)
    for job in jobs.values():
        job["files"].sort()
        job["key"] = "+".join(job["files"])
        job["artifact"] = "packages-{package}-{platform}-{id}".format(**job)
    platforms = list(RUNNERS)
    return sorted(
        jobs.values(),
        key=lambda job: (
            PACKAGES.index(job["package"]),
            platforms.index(job["platform"]),
            job["id"],
        ),
    )


def upstream_jobs(job, jobs):
    """The jobs building the upstream packages that a job builds and tests against.

    These are the packages earlier in PACKAGES of the same Python variant on the
    same platform, or of all Python variants for a noarch package, which is
    tested with the oldest and the newest Python.
    """
    upstream = PACKAGES[: PACKAGES.index(job["package"])]
    return [
        other
        for other in jobs
        if other["package"] in upstream
        and other["platform"] == job["platform"]
        and job["id"] in ("noarch", other["id"])
    ]


def glob(names):
    """A glob of actions/download-artifact matching exactly the given names."""
    # A brace expansion of a single name would match it literally with braces
    return names[0] if len(names) == 1 else "{" + ",".join(names) + "}"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--all", action="store_true", help="plan every job, published or not"
    )
    parser.add_argument(
        "--attempted",
        type=Path,
        help="file with the keys of jobs already attempted, one per line, to leave out",
    )
    parser.add_argument(
        "--summary", type=Path, help="file to append a Markdown summary to"
    )
    args = parser.parse_args()

    nightly_variants = yaml.safe_load(NIGHTLY_VARIANTS.read_text())
    # The first of channel_sources, e.g. https://prefix.dev/cyclus/nightly
    channel = nightly_variants["channel_sources"][0].split(",")[0]

    jobs = plan_jobs(python_overrides(nightly_variants))
    published = (
        set() if args.all else published_files(channel, {job["subdir"] for job in jobs})
    )
    attempted = set(args.attempted.read_text().split()) if args.attempted else set()

    def is_published(job):
        return not args.all and all(f in published for f in job["files"])

    planned = {package: [] for package in PACKAGES}
    # The keys of the planned jobs, which are planned before the jobs that
    # build against them, as the jobs are in dependency order
    building = set()
    summary = ["| Package files | Status |", "| --- | --- |"]
    for job in jobs:
        upstream = [u for u in upstream_jobs(job, jobs) if not is_published(u)]
        if is_published(job):
            status = "published"
        elif job["key"] in attempted:
            status = "failed before, retried by the nightly run"
        elif any(u["key"] not in building for u in upstream):
            status = "upstream not built, waits for it"
        else:
            status = "build"
            job["upstream"] = [f for u in upstream for f in u["files"]]
            job["upstream_artifacts"] = (
                glob([u["artifact"] for u in upstream]) if upstream else ""
            )
            planned[job["package"]].append(job)
            building.add(job["key"])
        summary.append(f"| {'<br>'.join(job['files'])} | {status} |")

    for package, package_jobs in planned.items():
        print(f"{package}={json.dumps(package_jobs)}")
    print(f"any={str(any(planned.values())).lower()}")

    if args.summary:
        with args.summary.open("a") as f:
            f.write("## Planned builds\n\n" + "\n".join(summary) + "\n")


if __name__ == "__main__":
    main()
