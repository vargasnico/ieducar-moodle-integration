#!/usr/bin/env bash
# Cenário 2: o Moodle está fora do ar.
# Esperado: o publicador tenta 3 vezes com backoff; depois a mensagem vai para a fila
# de retry (volta em 10s), sem travar as outras. Se o Moodle voltar antes de 5
# tentativas, tudo se resolve sozinho; se não, as mensagens vão para a DLQ e são
# reprocessadas depois (POST /dlq/reprocessar).
#
# Uso: cenario2-moodle-fora.sh [segundos fora do ar, padrão 30; use 90 para ver a DLQ]
source "$(dirname "$0")/comum.sh"
FORA=${1:-30}

etapa "1. Derrubando o Moodle"
docker compose stop moodle
pausa

etapa "2. Uma turma é renomeada no i-Educar e o detector publica a mudança"
alterar_ieducar renomear-turma
ciclo
pausa

etapa "3. Os comandos falham e esperam na fila de retry (Moodle fora por ${FORA}s)"
sleep "$FORA"
filas
logs publicador 2m 'tentativa|DLQ' 3
pausa

etapa "4. Moodle de volta"
docker compose start moodle
until docker compose exec -T moodle /opt/bitnami/php/bin/php -r \
  "exit(@file_get_contents('http://localhost:8080/login/index.php') === false ? 1 : 0);"; do sleep 2; done
dlq=$(docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages | awk '$1 == "publicador.dlq" { print $2 }')
if (( dlq > 0 )); then
  echo "    $dlq mensagem(ns) na DLQ; reprocessando:"
  curl -s -X POST "$PUBLICADOR/dlq/reprocessar"; echo
fi
esperar_filas
./scripts/verificar.sh
mariadb "SELECT fullname FROM mdl_course WHERE idnumber = 'ieducar-turma-3-componente-8'" | sed 's/^/    Moodle: /'
