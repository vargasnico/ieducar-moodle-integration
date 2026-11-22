#!/usr/bin/env bash
# Faz uma alteração de exemplo no i-Educar, para ver a sincronização incremental.
#
# Uso: scripts/alterar-ieducar.sh renomear-turma | vincular-diego
#   renomear-turma  alterna o sufixo " (renomeada)" no nome da primeira turma do ano
#   vincular-diego  alterna o vínculo do professor Diego Rocha com uma turma
source "$(dirname "$0")/falhas/comum.sh"
alterar_ieducar "${1:?uso: $0 renomear-turma | vincular-diego}"
