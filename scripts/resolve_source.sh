#!/usr/bin/env bash
# Resolve the head of the main branch of https://github.com/cyclus/<package>
# and print the values the recipe of <package> reads from the environment:
#
#   <PACKAGE>_GIT_REV  full SHA of the commit to build
#   <PACKAGE>_VERSION  <last tag>.post<commits since it>, or the tag itself
#
# The revision is resolved once and passed to every platform's build so that
# all platforms build the same commit even if main moves during the run.
#
# A .postN version sorts after the release it follows and before the next
# one (e.g. 1.6.0 < 1.6.0.post812 < 1.6.1), so the nightly is preferred over
# the stable release but still satisfies pins like cyclus 1.6.*.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <package>" >&2
  exit 1
fi

package="$1"
prefix="$(echo "${package}" | tr '[:lower:]-' '[:upper:]_')"

clone_dir="$(mktemp -d)"
trap 'rm -rf "${clone_dir}"' EXIT

# A blobless clone has the full history and tags, which git describe needs,
# without downloading any file contents.
git clone --quiet --filter=blob:none --no-checkout --branch main \
  "https://github.com/cyclus/${package}.git" "${clone_dir}"

git_rev="$(git -C "${clone_dir}" rev-parse HEAD)"
# e.g. 1.6.0-812-gc5f38f6c, or 1.6.0 on a tagged commit
description="$(git -C "${clone_dir}" describe --tags "${git_rev}")"
if [[ "${description}" =~ ^(.+)-([0-9]+)-g[0-9a-f]+$ ]]; then
  version="${BASH_REMATCH[1]}.post${BASH_REMATCH[2]}"
else
  version="${description}"
fi

echo "${prefix}_GIT_REV=${git_rev}"
echo "${prefix}_VERSION=${version}"
