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
#include "driver/gpio.h"
#include "freertos/timers.h"
#include "led_strip.h" //LED RGB
#include "sdkconfig.h"

#define TAG "ZIGBEE_SENSOR_ROUTER"

// --- Configuración ADC (Según pinout anterior) ---
#define ADC_CHANNEL_1 ADC1_CHANNEL_0 // Sensor 1 conectado a GPIO1
#define ADC_CHANNEL_2 ADC1_CHANNEL_1 // Sensor 2 conectado a GPIO2
#define ADC_CHANNEL_3 ADC1_CHANNEL_2 // Sensor 3 conectado a GPIO3
#define ADC_ATTEN ADC_ATTEN_DB_12 
#define ADC_UNIT ADC_UNIT_1
#define ADC_WIDTH ADC_WIDTH_BIT_12
#define ADC_VREF_MV 3300

// --- Configuración Sensor Corriente HSTS016L ---
#define SENSOR_ZERO_CURRENT_VOLTAGE_MV 1650.0f
#define SENSOR_SENSITIVITY_MV_PER_A    250.0f

// --- Configuración Zigbee ---
#define ZIGBEE_ENDPOINT 1
#define ZIGBEE_MAX_CHILDREN 10
#define SEND_INTERVAL_MS 10000

// --- Configuración Cluster Personalizado ---
#define ZIGBEE_CUSTOM_CLUSTER_ID        0xFC01
#define ATTR_ID_CURRENT_SENSOR_1        0x0001
#define ATTR_ID_CURRENT_SENSOR_2        0x0002
#define ATTR_ID_CURRENT_SENSOR_3        0x0003

// --- Configuración LED RGB ---
#define RGB_LED_GPIO 8//CONFIG_BLINK_GPIO // <--- CAMBIO: Usar GPIO de menuconfig o definir uno aquí (ej: GPIO_NUM_X)
#define BLINK_PERIOD_MS 1000          // Periodo de parpadeo total (500ms ON, 500ms OFF)

// <--- CAMBIO: Enum para los estados del LED RGB
typedef enum {
    LED_STATE_OFF = 0,            // Estado inicial o apagado explícito
    LED_STATE_INIT_BLINK,         // Amarillo parpadeante (Stack iniciado, antes de buscar)
    LED_STATE_SEARCHING_BLINK,    // Azul tenue parpadeante (Buscando red)
    LED_STATE_JOINING_BLINK,      // Naranja parpadeante (Intentando unirse/autenticarse)
    LED_STATE_CONNECTED_BLINK,    // Verde parpadeante (Conectado y operacional)
    LED_STATE_ERROR_BLINK,        // Rojo parpadeante (Error de conexión / Dejado la red)
} led_state_t;

// <--- CAMBIO: Estructura para colores RGB
typedef struct {
    uint8_t r;
    uint8_t g;
    uint8_t b;
} rgb_color_t;

// <--- CAMBIO: Definición de colores para cada estado (valores bajos para no saturar)
const rgb_color_t COLOR_OFF      = {0, 0, 0};//16, 0, 0 --> verde 0, 16, 0 --> rojo 0, 0, 16 --> azul
const rgb_color_t COLOR_YELLOW   = {16, 16, 0}; // Amarillo
const rgb_color_t COLOR_BLUE     = {0, 0, 16};  // Azul tenue
const rgb_color_t COLOR_ORANGE   = {255, 16, 0};  // Naranja
const rgb_color_t COLOR_GREEN    = {16, 0, 0};  // Verde
const rgb_color_t COLOR_RED      = {0, 16, 0};  // Rojo

static led_strip_handle_t g_led_strip = NULL; // <--- CAMBIO: Handle para el LED strip
static led_state_t g_led_state = LED_STATE_OFF;
static TimerHandle_t blink_timer = NULL;
static bool g_led_physical_state_on = false; // Indica si el LED está físicamente encendido (true) o apagado (false) en el ciclo de parpadeo
static rgb_color_t g_current_color = {0, 0, 0}; // Color objetivo para el estado actual

// --- Variables Globales ---
static SemaphoreHandle_t xZigbeeNetworkReadySemaphore = NULL;

// --- Funciones LED RGB ---

// <--- CAMBIO: Inicializa el LED RGB usando led_strip
static void led_init() {
    ESP_LOGI(TAG, "Configurando LED RGB en GPIO%d", RGB_LED_GPIO);
    led_strip_config_t strip_config = {
        .strip_gpio_num = RGB_LED_GPIO,
        .max_leds = 1, // Solo un LED 
    };
    // Asumimos RMT como backend, ajusta si usas SPI
    led_strip_rmt_config_t rmt_config = {
        .resolution_hz = 10 * 1000 * 1000, // 10MHz
        .flags.with_dma = false,
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &g_led_strip));
    if (g_led_strip) {
        led_strip_clear(g_led_strip); // Apagar al inicio
        ESP_LOGI(TAG, "LED RGB inicializado.");
    } else {
        ESP_LOGE(TAG, "Fallo al inicializar LED RGB!");
    }
}

// <--- CAMBIO: Enciende el LED con un color específico
static void led_set_rgb(uint8_t r, uint8_t g, uint8_t b) {
    if (g_led_strip) {
        led_strip_set_pixel(g_led_strip, 0, r, g, b);
        led_strip_refresh(g_led_strip);
        g_led_physical_state_on = (r > 0 || g > 0 || b > 0); // Está encendido si algún color > 0
    }
}

// Apaga el LED
static void led_off() {
    if (g_led_strip) {
        led_strip_clear(g_led_strip);
        // led_strip_refresh(g_led_strip); // Clear ya refresca implicitamente en algunas versiones? Mejor refrescar explícitamente.
         led_strip_refresh(g_led_strip);
        g_led_physical_state_on = false;
    }
}

// Callback del timer adaptado para RGB
static void blink_timer_callback(TimerHandle_t xTimer) {
    // Solo parpadea si estamos en un estado _BLINK
    if (g_led_state == LED_STATE_INIT_BLINK ||
        g_led_state == LED_STATE_SEARCHING_BLINK ||
        g_led_state == LED_STATE_JOINING_BLINK ||
        g_led_state == LED_STATE_CONNECTED_BLINK ||
        g_led_state == LED_STATE_ERROR_BLINK)
    {
        if (g_led_physical_state_on) {
            // Si está encendido, apagarlo
            led_off();
        } else {
            // Si está apagado, encenderlo con el color actual del estado
            led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
        }
    } else {
        // Si no es un estado de parpadeo, asegurar que esté apagado y detener timer
        led_off();
        if (xTimerIsTimerActive(blink_timer)) {
            xTimerStop(blink_timer, 0);
            ESP_LOGW(TAG, "Timer de parpadeo detenido porque el estado ya no es BLINK.");
        }
    }
}

// <--- CAMBIO: Establece el estado deseado del LED RGB
static void led_set_state(led_state_t new_state) {
    if (g_led_state == new_state) {
        return; // Ya estamos en este estado
    }
    ESP_LOGI(TAG, "Cambiando estado del LED de %d a %d", g_led_state, new_state);

    // Detener parpadeo anterior si estaba activo
    if (blink_timer && xTimerIsTimerActive(blink_timer)) {
        xTimerStop(blink_timer, 0);
        ESP_LOGI(TAG, "Timer de parpadeo detenido para cambio de estado.");
    }
    // Apagar LED antes de cambiar estado (evita flash de color incorrecto)
    led_off();
    vTaskDelay(pdMS_TO_TICKS(100)); // Pequeña pausa para asegurar que el LED se apague

    g_led_state = new_state;

    // Asignar el color correspondiente al nuevo estado
    switch (new_state) {
        case LED_STATE_INIT_BLINK:
            g_current_color = COLOR_YELLOW;
            break;
        case LED_STATE_SEARCHING_BLINK:
            g_current_color = COLOR_BLUE;
            break;
        case LED_STATE_JOINING_BLINK:
            g_current_color = COLOR_ORANGE;
            break;
        case LED_STATE_CONNECTED_BLINK:
            g_current_color = COLOR_GREEN;
            break;
        case LED_STATE_ERROR_BLINK:
            ESP_LOGW(TAG,"ESTOY DENTRO DE SWITCH Y CAMBIE A ROJO");
            g_current_color = COLOR_RED;
            break;
        case LED_STATE_OFF:
        default:
            g_current_color = COLOR_OFF;
            break;
    }

    // Si el nuevo estado requiere parpadeo
    if (new_state == LED_STATE_INIT_BLINK ||
        new_state == LED_STATE_SEARCHING_BLINK ||
        new_state == LED_STATE_JOINING_BLINK ||
        new_state == LED_STATE_CONNECTED_BLINK ||
        new_state == LED_STATE_ERROR_BLINK)
    {
        if (blink_timer == NULL) {
            blink_timer = xTimerCreate("BlinkTimer", pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), pdTRUE, (void *)0, blink_timer_callback);
            if (!blink_timer) {
                ESP_LOGE(TAG, "Fallo al crear el timer de parpadeo!");
                return;
            }
        }
        // Iniciar parpadeo inmediatamente (encendiendo primero)
        led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
        xTimerChangePeriod(blink_timer, pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), 0); // Periodo para medio ciclo
        xTimerStart(blink_timer, 0);
        ESP_LOGI(TAG, "Timer de parpadeo iniciado para estado %d.", new_state);
    } else { // LED_STATE_OFF
        led_off(); // Asegurar que esté apagado
    }
}


// --- Funciones ADC ---
static void adc_init()
{
    ESP_LOGI(TAG, "Inicializando ADC1...");
    ESP_ERROR_CHECK(adc1_config_width(ADC_WIDTH));
    ESP_LOGI(TAG, "Configurando Sensor 1 (ADC1_CHANNEL_%d -> GPIO1) con atenuación %d", ADC_CHANNEL_1, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_1, ADC_ATTEN));
    ESP_LOGI(TAG, "Configurando Sensor 2 (ADC1_CHANNEL_%d -> GPIO2) con atenuación %d", ADC_CHANNEL_2, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_2, ADC_ATTEN));
    ESP_LOGI(TAG, "Configurando Sensor 3 (ADC1_CHANNEL_%d -> GPIO3) con atenuación %d", ADC_CHANNEL_3, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_3, ADC_ATTEN));
    // ... (código de calibración opcional) ...
}

static int read_adc_voltage_mv(adc1_channel_t channel)
{
    int adc_raw = adc1_get_raw(channel);
    if (adc_raw < 0) {
        ESP_LOGE(TAG, "Error al leer ADC del canal %d", channel);
        return -1;
    }
    uint32_t voltage_mv = (uint32_t)(((float)adc_raw / 4095.0f) * ADC_VREF_MV);
    return (int)voltage_mv;
}

// --- Funciones Zigbee (Cluster Personalizado, Actualización) ---
typedef struct {
    float current_sensor_1;
    float current_sensor_2;
    float current_sensor_3;
} esp_zb_custom_cluster_cfg_t;

esp_zb_attribute_list_t* esp_zb_custom_cluster_create(esp_zb_custom_cluster_cfg_t *cfg) {

     esp_zb_attribute_list_t *attr_list = esp_zb_zcl_attr_list_create(ZIGBEE_CUSTOM_CLUSTER_ID);
    if (!attr_list) {
        ESP_LOGE(TAG, "Fallo al crear lista de atributos para cluster custom");
        return NULL;
    }
    esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_1, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                            ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_1));
    esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_2, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                            ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_2));
    esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_3, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                            ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_3));
    ESP_LOGI(TAG, "Cluster personalizado (ID: 0x%04X) creado con 3 atributos float", ZIGBEE_CUSTOM_CLUSTER_ID);
    return attr_list;
}

static void update_sensor_currents(float current_1, float current_2, float current_3)
{
    if (esp_zb_lock_acquire(portMAX_DELAY)) {
        esp_zb_zcl_status_t state1 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_1, &current_1, false);
        esp_zb_zcl_status_t state2 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_2, &current_2, false);
        esp_zb_zcl_status_t state3 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_3, &current_3, false);

        if (state1 != ESP_ZB_ZCL_STATUS_SUCCESS || state2 != ESP_ZB_ZCL_STATUS_SUCCESS || state3 != ESP_ZB_ZCL_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "Error al actualizar atributos de corriente: S1=%d, S2=%d, S3=%d", state1, state2, state3);
        } else {
            ESP_LOGI(TAG, "Zigbee: Atributos Cluster Custom actualizados: S1=%.2f A, S2=%.2f A, S3=%.2f A", current_1, current_2, current_3);
        }
        esp_zb_lock_release();
    } else {
        ESP_LOGE(TAG, "No se pudo adquirir el lock de Zigbee para actualizar atributos de corriente");
    }
}


// --- Manejador de Señales Zigbee ---
// <--- CAMBIO: Actualizado para usar los nuevos estados LED RGB
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct)
{
    uint32_t *p_sg_p       = signal_struct->p_app_signal;
    esp_err_t err_status = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig_type = *p_sg_p;

    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
            ESP_LOGI(TAG, "Zigbee stack initialized");
            led_set_state(LED_STATE_SEARCHING_BLINK); // <-- Azul parpadeante: Buscando red
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            break;
        case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
        case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
            // Consideramos que aquí está "Uniéndose" antes de saber el resultado
            led_set_state(LED_STATE_JOINING_BLINK); // <-- Naranja parpadeante: Intentando unirse
            if (err_status == ESP_OK) {
                ESP_LOGI(TAG, "Device established network successfully");
                led_set_state(LED_STATE_CONNECTED_BLINK); // <-- Verde parpadeante: Conectado
                if (xSemaphoreGive(xZigbeeNetworkReadySemaphore) != pdTRUE) {
                     ESP_LOGW(TAG, "Error al dar el semáforo de red lista.");
                }
            } else {
                ESP_LOGE(TAG, "Failed to establish network: %s (0x%x)", esp_err_to_name(err_status), err_status);
                led_set_state(LED_STATE_ERROR_BLINK); // <-- Rojo parpadeante: Error
                // Intentar reunirse de nuevo después de un retraso
                vTaskDelay(pdMS_TO_TICKS(5000));
                 led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            }
            break;
         case ESP_ZB_BDB_SIGNAL_STEERING_CANCELLED:
            ESP_LOGW(TAG, "Network steering cancelled or failed to find a network.");
            led_set_state(LED_STATE_ERROR_BLINK); // <-- Rojo parpadeante: Error/No encontrado
            // Intentar reunirse de nuevo después de un retraso
            vTaskDelay(pdMS_TO_TICKS(5000));
            led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            break;
        case ESP_ZB_ZDO_SIGNAL_LEAVE:
             esp_zb_zdo_signal_leave_params_t *leave_params = (esp_zb_zdo_signal_leave_params_t *)esp_zb_app_signal_get_params(p_sg_p);
             ESP_LOGI(TAG, "Device left the network (reason: %u)", leave_params->leave_type);
             led_set_state(LED_STATE_ERROR_BLINK); // <-- Rojo parpadeante: Dejado la red
             // Liberar semáforo si estaba tomado
             if (xSemaphoreGive(xZigbeeNetworkReadySemaphore) == pdTRUE) {
                 ESP_LOGI(TAG, "Semáforo liberado debido a salida de red.");
             }
             // Intentar reunirse de nuevo después de un retraso
             vTaskDelay(pdMS_TO_TICKS(5000));
              led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
             esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
             break;
        default:
            ESP_LOGI(TAG, "ZDO signal: %s (0x%x), status: %s (0x%x)", esp_zb_zdo_signal_to_string(sig_type), sig_type,
                     esp_err_to_name(err_status), err_status);
            break;
    }
}


// --- Tarea principal de Zigbee (Sin cambios en la lógica interna, solo usa el handler actualizado) ---
static void esp_zb_task(void *pvParameters)
{
    // ... (Configuración de Zigbee, creación de clusters (incluido el custom), etc. sin cambios) ...
    ESP_LOGI(TAG, "Iniciando la tarea esp_zb_task...");
    esp_zb_cfg_t zb_cfg;
    memset(&zb_cfg, 0, sizeof(esp_zb_cfg_t));
    //Prepara la estructuras o bloque de memoria antes de su uso. (por ejemplo, a cero para limpiar memoria)
    zb_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ROUTER; //Aqui se elige como Router o como coordinador
    zb_cfg.nwk_cfg.zczr_cfg.max_children = ZIGBEE_MAX_CHILDREN;//la linea indica que despues de conectarse al Coordinador otros se pueden unir a el tambiem
    zb_cfg.install_code_policy = false;//Codigo especial para unirse a un equipo de forma segura, talvez mas adelante.
    ESP_LOGI(TAG, "Rol Zigbee establecido como ROUTER (max_children=%d)", ZIGBEE_MAX_CHILDREN);

    esp_zb_init(&zb_cfg);//Inicia zigbbe despentando y preparando el dispositivo
    ESP_LOGI(TAG, "Stack Zigbee inicializado.");

    esp_zb_cluster_list_t *esp_zb_cluster_list = esp_zb_zcl_cluster_list_create();
    ESP_RETURN_ON_FALSE(esp_zb_cluster_list, , TAG, "¡Fallo al crear lista de clusters!");

    esp_zb_basic_cluster_cfg_t basic_cluster_cfg = {
        .zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE,
        .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE };

    esp_zb_attribute_list_t *esp_zb_basic_cluster = esp_zb_basic_cluster_create(&basic_cluster_cfg);
    ESP_RETURN_ON_FALSE(esp_zb_basic_cluster, , TAG, "¡Fallo al crear clúster Basic!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(esp_zb_cluster_list, esp_zb_basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    esp_zb_identify_cluster_cfg_t identify_cluster_cfg = { 
        .identify_time = 0 };

    esp_zb_attribute_list_t *esp_zb_identify_cluster = esp_zb_identify_cluster_create(&identify_cluster_cfg);
    ESP_RETURN_ON_FALSE(esp_zb_identify_cluster, , TAG, "¡Fallo al crear clúster Identify!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(esp_zb_cluster_list, esp_zb_identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    esp_zb_custom_cluster_cfg_t custom_cluster_cfg = {
        .current_sensor_1 = 0.0f,
        .current_sensor_2 = 0.0f,
        .current_sensor_3 = 0.0f
    };
    esp_zb_attribute_list_t *esp_zb_custom_cluster = esp_zb_custom_cluster_create(&custom_cluster_cfg);
    ESP_RETURN_ON_FALSE(esp_zb_custom_cluster, , TAG, "¡Fallo al crear clúster Custom!");
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_custom_cluster(esp_zb_cluster_list, esp_zb_custom_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));
    ESP_LOGI(TAG, "Clústeres Basic, Identify y Custom creados y añadidos.");

    esp_zb_ep_list_t *esp_zb_ep_list = esp_zb_ep_list_create();
    ESP_RETURN_ON_FALSE(esp_zb_ep_list, , TAG, "¡Fallo al crear lista de endpoints!");

    esp_zb_endpoint_config_t ep_config = {
        .endpoint = ZIGBEE_ENDPOINT,
        .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id = ESP_ZB_HA_SIMPLE_SENSOR_DEVICE_ID,
        .app_device_version = 0
    };
    ESP_LOGI(TAG, "Configuración de Endpoint: ID=%d, Profile=%04X, DeviceID=%04X",
             ep_config.endpoint, ep_config.app_profile_id, ep_config.app_device_id);

    esp_err_t add_ep_status = esp_zb_ep_list_add_ep(esp_zb_ep_list, esp_zb_cluster_list, ep_config);
    if (add_ep_status != ESP_OK) {
        ESP_LOGE(TAG, "¡Fallo al añadir endpoint a la lista! Error: %s", esp_err_to_name(add_ep_status));
        if (esp_zb_cluster_list) { free(esp_zb_cluster_list); } // Aproximación simple de limpieza
        if (esp_zb_ep_list) { free(esp_zb_ep_list); }
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "Endpoint añadido exitosamente.");

    ESP_ERROR_CHECK(esp_zb_device_register(esp_zb_ep_list));
    ESP_LOGI(TAG, "Dispositivo Zigbee (Endpoint y Clusters) registrado.");

    ESP_LOGI(TAG, "Arrancando el stack Zigbee y buscando red...");
    ESP_ERROR_CHECK(esp_zb_start(true));

    ESP_LOGI(TAG, "Stack Zigbee arrancado. Entrando en el bucle principal.");
    esp_zb_stack_main_loop();
}

// --- Tarea del Sensor (Sin cambios en la lógica interna, solo usa los canales correctos) ---
static void sensor_update_task(void *pvParameters) {
    // ... (Código sin cambios respecto a la versión anterior con los canales ADC corregidos) ...
     ESP_LOGI(TAG, "Iniciando sensor_update_task. Esperando a que la red Zigbee esté lista...");

    if (xSemaphoreTake(xZigbeeNetworkReadySemaphore, portMAX_DELAY) == pdTRUE) {
        ESP_LOGI(TAG, "¡Red Zigbee lista! Iniciando lecturas ADC y actualizaciones de corriente.");

        while (1) {
            int voltage_mv_1 = read_adc_voltage_mv(ADC_CHANNEL_1);
            int voltage_mv_2 = read_adc_voltage_mv(ADC_CHANNEL_2);
            int voltage_mv_3 = read_adc_voltage_mv(ADC_CHANNEL_3);

            float current_a_1 = 0.0f;
            float current_a_2 = 0.0f;
            float current_a_3 = 0.0f;

             if (voltage_mv_1 >= 0) {
                 current_a_1 = ((float)voltage_mv_1 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A;
                 ESP_LOGD(TAG, "Sensor 1 (GPIO1): Voltage=%d mV, Current=%.3f A", voltage_mv_1, current_a_1);
             } else {
                 ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 1 (GPIO1)");
             }

             if (voltage_mv_2 >= 0) {
                 current_a_2 = ((float)voltage_mv_2 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A;
                 ESP_LOGD(TAG, "Sensor 2 (GPIO2): Voltage=%d mV, Current=%.3f A", voltage_mv_2, current_a_2);
             } else {
                 ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 2 (GPIO2)");
             }

             if (voltage_mv_3 >= 0) {
                 current_a_3 = ((float)voltage_mv_3 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A;
                 ESP_LOGD(TAG, "Sensor 3 (GPIO3): Voltage=%d mV, Current=%.3f A", voltage_mv_3, current_a_3);
             } else {
                 ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 3 (GPIO3)");
             }

             update_sensor_currents(current_a_1, current_a_2, current_a_3);

             vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
        }

    } else {
         ESP_LOGE(TAG, "¡Timeout esperando el semáforo de red lista!");
         led_set_state(LED_STATE_ERROR_BLINK); // Rojo si no se conecta
    }

    ESP_LOGW(TAG, "sensor_update_task terminando.");
    led_set_state(LED_STATE_OFF);
    vTaskDelete(NULL);
}

// --- Inicialización Plataforma (Sin cambios) ---
void zigbee_platform_init()
{
    // ... (implementación sin cambios) ...
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
// <--- CAMBIO: Inicializa LED RGB y estado inicial amarillo
void app_main(void)
{
    ESP_LOGI(TAG, "--- Iniciando Router Zigbee con 3 Sensores de Corriente (LED RGB Pin %d) ---", RGB_LED_GPIO);

    xZigbeeNetworkReadySemaphore = xSemaphoreCreateBinary();
    if (xZigbeeNetworkReadySemaphore == NULL) {
        ESP_LOGE(TAG, "¡Fallo al crear el semáforo!");
        return;
    }

    led_init(); // <-- Inicializar LED RGB
    led_set_state(LED_STATE_INIT_BLINK); // <-- Estado inicial: Amarillo parpadeante

    zigbee_platform_init();
    adc_init();

    ESP_LOGI(TAG, "Creando la tarea esp_zb_task...");
    xTaskCreate(esp_zb_task, "zigbee_task", 4096 * 2, NULL, 5, NULL);

    ESP_LOGI(TAG, "Creando la tarea sensor_update_task...");
    xTaskCreate(sensor_update_task, "sensor_task", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "app_main: Inicialización completada. Tareas creadas.");
}