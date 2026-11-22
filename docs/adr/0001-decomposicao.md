# ADR 0001 — Três serviços, um por etapa do fluxo

**Status:** aceita · **Data:** 2026-11-22

## Contexto

No T1 a integração era um monólito: o mesmo processo lia o i-Educar, aplicava as
regras e escrevia no Moodle, tudo na mesma execução. Se o Moodle estivesse fora, a
execução inteira falhava. O T2 pede pelo menos três serviços, cada um com uma
responsabilidade clara e dono dos próprios dados.

## Decisão

Separar pelas três etapas do fluxo, que é o exemplo do enunciado:

| Serviço | Responsabilidade | Fala com | Dados próprios |
|---|---|---|---|
| **detector** | Descobrir o que mudou no i-Educar | i-Educar | `snapshot` (hash do último estado publicado de cada entidade) |
| **sincronizador** | Aplicar as regras: o que deve existir no Moodle | ninguém (só o broker) | `turma` e `inscricao` (para saber o que remover) |
| **publicador** | Escrever no Moodle sem duplicar | Moodle | `mapeamento` (idnumber → id do Moodle) |

Cada serviço tem um SQLite num volume próprio; um não tem como abrir o banco do
outro. O que um precisa saber do outro chega por mensagem. Cada serviço também tem o
próprio repositório, versão e imagem ([ADR 0005](0005-repositorios-e-deploy.md)).

## Alternativas

- **Um serviço por entidade** (professores, turmas, alunos): os três leriam o
  i-Educar e escreveriam no Moodle, triplicando o tratamento de falhas, e as
  inscrições dependem de usuários e cursos de serviços diferentes.
- **Dois serviços** (leitor e escritor): não atende ao mínimo de três, e o escritor
  misturaria regras de negócio com o tratamento de falhas do Moodle.

## Consequências

- Cada sistema externo fica isolado atrás de um serviço: com o Moodle fora, o
  detector e o sincronizador continuam funcionando e os comandos esperam na fila.
- O sincronizador não faz I/O externo, então as regras são testáveis sem rede.
- Custo: o Moodle fica alguns segundos atrás do i-Educar (consistência eventual), e
  passam a existir um broker e três bancos.
