#!/usr/bin/env bash
# Reprocessar a DLQ não desfaz uma alteração mais nova.
#
# 1. Com o Moodle fora, a turma é renomeada (versão A); os comandos esgotam as
#    tentativas e vão para a DLQ.
# 2. O Moodle volta e a turma é renomeada de novo (versão B), que é aplicada.
# 3. A DLQ (versão A, de um ciclo anterior) é reprocessada: o publicador vê que já
#    aplicou uma versão mais nova desses cursos e descarta a antiga. O Moodle fica com B.
source "$(dirname "$0")/comum.sh"
nome() { mariadb "SELECT fullname FROM mdl_course WHERE idnumber = 'ieducar-turma-3-componente-8'"; }
na_dlq() {
  docker compose exec -T rabbitmq rabbitmqctl -q list_queues name messages | awk '$1 == "publicador.dlq" { print $2 }'
}
moodle_no_ar() {
  until docker compose exec -T moodle /opt/bitnami/php/bin/php -r \
    "exit(@file_get_contents('http://localhost:8080/login/index.php') === false ? 1 : 0);"; do sleep 2; done
}

etapa "1. Moodle fora; turma renomeada (versão A); esperando os comandos irem para a DLQ"
echo "    Moodle agora: $(nome)"
docker compose stop moodle
alterar_ieducar renomear-turma
ciclo
until (( $(na_dlq) >= 9 )); do sleep 3; done
filas
pausa

etapa "2. Moodle de volta; turma renomeada de novo (versão B), aplicada normalmente"
docker compose start moodle
moodle_no_ar
alterar_ieducar renomear-turma
ciclo
esperar_filas
echo "    Moodle agora: $(nome)"
pausa

etapa "3. Reprocessando a DLQ (versão A, mais antiga)"
curl -s -X POST "$PUBLICADOR/dlq/reprocessar"; echo
esperar_filas
logs publicador 1m 'descartado' 3
echo "    Moodle agora: $(nome)   (continua a versão B)"
