# i-Educar → Moodle em microsserviços (T2)

Integração entre o [i-Educar](https://github.com/portabilis/i-educar) (gestão escolar,
fonte da verdade) e o [Moodle](https://moodle.org/) (ambiente virtual de aprendizagem).
No T1 ela era um monólito; no T2 são **três serviços que se comunicam por mensagens
no RabbitMQ**.

```
           ┌──────────┐   eventos    ┌───────────────┐   comandos   ┌────────────┐
i-Educar ─▶│ detector │─────────────▶│ sincronizador │─────────────▶│ publicador │─▶ Moodle
           └──────────┘  (RabbitMQ)  └───────────────┘  (RabbitMQ)  └────────────┘
           o que mudou?                    regras                  escreve sem duplicar
```

| Serviço | O que faz | Dados próprios |
|---|---|---|
| **detector** (`:8001`) | A cada 60s lê o i-Educar e publica um evento para cada servidor, turma ou aluno que mudou | snapshot (hash do último estado publicado) |
| **sincronizador** (`:8002`) | Aplica as regras do T1 e publica os comandos para o Moodle | turmas e inscrições já pedidas |
| **publicador** (`:8003`) | Aplica os comandos no Moodle, procurando antes de criar | mapeamento idnumber → id do Moodle |

Cada serviço tem **repositório, testes, Dockerfile, versão e CI próprios**, além do
seu SQLite (nenhum acessa o banco de outro):

| Repositório | Papel |
|---|---|
| [`ieducar-moodle-detector`](servicos/detector) | serviço detector |
| [`ieducar-moodle-sincronizador`](servicos/sincronizador) | serviço sincronizador |
| [`ieducar-moodle-publicador`](servicos/publicador) | serviço publicador |
| `ieducar-moodle-integration` (este) | orquestração: `docker-compose.yml`, topologia do RabbitMQ, i-Educar, Moodle, documentação e cenários de falha |

Os serviços entram aqui como git submodules em `servicos/`, e tudo sobe com um único
`docker compose up`: os três serviços, o RabbitMQ, o i-Educar e o Moodle.

**Documentação:**
- [`docs/architecture.md`](docs/architecture.md): arquitetura, catálogo de eventos e comparação com o T1.
- [`docs/failure-scenarios.md`](docs/failure-scenarios.md): os quatro cenários de falha e como reproduzir.
- ADRs: [decomposição](docs/adr/0001-decomposicao.md) · [comunicação](docs/adr/0002-comunicacao.md) ·
  [resiliência](docs/adr/0003-resiliencia.md) · [stack](docs/adr/0004-stack.md) ·
  [repositórios e deploy](docs/adr/0005-repositorios-e-deploy.md) ·
  [arquitetura interna](docs/adr/0006-arquitetura-interna.md).

## Como executar

Pré-requisitos: [Docker](https://docs.docker.com/get-docker/) com Compose v2.20+ e
[Git](https://git-scm.com/). No Windows, rode no Git Bash.

```bash
git clone --recurse-submodules https://github.com/vargasnico/ieducar-moodle-integration.git
cd ieducar-moodle-integration
./setup.sh
```

O `--recurse-submodules` baixa também os três serviços (e o i-Educar). Se clonou sem
ele: `git submodule update --init`.

O [`setup.sh`](setup.sh) sobe os containers, instala o i-Educar com dados de teste,
configura o Moodle e roda a primeira sincronização. A primeira vez leva 5–15 minutos;
as seguintes, cerca de 1 minuto. Com o i-Educar já instalado, basta
`docker compose up -d --build`.

## Roteiro: uma sincronização completa

**1. Os serviços estão no ar?**

```bash
curl localhost:8001/health; curl localhost:8002/health; curl localhost:8003/health
```

**2. Disparar um ciclo completo.** O parâmetro `completo=true` faz o detector reenviar
todas as entidades; sem ele, só o que mudou. A resposta traz o `correlation_id`:

```bash
curl -X POST "localhost:8001/ciclos?completo=true"
```

```json
{"correlation_id": "6523ebed-…", "eventos": {"ieducar.turma.alterada": 21, "ieducar.servidor.alterado": 4, "ieducar.aluno.alterado": 210}}
```

**3. Acompanhar as filas** na interface do RabbitMQ (http://localhost:15672,
`integracao` / `integracao`, aba *Queues*): a fila `publicador` enche e esvazia.

**4. Seguir a sincronização pelos três serviços**, pelo correlation ID nos logs:

```bash
docker compose logs detector sincronizador publicador | grep <correlation_id>
```

**5. Conferir o resultado no Moodle**, direto no banco:

```bash
./scripts/verificar.sh
```

```
usuários: 213   cursos: 177   inscrições: 1782
nenhum duplicado
```

Os cursos aparecem em http://localhost:8080 (`admin` / `admin123`), na categoria
**iEducar › 2026**.

**6. Uma mudança incremental.** Altere algo no i-Educar (http://localhost, `admin` /
`123456789`), por exemplo renomeie uma turma. No próximo ciclo (até 1 minuto, ou
`curl -X POST localhost:8001/ciclos`) o detector publica só aquela turma e os cursos
dela são renomeados no Moodle. Sem usar a interface:

```bash
./scripts/alterar-ieducar.sh renomear-turma && curl -X POST localhost:8001/ciclos
```

**7. Métricas:** `curl localhost:8003/metrics | grep -E "mensagens_total|sincronizacao_segundos"`.

## Cenários de falha

```bash
./scripts/falhas/cenario1-ieducar-fora.sh        # i-Educar fora do ar
./scripts/falhas/cenario2-moodle-fora.sh retry   # Moodle fora, volta antes da DLQ (dlq: até ir para a DLQ)
./scripts/falhas/cenario3-resposta-perdida.sh    # Moodle cria, mas a resposta não chega
./scripts/falhas/cenario4-reinicio.sh publicador # serviço morto no meio da sincronização
./scripts/falhas/dlq-versao-antiga.sh            # DLQ reprocessada não desfaz alteração mais nova
```

O compose sobe em **modo demonstração**, com o retry a cada 3s (o padrão dos serviços
é 10s), para os cenários serem rápidos; `RETRY_ATRASO=10 docker compose up -d
sincronizador publicador` volta ao valor normal.

`PAUSAR=1` antes do comando faz o script esperar um Enter a cada passo. Explicações e
saídas esperadas em [`docs/failure-scenarios.md`](docs/failure-scenarios.md).

## Regras da integração (as mesmas do T1)

| i-Educar | Moodle |
|---|---|
| Servidor ativo com vínculo com turma no ano letivo | Usuário `ieducar-servidor-<id>`, login pelo CPF |
| Turma × componente curricular | Um curso `ieducar-turma-<turma>-componente-<componente>`, na categoria do ano |
| Vínculo do professor com a turma e os componentes | Inscrição como `editingteacher` nos cursos desses componentes |
| Aluno ativo e enturmado | Usuário `ieducar-aluno-<código>`, inscrito como `student` nos cursos da sua série, menos os componentes de que tem dispensa |

- **Nada é apagado no Moodle:** quem deixa de ser professor ou aluno é suspenso, e o
  curso de uma turma desativada (ou de um componente retirado) é ocultado.
- **Identidade:** o vínculo com o i-Educar fica no `idnumber` do Moodle. Uma conta
  criada à mão com o mesmo login e sem `idnumber` é adotada.
- **E-mail:** a API do i-Educar não traz o e-mail do aluno; quem não tem e-mail recebe
  `<idnumber>@sem-email.invalid`.

**Dados de teste.** O `DemoSeeder` do i-Educar e o
[`scripts/ieducar_seed.php`](scripts/ieducar_seed.php) criam 21 turmas, 210 alunos e
quatro servidores:

| Servidor | Situação | No Moodle |
|---|---|---|
| Ana Paula Souza | professora de 1 turma, 2 componentes | usuário criado |
| Bruno Lima | professor unidocente de 1 turma | usuário criado |
| Carla Mendes | secretária | não entra |
| Diego Rocha | professor sem turma (o cenário 3 dá uma turma a ele) | não entra |

## APIs

| Serviço | Rota | Descrição |
|---|---|---|
| todos | `GET /health` | no ar e conectado ao RabbitMQ (200) ou não (503) |
| todos | `GET /metrics` | métricas no formato Prometheus |
| detector | `POST /ciclos?completo=false` | roda um ciclo de detecção agora |
| sincronizador | `GET /inscricoes` | inscrições pedidas, por papel |
| sincronizador, publicador | `POST /dlq/reprocessar` | devolve as mensagens da DLQ à fila |
| publicador | `POST /simular/resposta-perdida` | cenário 3: a próxima criação perde a resposta |

Documentação interativa em `/docs` de cada serviço (ex.: http://localhost:8001/docs).

## Configuração

Variáveis do `docker-compose.yml` (podem ficar num `.env` na raiz):

| Variável | Padrão | Descrição |
|---|---|---|
| `IEDUCAR_ACCESS_KEY` | `ieducar-access-key` | Chave da API do i-Educar (Configurações › Configurações do sistema) |
| `IEDUCAR_ESCOLA_ID`, `IEDUCAR_ANO_LETIVO` | — | Escola e ano; sem valor, as escolas ativas e o ano em andamento |
| `DETECTOR_INTERVALO` | `60` | Segundos entre ciclos de detecção |
| `MOODLE_TIMEOUT` | `10` | Timeout das chamadas ao Moodle (s) |
| `RETRY_ATRASO` | `3` (modo demonstração; padrão dos serviços: `10`) | Segundos na fila de retry antes de tentar de novo (máximo 10) |
| `RETRY_TENTATIVAS` | `5` | Falhas até a mensagem ir para a DLQ |
| `DETECTOR_PORT`, `SINCRONIZADOR_PORT`, `PUBLICADOR_PORT`, `RABBITMQ_UI_PORT` | `8001`, `8002`, `8003`, `15672` | Portas no host |

## Desenvolvimento e deploy de um serviço

Cada serviço é desenvolvido no próprio repositório (README de cada um), com testes
que rodam sem rede:

```bash
cd servicos/sincronizador
uv sync && uv run pytest
```

Para implantar **só aquele serviço** no ambiente, sem reiniciar os outros:

```bash
docker compose up -d --build sincronizador
```

Ou, com uma versão publicada pelo CI do serviço (imagem no GitHub Container Registry):

```bash
SINCRONIZADOR_IMAGEM=ghcr.io/<dono>/ieducar-moodle-sincronizador:v1.1.0 docker compose up -d sincronizador
```

Para fixar a versão nova no ambiente, mova o submodule e faça commit aqui:
`git -C servicos/sincronizador checkout v1.1.0 && git add servicos/sincronizador`.

Os serviços não compartilham código, e o contrato entre eles é o formato das
mensagens (catálogo em [`docs/architecture.md`](docs/architecture.md#catálogo-de-mensagens)).
Uma mudança incompatível num formato vira uma versão nova da mensagem (`.v2`).

## Estrutura

```
docker-compose.yml       # ambiente completo
setup.sh                 # sobe e prepara tudo
servicos/                # os três serviços (submodules, um repositório cada)
├── detector/            #   cada um em arquitetura hexagonal:
├── sincronizador/       #   dominio/ · aplicacao/ · portas.py · adaptadores/ · main.py
└── publicador/          #   (ADR 0006 e o README de cada serviço)
infra/rabbitmq/          # exchange, filas, retry e DLQ, criados quando o RabbitMQ sobe
docs/                    # architecture.md, failure-scenarios.md, adr/
scripts/                 # dados de teste, verificar.sh, falhas/ (cenários)
moodle/                  # preparação do Moodle (serviço moodle-setup)
i-educar/                # i-Educar (submodule)
```

Dentro de cada serviço o código segue a arquitetura hexagonal: o domínio (regras
puras) no centro, um caso de uso, portas (interfaces) e adaptadores para SQLite,
RabbitMQ, HTTP e o sistema externo ([ADR 0006](docs/adr/0006-arquitetura-interna.md)).
O código de infraestrutura comum (formato das mensagens, RabbitMQ com retry e DLQ,
logs) é copiado em cada serviço.

## Solução de problemas

- **`The container name ... is already in use`:** sobrou um container de outro
  compose com o mesmo nome; remova com `docker rm -f <nome>` e rode o `setup.sh` de novo.
- **Ciclo com `Chave de acesso inválida!`:** `IEDUCAR_ACCESS_KEY` diferente da chave
  configurada no i-Educar.
- **`Nenhuma escola ativa` / `Nenhum ano letivo em andamento`:** cadastre a escola e
  inicie o ano letivo no i-Educar, ou rode o `setup.sh`, que carrega os dados de teste.
- **Mensagens paradas na DLQ** (ex.: depois de o Moodle ficar muito tempo fora):
  `curl -X POST localhost:8003/dlq/reprocessar`.

## Autores

- Francisco Rotilli — [francisco.r@edu.pucrs.br](mailto:francisco.r@edu.pucrs.br)
- Nícolas Durgante Vargas — [nicolas.v02@edu.pucrs.br](mailto:nicolas.v02@edu.pucrs.br)
