#!/usr/bin/env bash
# Cenário 1: o i-Educar está fora do ar.
# Esperado: o detector tenta 3 vezes com backoff e o ciclo falha sem publicar nada.
# Uma mudança feita durante a queda não se perde: o primeiro ciclo depois da volta
# a encontra, porque o detector compara o estado inteiro com o snapshot.
source "$(dirname "$0")/comum.sh"

etapa "1. Derrubando o i-Educar (nginx)"
docker compose stop nginx
pausa

etapa "2. Durante a queda, uma turma é renomeada direto no banco do i-Educar"
alterar_ieducar renomear-turma
pausa

etapa "3. O ciclo do detector falha (timeout/conexão, 3 tentativas com backoff)"
ciclo
logs detector 1m 'Retrying|falhou' 4
pausa

etapa "4. i-Educar de volta: o próximo ciclo encontra a mudança"
docker compose start nginx
sleep 3
ciclo
esperar_filas
mariadb "SELECT fullname FROM mdl_course WHERE idnumber LIKE 'ieducar-turma-3-%' LIMIT 3" |
  sed 's/^/    Moodle: /'
