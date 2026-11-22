# ADR 0004 — Stack: Python + FastAPI, aio-pika, SQLite

**Status:** aceita · **Data:** 2026-11-22

## Contexto

A stack é livre. O grupo fez o T1 em Python 3.14 + FastAPI, com as regras de
integração validadas contra o i-Educar e o Moodle reais. O prazo do T2 é de uma
semana.

## Decisão

- **Python 3.14 + FastAPI** nos três serviços, para reaproveitar os clientes do
  i-Educar e do Moodle e as regras do T1.
- **aio-pika** para o RabbitMQ (assíncrono, reconecta sozinho, ack manual) e
  **tenacity** para o retry com backoff.
- **SQLite**, um arquivo por serviço num volume próprio: o isolamento dos dados é
  físico, e o volume de dados é pequeno.
- **Um repositório, um Dockerfile e uma imagem por serviço**, sem código
  compartilhado ([ADR 0005](0005-repositorios-e-deploy.md)). Cada serviço declara só
  as dependências que usa (o sincronizador, por exemplo, não tem cliente HTTP).
- **prometheus-client** para expor as métricas em `/metrics`.

## Alternativas

- **NestJS:** natural na disciplina, mas exigiria reescrever as regras e os clientes
  do T1 sem ganho para a arquitetura.
- **PostgreSQL por serviço:** mais próximo de produção, mas acrescenta containers sem
  necessidade neste volume.

## Consequências

- O T2 reaproveita quase todo o código de integração do T1.
- Com SQLite, cada serviço roda com uma instância só. Para escalar, a troca seria por
  um banco de rede.
- Os três serviços usam a mesma linguagem, mas nada obriga: o contrato entre eles é o
  formato das mensagens, e um serviço poderia ser reescrito em outra stack sem afetar
  os outros.
