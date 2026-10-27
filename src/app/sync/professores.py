"""Caso de integração 1: professores do i-Educar viram usuários do Moodle.

Professor é o servidor ativo com pelo menos um vínculo ativo com turma
(modules.professor_turma) no ano letivo da escola. Servidores sem turma
(secretaria, direção etc.) não entram no Moodle; quem perde a última turma é
suspenso.

Este caso só cria e mantém o usuário. O papel de professor é dado na inscrição em
cada curso (turma × componente curricular), no caso de turmas.
"""

import re

from app.sync.base import Sincronizacao
from app.sync.usuarios import PROFESSOR, Pessoa, sincronizar_usuarios


class ProfessorSync(Sincronizacao):
    async def executar(self, ano: int, escolas: list[int]) -> None:
        vinculos = await self.ieducar.listar_vinculos(ano, escolas)
        servidores = {s.servidor_id: s for s in await self.ieducar.listar_servidores()}
        self.resultado.conta("vinculos_lidos", len(vinculos))
        professores = {
            v.servidor_id
            for v in vinculos
            if v.ativo
            and v.servidor_id in servidores
            and servidores[v.servidor_id].ativo
        }
        await sincronizar_usuarios(
            self,
            PROFESSOR,
            {s.servidor_id: s.updated_at for s in servidores.values()},
            professores,
            self._pessoa,
        )

    async def _pessoa(self, servidor_id: int) -> Pessoa | None:
        # A listagem de servidores não traz o e-mail.
        servidor = await self.ieducar.obter_servidor(servidor_id)
        if servidor is None:
            return None
        idnumber = f"ieducar-servidor-{servidor_id}"
        cpf = re.sub(r"\D", "", servidor.cpf or "")
        return Pessoa(
            origem_id=servidor_id,
            idnumber=idnumber,
            # Login pelo CPF (só números) ou, sem CPF, pelo código do servidor.
            username=cpf or f"servidor{servidor_id}",
            nome=servidor.nome,
            email=servidor.email or self.email_provisorio(idnumber),
            updated_at=servidor.updated_at,
        )
