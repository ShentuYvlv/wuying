#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
PROJECT_NAME_DEFAULT="$(basename "$ROOT_DIR" | tr '[:upper:]' '[:lower:]')"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$PROJECT_NAME_DEFAULT}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-./.env}"

COMPOSE_FILES_DEFAULT=(-f docker-compose.yml)
if [[ -n "${COMPOSE_FILES:-}" ]]; then
  read -r -a COMPOSE_FILES_ARR <<< "$COMPOSE_FILES"
else
  COMPOSE_FILES_ARR=("${COMPOSE_FILES_DEFAULT[@]}")
fi

if [[ -f "$COMPOSE_ENV_FILE" ]]; then
  COMPOSE_ENV_ARGS=(--env-file "$COMPOSE_ENV_FILE")
else
  COMPOSE_ENV_ARGS=()
fi

detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    COMPOSE_IS_V1=0
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    COMPOSE_IS_V1=1
  else
    echo "未找到 docker compose/docker-compose" >&2
    exit 1
  fi
}

compose() {
  "${COMPOSE_CMD[@]}" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_FILES_ARR[@]}" "$@"
}

read_env_value() {
  local key="$1"
  local default_value="$2"
  local value="${!key:-}"

  if [[ -z "$value" && -f "$COMPOSE_ENV_FILE" ]]; then
    value="$(
      grep -E "^${key}=" "$COMPOSE_ENV_FILE" 2>/dev/null \
        | tail -n 1 \
        | cut -d= -f2- \
        | tr -d '\r' \
        | sed 's/^"//; s/"$//'
    )"
  fi

  printf '%s' "${value:-$default_value}"
}

ensure_network() {
  local network_name="$1"
  if ! docker network inspect "$network_name" >/dev/null 2>&1; then
    echo ">>> 创建 Docker 网络: $network_name"
    docker network create "$network_name" >/dev/null
  fi
}

ensure_external_networks() {
  ensure_network "$(read_env_value WUYING_SHARED_NETWORK wuying-crawler-shared)"
}

clean_for_compose_v1() {
  if [[ "${COMPOSE_IS_V1:-0}" -eq 1 ]]; then
    echo "检测到 docker-compose v1，执行兼容清理..."
    compose down --remove-orphans || true
    compose rm -f -s || true
    local container_ids
    container_ids="$(docker ps -a --filter "label=com.docker.compose.project=${PROJECT_NAME}" -q)"
    if [[ -n "$container_ids" ]]; then
      echo "$container_ids" | xargs docker rm -f || true
    fi
    docker network rm "${PROJECT_NAME}_default" >/dev/null 2>&1 || true
  fi
}

health_check() {
  local service_name="${WUYING_CRAWLER_SERVICE:-wuying-crawler}"
  echo ">>> 检查服务健康状态"
  if compose exec -T "$service_name" python - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY
  then
    echo ">>> 服务健康检查通过"
  else
    echo ">>> 服务健康检查失败，可执行 ./start.sh logs 查看日志" >&2
    return 1
  fi
}

usage() {
  cat <<'EOF'
用法:
  ./start.sh up        # 构建并启动服务（默认）
  ./start.sh down      # 停止并移除容器
  ./start.sh restart   # 强制重建并重启服务
  ./start.sh status    # 查看服务状态
  ./start.sh logs      # 查看日志（可加服务名）
  ./start.sh build     # 仅构建镜像
  ./start.sh health    # 检查 API 健康状态

环境变量:
  COMPOSE_FILES      覆盖 compose 文件，默认: "-f docker-compose.yml"
  COMPOSE_ENV_FILE   compose 变量文件，默认: "./.env"
EOF
}

CMD="${1:-up}"
shift || true

detect_compose

case "$CMD" in
  up)
    ensure_external_networks
    clean_for_compose_v1
    compose build
    compose up -d --force-recreate --remove-orphans "$@"
    health_check
    ;;
  down)
    compose down "$@"
    ;;
  restart)
    ensure_external_networks
    clean_for_compose_v1
    compose down --remove-orphans "$@" || true
    compose build
    compose up -d --force-recreate --remove-orphans "$@"
    health_check
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs -f "$@"
    ;;
  build)
    compose build "$@"
    ;;
  health)
    health_check
    ;;
  *)
    usage
    exit 1
    ;;
esac
