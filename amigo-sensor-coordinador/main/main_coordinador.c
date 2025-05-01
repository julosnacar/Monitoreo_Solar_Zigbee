/**
 * @file main_coordinator.c
 * @brief Basic Zigbee Coordinator for ESP32-H2
 *
 * Forms a Zigbee network and listens for Analog Input reports from sensor devices.
 */
#include <stdio.h>
#include <string.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <nvs_flash.h>
#include <esp_log.h>
#include <esp_check.h>
#include "main_coordinador.h"

#define TAG "COORDINADOR ZIGBEE"

// --- Zigbee Configuration ---
#define ZIGBEE_CHANNEL_MASK (1l << 15)       // Example: Use channel 15. Choose an unused channel in your area.
#define ZIGBEE_COORDINATOR_ENDPOINT 1        // Endpoint for the coordinator's application clusters
#define ZIGBEE_PERMIT_JOIN_DURATION 180      // Allow devices to join for 180 seconds after network formation

// --- Configuración Cluster Personalizado ---
#define ZIGBEE_CUSTOM_CLUSTER_ID        0xFC01 // ID de Cluster Personalizado
#define ATTR_ID_CURRENT_SENSOR_1        0x0001 // ID de Atributo para Corriente 1
#define ATTR_ID_CURRENT_SENSOR_2        0x0002 // ID de Atributo para Corriente 2
#define ATTR_ID_CURRENT_SENSOR_3        0x0003 // ID de Atributo para Corriente 3

// --- ZCL Attribute Handler ---
// This function gets called when ZCL commands targeted at this device's endpoints arrive.
// We are interested in 'Report Attributes' command from the Analog Input cluster.
esp_err_t esp_zb_zcl_attr_handler(const esp_zb_zcl_cmd_info_t *cmd_info, const void *user_data) {
    if (!cmd_info) {
        ESP_LOGE(TAG, "Invalid ZCL command info received");
        return ESP_FAIL;
    }

    ESP_LOGD(TAG, "ZCL attribute handler: Cluster=0x%04X, CmdDir=%d, IsCommon=%d, CmdID=0x%02X",
            cmd_info->cluster, cmd_info->command.direction, cmd_info->command.is_common, cmd_info->command.id);

    // Verificar si es un comando 'Report Attributes' entrante
    if (cmd_info->command.direction == ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV &&
        cmd_info->command.is_common &&
        cmd_info->command.id == 0x0A) // 0x0A es Report Attributes
    {
        esp_zb_zcl_report_attr_message_t *report_msg = (esp_zb_zcl_report_attr_message_t *)user_data;
        esp_zb_zcl_attribute_t *reported_attr = &(report_msg->attribute);

        uint16_t sender_short_addr = cmd_info->src_address.u.short_addr; // Identificador del sensor
        uint8_t sender_endpoint = cmd_info->src_endpoint;

        // --- CAMBIO: Verificar si es NUESTRO cluster personalizado ---
        if (cmd_info->cluster == ZIGBEE_CUSTOM_CLUSTER_ID &&
            reported_attr->data.type == ESP_ZB_ZCL_ATTR_TYPE_SINGLE) // Verificar que sea float
        {
            float received_current = *(float *)(reported_attr->data.value);

            // ---Determinar qué sensor es por el ID del atributo ---
            const char *sensor_name = "Desconocido";
            if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_1) {
                sensor_name = "Sensor 1";
            } else if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_2) {
                sensor_name = "Sensor 2";
            } else if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_3) {
                sensor_name = "Sensor 3";
            }

            // Imprimir el dato recibido específico
            ESP_LOGI(TAG, "Dato Recibido del Dispositivo [Addr: 0x%04X, EP: %d]: %s = %.3f A",
                    sender_short_addr, sender_endpoint, sensor_name, received_current);

            // --- Lógica para recolectar los 3 valores y enviar a AWS ---
            // Aquí necesitarías una estructura o diccionario (similar al script Python)
            // para almacenar temporalmente las lecturas de cada sensor (identificado por sender_short_addr)
            // hasta que tengas las 3, y luego enviar el conjunto completo.
            // Ejemplo MUY básico (no maneja múltiples sensores simultáneamente) TOMADO DOCUMENTACION:
            /*
            static float currents[3] = {NAN, NAN, NAN}; // Usar NAN de math.h si es posible
            static uint8_t received_mask = 0;
            int sensor_index = -1;

            if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_1) sensor_index = 0;
            else if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_2) sensor_index = 1;
            else if (reported_attr->id == ATTR_ID_CURRENT_SENSOR_3) sensor_index = 2;

            if (sensor_index != -1) {
                currents[sensor_index] = received_current;
                received_mask |= (1 << sensor_index);
            }

            if (received_mask == 0b111) { // Si tenemos los 3 bits (lecturas)
                ESP_LOGI(TAG, "¡Lecturas completas del sensor 0x%04X! S1=%.3f, S2=%.3f, S3=%.3f",
                         sender_short_addr, currents[0], currents[1], currents[2]);
                // ¡¡AQUÍ LLAMARÍAS A LA FUNCIÓN PARA ENVIAR A AWS/RASPBERRY PI!!
                // Por ejemplo: send_data_to_host(sender_short_addr, currents[0], currents[1], currents[2]);
                received_mask = 0; // Resetear para el próximo conjunto
                // currents[0] = currents[1] = currents[2] = NAN; // Limpiar opcionalmente
            }
            */
            // ------------------------------------------------------------------

        } else {
            ESP_LOGD(TAG, "Reporte de atributo recibido pero no procesado: Cluster 0x%04X, Atributo 0x%04X",
                    cmd_info->cluster, reported_attr->id);
        }
    } else {
        ESP_LOGD(TAG, "Comando ZCL no manejado recibido: Cluster 0x%04X, Comando 0x%02X, Dirección %d",
                cmd_info->cluster, cmd_info->command.id, cmd_info->command.direction);
    }
    return ESP_OK;
}

// --- Callback Principal de Acciones Zigbee Core ---
esp_err_t esp_zb_action_handler(esp_zb_core_action_callback_id_t callback_id, const void *message)
{
    esp_err_t ret = ESP_OK;

    switch (callback_id) {
        case ESP_ZB_CORE_CMD_REPORT_CONFIG_RESP_CB_ID:
            // Manejar respuestas de configuración de reportes si es necesario
            ESP_LOGI(TAG, "Recibida respuesta de Config Report");
            break;

        case ESP_ZB_CORE_REPORT_ATTR_CB_ID: // <--- ¡Este es el evento para reportes de atributos!
        {
            esp_zb_zcl_report_attr_message_t *report_message = (esp_zb_zcl_report_attr_message_t *)message;
            ESP_LOGD(TAG, "Recibido reporte de atributo desde 0x%04x (EP %d), Cluster 0x%04x, Attr 0x%04x",
                     report_message->src_address.u.short_addr, report_message->src_endpoint,
                     report_message->cluster, report_message->attribute.id);

            // Crear una estructura cmd_info simulada para pasar a tu handler original
            // (Esto es una adaptación, podrías refactorizar esp_zb_zcl_attr_handler para aceptar directamente report_message)
            esp_zb_zcl_cmd_info_t cmd_info;
            memset(&cmd_info, 0, sizeof(esp_zb_zcl_cmd_info_t)); // Inicializar a cero

            // Llenar los campos necesarios para tu handler
            cmd_info.status = report_message->status; // Usar el status del mensaje de reporte
            cmd_info.src_address = report_message->src_address;
            cmd_info.dst_endpoint = report_message->dst_endpoint; // El endpoint de nuestro coordinador
            cmd_info.src_endpoint = report_message->src_endpoint;
            cmd_info.cluster = report_message->cluster;
            cmd_info.profile = ESP_ZB_AF_HA_PROFILE_ID; // Asumir perfil HA
            // Simular un comando de reporte entrante
            cmd_info.command.id = 0x0A; // Report Attributes ID
            cmd_info.command.direction = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV; // Viniendo hacia nuestro servidor
            cmd_info.command.is_common = true; // Es un comando común

            // Llamar a manejador específico
            ret = esp_zb_zcl_attr_handler(&cmd_info, (void*)report_message);

            break;
        }

        case ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID:
            // Manejar notificaciones locales de cambio de atributo si es necesario
            ESP_LOGI(TAG, "Atributo local actualizado");
            break;

        // Añadir más casos para otros eventos que necesites manejar (ej. Identify, Scenes, etc.)
        // case ESP_ZB_CORE_IDENTIFY_EFFECT_CB_ID:
        //    ...
        //    break;

        default:
            ESP_LOGW(TAG, "Callback de acción no manejado: ID=0x%04x", callback_id);
            ret = ESP_FAIL; // O ESP_OK si quieres ignorar silenciosamente
            break;
    }
    return ret;
}
// --- Zigbee Stack Signal Handler ---
// Handles events like network formation, device joining, etc.
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {

    uint32_t *p_sg_p = signal_struct->p_app_signal;
    esp_err_t err_status = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig_type = *p_sg_p;

    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
            // Stack is initialized, tell Base Device Behavior (BDB) to try forming a network
            ESP_LOGI(TAG, "Stack inicializado, intentando formar red...");
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_NETWORK_FORMATION);
            break;

        case ESP_ZB_BDB_SIGNAL_FORMATION: // Signal related to network formation
            if (err_status == ESP_OK) {
                // Network formed successfully!
                esp_zb_ieee_addr_t extended_pan_id; // Declara una variable local
                esp_zb_get_extended_pan_id(extended_pan_id); // Obtén el ID extendido
                ESP_LOGI(TAG, "¡Red formada exitosamente! Dirección Coordinador: 0x%04X, Canal: %d, PAN ID Ext: %02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X",
                    esp_zb_get_short_address(),
                    esp_zb_get_current_channel(),
                    extended_pan_id[0],
                    extended_pan_id[1],
                    extended_pan_id[2],
                    extended_pan_id[3],
                    extended_pan_id[4],
                    extended_pan_id[5],
                    extended_pan_id[6],
                    extended_pan_id[7]);

                // Open the network for other devices to join for a limited time
                esp_zb_bdb_open_network(ZIGBEE_PERMIT_JOIN_DURATION);
                ESP_LOGI(TAG, "Red abierta para unirse durante %d segundos.", ZIGBEE_PERMIT_JOIN_DURATION);
            } else {
                // Failed to form the network
                ESP_LOGE(TAG, "Fallo al formar la red: %s (0x%x)", esp_err_to_name(err_status), err_status);
                // You might want to add retry logic here
                ESP_LOGI(TAG, "Reintentando formación de red en 5 segundos...");
                vTaskDelay(pdMS_TO_TICKS(5000));
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_NETWORK_FORMATION);
            }
            break;

        case ESP_ZB_ZDO_SIGNAL_DEVICE_ANNCE: { // A new device announced itself (joined or rejoined)
            esp_zb_zdo_signal_device_annce_params_t *dev_annce_params = (esp_zb_zdo_signal_device_annce_params_t *)esp_zb_app_signal_get_params(p_sg_p);
            ESP_LOGI(TAG, "Nuevo dispositivo unido/anunciado: Addr Corta=0x%04X, IEEE Addr=%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X, Capacidad=0x%02X",
                     dev_annce_params->device_short_addr,
                     dev_annce_params->ieee_addr[0], dev_annce_params->ieee_addr[1], dev_annce_params->ieee_addr[2],
                     dev_annce_params->ieee_addr[3], dev_annce_params->ieee_addr[4], dev_annce_params->ieee_addr[5],
                     dev_annce_params->ieee_addr[6], dev_annce_params->ieee_addr[7], dev_annce_params->capability);
            // You could potentially trigger an attribute read here if needed,
            // but relying on reporting configured by the sensor is usually better.
        } break;

        case ESP_ZB_ZDO_SIGNAL_LEAVE_INDICATION: { // A device sent a leave indication
            esp_zb_zdo_signal_leave_indication_params_t *leave_ind_params = (esp_zb_zdo_signal_leave_indication_params_t *)esp_zb_app_signal_get_params(p_sg_p);
             ESP_LOGI(TAG, "Dispositivo dejó la red: Addr Corta=0x%04X, IEEE Addr=%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X, Rejoin=%d",
                     leave_ind_params->short_addr,
                     leave_ind_params->device_addr[0], leave_ind_params->device_addr[1], leave_ind_params->device_addr[2],
                     leave_ind_params->device_addr[3], leave_ind_params->device_addr[4], leave_ind_params->device_addr[5],
                     leave_ind_params->device_addr[6], leave_ind_params->device_addr[7], leave_ind_params->rejoin);
             // Add logic to handle device leaving if necessary (e.g., remove from database)
        } break;

        // Handle other signals if needed (e.g., ESP_ZB_NWK_SIGNAL_PERMIT_JOIN_STATUS)
        default:
            ESP_LOGI(TAG, "Señal ZDO no manejada: Tipo=0x%x, Estado=%s (0x%x)",
                     sig_type, esp_err_to_name(err_status), err_status);
            break;
    }
}

// --- Zigbee Task ---
// Sets up Zigbee stack, registers clusters/endpoints, starts stack, enters main loop
static void esp_zb_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando tarea Zigbee Coordinador...");

    // 1. Configure Zigbee Stack as Coordinator
    esp_zb_cfg_t zb_cfg = ESP_ZB_ZC_CONFIG(); // Use default Coordinator config
    // zb_cfg.zczr_cfg.max_children can be adjusted if needed, default is usually sufficient

    // 2. Initialize Zigbee Stack
    esp_zb_init(&zb_cfg);
    ESP_LOGI(TAG, "Stack Zigbee inicializado como Coordinador.");

    // 3. Define Clusters for the Coordinator Endpoint
    // Coordinators typically need Basic and Identify (Server role) for network management.
    // To *receive* data from sensors using standard clusters, it needs those clusters in the *Client* role.
    esp_zb_cluster_list_t *cluster_list = esp_zb_zcl_cluster_list_create();
    ESP_RETURN_ON_FALSE(cluster_list, , TAG, "Fallo al crear lista de clusters");

    // Basic Cluster (Server) - Mandatory
    esp_zb_basic_cluster_cfg_t basic_cfg = { 
        .zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE, 
        .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE };
    esp_zb_attribute_list_t *basic_cluster = esp_zb_basic_cluster_create(&basic_cfg);
    ESP_RETURN_ON_FALSE(basic_cluster, , TAG, "Fallo al crear clúster Basic");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(cluster_list, basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    // Identify Cluster (Server) - Recommended
    esp_zb_identify_cluster_cfg_t identify_cfg = { .identify_time = 0 };
    esp_zb_attribute_list_t *identify_cluster = esp_zb_identify_cluster_create(&identify_cfg);
    ESP_RETURN_ON_FALSE(identify_cluster, , TAG, "Fallo al crear clúster Identify");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(cluster_list, identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    // -- Cluster Personalizado (Cliente) para recibir datos del sensor ---
    esp_zb_attribute_list_t *custom_cluster_client = esp_zb_zcl_attr_list_create(ZIGBEE_CUSTOM_CLUSTER_ID);
    ESP_RETURN_ON_FALSE(custom_cluster_client, , TAG, "Fallo al crear lista atributos Custom Client");
    // No se necesitan atributos específicos en el lado cliente para solo recibir reportes
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_custom_cluster(cluster_list, custom_cluster_client, ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE));
    ESP_LOGI(TAG, "Añadido Cluster Custom (ID: 0x%04X) como Cliente.", ZIGBEE_CUSTOM_CLUSTER_ID);
    
    /*// Analog Input Cluster (Client) - To receive reports from sensors
    esp_zb_attribute_list_t *analog_input_client_cluster = esp_zb_zcl_attr_list_create(ESP_ZB_ZCL_CLUSTER_ID_ANALOG_INPUT);
    ESP_RETURN_ON_FALSE(analog_input_client_cluster, , TAG, "Fallo al crear lista atributos Analog Input Client");
    // No attributes needed for client side usually, unless you want to store reporting config locally
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_analog_input_cluster(cluster_list, analog_input_client_cluster, ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE));
    ESP_LOGI(TAG, "Clústeres definidos: Basic(S), Identify(S), AnalogInput(C)");*/

    // 4. Define Endpoint for the Coordinator
    esp_zb_ep_list_t *ep_list = esp_zb_ep_list_create();
    ESP_RETURN_ON_FALSE(ep_list, , TAG, "Fallo al crear lista de endpoints");
    esp_zb_endpoint_config_t ep_config = {
        .endpoint = ZIGBEE_COORDINATOR_ENDPOINT,
        .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,           // Home Automation Profile
        .app_device_id = ESP_ZB_HA_COMBINED_INTERFACE_DEVICE_ID, // Or ESP_ZB_HA_HOME_GATEWAY_DEVICE_ID
        .app_device_version = 0
    };
    ESP_ERROR_CHECK(esp_zb_ep_list_add_ep(ep_list, cluster_list, ep_config));
    ESP_LOGI(TAG, "Endpoint %d creado.", ZIGBEE_COORDINATOR_ENDPOINT);

    // 5. Register the Coordinator Device (with its endpoint and clusters)
    ESP_ERROR_CHECK(esp_zb_device_register(ep_list));
    ESP_LOGI(TAG, "Dispositivo Coordinador registrado.");

    // 6. Register the ZCL attribute handler callback
    esp_zb_core_action_handler_register(esp_zb_action_handler);
    ESP_LOGI(TAG, "Manejador de atributos ZCL registrado.");

    // 7. Set the channel mask (optional but recommended for coordinators)
    ESP_ERROR_CHECK(esp_zb_set_primary_network_channel_set(ZIGBEE_CHANNEL_MASK));
    ESP_LOGI(TAG, "Máscara de canal establecida en 0x%lx", ZIGBEE_CHANNEL_MASK);

    // 8. Start the Zigbee Stack (will trigger signal handler for network formation)
    ESP_ERROR_CHECK(esp_zb_start(false)); // Use 'false' to let signal handler start formation
    ESP_LOGI(TAG, "Stack Zigbee iniciado, esperando formación de red...");

    // 9. Enter the main processing loop (never returns)
    esp_zb_stack_main_loop();

    // Cleanup (will likely not be reached)
    vTaskDelete(NULL);
}

// --- Platform Initialization ---
// Initializes NVS and the Zigbee platform configuration
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
    ESP_LOGI(TAG, "--- Iniciando Coordinador Zigbee ---");

    // Set Zigbee log level
    esp_log_level_set("Zigbee", ESP_LOG_INFO);

    // (NVS, Zigbee radio/host config)
    zigbee_platform_init();

    // Create and start the main Zigbee task
    xTaskCreate(esp_zb_task, "Zigbee_coordinator_task", 8192, NULL, 5, NULL); // Increased stack size for coordinator
}