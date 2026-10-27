<?php
// Completa os dados de teste do DemoSeeder para a integração.
//
// Pré-requisito: php artisan db:seed --class=DemoSeeder (escolas, turmas, alunos).
// Uso (na raiz do repositório):
//   docker compose exec -T php sh -c "cat > /tmp/seed.php" < scripts/ieducar_seed.php
//   docker compose exec php php artisan tinker --execute="require '/tmp/seed.php';"
//
// 1. Deixa as pessoas do DemoSeeder com cara de reais. Ele grava só um primeiro nome
//    (que se repete e parece duplicado no Moodle), pai e mãe de sexo aleatório,
//    e-mails aleatórios em domínios reais e tipo jurídico em parte das pessoas
//    físicas. Cada aluno ganha os sobrenomes da mãe e do pai, e cada pessoa um
//    e-mail derivado do nome no domínio reservado .example.
// 2. Cria quatro servidores, um para cada situação da regra "professor = servidor
//    ativo com vínculo ativo com turma no ano letivo".
// Pode ser executado de novo: só mexe em nomes sem sobrenome, em e-mails fora de
// .example e em servidores que ainda não existem (pelo nome).

use App\Models\LegacyRole;
use App\Models\LegacySchoolClass;
use App\Models\LegacySchoolClassTeacher;
use Database\Factories\EmployeeFactory;
use Database\Factories\LegacyEmployeeRoleFactory;
use Database\Factories\LegacyIndividualFactory;
use Database\Factories\LegacyPersonFactory;
use Database\Factories\LegacyRoleFactory;
use Database\Factories\LegacySchoolClassTeacherDisciplineFactory;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

// 1. Pessoas: nomes, e-mails e tipo (semente fixa: mesmo resultado sempre).
$faker = Faker\Factory::create('pt_BR');
$faker->seed(2026);
$usados = array_flip(DB::table('cadastro.pessoa')->whereNotNull('nome')->pluck('nome')->all());
$atualizar = fn (int $idpes, array $dados) => DB::table('cadastro.pessoa')
    ->where('idpes', $idpes)->update($dados + ['data_rev' => now()]);
$renomear = function (int $idpes, callable $gerar) use (&$usados, $atualizar) {
    do {
        $nome = $gerar();
    } while (isset($usados[$nome]));
    $usados[$nome] = true;
    $atualizar($idpes, ['nome' => $nome, 'slug' => Str::lower(Str::slug($nome, ' '))]);
    return $nome;
};

$familias = DB::table('pmieducar.aluno as a')
    ->join('cadastro.pessoa as p', 'p.idpes', '=', 'a.ref_idpes')
    ->join('cadastro.fisica as f', 'f.idpes', '=', 'a.ref_idpes')
    ->leftJoin('cadastro.pessoa as mae', 'mae.idpes', '=', 'f.idpes_mae')
    ->leftJoin('cadastro.pessoa as pai', 'pai.idpes', '=', 'f.idpes_pai')
    ->orderBy('a.cod_aluno')
    ->get(['p.idpes', 'p.nome', 'f.idpes_mae', 'mae.nome as mae', 'f.idpes_pai', 'pai.nome as pai']);
$renomeados = 0;
foreach ($familias as $familia) {
    $nome = $familia->nome;
    if (!str_contains($nome, ' ')) {
        // Primeiro nome + sobrenome da mãe + sobrenome do pai.
        $nome = $renomear($familia->idpes, fn () => "{$nome} {$faker->lastName()} {$faker->lastName()}");
        $renomeados++;
    }
    $sobrenomes = array_slice(explode(' ', $nome), 1);
    if ($familia->idpes_mae && !str_contains($familia->mae, ' ')) {
        $renomear($familia->idpes_mae, fn () => "{$faker->firstNameFemale()} {$sobrenomes[0]}");
        $renomeados++;
    }
    if ($familia->idpes_pai && !str_contains($familia->pai, ' ')) {
        $renomear($familia->idpes_pai, fn () => "{$faker->firstNameMale()} " . end($sobrenomes));
        $renomeados++;
    }
}
echo $renomeados ? "+ nome completo para {$renomeados} pessoa(s)\n" : "= pessoas já têm nome completo\n";

// E-mail a partir do nome; .example é reservado, então nenhuma mensagem chega a alguém.
$emails = array_flip(DB::table('cadastro.pessoa')->whereNotNull('email')->pluck('email')->all());
$semEmail = DB::table('cadastro.pessoa as p')
    ->join('cadastro.fisica as f', 'f.idpes', '=', 'p.idpes')
    ->where('p.nome', 'like', '% %')
    ->where(fn ($q) => $q->whereNull('p.email')->orWhere('p.email', 'not like', '%.example'))
    ->orderBy('p.idpes')
    ->get(['p.idpes', 'p.nome']);
foreach ($semEmail as $pessoa) {
    $partes = explode(' ', Str::lower(Str::ascii($pessoa->nome)));
    $base = $partes[0] . '.' . end($partes);
    for ($email = "{$base}@email.example", $n = 2; isset($emails[$email]); $n++) {
        $email = "{$base}{$n}@email.example";
    }
    $emails[$email] = true;
    $atualizar($pessoa->idpes, ['email' => $email]);
}
$escolas = DB::table('cadastro.pessoa as p')
    ->join('pmieducar.escola as e', 'e.ref_idpes', '=', 'p.idpes')
    ->where('p.email', 'not like', '%.example')
    ->get(['p.idpes', 'p.nome']);
foreach ($escolas as $escola) {
    $atualizar($escola->idpes, ['email' => 'secretaria@' . Str::slug($escola->nome) . '.example']);
}
$corrigidos = $semEmail->count() + $escolas->count();
echo $corrigidos ? "+ e-mail derivado do nome para {$corrigidos} pessoa(s)\n" : "= e-mails já derivados dos nomes\n";

// Pessoas físicas (com cadastro.fisica) que o DemoSeeder gravou como jurídicas.
$tipos = DB::table('cadastro.pessoa')->where('tipo', 'J')
    ->whereIn('idpes', DB::table('cadastro.fisica')->select('idpes'))
    ->whereNotIn('idpes', DB::table('cadastro.juridica')->select('idpes'))
    ->update(['tipo' => 'F']);
echo $tipos ? "+ tipo físico para {$tipos} pessoa(s)\n" : "= tipos já corretos\n";

// 2. Servidores.

$funcao = fn (string $nome, string $sigla, int $professor) => LegacyRole::query()->where('nm_funcao', $nome)->first()
    ?? LegacyRoleFactory::new()->create(['nm_funcao' => $nome, 'abreviatura' => $sigla, 'professor' => $professor]);

$professor = $funcao('Professor', 'PROF', 1);
$secretaria = $funcao('Secretário escolar', 'SEC', 0);

$turmas = LegacySchoolClass::query()
    ->join('pmieducar.escola_ano_letivo as eal', fn ($j) => $j
        ->on('eal.ref_cod_escola', '=', 'pmieducar.turma.ref_ref_cod_escola')
        ->on('eal.ano', '=', 'pmieducar.turma.ano'))
    ->where('eal.andamento', 1)
    ->where('pmieducar.turma.ativo', 1)
    ->orderBy('cod_turma')
    ->select('pmieducar.turma.*')
    ->limit(2)
    ->get();

if ($turmas->count() < 2) {
    echo "Rode antes: php artisan db:seed --class=DemoSeeder\n";
    return;
}

$componentesDaSerie = fn ($turma) => DB::table('modules.componente_curricular_ano_escolar')
    ->where('ano_escolar_id', $turma->ref_ref_cod_serie)
    ->orderBy('componente_curricular_id')
    ->pluck('componente_curricular_id');

$servidor = function (string $nome, string $cpf, LegacyRole $funcao) {
    $existente = DB::table('cadastro.pessoa as p')
        ->join('pmieducar.servidor as s', 's.cod_servidor', '=', 'p.idpes')
        ->where('p.nome', $nome)
        ->value('p.idpes');
    if ($existente) {
        echo "= {$nome} já existe (servidor {$existente})\n";
        return null;
    }
    $email = strtolower(strtok($nome, ' ')) . '@escola.example';
    $pessoa = LegacyPersonFactory::new()->create(['nome' => $nome, 'tipo' => 'F', 'email' => $email]);
    LegacyIndividualFactory::new()->create(['idpes' => $pessoa->idpes]);
    DB::table('cadastro.fisica')->where('idpes', $pessoa->idpes)->update(['cpf' => $cpf]);
    EmployeeFactory::new()->create(['id' => $pessoa->idpes]);
    LegacyEmployeeRoleFactory::new()->create([
        'ref_cod_servidor' => $pessoa->idpes,
        'ref_cod_funcao' => $funcao->getKey(),
    ]);
    echo "+ {$nome}: servidor {$pessoa->idpes}, função {$funcao->nm_funcao}\n";
    return $pessoa->idpes;
};

$vincular = function (int $servidorId, $turma, $componentes) {
    // Criado direto pelo model: LegacySchoolClassTeacherFactory cria um servidor
    // extra a cada uso (servidor_id não é avaliado de forma preguiçosa).
    $vinculo = LegacySchoolClassTeacher::query()->forceCreate([
        'ano' => $turma->ano,
        'instituicao_id' => $turma->ref_cod_instituicao,
        'turma_id' => $turma->cod_turma,
        'servidor_id' => $servidorId,
        'turno_id' => $turma->turma_turno_id,
        'funcao_exercida' => 1, // Docente
        'tipo_vinculo' => 1,
    ]);
    // A tela grava updated_at com CURRENT_TIMESTAMP (UTC); o model grava no fuso
    // da aplicação. Igualamos à tela para o teste refletir o uso real.
    DB::table('modules.professor_turma')->where('id', $vinculo->getKey())
        ->update(['updated_at' => DB::raw('CURRENT_TIMESTAMP')]);
    foreach ($componentes as $componenteId) {
        LegacySchoolClassTeacherDisciplineFactory::new()->create([
            'professor_turma_id' => $vinculo->getKey(),
            'componente_curricular_id' => $componenteId,
        ]);
    }
    echo "  vínculo {$vinculo->getKey()}: turma {$turma->cod_turma} ({$turma->nm_turma}), "
        . count($componentes) . " componente(s)\n";
};

[$turmaA, $turmaB] = [$turmas[0], $turmas[1]];

// 1. Professora com turma e componentes → deve ser criada no Moodle.
if ($id = $servidor('Ana Paula Souza', '11144477735', $professor)) {
    $vincular($id, $turmaA, $componentesDaSerie($turmaA)->take(2)->all());
}
// 2. Professor unidocente: todos os componentes da série. Na tela do i-Educar os
//    componentes são obrigatórios para a função Docente; vínculos sem componentes
//    não aparecem na API (servidores-disciplinas-turmas).
if ($id = $servidor('Bruno Lima', '22255588846', $professor)) {
    $vincular($id, $turmaB, $componentesDaSerie($turmaB)->all());
}
// 3. Secretário: servidor sem função de professor e sem turma → não entra.
$servidor('Carla Mendes', '33366699957', $secretaria);
// 4. Função de professor, mas sem turma no ano → não entra.
$servidor('Diego Rocha', '44477700068', $professor);
