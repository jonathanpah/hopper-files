#!/usr/bin/env bash
set -euo pipefail

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
treeish="${1:-HEAD}"
temporary_root="$(mktemp -d)"
cleanup() {
  rm -rf -- "$temporary_root"
}
trap cleanup EXIT

archive_root="$temporary_root/source"
tools_root="$temporary_root/tools"
wheel_root="$temporary_root/wheel"
site_packages="$tools_root/site-packages"
mkdir -p "$archive_root"
mkdir -p "$wheel_root"

git -C "$repository_root" rev-parse --is-inside-work-tree >/dev/null
tree_object="$(git -C "$repository_root" rev-parse --verify "${treeish}^{tree}")"
git -C "$repository_root" archive --format=tar "$tree_object" | tar -xf - -C "$archive_root"

mkdir -p "$tools_root" "$temporary_root/home"
if ! /usr/bin/python3 -m pip --version >/dev/null 2>&1; then
  printf '%s\n' 'The operating-system Python pip is required; install it from the signed distribution repository.' >&2
  exit 2
fi

run_pip() {
  operation="$1"
  shift
  env -i \
    HOME="$temporary_root/home" \
    PATH="/usr/bin:/bin" \
    PIP_CONFIG_FILE=/dev/null \
    PYTHONNOUSERSITE=1 \
    PYTHONPATH="$site_packages" \
    /usr/bin/python3 -m pip \
      --isolated \
      "$operation" \
      --disable-pip-version-check \
      --no-input \
      --index-url https://pypi.org/simple \
      "$@"
}

run_python() {
  env -i \
    HOME="$temporary_root/home" \
    PATH="/usr/bin:/bin" \
    PYTHONPATH="$site_packages" \
    /usr/bin/python3 "$@"
}

run_pip install --require-hashes --only-binary=:all: --target "$site_packages" -r "$archive_root/requirements.lock"
run_pip wheel --no-index --no-build-isolation --no-deps --wheel-dir "$wheel_root" "$archive_root"
wheel_paths=("$wheel_root"/*.whl)
test "${#wheel_paths[@]}" -eq 1
run_python "$archive_root/scripts/verify_built_wheel.py" "${wheel_paths[0]}"
run_pip install --no-index --no-deps --target "$site_packages" "${wheel_paths[0]}"
run_python "$archive_root/scripts/verify_repository.py"
run_python "$archive_root/scripts/verify_files_integration.py"
run_python "$archive_root/scripts/verify_trash_integration.py"
run_python "$archive_root/scripts/verify_navigation_integration.py"
run_python "$archive_root/scripts/verify_editor_integration.py"
run_python "$archive_root/scripts/verify_images_integration.py"
run_python -m pytest "$archive_root/tests"
