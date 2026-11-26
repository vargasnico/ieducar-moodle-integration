#!/usr/bin/env bash
# Cenário 2: o Moodle está fora do ar.
# Esperado: o publicador tenta 3 vezes com backoff; depois a mensagem vai para a fila
# de retry (volta depois de RETRY_ATRASO segundos), sem travar as outras.
#   retry  o Moodle volta antes das tentativas acabarem: tudo se resolve sozinho.
#   dlq    o Moodle fica fora até as tentativas acabarem: as mensagens vão para a DLQ
#          e são reprocessadas depois da volta (POST /dlq/reprocessar).
#
# Uso: cenario2-moodle-fora.sh [retry|dlq]   (padrão: retry)
source "$(dirname "$0")/comum.sh"
MODO=${1:-retry}
[[ $MODO == retry || $MODO == dlq ]] || { echo "uso: $0 [retry|dlq]" >&2; exit 2; }
ATRASO=$(atraso_retry)

etapa "1. Derrubando o Moodle"
moodle_fora
pausa

etapa "2. Uma turma é renomeada no i-Educar e o detector publica a mudança"
alterar_ieducar renomear-turma
ciclo
pausa

if [[ $MODO == retry ]]; then
  etapa "3. Os comandos falham e esperam na fila de retry (volta a cada ${ATRASO}s)"
  until (( $(contar_fila publicador.retry) > 0 )); do sleep 1; done
  sleep "$ATRASO"
  filas
  logs publicador 1m 'tentativa' 3
else
  etapa "3. O Moodle continua fora até as tentativas acabarem: as mensagens vão para a DLQ"
  until (( $(contar_fila publicador.dlq) >= 9 )); do sleep 2; done
  filas
  logs publicador 1m 'DLQ' 3
fi
pausa

etapa "4. Moodle de volta"
moodle_volta
if (( $(contar_fila publicador.dlq) > 0 )); then
  echo "    $(contar_fila publicador.dlq) mensagem(ns) na DLQ; reprocessando:"
  curl -s -X POST "$PUBLICADOR/dlq/reprocessar"; echo
fi
esperar_filas
./scripts/verificar.sh
mariadb "SELECT fullname FROM mdl_course WHERE idnumber = 'ieducar-turma-3-componente-8'" | sed 's/^/    Moodle: /'
