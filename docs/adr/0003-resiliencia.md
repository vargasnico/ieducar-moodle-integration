# ADR 0003 — Resiliência: idempotência, retry com backoff e DLQ

**Status:** aceita · **Data:** 2026-11-22

## Contexto

O enunciado pede consumidores idempotentes, timeout e retry com backoff nas chamadas
ao i-Educar e ao Moodle, e uma DLQ para as mensagens que falham repetidamente. A
arquitetura precisa responder a quatro cenários: i-Educar fora, Moodle fora, o
Moodle cria mas a resposta se perde, e um serviço reinicia no meio.

## Decisão

1. **Timeout** em todo cliente HTTP: 3s para conectar e 10s para responder.
2. **Retry com backoff exponencial** (biblioteca `tenacity`): 3 tentativas, com
   esperas de ~0,5s, 1s, 2s e um pouco de aleatoriedade (jitter). Só para falhas
   passageiras: rede, timeout e erro 5xx.
   - No detector, na chamada HTTP (leitura pode ser repetida à vontade).
   - No publicador, na **operação inteira** (procurar + criar), e não só na chamada
     de criação. Se o Moodle criou e a resposta se perdeu, a nova tentativa encontra
     o que foi criado em vez de tentar criar de novo.
3. **Fila de retry e DLQ no RabbitMQ.** Se as tentativas acabarem, a mensagem vai
   para `<fila>.retry`, uma fila sem consumidor que a devolve à fila principal depois
   de `RETRY_ATRASO` segundos (padrão 10). Enquanto isso as outras mensagens seguem.
   Depois de `RETRY_TENTATIVAS` falhas (padrão 5) a mensagem vai para `<fila>.dlq` e
   fica lá até `POST /dlq/reprocessar`. A fila tem TTL de 10s como teto e cada
   mensagem leva a própria expiração, então o atraso muda sem recriar a fila (o
   compose usa 3s no modo demonstração).
4. **Idempotência: procurar antes de criar.** Todo usuário e curso criado pela
   integração leva um `idnumber` (ex.: `ieducar-aluno-20`). O publicador procura por
   ele antes de criar e, se existe, só atualiza. Inscrever ou desinscrever duas vezes
   não muda nada no Moodle. Com comandos de estado desejado (ADR 0002), processar a
   mesma mensagem duas vezes é inofensivo.
5. **Ack manual.** O RabbitMQ só apaga a mensagem depois que o serviço termina de
   tratá-la. Se o serviço morrer no meio, ela é entregue de novo.
6. **Publicar antes de gravar.** O detector publica os eventos e só depois grava o
   snapshot; o sincronizador publica os comandos e só depois grava o estado. Uma
   queda entre as duas coisas faz a mensagem ser publicada de novo no próximo ciclo
   ou na reentrega, o que é inofensivo pelo item 4. Nada se perde.
7. **Versão por entidade.** Toda mensagem leva `ciclo_iniciado_em`, o início do
   ciclo do detector que a originou, e um ciclo posterior sempre descreve um estado
   mais novo. O sincronizador (por turma, servidor e aluno) e o publicador (por
   usuário, curso e inscrição) guardam o ciclo da última mensagem aplicada a cada
   entidade e **descartam** a de um ciclo anterior. Sem isso, reprocessar a DLQ podia
   desfazer uma alteração mais nova: reativar um aluno já suspenso ou reinscrever um
   aluno já dispensado (e essa reinscrição não seria corrigida depois, porque o
   sincronizador não sabe dela). Mensagens de um mesmo ciclo (reentregas) continuam
   sendo aplicadas, e o publicador trata uma entidade por vez.

## Alternativas

- **Circuit breaker:** desejável no enunciado, mas não implementado para manter a
  solução simples; o retry com backoff e a fila de retry já evitam martelar um
  sistema caído.
- **Outbox/inbox** (gravar a mensagem na mesma transação do estado): garantia mais
  forte, mas com mais código e tabelas. "Publicar antes de gravar" mais idempotência
  dá o mesmo resultado prático aqui.

## Consequências

- Os quatro cenários têm resposta e podem ser reproduzidos
  ([`failure-scenarios.md`](../failure-scenarios.md)).
- Uma queda do Moodle maior que ~1 minuto (5 tentativas × 10s, no padrão) manda as
  mensagens para a DLQ; elas precisam ser reprocessadas à mão depois que ele volta.
- Um erro permanente (ex.: login que já pertence a outra conta) também passa pelas 5
  tentativas antes de ir para a DLQ.
- Reprocessar a DLQ é sempre seguro: o que ainda vale é aplicado e o que ficou velho é
  descartado (métricas `mensagens_obsoletas_total` e `eventos_obsoletos_total`). O
  mesmo vale para mensagens que saem fora de ordem pela fila de retry.
- A versão depende do relógio do detector, que é o único a gerá-la.
