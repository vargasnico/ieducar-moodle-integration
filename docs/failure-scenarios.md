# Cenários de falha

Cada cenário tem um script em [`scripts/falhas/`](../scripts/falhas). O script provoca
a falha no ambiente real (`docker compose up`), mostra o que acontece e confere o
resultado direto no banco do Moodle com [`scripts/verificar.sh`](../scripts/verificar.sh),
que conta usuários, cursos e inscrições e procura duplicados. Com `PAUSAR=1` o script
espera um Enter entre os passos, para apresentar.

| # | Falha | Resposta da arquitetura | Script |
|---|---|---|---|
| 1 | i-Educar fora do ar | Timeout + 3 tentativas com backoff; o ciclo falha sem publicar nada; o próximo ciclo encontra tudo o que mudou | `cenario1-ieducar-fora.sh` |
| 2 | Moodle fora do ar | Os comandos voltam pela fila de retry (10s) sem travar os outros; depois de 5 falhas vão para a DLQ, que é reprocessada | `cenario2-moodle-fora.sh [segundos]` |
| 3 | O Moodle cria, mas a resposta não chega | O retry repete a operação inteira: procura pelo `idnumber`, acha o que foi criado e só atualiza | `cenario3-resposta-perdida.sh` |
| 4 | Serviço reinicia no meio | Ack manual: o que não foi confirmado volta para a fila; tratar de novo não duplica | `cenario4-reinicio.sh [serviço]` |

Saídas abaixo: execuções de 22/11/2026 com o seed do `setup.sh`.

## 1. O i-Educar está fora do ar

**Resposta:**
- O cliente do i-Educar tem timeout de 3s para conectar e 10s para responder, e cada
  consulta é repetida 3 vezes com backoff exponencial.
- Se ainda falhar, o ciclo é abortado **antes de publicar qualquer coisa**: o detector
  só publica no fim, depois de ler tudo.
- Erro de rede é falha, não "lista vazia": ninguém é suspenso por engano.
- **Nada se perde:** o detector compara o estado inteiro com o snapshot, então o que
  mudou durante a queda aparece no primeiro ciclo depois da volta.

**Reproduzir:** `./scripts/falhas/cenario1-ieducar-fora.sh`. O script para o `nginx`
do i-Educar, renomeia uma turma direto no banco, roda um ciclo (que falha), sobe o
`nginx` e roda outro ciclo.

```
==> 3. O ciclo do detector falha (timeout/conexão, 3 tentativas com backoff)
    {"detail":"Falha ao ler o i-Educar: ConnectError('[Errno -3] Temporary failure in name resolution')"}
    "msg": "Retrying detector.ieducar.IEducarClient._get.<locals>.chamar in 0.943 seconds as it raised ConnectError…"
    "msg": "Retrying … in 1.18 seconds as it raised ConnectError…"
    "msg": "Ciclo de detecção falhou: ConnectError(…)"
==> 4. i-Educar de volta: o próximo ciclo encontra a mudança
    {"correlation_id":"69f422bf-…","eventos":{"ieducar.turma.alterada":1}}
    Moodle: 1º ano (1º ano) Matutino (renomeada) – Geografia (2026)
```

## 2. O Moodle está fora do ar

**Resposta:**
- O detector e o sincronizador continuam funcionando; os comandos se acumulam na fila
  do publicador (duráveis).
- O publicador tenta cada comando 3 vezes com backoff. Se falhar, publica a mensagem
  em `publicador.retry`, onde ela espera 10s e volta, e passa para a próxima. Uma
  mensagem com problema não trava a fila.
- Depois de 5 falhas (~1 minuto) a mensagem vai para `publicador.dlq`. Com o Moodle de
  volta, `POST localhost:8003/dlq/reprocessar` devolve as mensagens à fila.

**Reproduzir:** `./scripts/falhas/cenario2-moodle-fora.sh 30`. O número é por quantos
segundos o Moodle fica parado: com 30 a fila de retry resolve sozinha; com 90 as
mensagens vão para a DLQ e o script as reprocessa.

```
# Moodle fora por 30s
==> 3. Os comandos falham e esperam na fila de retry (Moodle fora por 30s)
    fila publicador.retry: 9 mensagem(ns)
    "msg": "moodle.curso.sincronizar.v1 falhou (tentativa 3 de 5), nova tentativa em 10s: ConnectError(…)"
==> 4. Moodle de volta
    usuários: 213   cursos: 177   inscrições: 1782
    nenhum duplicado

# Moodle fora por 90s
    fila publicador.dlq: 9 mensagem(ns)
    "msg": "moodle.curso.sincronizar.v1 falhou 5 vezes, enviada à DLQ: ConnectError(…)"
==> 4. Moodle de volta
    9 mensagem(ns) na DLQ; reprocessando:
{"reprocessadas":9}
    nenhum duplicado
```

## 3. O Moodle cria o registro, mas a resposta HTTP não chega

**O problema:** a integração recebe um timeout sem saber se a criação aconteceu.
Criar de novo daria erro ("login já existe") ou um duplicado.

**Resposta:**
- Todo usuário e curso criado pela integração leva um `idnumber`
  (`ieducar-servidor-639`), e o publicador **procura antes de criar**.
- O retry repete a operação inteira (procurar + criar), não só a chamada de criação.
  Na segunda tentativa ele encontra o usuário que o Moodle acabou de criar e só o
  atualiza.

**Reproduzir:** `./scripts/falhas/cenario3-resposta-perdida.sh`. O professor Diego
Rocha (sem turma no seed) ganha uma turma, e o Moodle precisa criar o usuário dele.

Não dá para atingir só a chamada de criação derrubando a rede. Por isso o publicador
tem `POST /simular/resposta-perdida`: na próxima criação, o Moodle cria de verdade e
o publicador descarta a resposta, levantando o mesmo `ReadTimeout` de uma resposta
que não chegou. Para poder repetir a demonstração, o script antes tira a turma do
Diego e apaga o usuário dele no Moodle.

```
==> 3. O que o publicador fez
    "msg": "Criando usuário ieducar-servidor-639"
    "msg": "SIMULAÇÃO: o Moodle criou ieducar-servidor-639, mas a resposta foi descartada"
    "msg": "Retrying publicador.publicacao.Publicador.tratar in 0.627 seconds as it raised ReadTimeout…"
==> 4. Resultado
    usuários ieducar-servidor-639 no Moodle: 1
    nenhum duplicado
```

A inscrição do Diego chegou antes de o usuário existir, falhou uma vez
("tentativa 1 de 5") e passou na volta da fila de retry.

## 4. Um serviço reinicia no meio da sincronização

**Resposta:**
- **Ack manual:** o RabbitMQ só apaga uma mensagem depois que o serviço termina de
  tratá-la. O que o serviço estava processando quando morreu volta para a fila e é
  tratado quando ele sobe.
- **Idempotência:** tratar de novo um comando já aplicado não muda nada (procurar
  antes de criar; inscrever duas vezes é o mesmo que uma).
- **Publicar antes de gravar** (detector e sincronizador): se o serviço morre entre
  publicar e gravar o estado, as mensagens são publicadas de novo na próxima vez, o
  que é inofensivo. O contrário (gravar e não publicar) perderia mudanças.

**Reproduzir:** `./scripts/falhas/cenario4-reinicio.sh publicador` (ou
`sincronizador`, `detector`). O script roda um ciclo completo (~2.170 comandos),
mata o serviço com SIGKILL no meio, sobe de novo e confere o Moodle antes e depois.

```
==> 1. Sincronização completa (reenvia tudo: ~2 mil comandos)
    fila publicador: 888 mensagem(ns)
==> 2. Matando o publicador no meio (SIGKILL)
    fila publicador: 842 mensagem(ns)
==> 4. Depois
    usuários: 213   cursos: 177   inscrições: 1782
    nenhum duplicado
```

O mesmo resultado matando o sincronizador.
