#include <stdio.h>
#include <inttypes.h>
#include <string.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include <driver/adc.h>
#include <nvs_flash.h>
#include <esp_log.h>
#include <esp_check.h>
#include <main.h>
#include "driver/gpio.h"     // Necesario para GPIO
#include "freertos/timers.h" // Necesario para el Timer

#define TAG "ZIGBEE_ADC_ROUTER"

// --- Configuración ADC ---
#define ADC_CHANNEL ADC1_CHANNEL_1
#define ADC_ATTEN ADC_ATTEN_DB_12
#define ADC_UNIT ADC_UNIT_1
#define ADC_WIDTH ADC_WIDTH_BIT_12

// --- Configuración Zigbee ---
#define ZIGBEE_ENDPOINT 1
#define ZIGBEE_MAX_CHILDREN 10
#define SEND_INTERVAL_MS 10000

// --- Configuración LED ---
#define LED_GPIO GPIO_NUM_8       // <--- ¡PIN ÚNICO PARA EL LED!
#define LED_ON   1                // Asumiendo que 1 enciende el LED
#define LED_OFF  0                // Asumiendo que 0 apaga el LED
#define BLINK_PERIOD_MS 1000      // Periodo de parpadeo (1000ms = 1 segundo ON, 1 segundo OFF)

typedef enum {
    LED_STATE_OFF,                // LED apagado (estado inicial y de error/desconexión)
    LED_STATE_CONNECTED_BLINKING, // LED parpadeando (cuando está conectado)
} led_state_t;

static led_state_t g_led_state = LED_STATE_OFF;
static TimerHandle_t blink_timer = NULL;
static bool g_led_physical_state = false; // Estado físico actual del LED (on/off)

// --- Variables Globales ---
static SemaphoreHandle_t xZigbeeNetworkReadySemaphore = NULL;
// No necesitamos g_is_network_connected, podemos usar g_led_state directamente

// --- Funciones LED ---

// Inicializa el GPIO del LED
static void led_init() {
    gpio_reset_pin(LED_GPIO); // Asegurar que el pin esté limpio
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_GPIO, LED_OFF); // Empezar apagado
    g_led_physical_state = false;
    ESP_LOGI(TAG, "LED (GPIO%d) inicializado.", LED_GPIO);
}

// Establece el estado físico del LED
static void led_set_physical(bool on) {
    gpio_set_level(LED_GPIO, on ? LED_ON : LED_OFF);
    g_led_physical_state = on;
}

// Callback del timer para el parpadeo
static void blink_timer_callback(TimerHandle_t xTimer) {
    // Solo parpadea si estamos en el estado CONNECTED_BLINKING
    if (g_led_state == LED_STATE_CONNECTED_BLINKING) {
        led_set_physical(!g_led_physical_state); // Invierte el estado actual
    } else {
        // Si por alguna razón el timer sigue activo en otro estado, apagar el LED y detener el timer
        led_set_physical(false); // Apagar
        if (xTimerIsTimerActive(blink_timer)) {
            xTimerStop(blink_timer, 0);
            ESP_LOGW(TAG, "Timer de parpadeo detenido porque el estado ya no es CONNECTED_BLINKING.");
        }
    }
}

// Establece el estado deseado del LED
static void led_set_state(led_state_t new_state) {
    if (g_led_state == new_state) {
        return; // Ya estamos en este estado
    }
    ESP_LOGI(TAG, "Cambiando estado del LED de %d a %d", g_led_state, new_state);
    g_led_state = new_state;

    if (blink_timer == NULL) {
        blink_timer = xTimerCreate("BlinkTimer", pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), pdTRUE, (void *)0, blink_timer_callback);
        if (!blink_timer) {
            ESP_LOGE(TAG, "Fallo al crear el timer de parpadeo!");
            return;
        }
    }

    if (new_state == LED_STATE_CONNECTED_BLINKING) {
        // Asegurarse que el timer está activo y parpadeando
        if (!xTimerIsTimerActive(blink_timer)) {
             // Iniciar parpadeo inmediatamente
             led_set_physical(true); // Encender primero
             xTimerChangePeriod(blink_timer, pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), 0); // Periodo para medio ciclo
             xTimerStart(blink_timer, 0);
             ESP_LOGI(TAG, "Timer de parpadeo iniciado.");
        }
    } else { // LED_STATE_OFF
        // Detener el timer si está activo
        if (xTimerIsTimerActive(blink_timer)) {
            xTimerStop(blink_timer, 0);
            ESP_LOGI(TAG, "Timer de parpadeo detenido.");
        }
        // Asegurarse que el LED esté apagado
        led_set_physical(false);
    }
}


// --- Funciones ADC ---
static void adc_init()
{
    ESP_LOGI(TAG, "Inicializando ADC1 Canal %d con atenuación %d", ADC_CHANNEL, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_width(ADC_WIDTH));
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL, ADC_ATTEN));
}

static int read_adc()
{
    int val = adc1_get_raw(ADC_CHANNEL);
    if (val < 0) {
        ESP_LOGE(TAG, "Error al leer ADC");
        return -1;
    }
    return val;
}

// --- Funciones Zigbee ---
static void update_zigbee_present_value(float scaled_value, int raw_value)
{
    float float_value_to_send = scaled_value;

    if (esp_zb_lock_acquire(portMAX_DELAY)) {
        esp_zb_zcl_status_t state = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT,
            ESP_ZB_ZCL_CLUSTER_ID_ANALOG_INPUT,
            ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ESP_ZB_ZCL_ATTR_ANALOG_INPUT_PRESENT_VALUE_ID,
            &float_value_to_send,
            false);

        if (state != ESP_ZB_ZCL_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "Error al actualizar atributo Analog Input: %d", state);
        } else {
            ESP_LOGI(TAG, "Zigbee: Atributo Analog Input actualizado a %.2f (raw: %d)", float_value_to_send, raw_value);
        }
        esp_zb_lock_release();
     } else {
         ESP_LOGE(TAG, "No se pudo adquirir el lock de Zigbee para actualizar atributo");
     }
}

// --- Manejador de Señales Zigbee ---
// Modificado para controlar LED ON/OFF
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct)
{
    uint32_t *p_sg_p       = signal_struct->p_app_signal;
    esp_err_t err_status = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig_type = *p_sg_p;

    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
            ESP_LOGI(TAG, "Zigbee stack initialized");
            led_set_state(LED_STATE_OFF); // <--- LED APAGADO mientras busca
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            break;
        case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
        case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
            if (err_status == ESP_OK) {
                ESP_LOGI(TAG, "Device established network successfully");
                led_set_state(LED_STATE_CONNECTED_BLINKING); // <--- ¡LED PARPADEA al conectarse!
                if (xSemaphoreGive(xZigbeeNetworkReadySemaphore) != pdTRUE) {
                     ESP_LOGW(TAG, "Error al dar el semáforo de red lista.");
                }
            } else {
                ESP_LOGE(TAG, "Failed to establish network: %s (0x%x)", esp_err_to_name(err_status), err_status);
                led_set_state(LED_STATE_OFF); // <--- LED APAGADO si falla
                vTaskDelay(pdMS_TO_TICKS(5000));
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            }
            break;
        case ESP_ZB_BDB_SIGNAL_STEERING_CANCELLED:
            ESP_LOGW(TAG, "Network steering cancelled or failed to find a network.");
            led_set_state(LED_STATE_OFF); // <--- LED APAGADO si no encuentra red
            vTaskDelay(pdMS_TO_TICKS(5000));
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            break;
        case ESP_ZB_ZDO_SIGNAL_LEAVE:
            esp_zb_zdo_signal_leave_params_t *leave_params = (esp_zb_zdo_signal_leave_params_t *)esp_zb_app_signal_get_params(p_sg_p);
            ESP_LOGI(TAG, "Device left the network (reason: %u)", leave_params->leave_type);
            led_set_state(LED_STATE_OFF); // <--- LED APAGADO si deja la red
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); // Intentar reunirse
            break;
        default:
            ESP_LOGI(TAG, "ZDO signal: %s (0x%x), status: %s (0x%x)", esp_zb_zdo_signal_to_string(sig_type), sig_type,
                     esp_err_to_name(err_status), err_status);
            break;
    }
}

// --- Tarea principal de Zigbee ---
static void esp_zb_task(void *pvParameters)
{
    ESP_LOGI(TAG, "Iniciando la tarea esp_zb_task...");
     esp_zb_cfg_t zb_cfg;
    memset(&zb_cfg, 0, sizeof(esp_zb_cfg_t));
    zb_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ROUTER; // Correcto: Actúa como Router
    zb_cfg.nwk_cfg.zczr_cfg.max_children = ZIGBEE_MAX_CHILDREN;
    zb_cfg.install_code_policy = false;
    ESP_LOGI(TAG, "Rol Zigbee establecido como ROUTER (max_children=%d)", ZIGBEE_MAX_CHILDREN);

    esp_zb_init(&zb_cfg);
    ESP_LOGI(TAG, "Stack Zigbee inicializado.");

    // --- Definir Clusters ---
    esp_zb_cluster_list_t *esp_zb_cluster_list = esp_zb_zcl_cluster_list_create();
    ESP_RETURN_ON_FALSE(esp_zb_cluster_list, , TAG, "¡Fallo al crear lista de clusters!");

    // Basic Cluster
    esp_zb_basic_cluster_cfg_t basic_cluster_cfg = { 
        .zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE, 
        .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE };
    esp_zb_attribute_list_t *esp_zb_basic_cluster = esp_zb_basic_cluster_create(&basic_cluster_cfg);
    ESP_RETURN_ON_FALSE(esp_zb_basic_cluster, , TAG, "¡Fallo al crear clúster Basic!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(esp_zb_cluster_list, esp_zb_basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    // Identify Cluster
    esp_zb_identify_cluster_cfg_t identify_cluster_cfg = { .identify_time = 0 };
    esp_zb_attribute_list_t *esp_zb_identify_cluster = esp_zb_identify_cluster_create(&identify_cluster_cfg);
    ESP_RETURN_ON_FALSE(esp_zb_identify_cluster, , TAG, "¡Fallo al crear clúster Identify!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(esp_zb_cluster_list, esp_zb_identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    // Analog Input Cluster
    float initial_value = 0.0f;
    uint8_t status_flags = ESP_ZB_ZCL_ANALOG_INPUT_STATUS_FLAG_NORMAL;
    esp_zb_analog_input_cluster_cfg_t analog_input_cluster_cfg = { 
        .present_value = initial_value, 
        .status_flags = status_flags };
    esp_zb_attribute_list_t *esp_zb_analog_input_cluster = esp_zb_analog_input_cluster_create(&analog_input_cluster_cfg);
     ESP_RETURN_ON_FALSE(esp_zb_analog_input_cluster, , TAG, "¡Fallo al crear clúster Analog Input!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_analog_input_cluster(esp_zb_cluster_list, esp_zb_analog_input_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    ESP_LOGI(TAG, "Clústeres Basic, Identify y Analog Input creados.");

    // --- Definir Endpoint ---
    esp_zb_ep_list_t *esp_zb_ep_list = esp_zb_ep_list_create();
    ESP_RETURN_ON_FALSE(esp_zb_ep_list, , TAG, "¡Fallo al crear lista de endpoints!");

    esp_zb_endpoint_config_t ep_config = {
        .endpoint = ZIGBEE_ENDPOINT,
        .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id = ESP_ZB_HA_SIMPLE_SENSOR_DEVICE_ID, // Usar Simple Sensor ID
        .app_device_version = 0
    };
    ESP_LOGI(TAG, "Configuración de Endpoint: ID=%d, Profile=%04X, DeviceID=%04X",
             ep_config.endpoint, ep_config.app_profile_id, ep_config.app_device_id);

    esp_err_t add_ep_status = esp_zb_ep_list_add_ep(esp_zb_ep_list, esp_zb_cluster_list, ep_config);
    if (add_ep_status != ESP_OK) {
         ESP_LOGE(TAG, "¡Fallo al añadir endpoint a la lista! Error: %s", esp_err_to_name(add_ep_status));
         if (esp_zb_ep_list) { free(esp_zb_ep_list); } // Liberar memoria si falla
         vTaskDelete(NULL);
         return;
    }
    ESP_LOGI(TAG, "Endpoint añadido exitosamente.");

    ESP_ERROR_CHECK(esp_zb_device_register(esp_zb_ep_list));
    ESP_LOGI(TAG, "Dispositivo Zigbee (Endpoint y Clusters) registrado.");

    // --- Arrancar Stack ---
    ESP_LOGI(TAG, "Arrancando el stack Zigbee y buscando red...");
    ESP_ERROR_CHECK(esp_zb_start(true)); // Intenta Network Steering

    // --- Bucle Principal ---
    ESP_LOGI(TAG, "Stack Zigbee arrancado. Entrando en el bucle principal.");
    esp_zb_stack_main_loop(); // No debería retornar
}

// --- Tarea del Sensor ---
static void sensor_update_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando sensor_update_task. Esperando a que la red Zigbee esté lista...");

    if (xSemaphoreTake(xZigbeeNetworkReadySemaphore, portMAX_DELAY) == pdTRUE) {
        ESP_LOGI(TAG, "¡Red Zigbee lista! Iniciando lecturas ADC y actualizaciones.");

        while (1) {
            int adc_raw = read_adc();

            if (adc_raw >= 0) {
                const float ADC_VREF_APPROX = 3.1f;
                float voltage = ((float)adc_raw / 4095.0f) * ADC_VREF_APPROX;
                float scaled_value = (voltage / ADC_VREF_APPROX) * 100.0f;
                if (scaled_value < 0.0f) scaled_value = 0.0f;
                if (scaled_value > 100.0f) scaled_value = 100.0f;

                ESP_LOGD(TAG, "Valor ADC escalado: %.2f %% (Voltage: %.3f V, Raw: %d)", scaled_value, voltage, adc_raw);
                update_zigbee_present_value(scaled_value, adc_raw);

            } else {
                ESP_LOGW(TAG, "Lectura ADC inválida, saltando actualización Zigbee.");
                // Podrías poner led_set_state(LED_STATE_OFF) aquí si quieres que un error de ADC apague el parpadeo
            }
            vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
        }
    } else {
         ESP_LOGE(TAG, "¡Timeout esperando el semáforo de red lista!");
         led_set_state(LED_STATE_OFF); // Apagar si no se conecta
    }

    ESP_LOGW(TAG, "sensor_update_task terminando.");
    led_set_state(LED_STATE_OFF); // Asegurar apagado al salir
    vTaskDelete(NULL);
}

// --- Inicialización Plataforma ---
void zigbee_platform_init()
{
    esp_zb_platform_config_t config = {
        .radio_config = ESP_ZB_DEFAULT_RADIO_CONFIG(),
        .host_config = ESP_ZB_DEFAULT_HOST_CONFIG(),
    };
    ESP_LOGI(TAG, "1. Inicializando NVS (desde zigbee_platform_init)...");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
      ESP_LOGW(TAG, "Problema con NVS, borrando y reintentando...");
      ESP_ERROR_CHECK(nvs_flash_erase());
      ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG,"NVS inicializado.");
    ESP_LOGI(TAG, "2. Configurando plataforma Zigbee (desde zigbee_platform_init)...");
    ESP_ERROR_CHECK(esp_zb_platform_config(&config));
    ESP_LOGI(TAG, "Plataforma Zigbee configurada.");
}

// --- Función Principal ---
void app_main(void)
{
    ESP_LOGI(TAG, "--- Iniciando Sensor ADC + Router Zigbee (LED Pin %d) ---", LED_GPIO);

    xZigbeeNetworkReadySemaphore = xSemaphoreCreateBinary();
    if (xZigbeeNetworkReadySemaphore == NULL) {
        ESP_LOGE(TAG, "¡Fallo al crear el semáforo!");
        return;
    }

    led_init(); // <-- Inicializar LED (empieza apagado)
    led_set_state(LED_STATE_OFF); // <-- Asegurar estado inicial apagado

    zigbee_platform_init();
    adc_init();

    ESP_LOGI(TAG, "Creando la tarea esp_zb_task...");
    xTaskCreate(esp_zb_task, "zigbee_task", 4096, NULL, 5, NULL);

    ESP_LOGI(TAG, "Creando la tarea sensor_update_task...");
    xTaskCreate(sensor_update_task, "sensor_task", 3072, NULL, 4, NULL);

    ESP_LOGI(TAG, "app_main: Inicialización completada. Tareas creadas.");
}

//1 minuto 10 segundos