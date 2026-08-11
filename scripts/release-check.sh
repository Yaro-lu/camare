#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'release check failed: %s\n' "$1" >&2
  exit 1
}

require_file() {
  local path="$1"
  [[ -f "$path" && -s "$path" ]] || die "required release file is missing or empty: $path"
  [[ ! -L "$path" ]] || die "release file must not be a symbolic link: $path"
}

require_digest() {
  local digest="$1"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid sha256 image digest"
}

preflight() {
  [[ $# -eq 2 ]] || die "usage: release-check.sh preflight <vX.Y.Z> <main-ref>"
  local tag="$1"
  local main_ref="$2"
  local version object_type tag_commit head_commit

  [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || die "tag must match vX.Y.Z without leading zeroes"
  git show-ref --verify --quiet "refs/tags/$tag" || die "tag is not present in this checkout"

  object_type="$(git cat-file -t "refs/tags/$tag")"
  [[ "$object_type" == "tag" ]] || die "release tags must be annotated"

  tag_commit="$(git rev-parse "refs/tags/$tag^{commit}")"
  head_commit="$(git rev-parse HEAD)"
  [[ "$tag_commit" == "$head_commit" ]] || die "checked-out commit does not match the release tag"
  git rev-parse --verify --quiet "$main_ref^{commit}" >/dev/null \
    || die "main branch reference is unavailable"
  git merge-base --is-ancestor "$tag_commit" "$main_ref" \
    || die "release tag is not contained in main"

  version="${tag#v}"
  require_file LICENSE
  require_file THIRD_PARTY_NOTICES.md
  require_file CHANGELOG.md
  grep -Fqx "## [$version]" CHANGELOG.md \
    || die "CHANGELOG.md has no release heading for $version"
  grep -Fq "MIT License" LICENSE || die "LICENSE is not the expected MIT license"
  grep -Fq "Frigate" THIRD_PARTY_NOTICES.md \
    || die "THIRD_PARTY_NOTICES.md is incomplete"
}

check_manifest() {
  [[ $# -eq 3 ]] \
    || die "usage: release-check.sh manifest <image-ref> <amd64-digest> <arm64-digest>"
  local image_ref="$1"
  local expected_amd64="$2"
  local expected_arm64="$3"
  local manifest_json actual_amd64 actual_arm64

  require_digest "$expected_amd64"
  require_digest "$expected_arm64"
  command -v docker >/dev/null || die "docker is required for manifest validation"
  command -v jq >/dev/null || die "jq is required for manifest validation"

  manifest_json="$(docker buildx imagetools inspect --raw "$image_ref")"
  printf '%s' "$manifest_json" | jq -e '
    (.manifests | type == "array" and length == 2) and
    ([.manifests[] | select(
      .platform.os == "linux" and
      .platform.architecture == "amd64" and
      ((.platform.variant // "") == "")
    )] | length == 1) and
    ([.manifests[] | select(
      .platform.os == "linux" and
      .platform.architecture == "arm64" and
      ((.platform.variant // "") == "" or .platform.variant == "v8")
    )] | length == 1)
  ' >/dev/null || die "manifest must contain exactly linux/amd64 and linux/arm64"

  actual_amd64="$(printf '%s' "$manifest_json" | jq -r '
    .manifests[] |
    select(.platform.os == "linux" and .platform.architecture == "amd64") |
    .digest
  ')"
  actual_arm64="$(printf '%s' "$manifest_json" | jq -r '
    .manifests[] |
    select(.platform.os == "linux" and .platform.architecture == "arm64") |
    .digest
  ')"
  [[ "$actual_amd64" == "$expected_amd64" ]] \
    || die "amd64 manifest digest does not match the scanned image"
  [[ "$actual_arm64" == "$expected_arm64" ]] \
    || die "arm64 manifest digest does not match the scanned image"
}

check_checksums() {
  [[ $# -eq 2 ]] || die "usage: release-check.sh checksums <vX.Y.Z> <asset-dir>"
  local tag="$1"
  local asset_dir="$2"
  local version expected_file listed_file asset
  local -a expected_assets

  [[ "$tag" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || die "tag must match vX.Y.Z without leading zeroes"
  [[ -d "$asset_dir" && ! -L "$asset_dir" ]] || die "asset directory is invalid"
  version="${tag#v}"
  expected_assets=(
    "camare-$version.tar.gz"
    "image-digests.txt"
    "camare-portal-linux-amd64.spdx.json"
    "camare-portal-linux-arm64.spdx.json"
    "camare-lan-caddy-linux-amd64.spdx.json"
    "camare-lan-caddy-linux-arm64.spdx.json"
  )

  for asset in "${expected_assets[@]}"; do
    require_file "$asset_dir/$asset"
  done
  require_file "$asset_dir/SHA256SUMS"
  if find "$asset_dir" -mindepth 1 -maxdepth 1 -type l -print -quit | grep -q .; then
    die "release assets must not contain symbolic links"
  fi

  (
    cd "$asset_dir"
    sha256sum --check --strict --quiet SHA256SUMS
  ) || die "release asset checksum verification failed"

  expected_file="$(mktemp)"
  listed_file="$(mktemp)"
  trap 'rm -f "$expected_file" "$listed_file"' RETURN
  printf '%s\n' "${expected_assets[@]}" | LC_ALL=C sort > "$expected_file"
  awk '{ name=$2; sub(/^\*/, "", name); print name }' "$asset_dir/SHA256SUMS" \
    | LC_ALL=C sort > "$listed_file"
  diff -u "$expected_file" "$listed_file" >/dev/null \
    || die "SHA256SUMS must list every expected asset exactly once"

  for asset in "$asset_dir"/*.spdx.json; do
    jq -e '.spdxVersion | startswith("SPDX-")' "$asset" >/dev/null \
      || die "standalone SBOM is not valid SPDX JSON"
  done
}

case "${1:-}" in
  preflight)
    shift
    preflight "$@"
    ;;
  manifest)
    shift
    check_manifest "$@"
    ;;
  checksums)
    shift
    check_checksums "$@"
    ;;
  *)
    die "usage: release-check.sh {preflight|manifest|checksums} ..."
    ;;
esac
