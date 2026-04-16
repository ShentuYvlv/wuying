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

validate_required_env() {
  local missing=()
  local key
  local device_pool_file
  local device_pool_has_enabled=0
  local device_pool_has_adb_endpoint=0

  for key in \
    SCRAPER_API_KEY \
    CRAWLER_CALLBACK_URL \
    CRAWLER_CALLBACK_API_KEY
  do
    if [[ -z "$(read_env_value "$key" "")" ]]; then
      missing+=("$key")
    fi
  done

  device_pool_file="$(read_env_value DEVICE_POOL_FILE config/device_pool.json)"
  if [[ -n "$device_pool_file" && -f "$device_pool_file" ]]; then
    if python - "$device_pool_file" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

enabled = [
    item for item in data
    if isinstance(item, dict) and bool(item.get("enabled", True))
]
raise SystemExit(0 if enabled else 1)
PY
    then
      device_pool_has_enabled=1
    fi

    if python - "$device_pool_file" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

for item in data:
    if not isinstance(item, dict):
        continue
    if not bool(item.get("enabled", True)):
        continue
    adb_endpoint = str(item.get("adb_endpoint") or "").strip()
    if adb_endpoint:
        raise SystemExit(0)
raise SystemExit(1)
PY
    then
      device_pool_has_adb_endpoint=1
    fi
  fi

  if [[ -z "$(read_env_value WUYING_INSTANCE_IDS "")" && "$device_pool_has_enabled" -eq 0 ]]; then
    missing+=("WUYING_INSTANCE_IDS 或 DEVICE_POOL_FILE(至少一台 enabled 设备)")
  fi

  local manual_adb_endpoint
  local access_key_id
  local access_key_secret
  manual_adb_endpoint="$(read_env_value WUYING_MANUAL_ADB_ENDPOINT "")"
  access_key_id="$(read_env_value ALIBABA_CLOUD_ACCESS_KEY_ID "")"
  access_key_secret="$(read_env_value ALIBABA_CLOUD_ACCESS_KEY_SECRET "")"
  if [[ -z "$manual_adb_endpoint" && "$device_pool_has_adb_endpoint" -eq 0 && ( -z "$access_key_id" || -z "$access_key_secret" ) ]]; then
    missing+=("WUYING_MANUAL_ADB_ENDPOINT 或 设备池 adb_endpoint 或 ALIBABA_CLOUD_ACCESS_KEY_ID/ALIBABA_CLOUD_ACCESS_KEY_SECRET")
  fi

  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi

  echo ">>> 启动配置不完整，请先创建并填写 ${COMPOSE_ENV_FILE}" >&2
  echo ">>> 缺少配置:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo ">>> 可先执行: cp .env.example .env" >&2
  return 1
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
  local attempts="${WUYING_HEALTH_ATTEMPTS:-30}"
  local interval_seconds="${WUYING_HEALTH_INTERVAL_SECONDS:-2}"
  local i

  echo ">>> 检查服务健康状态"

  for ((i = 1; i <= attempts; i++)); do
    if compose exec -T "$service_name" python - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY
    then
      echo ">>> 服务健康检查通过"
      return 0
    fi

    echo ">>> 健康检查未通过，等待重试 ${i}/${attempts}"
    sleep "$interval_seconds"
  done

  echo ">>> 服务健康检查失败，可执行 ./start.sh logs 查看日志" >&2
  compose ps >&2 || true
  compose logs --tail=120 "$service_name" >&2 || true
  return 1
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
    validate_required_env
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
    validate_required_env
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
