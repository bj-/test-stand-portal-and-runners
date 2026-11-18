#!/usr/bin/env bash
# Bootstrap for Ubuntu 24.04+ and macOS. No privileged actions in --check/--dry-run.
set -euo pipefail

workspace='' portal_source='' install_name='' install_port='' create_admin=0 mode=install
usage() {
  printf '%s\n' 'Usage: bash installer/install.sh --workspace PATH --portal-source PATH [--name NAME] [--port PORT] [--create-admin] [--check | --plan | --dry-run]'
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
    --check) [[ $mode == install ]] || fail 'Выберите только один режим'; mode=check; shift;;
    --plan|--dry-run) [[ $mode == install ]] || fail 'Выберите только один режим'; mode=plan; shift;;
    -h|--help) usage; exit 0;;
    *) fail "Неизвестный аргумент: $1";;
  esac
done
[[ -n $workspace && -n $portal_source ]] || { usage >&2; exit 1; }
[[ -f $portal_source/docker-compose.yaml && -f $portal_source/Dockerfile ]] || fail 'portal-source должен содержать docker-compose.yaml и Dockerfile продукта'
if [[ -n $install_name && ! $install_name =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]]; then fail 'Некорректное имя (1–40 букв a-z, цифр, _ или -, начинается с буквы/цифры)'; fi
if [[ -n $install_port ]]; then
  [[ $install_port =~ ^[0-9]{4,5}$ ]] || fail 'Порт должен быть от 1024 до 65535'
  ((10#$install_port >= 1024 && 10#$install_port <= 65535)) || fail 'Порт должен быть от 1024 до 65535'
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
launcher="$script_dir/../local/portal_local.py"
platform=$(uname -s)
rerun() {
  printf 'Повторить: bash %q --workspace %q --portal-source %q' "$script_dir/install.sh" "$workspace" "$portal_source"
  [[ -z $install_name ]] || printf ' --name %q' "$install_name"
  [[ -z $install_port ]] || printf ' --port %q' "$install_port"
  ((create_admin == 0)) || printf ' --create-admin'
  printf '\n'
}
resume() { printf '%s\n' "$*" >&2; rerun >&2; exit 20; }
case "$platform" in
  Darwin) ;;
  Linux)
    [[ -f /etc/os-release ]] || fail 'Поддерживается Ubuntu 24.04+ или macOS'
    os_id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
    version_id=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')
    [[ $os_id == ubuntu && ${version_id%%.*} =~ ^[0-9]+$ ]] || fail 'Linux bootstrap поддерживает только Ubuntu 24.04+'
    ((${version_id%%.*} >= 24)) || fail 'Нужна Ubuntu 24.04+'
    ;;
  *) fail 'Этот скрипт поддерживает Ubuntu 24.04+ и macOS; для Windows используйте install.ps1';;
esac
if [[ $mode == plan ]]; then
  printf '%s\n' 'План: проверить Python >=3.12, Git, Docker Compose v2 и Linux daemon.'
  if [[ $platform == Darwin ]]; then
    printf '%s\n' 'При необходимости: официальный Homebrew installer; brew install python@3.12 git; brew install --cask docker-desktop; open -a Docker.'
  else
    printf '%s\n' 'При необходимости: sudo apt-get install python3 git ca-certificates curl; официальный Docker apt repo/key; docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; systemctl enable --now docker.' 'При необходимости: добавить текущего пользователя в группу docker; завершить с кодом 20 для перелогина.'
  fi
  printf '%s\n' 'Затем launcher init, doctor, up; ожидание /api/health; автоматическая admin-init: admin/admin только при пустой базе пользователей, существующие учётные записи сохраняются. Затем agent-init и agent-start: токен host-агента сохраняется, фоновый worker запускается автоматически.'
  rerun
  exit 0
fi

python_bin=''
pick_python() {
  local candidate
  for candidate in python3.12 python3 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
      python_bin=$(command -v "$candidate"); return 0
    fi
  done
  return 1
}
working_docker() { pick_python && "$python_bin" "$script_dir/probe_docker.py"; }
if [[ $mode == check ]]; then
  printf '%s\n' 'При установке после health выполняется автоматическая admin-init; сейчас база пользователей не изменяется и host-агент не запускается (agent-init/agent-start только при установке).'
  missing=0
  if pick_python; then printf 'Python >=3.12: OK\n'; else printf 'Python >=3.12: отсутствует\n'; missing=1; fi
  if command -v git >/dev/null 2>&1; then printf 'Git: OK\n'; else printf 'Git: отсутствует\n'; missing=1; fi
  if working_docker; then printf 'Docker Compose + Linux daemon: OK\n'; else printf 'Docker Compose/Linux daemon: недоступен\n'; missing=1; fi
  exit "$missing"
fi

if [[ $platform == Darwin ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    for brew_path in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [[ -x $brew_path ]]; then export PATH="$(dirname -- "$brew_path"):$PATH"; break; fi
    done
  fi
  if ! pick_python || ! command -v git >/dev/null 2>&1 || ! working_docker; then
    if ! command -v brew >/dev/null 2>&1; then
      printf '%s\n' 'Установка Homebrew из официального installer; отвечайте на его запросы.'
      # Download to an isolated file, then run Bash explicitly; no eval.
      brew_installer=$(mktemp -t portal-homebrew.XXXXXX)
      curl --fail --show-error --location https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh --output "$brew_installer"
      /bin/bash "$brew_installer"
      for brew_path in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [[ -x $brew_path ]]; then export PATH="$(dirname -- "$brew_path"):$PATH"; break; fi
      done
      command -v brew >/dev/null 2>&1 || fail 'Homebrew недоступен после установки; проверьте его сообщения'
    fi
    pick_python || brew install python@3.12
    command -v git >/dev/null 2>&1 || brew install git
    if ! working_docker && [[ ! -d /Applications/Docker.app ]]; then brew install --cask docker-desktop; fi
  fi
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
  if ! working_docker; then
    printf '%s\n' 'Запускаю Docker Desktop. Откройте Docker и самостоятельно завершите первый запуск/соглашение.'
    open -a Docker
  fi
else
  ((EUID != 0)) || fail 'Запустите installer от обычного пользователя; для установки он сам вызовет sudo'
  command -v sudo >/dev/null 2>&1 || fail 'Для установки нужен sudo'
  if ! pick_python || ! command -v git >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 git ca-certificates curl
  fi
  if ! command -v docker >/dev/null 2>&1; then
    conflicts=()
    for package in docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc; do
      if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then conflicts+=("$package"); fi
    done
    ((${#conflicts[@]} == 0)) || fail "Найдены конфликтующие пакеты: ${conflicts[*]}. Разберите их по инструкции Docker для Ubuntu; installer ничего не удаляет."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    # Do not overwrite an unrelated repository definition or key.
    if [[ ! -e /etc/apt/keyrings/docker.asc ]]; then
      curl --fail --show-error --location https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
      sudo chmod a+r /etc/apt/keyrings/docker.asc
    fi
    if [[ ! -e /etc/apt/sources.list.d/docker.sources ]]; then
      if [[ -e /etc/apt/sources.list.d/docker.list ]]; then fail 'Уже есть docker.list; проверьте существующий Docker apt repo вручную'; fi
      codename=$(sed -n 's/^UBUNTU_CODENAME=//p' /etc/os-release | tr -d '"')
      [[ -n $codename ]] || codename=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | tr -d '"')
      [[ $codename =~ ^[a-z]+$ ]] || fail 'Не удалось определить Ubuntu codename'
      architecture=$(dpkg --print-architecture)
      printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$codename" "$architecture" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
    fi
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker
  elif ! docker compose version >/dev/null 2>&1; then
    fail 'Docker уже установлен, но Compose v2 недоступен. Установите совместимый Compose plugin по инструкции своего Docker; существующую установку installer не заменяет.'
  fi
  if ! working_docker; then
    if [[ -S /var/run/docker.sock && ! -w /var/run/docker.sock ]]; then
      if ! id -nG | tr ' ' '\n' | grep -qx docker; then
        sudo usermod -aG docker "$(id -un)"
        resume 'Пользователь добавлен в группу docker. Полностью выйдите из сеанса и войдите снова. Членство в docker даёт расширенные полномочия на хосте.'
      fi
      resume 'Нет доступа к Docker socket. Полностью выйдите из сеанса и войдите снова; затем повторите installer.'
    fi
    sudo systemctl start docker
    if [[ -S /var/run/docker.sock && ! -w /var/run/docker.sock ]]; then
      if ! id -nG | tr ' ' '\n' | grep -qx docker; then
        sudo usermod -aG docker "$(id -un)"
      fi
      resume 'Docker запущен, но текущий сеанс не имеет доступа к socket. Полностью выйдите из сеанса и войдите снова.'
    fi
  fi
fi

pick_python || fail 'Python >=3.12 недоступен после установки'
printf '%s\n' 'Ожидаю Docker daemon (до 120 секунд)…'
deadline=$((SECONDS + 120))
until working_docker; do
  if ((SECONDS >= deadline)); then resume 'Docker пока недоступен. Запустите Docker Desktop/Engine с Linux containers, завершите первый запуск и повторите installer.'; fi
  sleep 3
done
init_args=(init)
[[ -z $install_name ]] || init_args+=(--name "$install_name")
[[ -z $install_port ]] || init_args+=(--port "$install_port")
"$python_bin" "$launcher" --workspace "$workspace" --portal-source "$portal_source" "${init_args[@]}"
"$python_bin" "$launcher" --workspace "$workspace" doctor
"$python_bin" "$launcher" --workspace "$workspace" up
"$python_bin" "$script_dir/wait_health.py" --workspace "$workspace" --timeout 180
"$python_bin" "$launcher" --workspace "$workspace" admin-init
"$python_bin" "$launcher" --workspace "$workspace" agent-init
"$python_bin" "$launcher" --workspace "$workspace" agent-start
