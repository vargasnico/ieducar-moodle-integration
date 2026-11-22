<?php
// Alterações no i-Educar usadas nas demonstrações dos cenários de falha. Rodado pelo
// tinker do i-Educar (scripts/falhas/comum.sh, função alterar_ieducar); ACAO escolhe:
//
//   renomear-turma   alterna o sufixo " (renomeada)" no nome da primeira turma do
//                    ano: muda o nome de todos os cursos dela no Moodle.
//   vincular-diego   alterna o vínculo do professor Diego Rocha (sem turma no seed)
//                    com o primeiro componente da primeira turma: cria (ou suspende)
//                    o usuário dele no Moodle e a inscrição como professor.

use App\Models\LegacySchoolClassTeacher;
use Illuminate\Support\Facades\DB;

$acao = getenv('ACAO') ?: '';
$turma = DB::table('pmieducar.turma')->where('ativo', 1)
    ->where('ano', DB::table('pmieducar.turma')->max('ano'))
    ->orderBy('cod_turma')->first();

if ($acao === 'renomear-turma') {
    $sufixo = ' (renomeada)';
    $nome = str_ends_with($turma->nm_turma, $sufixo)
        ? substr($turma->nm_turma, 0, -strlen($sufixo))
        : $turma->nm_turma . $sufixo;
    DB::table('pmieducar.turma')->where('cod_turma', $turma->cod_turma)
        ->update(['nm_turma' => $nome, 'updated_at' => DB::raw('CURRENT_TIMESTAMP')]);
    echo "Turma {$turma->cod_turma} renomeada para \"{$nome}\"\n";
} elseif ($acao === 'vincular-diego') {
    $diego = DB::table('cadastro.pessoa as p')
        ->join('pmieducar.servidor as s', 's.cod_servidor', '=', 'p.idpes')
        ->where('p.nome', 'Diego Rocha')->value('p.idpes');
    if (!$diego) {
        fwrite(STDERR, "Servidor Diego Rocha não encontrado (rode o seed do setup.sh)\n");
        exit(1);
    }
    $vinculo = LegacySchoolClassTeacher::query()->where('servidor_id', $diego)
        ->where('turma_id', $turma->cod_turma)->first();
    if ($vinculo) {
        DB::table('modules.professor_turma_disciplina')
            ->where('professor_turma_id', $vinculo->getKey())->delete();
        $vinculo->delete();
        echo "Vínculo de Diego Rocha com a turma {$turma->cod_turma} removido\n";
    } else {
        $componente = DB::table('modules.componente_curricular_ano_escolar')
            ->where('ano_escolar_id', $turma->ref_ref_cod_serie)
            ->orderBy('componente_curricular_id')->value('componente_curricular_id');
        $vinculo = LegacySchoolClassTeacher::query()->forceCreate([
            'ano' => $turma->ano,
            'instituicao_id' => $turma->ref_cod_instituicao,
            'turma_id' => $turma->cod_turma,
            'servidor_id' => $diego,
            'turno_id' => $turma->turma_turno_id,
            'funcao_exercida' => 1,
            'tipo_vinculo' => 1,
        ]);
        DB::table('modules.professor_turma_disciplina')->insert([
            'professor_turma_id' => $vinculo->getKey(),
            'componente_curricular_id' => $componente,
        ]);
        echo "Diego Rocha vinculado à turma {$turma->cod_turma}, componente {$componente}\n";
    }
} else {
    fwrite(STDERR, "ACAO deve ser renomear-turma ou vincular-diego\n");
    exit(2);
}
