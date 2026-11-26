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

# Mensagens numa fila (ex.: contar_fila publicador.dlq).
contar_fila() {
  docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages | awk -v f="$1" '$1 == f { print $2 }'
}

# Atraso da fila de retry configurado no publicador (RETRY_ATRASO, padrão 10s).
atraso_retry() { docker compose exec -T publicador printenv RETRY_ATRASO 2>/dev/null || echo 10; }

# O Moodle "cai" para a integração: o container é desconectado da rede do compose.
# Mais rápido que parar o container (que leva 20-40s para subir de novo): a falha é
# imediata (o nome "moodle" deixa de existir) e a volta é instantânea.
REDE_MOODLE=""
moodle_fora() {
  REDE_MOODLE=$(docker inspect moodle -f '{{range $rede, $_ := .NetworkSettings.Networks}}{{$rede}}{{end}}')
  docker network disconnect "$REDE_MOODLE" moodle
  echo "    Moodle desconectado da rede ($REDE_MOODLE): fora do ar para a integração"
  # Se o script for interrompido no meio, reconecta ao sair.
  trap moodle_volta EXIT
}
moodle_volta() {
  [[ -n $REDE_MOODLE ]] || return 0
  docker network connect --alias moodle "$REDE_MOODLE" moodle 2>/dev/null || true
  echo "    Moodle reconectado à rede"
  REDE_MOODLE=""
  trap - EXIT
}
