#!/usr/bin/env bash
# Cenário 3: o Moodle cria o usuário, mas a resposta HTTP não chega.
# Esperado: o publicador vê um timeout e repete a operação inteira: procura o usuário
# pelo idnumber, encontra o que o Moodle acabou de criar e só o atualiza. Um usuário,
# nenhum erro.
#
# O professor Diego Rocha (sem turma no seed) ganha uma turma, e o Moodle precisa
# criar o usuário dele. O publicador tem um modo de simulação que deixa a criação
# acontecer e descarta a resposta (POST /simular/resposta-perdida).
source "$(dirname "$0")/comum.sh"
sql() { docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -Atc "$0"' "$1"; }
diego=$(sql "SELECT p.idpes FROM cadastro.pessoa p JOIN pmieducar.servidor s ON s.cod_servidor = p.idpes WHERE p.nome = 'Diego Rocha'")
idnumber=ieducar-servidor-$diego

etapa "0. Preparação: Diego sem turma e sem usuário no Moodle"
if [[ $(sql "SELECT COUNT(*) FROM modules.professor_turma WHERE servidor_id = $diego") != 0 ]]; then
  alterar_ieducar vincular-diego && ciclo >/dev/null && esperar_filas
fi
# Como o Moodle faz ao excluir um usuário: marca como excluído e libera login e idnumber.
mariadb "UPDATE mdl_user SET deleted = 1, idnumber = '', username = CONCAT('excluido', id, UNIX_TIMESTAMP()) WHERE idnumber = '$idnumber'"
echo "    usuários $idnumber no Moodle: $(mariadb "SELECT COUNT(*) FROM mdl_user WHERE idnumber = '$idnumber'")"
pausa

etapa "1. Ligando a simulação: a próxima criação perde a resposta"
curl -s -X POST "$PUBLICADOR/simular/resposta-perdida"; echo
pausa

etapa "2. Diego recebe uma turma no i-Educar"
alterar_ieducar vincular-diego
ciclo
esperar_filas
pausa

etapa "3. O que o publicador fez"
logs publicador 2m 'Criando usuário|SIMULAÇÃO|Retrying' 3
pausa

etapa "4. Resultado"
echo "    usuários $idnumber no Moodle: $(mariadb "SELECT COUNT(*) FROM mdl_user WHERE idnumber = '$idnumber' AND deleted = 0")"
./scripts/verificar.sh
