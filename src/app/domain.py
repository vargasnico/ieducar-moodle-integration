"""Dados do i-Educar que a integração usa, independentes de como a API os entrega."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Escola:
    escola_id: int
    nome: str
    ativo: bool


@dataclass(frozen=True)
class Servidor:
    servidor_id: int
    nome: str
    cpf: str | None
    ativo: bool
    updated_at: str
    email: str | None = None


@dataclass(frozen=True)
class Vinculo:
    """Alocação de um servidor como professor numa turma (modules.professor_turma)."""

    vinculo_id: int
    servidor_id: int
    turma_id: int
    componentes: tuple[int, ...]
    ativo: bool
    updated_at: str


@dataclass(frozen=True)
class Turma:
    turma_id: int
    nome: str
    ano: int
    escola_id: int
    # Uma turma multisseriada tem mais de uma série.
    series: tuple[int, ...]
    ativo: bool
    updated_at: str


@dataclass(frozen=True)
class ComponenteCurricular:
    componente_id: int
    nome: str
    abreviatura: str


@dataclass(frozen=True)
class Aluno:
    aluno_id: int
    nome: str
    nome_social: str | None
    ativo: bool
    updated_at: str


@dataclass(frozen=True)
class Matricula:
    """Vínculo do aluno com uma série num ano (muda a cada rematrícula)."""

    matricula_id: int
    aluno_id: int
    escola_id: int
    ativo: bool


@dataclass(frozen=True)
class Enturmacao:
    """Matrícula alocada numa turma (pmieducar.matricula_turma)."""

    enturmacao_id: int
    matricula_id: int
    turma_id: int
    serie_id: int
    # Sem exclusão e sem data de saída. Transferência, remanejamento, abandono e
    # falecimento mantêm deleted_at vazio (o aluno segue no diário) e só preenchem
    # a data de saída.
    ativa: bool


@dataclass(frozen=True)
class Dispensa:
    matricula_id: int
    componente_id: int
    ativa: bool
