#!/usr/bin/env bash
# Conta, direto no banco do Moodle, os usuários, cursos e inscrições criados pela
# integração e verifica se há duplicados. Use antes e depois dos cenários de falha.

source "$(dirname "$0")/falhas/comum.sh"

etapa "Moodle"
read -r u ud <<<"$(mariadb "SELECT COUNT(*), COUNT(DISTINCT idnumber) FROM mdl_user WHERE idnumber LIKE 'ieducar-%' AND deleted = 0")"
read -r c cd <<<"$(mariadb "SELECT COUNT(*), COUNT(DISTINCT idnumber) FROM mdl_course WHERE idnumber LIKE 'ieducar-%'")"
read -r i id <<<"$(mariadb "SELECT COUNT(*), COUNT(DISTINCT ue.userid, e.courseid) FROM mdl_user_enrolments ue JOIN mdl_enrol e ON e.id = ue.enrolid JOIN mdl_course c ON c.id = e.courseid WHERE c.idnumber LIKE 'ieducar-%'")"
echo "    usuários: $u   cursos: $c   inscrições: $i"
if (( u == ud && c == cd && i == id )); then
  echo "    nenhum duplicado"
else
  echo "    DUPLICADOS: $((u - ud)) usuários, $((c - cd)) cursos, $((i - id)) inscrições"
  exit 1
fi

etapa "Filas com mensagens"
saida=$(filas)
echo "${saida:-    todas vazias}"
