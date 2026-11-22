# ADR 0002 — Comunicação por eventos e comandos no RabbitMQ

**Status:** aceita · **Data:** 2026-11-22

## Contexto

Os três serviços formam um pipeline (detector → sincronizador → publicador). É
preciso decidir como eles conversam, com qual broker e o que vai em cada mensagem.

## Decisão

- **Tudo entre os serviços é assíncrono, pelo RabbitMQ.** Não há chamada síncrona
  entre eles. REST existe só na borda: disparar um ciclo (`POST /ciclos`), health
  check, métricas e reprocessar a DLQ.
- **RabbitMQ, e não Kafka:** retry com atraso e dead-letter queue saem prontos (TTL +
  dead-letter); o ack é por mensagem, então uma mensagem com problema não trava as
  outras; e sobe num único container. Não precisamos do que o Kafka tem a mais
  (replay do histórico, volume muito alto).
- **Uma exchange `integracao` (topic) e duas filas:** `sincronizador` recebe
  `ieducar.#` e `publicador` recebe `moodle.#`.
- **Mensagens com o estado completo:**
  - os eventos dizem como a entidade está agora ("o aluno 20 está nestas turmas"), e
    não o que mudou;
  - os comandos dizem como o Moodle deve ficar ("este usuário deve existir assim").
  Repetir uma mensagem dá o mesmo resultado, e é isso que torna os consumidores
  idempotentes sem controle extra.
- **Nome e versão:** a routing key é `<tipo>.v<versão>` (ex.:
  `ieducar.aluno.alterado.v1`). Uma mudança incompatível no formato publica `v2`.
  Catálogo em [`architecture.md`](../architecture.md#catálogo-de-mensagens).

## Alternativas

- **REST entre os serviços:** mais simples de depurar, mas o Moodle fora derrubaria
  a cadeia inteira, e não haveria fila nem DLQ.
- **Eventos de diferença** ("aluno entrou na turma 3"): mensagens menores, mas uma
  mensagem perdida ou repetida deixaria o Moodle errado.

## Consequências

- O Moodle fica alguns segundos atrás do i-Educar.
- Uma mensagem pode ser entregue mais de uma vez, então todo consumidor precisa ser
  idempotente (ADR 0003).
- Uma inscrição pode chegar ao publicador antes do usuário ou do curso: ela falha e
  volta pela fila de retry até eles existirem.
