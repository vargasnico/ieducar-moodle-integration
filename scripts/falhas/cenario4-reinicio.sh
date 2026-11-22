#!/usr/bin/env bash
# Cenário 4: um serviço reinicia no meio da sincronização.
# Esperado: nada se perde e nada se duplica. O RabbitMQ só apaga a mensagem depois do
# ack (dado ao fim do tratamento): o que estava sendo processado volta para a fila e é
# tratado de novo quando o serviço sobe; como os tratadores são idempotentes
# (procurar antes de criar), repetir não duplica.
#
# Uso: cenario4-reinicio.sh [publicador|sincronizador|detector]
source "$(dirname "$0")/comum.sh"
SERVICO=${1:-publicador}

etapa "0. Antes"
./scripts/verificar.sh
pausa

etapa "1. Sincronização completa (reenvia tudo: ~2 mil comandos)"
if [[ $SERVICO == detector ]]; then
  ciclo true & sleep 2
else
  ciclo true
  sleep 5
fi
filas
pausa

etapa "2. Matando o $SERVICO no meio (SIGKILL)"
docker compose kill "$SERVICO"
filas
sleep 5
pausa

etapa "3. $SERVICO de volta"
docker compose start "$SERVICO"
[[ $SERVICO == detector ]] && { sleep 5; ciclo true; }
esperar_filas
pausa

etapa "4. Depois"
./scripts/verificar.sh
