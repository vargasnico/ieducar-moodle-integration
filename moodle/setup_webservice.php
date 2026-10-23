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
const ROLES = ['editingteacher', 'teacher', 'student'];

// Funções expostas ao microsserviço. As de curso/matrícula já ficam liberadas
// para os próximos casos (turmas, componentes curriculares e alunos).
const SERVICE_FUNCTIONS = [
    'core_webservice_get_site_info',
    'core_user_create_users',
    'core_user_update_users',
    'core_user_get_users_by_field',
    'core_role_assign_roles',
    'core_role_unassign_roles',
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

// 4. Papéis usados nas inscrições em cursos (casos de turmas e alunos).
//    O professor recebe o papel no curso de cada componente que leciona, nunca
//    na categoria inteira. Versões anteriores deste script liberavam o papel de
//    professor no contexto de categoria; isso é revertido para o padrão do Moodle.
$roleids = [];
foreach (ROLES as $shortname) {
    $roleids[$shortname] = (int) $DB->get_field('role', 'id', ['shortname' => $shortname], MUST_EXIST);
}
$levels = array_values(get_role_contextlevels($roleids['editingteacher']));
if (in_array(CONTEXT_COURSECAT, $levels)) {
    set_role_contextlevels($roleids['editingteacher'], array_diff($levels, [CONTEXT_COURSECAT]));
    info('Papel editingteacher restrito novamente a cursos (padrão do Moodle)');
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
