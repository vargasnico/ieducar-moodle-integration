#!/usr/bin/env bash
# Conta, direto no banco do Moodle, os usuários, cursos e inscrições criados pela
# integração e verifica se há duplicados. Use antes e depois dos cenários de falha.
#
# Sai com 0 se está tudo certo, 1 se encontrou duplicados e 2 se não conseguiu
# consultar o Moodle ou o RabbitMQ (container parado, por exemplo).

source "$(dirname "$0")/falhas/comum.sh"

nao_consegui() {
  echo "    Não consegui consultar $1. O container $2 está no ar? (docker compose ps)" >&2
  exit 2
}

# Uma consulta que devolve dois números; qualquer outra coisa é falha na consulta.
contar() {
  local saida
  saida=$(mariadb "$1" 2>/dev/null) || nao_consegui "o banco do Moodle" moodle-mariadb
  [[ $saida =~ ^[0-9]+[[:space:]]+[0-9]+$ ]] || nao_consegui "o banco do Moodle" moodle-mariadb
  echo "$saida"
}

etapa "Moodle"
# Atribuição antes do read: se a consulta falha, o script para aqui (set -e), uma vez só.
r=$(contar "SELECT COUNT(*), COUNT(DISTINCT idnumber) FROM mdl_user WHERE idnumber LIKE 'ieducar-%' AND deleted = 0")
read -r u ud <<<"$r"
r=$(contar "SELECT COUNT(*), COUNT(DISTINCT idnumber) FROM mdl_course WHERE idnumber LIKE 'ieducar-%'")
read -r c cd <<<"$r"
r=$(contar "SELECT COUNT(*), COUNT(DISTINCT ue.userid, e.courseid) FROM mdl_user_enrolments ue JOIN mdl_enrol e ON e.id = ue.enrolid JOIN mdl_course c ON c.id = e.courseid WHERE c.idnumber LIKE 'ieducar-%'")
read -r i id <<<"$r"
echo "    usuários: $u   cursos: $c   inscrições: $i"
if (( u == ud && c == cd && i == id )); then
  echo "    nenhum duplicado"
else
  echo "    DUPLICADOS: $((u - ud)) usuários, $((c - cd)) cursos, $((i - id)) inscrições"
  exit 1
fi

etapa "Filas com mensagens"
docker compose exec -T rabbitmq rabbitmqctl -q status >/dev/null 2>&1 || nao_consegui "o RabbitMQ" rabbitmq
saida=$(filas)
echo "${saida:-    todas vazias}"
