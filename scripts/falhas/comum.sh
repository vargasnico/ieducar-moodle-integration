# Funções usadas pelos scripts de cenários de falha (source comum.sh).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

DETECTOR=http://localhost:${DETECTOR_PORT:-8001}
PUBLICADOR=http://localhost:${PUBLICADOR_PORT:-8003}

etapa() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
pausa() { [[ "${PAUSAR:-0}" == 1 ]] && read -rp "    [Enter] " _ || true; }

# Roda um ciclo do detector (espera o agendado terminar, se houver um rodando).
ciclo() {
  local r
  for _ in $(seq 60); do
    r=$(curl -s -X POST "$DETECTOR/ciclos?completo=${1:-false}")
    grep -q "em andamento" <<<"$r" || break
    sleep 1
  done
  echo "    $r"
}

# Mensagens em cada fila do RabbitMQ (só as que não estão vazias).
filas() {
  docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages |
    awk 'NR > 1 && $2 > 0 { printf "    fila %s: %s mensagem(ns)\n", $1, $2 }'
}

# Espera todas as filas principais e de retry esvaziarem.
esperar_filas() {
  for _ in $(seq "${1:-300}"); do
    total=$(docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages |
      awk 'NR > 1 && $1 !~ /dlq$/ { s += $2 } END { print s + 0 }')
    [[ $total == 0 ]] && return
    sleep 2
  done
}

mariadb() {
  docker compose exec -T moodle-mariadb sh -c \
    'mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE" -N -B -e "$0"' "$1"
}

alterar_ieducar() {
  docker compose exec -T php sh -c 'cat > /tmp/alterar.php' <scripts/falhas/ieducar_alterar.php
  docker compose exec -T -e ACAO="$1" php php artisan tinker --execute="require '/tmp/alterar.php';" |
    sed 's/^/    i-Educar: /'
}

logs() { docker compose logs --since "$2" "$1" | grep -E "$3" | tail -n "${4:-5}" | cut -c1-230; }
