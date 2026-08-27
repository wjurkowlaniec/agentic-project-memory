#!/bin/bash
set -euo pipefail

usage() {
    printf '%s\n' "Usage: $0 --project-root PATH [--name NAME] [--embedding-model MODEL] [--python PYTHON] [--no-rules] [--skip-tool-install]" >&2
}

project_root=''
project_name=''
embedding_model=''
python_spec='3.11'
no_rules=0
skip_tool_install=0

while (($# > 0)); do
    case "$1" in
        --project-root)
            (($# >= 2)) || { usage; exit 2; }
            project_root=$2
            shift 2
            ;;
        --name)
            (($# >= 2)) || { usage; exit 2; }
            project_name=$2
            shift 2
            ;;
        --embedding-model)
            (($# >= 2)) || { usage; exit 2; }
            embedding_model=$2
            shift 2
            ;;
        --python)
            (($# >= 2)) || { usage; exit 2; }
            python_spec=$2
            shift 2
            ;;
        --no-rules)
            no_rules=1
            shift
            ;;
        --skip-tool-install)
            skip_tool_install=1
            shift
            ;;
        -h|--help)
            usage >&2
            exit 0
            ;;
        *)
            printf 'error: unknown argument: %s\n' "$1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$project_root" ]]; then
    printf 'error: --project-root is required\n' >&2
    usage
    exit 2
fi

project_root=$(cd -- "$project_root" && pwd -P)
if [[ -z "$project_name" ]]; then
    project_name=${project_root##*/}
fi
if [[ -z "$project_name" ]]; then
    printf 'error: cannot derive project name from --project-root\n' >&2
    exit 2
fi

command -v uv >/dev/null 2>&1 || {
    printf 'error: uv is required (install it before running this installer)\n' >&2
    exit 2
}

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if ((skip_tool_install == 0)); then
    uv tool install --force --editable "$repo_root" --python "$python_spec"
fi

uv_bin_dir=$(uv tool dir --bin)
pmem="$uv_bin_dir/pmem"
if [[ ! -x "$pmem" ]]; then
    printf 'error: pmem executable not found under uv tool bin directory: %s\n' "$uv_bin_dir" >&2
    exit 2
fi

if [[ ":${PATH}:" != *":${uv_bin_dir}:"* ]]; then
    printf 'warning: uv tool bin directory is not on PATH: %s\n' "$uv_bin_dir" >&2
    printf '  export PATH=%q:$PATH\n' "$uv_bin_dir" >&2
fi

if help_output=$("$pmem" --help 2>&1); then
    :
else
    help_status=$?
    printf 'error: pmem --help failed (exit %s); reinstall the editable tool and retry\n' "$help_status" >&2
    exit 2
fi

if "$pmem" status --root "$project_root" >/dev/null 2>&1; then
    printf 'Existing project registration preserved: %s\n' "$project_root"
else
    init_args=(init --root "$project_root" --name "$project_name")
    if [[ -n "$embedding_model" ]]; then
        export PMEM_EMBEDDING_MODEL="$embedding_model"
    fi
    "$pmem" "${init_args[@]}"
fi

if ((no_rules == 0)); then
    "$pmem" install-rules --root "$project_root" --create-agents
fi

"$pmem" status --root "$project_root"
printf '\nNext manual commands (run after loading Nomic):\n'
printf '  pmem sync --root %q\n' "$project_root"
printf '  pmem search --root %q <query words>\n' "$project_root"
printf '  pmem preflight --root %q "<current user request>"\n' "$project_root"
printf 'Nomic must be loaded before embedding sync; this installer never loads models or runs sync/search/preflight.\n'
