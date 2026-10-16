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
const TEACHER_ROLE = 'editingteacher';

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

// 3. Categoria onde ficam os cursos e a atribuição de professores do i-Educar.
$category = $DB->get_record('course_categories', ['idnumber' => CATEGORY_IDNUMBER]);
if (!$category) {
    $category = core_course_category::create([
        'name' => 'iEducar',
        'idnumber' => CATEGORY_IDNUMBER,
        'parent' => 0,
        'description' => 'Cursos e professores sincronizados a partir do i-Educar.',
    ]);
    info('Categoria iEducar criada');
}
$categorycontext = context_coursecat::instance($category->id);

// 4. Permite atribuir o papel de professor no contexto de categoria.
$role = $DB->get_record('role', ['shortname' => TEACHER_ROLE], '*', MUST_EXIST);
$levels = array_values(get_role_contextlevels($role->id));
if (!in_array(CONTEXT_COURSECAT, $levels)) {
    $levels[] = CONTEXT_COURSECAT;
    set_role_contextlevels($role->id, $levels);
    info('Papel editingteacher liberado no contexto de categoria');
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
    'teacher_role_id' => (int) $role->id,
    'category_id' => (int) $category->id,
    'category_context_id' => (int) $categorycontext->id,
], JSON_PRETTY_PRINT) . "\n";
