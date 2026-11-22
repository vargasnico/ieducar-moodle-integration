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
   para `<fila>.retry`, uma fila sem consumidor com TTL de 10s que a devolve à fila
   principal. Enquanto isso as outras mensagens seguem. Depois de 5 falhas a
   mensagem vai para `<fila>.dlq` e fica lá até `POST /dlq/reprocessar`.
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
- Uma queda do Moodle maior que ~1 minuto (5 tentativas × 10s) manda as mensagens
  para a DLQ; elas precisam ser reprocessadas à mão depois que ele volta.
- Um erro permanente (ex.: login que já pertence a outra conta) também passa pelas 5
  tentativas antes de ir para a DLQ.
- Limitação: duas versões da mesma entidade podem ser aplicadas fora de ordem se uma
  delas passar pela fila de retry. Um ciclo completo (`POST /ciclos?completo=true`)
  corrige.
