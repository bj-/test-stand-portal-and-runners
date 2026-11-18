#!/usr/bin/env bash
# One command: bootstrap the portal, then prepare and run the neutral example.
set -euo pipefail
workspace='' portal_source='' install_name='' install_port='' create_admin=0 retry=0 mode=install
usage() {
  printf '%s\n' 'Usage: bash deploy.sh --workspace PATH --portal-source PATH [--name NAME] [--port PORT] [--create-admin] [--retry] [--check | --dry-run]'
}
fail() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --workspace|--portal-source|--name|--port)
      (($# >= 2)) || fail "Для $1 требуется значение"
      case "$1" in
        --workspace) workspace=$2;; --portal-source) portal_source=$2;;
        --name) install_name=$2;; --port) install_port=$2;;
      esac
      shift 2;;
    --create-admin) create_admin=1; shift;;
    --retry) retry=1; shift;;
    --check) [[ $mode == install ]] || fail 'Выберите только один режим'; mode=check; shift;;
    --dry-run) [[ $mode == install ]] || fail 'Выберите только один режим'; mode=plan; shift;;
    -h|--help) usage; exit 0;;
    *) fail "Неизвестный аргумент: $1";;
  esac
done
[[ -n $workspace && -n $portal_source ]] || { usage >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
rerun() {
  printf 'Повторить: bash %q --workspace %q --portal-source %q' "$script_dir/deploy.sh" "$workspace" "$portal_source"
  [[ -z $install_name ]] || printf ' --name %q' "$install_name"
  [[ -z $install_port ]] || printf ' --port %q' "$install_port"
  ((create_admin == 0)) || printf ' --create-admin'
  ((retry == 0)) || printf ' --retry'
  printf '\n'
}
installer_args=(--workspace "$workspace/portal" --portal-source "$portal_source")
[[ -z $install_name ]] || installer_args+=(--name "$install_name")
[[ -z $install_port ]] || installer_args+=(--port "$install_port")
((create_admin == 0)) || installer_args+=(--create-admin)
[[ $mode != check ]] || installer_args+=(--check)
[[ $mode != plan ]] || installer_args+=(--dry-run)
if [[ $mode != install ]]; then
  printf 'План примера: installer/deploy_demo.py --workspace %q' "$workspace/example"
  ((retry == 0)) || printf ' --retry'
  printf ' (после успешной установки портала; сейчас не выполняется).\n'
fi
status=0
bash "$script_dir/installer/install.sh" "${installer_args[@]}" || status=$?
if ((status != 0)); then
  if ((status == 20)); then printf '%s\n' 'Завершите действие, указанное installer, затем продолжите всей командой развёртывания.' >&2; fi
  rerun >&2
  exit "$status"
fi
[[ $mode == install ]] || exit 0
# The installer is a child process: its PATH changes do not reach this shell.
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
python_bin=''
for candidate in python3.12 python3 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
    python_bin=$(command -v "$candidate"); break
  fi
done
if [[ -z $python_bin ]]; then
  printf '%s\n' 'Python >=3.12 установлен, но недоступен. Откройте новый терминал и повторите команду.' >&2
  rerun >&2
  exit 20
fi
demo_args=("$script_dir/installer/deploy_demo.py" --workspace "$workspace/example")
((retry == 0)) || demo_args+=(--retry)
status=0
"$python_bin" "${demo_args[@]}" || status=$?
if ((status != 0)); then rerun >&2; fi
exit "$status"
