# ADR 0005 — Um repositório por serviço, com deploy independente

**Status:** aceita · **Data:** 2026-11-22

## Contexto

Na primeira versão do T2 os três serviços ficavam num único repositório, com uma
pasta `comum/` compartilhada e uma única imagem Docker. Eles rodavam em containers
separados, mas não eram independentes: mudar o código comum mexia nos três, os três
tinham as mesmas dependências e versão, e não havia como publicar um sem os outros.
Microsserviços pedem que cada serviço possa ser desenvolvido, testado, versionado e
implantado sozinho.

## Decisão

- **Um repositório por serviço:** `ieducar-moodle-detector`,
  `ieducar-moodle-sincronizador` e `ieducar-moodle-publicador`. Cada um tem o próprio
  `pyproject.toml` (só as dependências que usa), `uv.lock`, testes, `Dockerfile`,
  README com o seu contrato, versão (tag `v1.0.0`) e pipeline de CI (testes e, a cada
  tag, a imagem `ghcr.io/<dono>/ieducar-moodle-<serviço>:<tag>`).
- **Um repositório de orquestração**, `ieducar-moodle-integration` (este): o
  `docker-compose.yml` do ambiente completo, a topologia do RabbitMQ, o i-Educar e o
  Moodle, a documentação de arquitetura (ADRs, catálogo de mensagens) e os scripts
  dos cenários de falha. Os serviços entram como **git submodules** em `servicos/`,
  cada um fixado numa versão: `git clone --recurse-submodules` mais `./setup.sh`
  (ou `docker compose up`) continua subindo tudo.
- **Nenhum código compartilhado.** O contrato entre os serviços é o **formato das
  mensagens** (documentado no catálogo), não uma biblioteca. O pouco de
  infraestrutura que os três usam (formato da mensagem, broker com retry e DLQ, logs,
  ~250 linhas) é copiado em cada repositório.
- **A topologia do RabbitMQ pertence ao ambiente** (`infra/rabbitmq/definitions.json`,
  carregado quando o broker sobe), e não a um serviço. Cada consumidor também
  declara a própria fila, o que não muda nada se ela já existe e permite rodá-lo
  sozinho em desenvolvimento. Nenhum serviço declara a fila de outro.
- **Deploy de um serviço só:** `docker compose up -d --build publicador` reconstrói e
  reinicia apenas ele. Para usar uma versão publicada em vez do código local:
  `PUBLICADOR_IMAGEM=ghcr.io/<dono>/ieducar-moodle-publicador:v1.1.0 docker compose up -d publicador`.

## Alternativas

- **Monorepo com pastas por serviço** (a versão anterior): mais simples de clonar e
  de mudar várias partes ao mesmo tempo, mas o build e a versão eram compartilhados,
  e o código comum acoplava os três.
- **Biblioteca compartilhada publicada** (pacote `comum` versionado): evita a cópia,
  mas cria uma dependência de release entre todos os serviços. Para ~250 linhas que
  quase nunca mudam, copiar é mais barato.

## Consequências

- Cada serviço evolui sozinho: o sincronizador pode mudar uma regra, rodar os seus
  testes e ser implantado sem rebuild nem restart do detector e do publicador.
- Uma mudança no formato de uma mensagem afeta quem a produz e quem a consome. Ela
  tem de ser compatível (campo novo opcional) ou virar uma versão nova
  (`moodle.usuario.sincronizar.v2`) publicada em paralelo até o consumidor migrar.
- Corrigir um bug no código copiado (ex.: `broker.py`) exige repetir a correção nos
  três repositórios.
- Atualizar a versão de um serviço no ambiente é um commit no repositório de
  orquestração que move o submodule (`git -C servicos/publicador pull` e
  `git add servicos/publicador`).
