#!/usr/bin/env bash
# Sobe e prepara todo o ambiente: i-Educar instalado (com dados de teste), Moodle
# configurado e a integração já sincronizada. Pode ser executado de novo a qualquer
# momento: cada passo confere o que já foi feito antes de agir.
#
# Uso: ./setup.sh [--sem-dados]
#   --sem-dados  não carrega escolas, turmas e professores de teste no i-Educar
#
# Funciona no Linux, no macOS e no Windows pelo Git Bash (instalado com o Git).

set -Eeuo pipefail
cd "$(dirname "$0")"
# O Git Bash converte argumentos como /tmp/x em C:/...; os caminhos aqui são do container.
export MSYS_NO_PATHCONV=1
# Sem isso o buildx anexa a cada build uma attestation com data/hora, a imagem ganha um
# ID novo mesmo com tudo em cache e o compose recria os containers a cada execução.
export BUILDX_NO_DEFAULT_ATTESTATIONS=1

DADOS_DEMO=1
for arg in "$@"; do
  case "$arg" in
    --sem-dados) DADOS_DEMO=0 ;;
    -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opção desconhecida: $arg (veja --help)" >&2; exit 2 ;;
  esac
done

etapa() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
falha() { printf '\n\033[1;31mErro: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'falha "o passo acima falhou. Veja a mensagem do comando e a seção \"Solução de problemas\" do README."' ERR

ieducar() { docker compose exec -T php "$@"; }
artisan() { ieducar php artisan "$@"; }
sql() { docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -Atc "$0"' "$1"; }

# Chama a API da integração de dentro do container (não depende de curl nem da porta
# publicada). Imprime o JSON formatado; o código de saída é o status HTTP / 100.
api() {
  docker compose exec -T integration python -c '
import json, sys, urllib.error, urllib.request
req = urllib.request.Request("http://localhost:8000" + sys.argv[2], method=sys.argv[1])
try:
    with urllib.request.urlopen(req, timeout=600) as r:
        status, body = r.status, r.read()
except urllib.error.HTTPError as e:
    status, body = e.code, e.read()
print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
sys.exit(status // 100 if status >= 400 else 0)
' "$1" "$2"
}

# Altera (ou acrescenta) uma variável em i-educar/.env; marca se mudou de valor.
ENV_MUDOU=0
definir_env() {
  grep -qx "$1=$2" i-educar/.env && return
  ENV_MUDOU=1
  if grep -q "^$1=" i-educar/.env; then
    sed -i.bak "s/^$1=.*/$1=$2/" i-educar/.env && rm -f i-educar/.env.bak
  else
    printf '%s=%s\n' "$1" "$2" >>i-educar/.env
  fi
}

porta() { docker compose port "$1" "$2" 2>/dev/null | sed 's/.*://'; }

# Compilar os relatórios leva uns 2 minutos; só é preciso se algum .jrxml não tiver
# o .jasper correspondente ou tiver sido alterado depois dele.
relatorios_desatualizados() {
  local f
  for f in $(find i-educar-reports/ieducar/ReportSources -name '*.jrxml'); do
    [[ "${f%.jrxml}.jasper" -nt "$f" ]] || return 0
  done
  return 1
}

# ---------------------------------------------------------------------------

etapa "Verificando pré-requisitos"
command -v docker >/dev/null || falha "Docker não encontrado: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || falha "o Docker não está rodando (ou seu usuário não tem permissão para usá-lo)."
versao=$(docker compose version --short 2>/dev/null) || falha "Docker Compose v2 não encontrado."
IFS=. read -r maior menor _ <<<"${versao#v}"
((maior > 2 || (maior == 2 && menor >= 20))) ||
  falha "Docker Compose $versao é antigo; o projeto precisa da versão 2.20 ou mais nova."
info "Docker Compose $versao"

etapa "Submodules (i-Educar e Módulo de Relatórios)"
if [[ -f i-educar/artisan && -f i-educar-reports/composer.json ]]; then
  info "já baixados"
else
  command -v git >/dev/null || falha "Git não encontrado: https://git-scm.com/"
  git submodule update --init
fi

etapa "Configuração do i-Educar (i-educar/.env)"
if [[ -f i-educar/.env ]]; then
  info "já existe"
else
  cp i-educar/.env.example i-educar/.env
  info "criado a partir de .env.example"
fi
# No Linux os arquivos montados mantêm o dono do host, então o usuário do container
# PHP precisa ter o mesmo UID/GID para escrever em vendor/, storage/ etc.
if [[ "$(uname -s)" == Linux ]]; then
  definir_env HOST_UID "$(id -u)"
  definir_env HOST_GID "$(id -g)"
  info "HOST_UID/HOST_GID = $(id -u)/$(id -g)"
fi

etapa "Subindo i-Educar e Moodle"
# A imagem PHP do i-Educar é construída na primeira vez pelo próprio compose; só
# precisa ser refeita quando HOST_UID/HOST_GID mudam.
((ENV_MUDOU)) && docker compose build php
# O Moodle já começa a se instalar em paralelo à instalação do i-Educar.
docker compose up -d php postgres redis moodle
for _ in $(seq 60); do
  docker compose exec -T postgres pg_isready -q && break
  sleep 2
done
docker compose exec -T postgres pg_isready -q || falha "o Postgres do i-Educar não respondeu."

etapa "Instalando o i-Educar (na primeira vez leva alguns minutos)"
# O Docker cria como root os diretórios do ponto de montagem do Módulo de Relatórios,
# e o composer plug-and-play precisa gravar em packages/.
docker compose exec -T -u root php sh -c '
  chown "$(stat -c %u:%g .)" packages packages/portabilis 2>/dev/null || true
  find bootstrap/cache storage -type d -exec chmod 777 {} +'
# A dependência do Módulo de Relatórios via .zip falha ao extrair no Windows.
ieducar composer config --global preferred-install.cossou/jasperphp source
ieducar composer install --no-interaction --no-progress
ieducar composer plug-and-play
grep -q '^APP_KEY=..*' i-educar/.env || artisan key:generate --force --ansi
[[ -L i-educar/public/storage || -e i-educar/public/storage ]] || artisan storage:link --ansi
artisan migrate --force --ansi
if [[ $(sql 'select count(*) from public.countries') == 0 ]]; then
  artisan db:seed --force --ansi
else
  info "dados básicos (países, estados, cidades) já carregados"
fi
if relatorios_desatualizados; then
  artisan community:reports:install
else
  artisan community:reports:install --no-compile
  info "relatórios já compilados"
fi
artisan vendor:publish --tag=reports-assets --ansi

if ((DADOS_DEMO)); then
  etapa "Dados de teste no i-Educar"
  if [[ $(sql 'select count(*) from pmieducar.escola') == 0 ]]; then
    artisan db:seed --class=DemoSeeder --force --ansi
  else
    info "já existem escolas cadastradas; DemoSeeder não executado"
  fi
  ieducar sh -c 'cat > /tmp/seed_professores.php' <scripts/ieducar_seed_professores.php
  artisan tinker --execute="require '/tmp/seed_professores.php';"
fi

etapa "Subindo o restante (na primeira vez o Moodle leva alguns minutos para instalar)"
docker compose build integration
docker compose up -d
for _ in $(seq 30); do
  api GET /health >/dev/null 2>&1 && break
  sleep 2
done
api GET /health >/dev/null 2>&1 || falha "a integração não respondeu; veja: docker compose logs integration"

etapa "Conferindo acesso da integração ao i-Educar e ao Moodle"
api GET /health/dependencias || true

if ((DADOS_DEMO)); then
  etapa "Primeira sincronização de professores"
  # A sincronização agendada roda ao subir a integração; se ainda estiver em
  # andamento a API responde 409 e tentamos de novo.
  for _ in $(seq 30); do
    if resultado=$(api POST '/sync/professores?completo=true' 2>&1); then break; fi
    sleep 5
  done
  printf '%s\n' "$resultado"
fi

etapa "Pronto!"
cat <<EOF
    Integração (docs)  http://localhost:$(porta integration 8000)/docs
    i-Educar           http://localhost:$(porta nginx 80)        admin / 123456789
    Moodle             http://localhost:$(porta moodle 8080)     admin / admin123

    A integração sincroniza sozinha a cada 5 minutos; para forçar:
      curl -X POST "http://localhost:$(porta integration 8000)/sync/professores?completo=true"
EOF
