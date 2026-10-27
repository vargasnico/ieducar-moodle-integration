<?php
// Configura o Moodle para receber a integração com o i-Educar.
//
// Idempotente: pode ser executado a cada `docker compose up`. Ao final imprime
// em stdout um JSON com o token e os IDs que a aplicação de integração usa.
// Mensagens de progresso vão para stderr.

define('CLI_SCRIPT', true);

require('/bitnami/moodle/config.php');
require_once($CFG->libdir . '/accesslib.php');

const SERVICE_NAME = 'iEducar Integration';
const SERVICE_SHORTNAME = 'ieducar_integration';
const CATEGORY_IDNUMBER = 'ieducar';
const ROLES = ['editingteacher', 'student'];

// Funções que a aplicação de integração chama.
const SERVICE_FUNCTIONS = [
    'core_webservice_get_site_info',
    'core_user_create_users',
    'core_user_update_users',
    'core_user_get_users_by_field',
    'core_course_get_categories',
    'core_course_create_categories',
    'core_course_get_courses_by_field',
    'core_course_create_courses',
    'core_course_update_courses',
    'enrol_manual_enrol_users',
    'enrol_manual_unenrol_users',
];

function info(string $message): void {
    fwrite(STDERR, "[moodle-setup] {$message}\n");
}

// 1. Habilita Web Services e o protocolo REST.
set_config('enablewebservices', 1);
$protocols = array_filter(explode(',', (string) get_config('core', 'webserviceprotocols')));
if (!in_array('rest', $protocols)) {
    $protocols[] = 'rest';
    set_config('webserviceprotocols', implode(',', $protocols));
}
info('Web services REST habilitados');

// O ambiente não tem servidor de e-mail: sem isso o Moodle falha ao mandar a
// mensagem de boas-vindas do curso e a inscrição pela API é recusada
// ("Message was not sent"). Com noemailever o envio é ignorado com sucesso.
set_config('noemailever', 1);
info('Envio de e-mails desativado (noemailever)');

// O e-mail vem do i-Educar e pode se repetir (irmãos com o e-mail do responsável,
// servidor que também é aluno). Sem isso o Moodle recusa o usuário inteiro.
set_config('allowaccountssameemail', 1);
info('E-mail repetido entre usuários permitido (allowaccountssameemail)');

// 2. Serviço externo com as funções da integração.
$service = $DB->get_record('external_services', ['shortname' => SERVICE_SHORTNAME]);
if (!$service) {
    $service = (object) [
        'name' => SERVICE_NAME,
        'shortname' => SERVICE_SHORTNAME,
        'enabled' => 1,
        'restrictedusers' => 0,
        'downloadfiles' => 0,
        'uploadfiles' => 0,
        'timecreated' => time(),
        'timemodified' => time(),
    ];
    $service->id = $DB->insert_record('external_services', $service);
    info('Serviço externo criado');
}
foreach (SERVICE_FUNCTIONS as $function) {
    $params = ['externalserviceid' => $service->id, 'functionname' => $function];
    if (!$DB->record_exists('external_services_functions', $params)) {
        $DB->insert_record('external_services_functions', (object) $params);
    }
}

// 3. Categoria onde ficam os cursos sincronizados do i-Educar.
$category = $DB->get_record('course_categories', ['idnumber' => CATEGORY_IDNUMBER]);
if (!$category) {
    $category = core_course_category::create([
        'name' => 'iEducar',
        'idnumber' => CATEGORY_IDNUMBER,
        'parent' => 0,
        'description' => 'Cursos sincronizados a partir do i-Educar.',
    ]);
    info('Categoria iEducar criada');
}

// 3b. Sem mensagem de boas-vindas nas inscrições manuais. Desde o Moodle 4.4 ela vem
//     ligada e é enviada a cada inscrito, com o professor do curso como remetente. O
//     noemailever acima só evita o e-mail: a notificação continua sendo gerada no
//     Moodle, uma por aluno em cada curso, milhares numa sincronização. O padrão vale
//     para cursos novos; os cursos da integração que já existem são corrigidos aqui.
if (get_config('enrol_manual', 'sendcoursewelcomemessage')) {
    set_config('sendcoursewelcomemessage', 0, 'enrol_manual');
    info('Mensagem de boas-vindas da inscrição manual desligada por padrão');
}
$corrigidas = $DB->count_records_sql(
    "SELECT COUNT(1) FROM {enrol} e
       JOIN {course} c ON c.id = e.courseid
       JOIN {course_categories} cc ON cc.id = c.category
      WHERE e.enrol = 'manual' AND e.customint1 <> 0
        AND (cc.id = :categoria OR " . $DB->sql_like('cc.path', ':caminho') . ')',
    ['categoria' => $category->id, 'caminho' => "/{$category->id}/%"]
);
if ($corrigidas) {
    $DB->execute(
        "UPDATE {enrol} SET customint1 = 0
          WHERE enrol = 'manual' AND customint1 <> 0 AND courseid IN (
                SELECT c.id FROM {course} c
                  JOIN {course_categories} cc ON cc.id = c.category
                 WHERE cc.id = :categoria OR " . $DB->sql_like('cc.path', ':caminho') . ')',
        ['categoria' => $category->id, 'caminho' => "/{$category->id}/%"]
    );
    info("Mensagem de boas-vindas desligada em {$corrigidas} curso(s) da integração");
}

// 4. Papéis usados nas inscrições: o professor recebe o papel no curso de cada
//    componente que leciona, nunca na categoria inteira.
$roleids = [];
foreach (ROLES as $shortname) {
    $roleids[$shortname] = (int) $DB->get_field('role', 'id', ['shortname' => $shortname], MUST_EXIST);
}

// 5. Token permanente do administrador para o serviço.
$admin = get_admin();
$token = $DB->get_record('external_tokens', [
    'externalserviceid' => $service->id,
    'userid' => $admin->id,
    'tokentype' => EXTERNAL_TOKEN_PERMANENT,
]);
$tokenvalue = $token
    ? $token->token
    : \core_external\util::generate_token(
        EXTERNAL_TOKEN_PERMANENT,
        $service,
        $admin->id,
        context_system::instance(),
        0,
        '',
        SERVICE_NAME
    );
info('Token disponível');

echo json_encode([
    'token' => $tokenvalue,
    'service' => SERVICE_SHORTNAME,
    'role_ids' => $roleids,
    'category_id' => (int) $category->id,
], JSON_PRETTY_PRINT) . "\n";
