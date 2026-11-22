# ADR 0006 — Arquitetura hexagonal dentro de cada serviço

**Status:** aceita · **Data:** 2026-11-22

## Contexto

No T1 a integração tinha camadas (API, casos de integração, regras compartilhadas,
interfaces e infraestrutura) e interfaces (`Protocol`) separando as regras dos
clientes HTTP. Na primeira versão do T2, cada serviço ficou com todos os arquivos numa
pasta só, e alguns misturavam responsabilidades: o sincronizador decidia os comandos,
gravava no SQLite e publicava no RabbitMQ no mesmo método. Isso dificultava testar as
regras sem banco e broker, e explicar onde cada decisão mora.

## Decisão

Os três serviços seguem a mesma estrutura hexagonal (portas e adaptadores):

```
src/<serviço>/
├── dominio/      regras puras: sem banco, rede nem framework
├── aplicacao/    o caso de uso do serviço: orquestra domínio e portas
├── portas.py     interfaces (Protocol) do que o caso de uso precisa do mundo externo
├── adaptadores/  implementações das portas (SQLite, RabbitMQ, i-Educar, Moodle) e
│                 entradas do serviço (HTTP, consumo de mensagens, agendador)
├── infra/        logs, métricas, retry
├── mensagem.py   formato das mensagens (o contrato com os outros serviços)
└── main.py       composição: cria os adaptadores e os injeta no caso de uso
```

| Serviço | Domínio | Caso de uso | Portas |
|---|---|---|---|
| detector | montar o estado de cada entidade; detectar o que mudou desde o snapshot | `RodarCiclo` | `IEducar`, `Snapshot`, `PublicadorDeEventos` |
| sincronizador | regras do T1; planejar os comandos a partir do evento e do estado anterior; versão | `TratarEvento` | `EstadoSincronizacao`, `PublicadorDeComandos` |
| publicador | decidir entre atualizar, criar ou nada a fazer (procurar antes de criar); versão | `AplicarComando` | `Moodle`, `EstadoPublicador` |

- O **domínio** só recebe e devolve dados: por exemplo, `planejar_usuario(usuario,
  papel, cursos, antes) → Plano` ou `decidir_usuario(por_idnumber, por_login,
  suspenso) → Atualizar | Criar | NadaAFazer`.
- O **caso de uso** decide a ordem das coisas, e é nele que ficam as garantias de
  resiliência: publicar antes de gravar, descartar a versão antiga, retry da operação
  inteira, uma entidade por vez.
- A **simulação** do cenário 3 é um adaptador que embrulha o do Moodle
  (`MoodleComFalhaSimulada`) e implementa a mesma porta: o caso de uso não sabe que
  está sendo testado.

## Alternativas

- **Pasta única por serviço** (a versão anterior): menos arquivos, mas regras, I/O e
  ordem de execução misturados.
- **Camadas tradicionais** (controller → service → repository): parecido, mas a
  dependência aponta para o banco. No hexagonal o domínio não depende de nada, e o
  RabbitMQ é só mais um adaptador, de entrada e de saída.

## Consequências

- As regras são testadas como funções puras (`tests/test_dominio.py` em cada serviço),
  e o caso de uso com adaptadores em memória, sem rede.
- Trocar SQLite por PostgreSQL, ou RabbitMQ por outro broker, é escrever um adaptador
  novo para a mesma porta.
- Mais arquivos por serviço (cerca de 15) e uma indireção a mais (as portas). O
  comportamento não mudou: os testes e todos os cenários de falha passam como antes.
