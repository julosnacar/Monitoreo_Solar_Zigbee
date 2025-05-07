/**
 * @file main_coordinator.c
 * @brief Basic Zigbee Coordinator for ESP32-H2
 *
 * Forms a Zigbee network, configures reporting on sensor routers,
 * and listens for custom current sensor reports.
 */
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>
#include <esp_log.h>
#include <esp_check.h>
#include "main_coordinador.h" // Asegúrate que este archivo exista y tenga los defines correctos para el coordinador


#define TAG "COORDINADOR_ZIGBEE_MULTI"

// --- Zigbee Configuration ---
#define ZIGBEE_CHANNEL_MASK (1l << 15)
#define ZIGBEE_COORDINATOR_ENDPOINT 1
#define ZIGBEE_PERMIT_JOIN_DURATION 0xFF // Segundos

// --- Configuración Cluster Personalizado (debe coincidir con el router) ---
#define ZIGBEE_CUSTOM_CLUSTER_ID        0xFC01
#define ATTR_ID_CURRENT_SENSOR_1        0x0001
#define ATTR_ID_CURRENT_SENSOR_2        0x0002
#define ATTR_ID_CURRENT_SENSOR_3        0x0003

// --- Estructura para almacenar datos de cada router sensor ---
#define MAX_TEST_ROUTERS 5
typedef struct {
    uint16_t short_addr;
    float current_s1;
    float current_s2;
    float current_s3;
    uint8_t received_mask;
    bool is_active;
    uint8_t router_app_endpoint; // Guardar el endpoint del router donde está el cluster
} router_sensor_data_t;

static router_sensor_data_t g_sensor_data_table[MAX_TEST_ROUTERS];

// --- Funciones de ayuda para la tabla de sensores ---
static void init_sensor_data_table() {
    for (int i = 0; i < MAX_TEST_ROUTERS; ++i) {
        g_sensor_data_table[i].short_addr = 0xFFFF;
        g_sensor_data_table[i].current_s1 = NAN;
        g_sensor_data_table[i].current_s2 = NAN;
        g_sensor_data_table[i].current_s3 = NAN;
        g_sensor_data_table[i].received_mask = 0;
        g_sensor_data_table[i].is_active = false;
        g_sensor_data_table[i].router_app_endpoint = 0; // Inicializar
    }
}

static router_sensor_data_t* find_or_add_sensor_entry(uint16_t short_addr, uint8_t app_endpoint) {
    for (int i = 0; i < MAX_TEST_ROUTERS; ++i) {
        if (g_sensor_data_table[i].is_active && g_sensor_data_table[i].short_addr == short_addr) {
            // Actualizar endpoint si es diferente (poco probable para el mismo dispositivo)
            if (g_sensor_data_table[i].router_app_endpoint != app_endpoint) {
                 ESP_LOGW(TAG, "Router 0x%04X cambió de endpoint (antes %d, ahora %d)", short_addr, g_sensor_data_table[i].router_app_endpoint, app_endpoint);
                 g_sensor_data_table[i].router_app_endpoint = app_endpoint;
            }
            return &g_sensor_data_table[i];
        }
    }
    for (int i = 0; i < MAX_TEST_ROUTERS; ++i) {
        if (!g_sensor_data_table[i].is_active) {
            g_sensor_data_table[i].short_addr = short_addr;
            g_sensor_data_table[i].router_app_endpoint = app_endpoint; // Guardar el endpoint
            g_sensor_data_table[i].current_s1 = NAN;
            g_sensor_data_table[i].current_s2 = NAN;
            g_sensor_data_table[i].current_s3 = NAN;
            g_sensor_data_table[i].received_mask = 0;
            g_sensor_data_table[i].is_active = true;
            ESP_LOGI(TAG, "Nuevo router sensor (0x%04X en EP %d) añadido a la tabla en índice %d", short_addr, app_endpoint, i);
            return &g_sensor_data_table[i];
        }
    }
    ESP_LOGW(TAG, "Tabla de sensores llena, no se pudo agregar 0x%04X", short_addr);
    return NULL;
}

// --- Función para enviar Configure Reporting ---
static void send_configure_reporting(uint16_t device_short_addr, uint8_t device_endpoint, uint16_t attribute_id) {
    esp_zb_zcl_config_report_cmd_t cfg_report_cmd;
    memset(&cfg_report_cmd, 0, sizeof(esp_zb_zcl_config_report_cmd_t));

    cfg_report_cmd.zcl_basic_cmd.dst_addr_u.addr_short = device_short_addr;
    cfg_report_cmd.zcl_basic_cmd.dst_endpoint = device_endpoint;
    cfg_report_cmd.zcl_basic_cmd.src_endpoint = ZIGBEE_COORDINATOR_ENDPOINT;
    cfg_report_cmd.address_mode = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT;
    cfg_report_cmd.clusterID = ZIGBEE_CUSTOM_CLUSTER_ID;
    cfg_report_cmd.direction = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV;
    cfg_report_cmd.manuf_specific = 0; // El comando Configure Reporting en sí no es manuf-specific
                                       // Si el *atributo* fuera manuf-specific, 
                                       //se indicaría en el registro del atributo.

    esp_zb_zcl_config_report_record_t record;
    memset(&record, 0, sizeof(esp_zb_zcl_config_report_record_t));

    record.direction = ESP_ZB_ZCL_REPORT_DIRECTION_SEND; // El router debe ENVIAR reportes
    record.attributeID = attribute_id;
    record.attrType = ESP_ZB_ZCL_ATTR_TYPE_SINGLE; // Tipo float
    record.min_interval = 3;    // Min: 1 segundo (0x0001)
    record.max_interval = 6;   // Max: 10 segundos (0x000A)
                                // Si max_interval es 0xFFFF, solo reporta por cambio.
                                // Si reportable_change es NULL (o el tipo de dato no soporta cambio), 
                                //solo reporta por tiempo.
    record.reportable_change = NULL; // <-- **PARA PRUEBA**

    float rep_change_val = 0.05f; // Reportar si la corriente cambia al menos 0.05A
                                  // Para atributos discretos (bool, enum), reportable_change se ignora y debe ser NULL.
                                  // Para numéricos, es el delta.
    record.reportable_change = &rep_change_val;

    cfg_report_cmd.record_number = 1;
    cfg_report_cmd.record_field = &record;

    uint8_t tsn = esp_zb_zcl_config_report_cmd_req(&cfg_report_cmd);
    ESP_LOGI(TAG, "Enviado Configure Reporting para Attr 0x%04X a 0x%04X EP%d (TSN: %d)",
             attribute_id, device_short_addr, device_endpoint, tsn);
}


// --- Callback Principal de Acciones Zigbee Core ---
esp_err_t esp_zb_action_handler(esp_zb_core_action_callback_id_t callback_id, const void *message) {
    esp_err_t ret = ESP_OK;

    if (message == NULL && callback_id != ESP_ZB_CORE_CMD_DEFAULT_RESP_CB_ID) { // Default Resp puede no tener payload si es genérico
        ESP_LOGE(TAG, "Mensaje nulo recibido para callback ID 0x%04X", callback_id);
        // No siempre es un error fatal, algunos callbacks pueden no tener 'message'
        // pero para REPORT_ATTR_CB_ID sí es esperado.
        if (callback_id == ESP_ZB_CORE_REPORT_ATTR_CB_ID) return ESP_FAIL;
    }


    switch (callback_id) {
        case ESP_ZB_CORE_CMD_REPORT_CONFIG_RESP_CB_ID:
        {
            const esp_zb_zcl_cmd_config_report_resp_message_t *resp_msg = (const esp_zb_zcl_cmd_config_report_resp_message_t *)message;
            ESP_LOGI(TAG, "Respuesta de Configure Reporting desde 0x%04X (EP%d), status comando: 0x%02X:",
                     resp_msg->info.src_address.u.short_addr, resp_msg->info.src_endpoint, resp_msg->info.status);
            const esp_zb_zcl_config_report_resp_variable_t *var = resp_msg->variables;
            while (var) {
                ESP_LOGI(TAG, "  Attr 0x%04X, Status Attr 0x%02X, Direction 0x%02X",
                         var->attribute_id, var->status, var->direction);
                var = var->next;
            }
            break;
        }
        case ESP_ZB_CORE_REPORT_ATTR_CB_ID:
        {
            const esp_zb_zcl_report_attr_message_t *report_msg = (const esp_zb_zcl_report_attr_message_t *)message;
            // Ya se añadió el null check arriba.
            ESP_LOGI(TAG, ">>>> REPORTE DE ATRIBUTO RECIBIDO EN COORDINADOR <<<<");
            ESP_LOGI(TAG, "Desde 0x%04X, EP %d, Cluster 0x%04X, AttrID 0x%04X, Tipo 0x%02X",
                report_msg->src_address.u.short_addr, report_msg->src_endpoint,
                report_msg->cluster, report_msg->attribute.id, report_msg->attribute.data.type);

            uint16_t sender_short_addr = report_msg->src_address.u.short_addr;
            esp_zb_ieee_addr_t sender_ieee_addr;
            memcpy(sender_ieee_addr, report_msg->src_address.u.ieee_addr, sizeof(esp_zb_ieee_addr_t));
            uint8_t sender_endpoint = report_msg->src_endpoint;

            ESP_LOGD(TAG, "Procesando REPORTE: AddrCorta=0x%04X, IEEE=%02X%02X..., EP=%d, Cluster=0x%04X, AttrID=0x%04X",
                     sender_short_addr, sender_ieee_addr[0], sender_ieee_addr[1], sender_endpoint,
                     report_msg->cluster, report_msg->attribute.id);

            if (report_msg->cluster == ZIGBEE_CUSTOM_CLUSTER_ID &&
                report_msg->attribute.data.type == ESP_ZB_ZCL_ATTR_TYPE_SINGLE)
            {
                float received_current;
                if (report_msg->attribute.data.value && report_msg->attribute.data.size >= sizeof(float)) {
                    memcpy(&received_current, report_msg->attribute.data.value, sizeof(float));
                } else {
                    ESP_LOGE(TAG, "Valor de atributo nulo o tamaño incorrecto para float (Addr=0x%04X).", sender_short_addr);
                    break;
                }

                router_sensor_data_t *sensor_entry = find_or_add_sensor_entry(sender_short_addr, sender_endpoint);
                if (!sensor_entry) {
                    ESP_LOGW(TAG, "No se pudo procesar el reporte de 0x%04X, tabla llena o error.", sender_short_addr);
                    break;
                }

                const char *sensor_name_log = "Sensor Desconocido";
                int current_attr_bit = 0;

                switch (report_msg->attribute.id) {
                    case ATTR_ID_CURRENT_SENSOR_1:
                        sensor_entry->current_s1 = received_current;
                        sensor_entry->received_mask |= (1 << 0);
                        sensor_name_log = "Corriente S1";
                        current_attr_bit = (1 << 0);
                        break;
                    case ATTR_ID_CURRENT_SENSOR_2:
                        sensor_entry->current_s2 = received_current;
                        sensor_entry->received_mask |= (1 << 1);
                        sensor_name_log = "Corriente S2";
                        current_attr_bit = (1 << 1);
                        break;
                    case ATTR_ID_CURRENT_SENSOR_3:
                        sensor_entry->current_s3 = received_current;
                        sensor_entry->received_mask |= (1 << 2);
                        sensor_name_log = "Corriente S3";
                        current_attr_bit = (1 << 2);
                        break;
                    default:
                        ESP_LOGW(TAG, "ID de atributo (0x%04X) no reconocido en cluster 0x%04X de 0x%04X.",
                                 report_msg->attribute.id, report_msg->cluster, sender_short_addr);
                        break;
                }

                if (current_attr_bit != 0) {
                    ESP_LOGI(TAG, "Dispositivo [AddrCorta:0x%04X, EP:%d] -> %s: %.3f A (Mask: 0x%02X)",
                             sender_short_addr, sender_endpoint,
                             sensor_name_log, received_current, sensor_entry->received_mask);

                    if (sensor_entry->received_mask == 0b111) {
                        ESP_LOGI(TAG, "¡LECTURAS COMPLETAS DE 0x%04X (EP:%d)! S1=%.3f A, S2=%.3f A, S3=%.3f A",
                                 sensor_entry->short_addr, sensor_entry->router_app_endpoint,
                                 sensor_entry->current_s1, sensor_entry->current_s2, sensor_entry->current_s3);

                        // Aquí enviarías los datos combinados...

                        sensor_entry->received_mask = 0;
                        sensor_entry->current_s1 = NAN;
                        sensor_entry->current_s2 = NAN;
                        sensor_entry->current_s3 = NAN;
                        ESP_LOGD(TAG, "Datos de 0x%04X procesados y reseteados.", sensor_entry->short_addr);
                    }
                }
            } else {
                 ESP_LOGD(TAG, "Reporte de atributo no es del cluster/tipo esperado: Cluster 0x%04X, AttrID 0x%04X, Tipo 0x%02X",
                        report_msg->cluster, report_msg->attribute.id, report_msg->attribute.data.type);
            }
            break;
        }
        default:
            ESP_LOGD(TAG, "Callback de acción no manejado en handler: ID=0x%04x", callback_id);
            break;
    }
    return ret;
}


// --- Zigbee Stack Signal Handler ---
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *p_sg_p = signal_struct->p_app_signal;
    esp_err_t err_status = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig_type = *p_sg_p;

    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
            ESP_LOGI(TAG, "Stack inicializado, intentando formar red...");
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_NETWORK_FORMATION);
            break;

        case ESP_ZB_BDB_SIGNAL_FORMATION:
            if (err_status == ESP_OK) {
                esp_zb_ieee_addr_t extended_pan_id;
                esp_zb_get_extended_pan_id(extended_pan_id);
                ESP_LOGI(TAG, "¡Red formada! Addr: 0x%04X, Canal: %d, EPANID: %02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X",
                         esp_zb_get_short_address(), esp_zb_get_current_channel(),
                         extended_pan_id[0], extended_pan_id[1], extended_pan_id[2], extended_pan_id[3],
                         extended_pan_id[4], extended_pan_id[5], extended_pan_id[6], extended_pan_id[7]);
                esp_zb_bdb_open_network(ZIGBEE_PERMIT_JOIN_DURATION);
                ESP_LOGI(TAG, "Red abierta para unirse durante %d segundos.", ZIGBEE_PERMIT_JOIN_DURATION);
            } else {
                ESP_LOGE(TAG, "Fallo al formar la red: %s (0x%x)", esp_err_to_name(err_status), err_status);
                ESP_LOGI(TAG, "Reintentando formación de red en 5 segundos...");
                vTaskDelay(pdMS_TO_TICKS(5000));
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_NETWORK_FORMATION);
            }
            break;

        case ESP_ZB_ZDO_SIGNAL_DEVICE_ANNCE: {
            esp_zb_zdo_signal_device_annce_params_t *dev_annce_params = (esp_zb_zdo_signal_device_annce_params_t *)esp_zb_app_signal_get_params(p_sg_p);
            ESP_LOGI(TAG, "Nuevo dispositivo unido/anunciado: Addr Corta=0x%04X, IEEE Addr=%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X",
                     dev_annce_params->device_short_addr,
                     dev_annce_params->ieee_addr[0], dev_annce_params->ieee_addr[1], dev_annce_params->ieee_addr[2],
                     dev_annce_params->ieee_addr[3], dev_annce_params->ieee_addr[4], dev_annce_params->ieee_addr[5],
                     dev_annce_params->ieee_addr[6], dev_annce_params->ieee_addr[7]);
            
            // --- ENVIAR CONFIGURE REPORTING ---
            // Necesitamos el endpoint del router donde está el cluster custom.
            // Si todos tus routers usan el mismo endpoint para el cluster, puedes hardcodearlo.
            // Si no, necesitarías hacer un Simple Descriptor Request para descubrirlo.
            // Para este ejemplo, asumimos que el router usa ZIGBEE_ENDPOINT (que es 1 en tu router)
            uint8_t router_app_endpoint = 1; // Endpoint del router

            // Es buena idea registrar el dispositivo en nuestra tabla interna aquí también.
            find_or_add_sensor_entry(dev_annce_params->device_short_addr, router_app_endpoint);


            ESP_LOGI(TAG, "Configurando reportes para dispositivo 0x%04X en EP %d...", dev_annce_params->device_short_addr, router_app_endpoint);
            send_configure_reporting(dev_annce_params->device_short_addr, router_app_endpoint, ATTR_ID_CURRENT_SENSOR_1);
            vTaskDelay(pdMS_TO_TICKS(300)); // Pausa un poco más larga para asegurar procesamiento
            send_configure_reporting(dev_annce_params->device_short_addr, router_app_endpoint, ATTR_ID_CURRENT_SENSOR_2);
            vTaskDelay(pdMS_TO_TICKS(300));
            send_configure_reporting(dev_annce_params->device_short_addr, router_app_endpoint, ATTR_ID_CURRENT_SENSOR_3);

        } break;

        case ESP_ZB_ZDO_SIGNAL_LEAVE_INDICATION: {
            esp_zb_zdo_signal_leave_indication_params_t *leave_ind_params = (esp_zb_zdo_signal_leave_indication_params_t *)esp_zb_app_signal_get_params(p_sg_p);
             ESP_LOGW(TAG, "Dispositivo dejó la red: Addr Corta=0x%04X, IEEE Addr=%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X, Rejoin=%d",
                     leave_ind_params->short_addr,
                     leave_ind_params->device_addr[0], leave_ind_params->device_addr[1], leave_ind_params->device_addr[2],
                     leave_ind_params->device_addr[3], leave_ind_params->device_addr[4], leave_ind_params->device_addr[5],
                     leave_ind_params->device_addr[6], leave_ind_params->device_addr[7], leave_ind_params->rejoin);
            for (int i = 0; i < MAX_TEST_ROUTERS; ++i) {
                if (g_sensor_data_table[i].is_active && g_sensor_data_table[i].short_addr == leave_ind_params->short_addr) {
                    ESP_LOGI(TAG, "Marcando router sensor 0x%04X como inactivo.", leave_ind_params->short_addr);
                    g_sensor_data_table[i].is_active = false;
                    g_sensor_data_table[i].short_addr = 0xFFFF;
                    break;
                }
            }
        } break;

        default:
            ESP_LOGD(TAG, "Señal ZDO no manejada explícitamente: %s (0x%x), Estado=%s (0x%x)",
                     esp_zb_zdo_signal_to_string(sig_type), sig_type,
                     esp_err_to_name(err_status), err_status);
            break;
    }
}

// --- Zigbee Task ---
static void esp_zb_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando tarea Zigbee Coordinador...");

    esp_zb_cfg_t zb_cfg = ESP_ZB_ZC_CONFIG();
    esp_zb_init(&zb_cfg);
    ESP_LOGI(TAG, "Stack Zigbee inicializado como Coordinador.");

    esp_zb_cluster_list_t *cluster_list = esp_zb_zcl_cluster_list_create();
    ESP_RETURN_ON_FALSE(cluster_list, , TAG, "Fallo al crear lista de clusters");

    esp_zb_basic_cluster_cfg_t basic_cfg = {
        .zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE,
        .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE
    };
    esp_zb_attribute_list_t *basic_cluster = esp_zb_basic_cluster_create(&basic_cfg);
    ESP_RETURN_ON_FALSE(basic_cluster, , TAG, "Fallo al crear clúster Basic");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(cluster_list, basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    esp_zb_identify_cluster_cfg_t identify_cfg = { .identify_time = 0 };
    esp_zb_attribute_list_t *identify_cluster = esp_zb_identify_cluster_create(&identify_cfg);
    ESP_RETURN_ON_FALSE(identify_cluster, , TAG, "Fallo al crear clúster Identify");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(cluster_list, identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    esp_zb_attribute_list_t *custom_cluster_client = esp_zb_zcl_attr_list_create(ZIGBEE_CUSTOM_CLUSTER_ID);
    ESP_RETURN_ON_FALSE(custom_cluster_client, , TAG, "Fallo al crear lista atributos Custom Client");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_custom_cluster(cluster_list, custom_cluster_client, ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE));
    ESP_LOGI(TAG, "Añadido Cluster Custom (ID: 0x%04X) como CLIENTE.", ZIGBEE_CUSTOM_CLUSTER_ID);

    esp_zb_ep_list_t *ep_list = esp_zb_ep_list_create();
    ESP_RETURN_ON_FALSE(ep_list, , TAG, "Fallo al crear lista de endpoints");
    esp_zb_endpoint_config_t ep_config = {
        .endpoint = ZIGBEE_COORDINATOR_ENDPOINT,
        .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id = ESP_ZB_HA_COMBINED_INTERFACE_DEVICE_ID,
        .app_device_version = 0
    };
    ESP_ERROR_CHECK(esp_zb_ep_list_add_ep(ep_list, cluster_list, ep_config));
    ESP_LOGI(TAG, "Endpoint %d creado.", ZIGBEE_COORDINATOR_ENDPOINT);

    ESP_ERROR_CHECK(esp_zb_device_register(ep_list));
    ESP_LOGI(TAG, "Dispositivo Coordinador registrado.");

    esp_zb_core_action_handler_register(esp_zb_action_handler);
    ESP_LOGI(TAG, "Manejador de acciones ZCL (esp_zb_action_handler) registrado.");

    ESP_ERROR_CHECK(esp_zb_set_primary_network_channel_set(ZIGBEE_CHANNEL_MASK));
    ESP_LOGI(TAG, "Máscara de canal primaria establecida en 0x%lx", ZIGBEE_CHANNEL_MASK);

    ESP_ERROR_CHECK(esp_zb_start(false));
    ESP_LOGI(TAG, "Stack Zigbee iniciado, esperando formación de red...");

    esp_zb_stack_main_loop();
    vTaskDelete(NULL);
}

// --- Platform Initialization ---
void zigbee_platform_init() {
    esp_zb_platform_config_t config = {
        .radio_config = ESP_ZB_DEFAULT_RADIO_CONFIG(),
        .host_config = ESP_ZB_DEFAULT_HOST_CONFIG(),
    };
    ESP_LOGI(TAG, "Inicializando NVS...");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Problema con NVS, borrando y reintentando...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS inicializado.");
    ESP_LOGI(TAG, "Configurando plataforma Zigbee...");
    ESP_ERROR_CHECK(esp_zb_platform_config(&config));
    ESP_LOGI(TAG, "Plataforma Zigbee configurada.");
}

// --- Main Application Entry Point ---
void app_main(void) {
    ESP_LOGI(TAG, "--- Iniciando Coordinador Zigbee (Manejo Múltiple Sensores) ---");
    // Configurar el nivel de log global o para tags específicos
    esp_log_level_set(TAG, ESP_LOG_DEBUG); // Log más detallado para nuestro módulo
    esp_log_level_set("Zigbee", ESP_LOG_INFO); // Logs del stack Zigbee en INFO
    // Para depuración profunda del stack, puedes usar ESP_LOG_DEBUG o ESP_LOG_VERBOSE para "Zigbee"
    // y activar trazas del stack con esp_zb_set_trace_level_mask()

    init_sensor_data_table();
    zigbee_platform_init();
    xTaskCreate(esp_zb_task, "Zigbee_coord_task", 8192 * 2, NULL, 5, NULL);
}