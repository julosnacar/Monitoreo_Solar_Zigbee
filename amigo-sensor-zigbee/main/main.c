#include <stdio.h>
#include <inttypes.h>
#include <string.h>
#include <math.h> // Para NAN
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include <nvs_flash.h>
#include <esp_log.h>
#include <esp_check.h>
#include <main.h> 
#include "driver/gpio.h"
#include "freertos/timers.h"
#include "led_strip.h" //LED RGB
#include "sdkconfig.h"

// =========================================================================
// ======================= INICIO DE CAMBIOS ADC ===========================
// =========================================================================
//INCLUDES PARA ADC Y CALIBRACIÓN
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
// =========================================================================
// ======================== FIN DE CAMBIOS ADC =============================
// =========================================================================

#define TAG "ZIGBEE_SENSOR_ROUTER"

// --- Configuración ADC (NUEVO API) ---
// Los canales ADC se definen para la configuración de adc_oneshot
#define ADC_INPUT_CHAN0 ADC_CHANNEL_0 // GPIO1 -> ADC1_CH0 
#define ADC_INPUT_CHAN1 ADC_CHANNEL_1 // GPIO2 -> ADC1_CH1
#define ADC_INPUT_CHAN2 ADC_CHANNEL_2 // GPIO3 -> ADC1_CH2

// Configuración común para los canales ADC
static const adc_atten_t      g_adc_atten     = ADC_ATTEN_DB_12; // Atenuación (0, 2.5, 6, 12 dB)
static const adc_bitwidth_t   g_adc_bitwidth  = ADC_BITWIDTH_12; // Resolución (o ADC_BITWIDTH_DEFAULT)

// Handles para el driver ADC oneshot y calibración
static adc_oneshot_unit_handle_t g_adc1_handle;
static adc_cali_handle_t g_adc1_cali_handle = NULL; // Un solo handle si la config (atten, bitwidth) es la misma para todos los canales en ADC1
static bool g_adc1_calibrated = false;

// --- Configuración Sensor Corriente HSTS016L ---
#define SENSOR_ZERO_CURRENT_VOLTAGE_MV 1650.0f
#define SENSOR_SENSITIVITY_MV_PER_A    250.0f

// --- Configuración Zigbee ---
#define ZIGBEE_ENDPOINT 1
#define ZIGBEE_MAX_CHILDREN 10
#define SEND_INTERVAL_MS 10000
#define ZIGBEE_REJOIN_DELAY_MS 5000

// --- Configuración Cluster Personalizado ---
#define ZIGBEE_CUSTOM_CLUSTER_ID        0xFC01
#define ATTR_ID_CURRENT_SENSOR_1        0x0001
#define ATTR_ID_CURRENT_SENSOR_2        0x0002
#define ATTR_ID_CURRENT_SENSOR_3        0x0003

// --- Configuración LED RGB ---
#define RGB_LED_GPIO 8
#define BLINK_PERIOD_MS 1000

typedef enum {
    LED_STATE_OFF = 0, LED_STATE_INIT_BLINK, LED_STATE_SEARCHING_BLINK,
    LED_STATE_JOINING_BLINK, LED_STATE_CONNECTED_BLINK, LED_STATE_ERROR_BLINK,
} led_state_t;
typedef struct { uint8_t r; uint8_t g; uint8_t b; } rgb_color_t;
const rgb_color_t COLOR_OFF = {0,0,0}, COLOR_YELLOW = {16,16,0}, COLOR_BLUE = {0,0,16},
                  COLOR_ORANGE = {30,10,0}, COLOR_GREEN = {16,0,0}, COLOR_RED = {0,16,0};
static led_strip_handle_t g_led_strip = NULL;
static volatile led_state_t g_led_state = LED_STATE_OFF;
static TimerHandle_t blink_timer = NULL;
static bool g_led_physical_state_on = false;
static rgb_color_t g_current_color = {0,0,0};

static SemaphoreHandle_t xZigbeeNetworkReadySemaphore = NULL;
static volatile bool g_is_rejoining = false;

// --- Funciones LED (sin cambios significativos) ---
static void led_init() {
    ESP_LOGI(TAG, "Configurando LED RGB en GPIO%d", RGB_LED_GPIO);
    led_strip_config_t strip_config = { .strip_gpio_num = RGB_LED_GPIO, .max_leds = 1, .led_pixel_format = LED_PIXEL_FORMAT_GRB, .led_model = LED_MODEL_WS2812, .flags.invert_out = false, };
    led_strip_rmt_config_t rmt_config = { .clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10 * 1000 * 1000, .flags.with_dma = false, };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &g_led_strip));
    if (g_led_strip) { led_strip_clear(g_led_strip); ESP_LOGI(TAG, "LED RGB inicializado."); } else { ESP_LOGE(TAG, "Fallo al inicializar LED RGB!"); }
}
static void led_set_rgb(uint8_t r, uint8_t g, uint8_t b) {
    if (g_led_strip) {
        g_led_physical_state_on = (r > 0 || g > 0 || b > 0);
        esp_err_t err = led_strip_set_pixel(g_led_strip, 0, r, g, b); if (err != ESP_OK) ESP_LOGE(TAG, "Error led_strip_set_pixel: %s", esp_err_to_name(err));
        err = led_strip_refresh(g_led_strip); if (err != ESP_OK) ESP_LOGE(TAG, "Error led_strip_refresh: %s", esp_err_to_name(err));
    }
}
static void led_off() {
    if (g_led_strip) {
        g_led_physical_state_on = false;
        esp_err_t err = led_strip_clear(g_led_strip); if (err != ESP_OK) ESP_LOGE(TAG, "Error led_strip_clear: %s", esp_err_to_name(err));
        err = led_strip_refresh(g_led_strip); if (err != ESP_OK) ESP_LOGE(TAG, "Error led_strip_refresh after clear: %s", esp_err_to_name(err));
    }
}
static void blink_timer_callback(TimerHandle_t xTimer) {
    led_state_t current_state = g_led_state;
    if (current_state >= LED_STATE_INIT_BLINK && current_state <= LED_STATE_ERROR_BLINK) {
        if (g_led_physical_state_on) led_off(); else led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
    }
}
static void led_set_state(led_state_t new_state) {
    if (g_led_state == new_state) return;
    ESP_LOGI(TAG, "Cambiando estado del LED de %d a %d", g_led_state, new_state);
    if (blink_timer && xTimerIsTimerActive(blink_timer)) { xTimerStop(blink_timer, 0); ESP_LOGI(TAG, "Timer de parpadeo detenido."); }
    led_off(); g_led_state = new_state;
    switch (new_state) {
        case LED_STATE_INIT_BLINK:      g_current_color = COLOR_YELLOW; break; case LED_STATE_SEARCHING_BLINK: g_current_color = COLOR_BLUE;   break;
        case LED_STATE_JOINING_BLINK:   g_current_color = COLOR_ORANGE; break; case LED_STATE_CONNECTED_BLINK: g_current_color = COLOR_GREEN;  break;
        case LED_STATE_ERROR_BLINK:     g_current_color = COLOR_RED;    break; case LED_STATE_OFF: default:    g_current_color = COLOR_OFF;    break;
    }
    if (new_state >= LED_STATE_INIT_BLINK && new_state <= LED_STATE_ERROR_BLINK) {
        if (blink_timer == NULL) {
            blink_timer = xTimerCreate("BlinkTimer", pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), pdTRUE, (void *)0, blink_timer_callback);
            if (!blink_timer) { ESP_LOGE(TAG, "Fallo al crear timer!"); g_led_state = LED_STATE_OFF; return; }
            ESP_LOGI(TAG, "Timer de parpadeo creado.");
        }
        xTimerChangePeriod(blink_timer, pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), 0); led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
        if (xTimerStart(blink_timer, 0) != pdPASS) { ESP_LOGE(TAG, "Fallo al iniciar timer!"); led_off(); g_led_state = LED_STATE_OFF; }
        else { ESP_LOGI(TAG, "Timer de parpadeo iniciado para estado %d.", new_state); }
    } else { led_off(); ESP_LOGI(TAG, "LED apagado para estado %d.", new_state); }
}

// =========================================================================
// ======================= INICIO DE CAMBIOS ADC ===========================
// =========================================================================
// --- Funciones ADC (NUEVO API) ---

// Función auxiliar para intentar calibrar ADC1 con una configuración dada
static bool adc_calibration_init_scheme(adc_unit_t unit, adc_atten_t atten, adc_cali_handle_t *out_handle)
{
    adc_cali_handle_t handle = NULL;
    esp_err_t ret = ESP_FAIL;
    bool calibrated = false;

    ESP_LOGI(TAG, "Intentando calibración para ADC Unit %d, Atten %d", unit, atten);

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    ESP_LOGI(TAG, "Intentando calibración por Curve Fitting...");
    adc_cali_curve_fitting_config_t cali_config_curve = {
        .unit_id = unit,
        .atten = atten,
        .bitwidth = g_adc_bitwidth, // Usar la variable global de bitwidth
    };
    ret = adc_cali_create_scheme_curve_fitting(&cali_config_curve, &handle);
    if (ret == ESP_OK) {
        calibrated = true;
    }
#endif

#if ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    if (!calibrated) {
        ESP_LOGI(TAG, "Intentando calibración Lineal...");
        adc_cali_line_fitting_config_t cali_config_line = {
            .unit_id = unit,
            .atten = atten,
            .bitwidth = g_adc_bitwidth, // Usar la variable global de bitwidth
        };
        ret = adc_cali_create_scheme_line_fitting(&cali_config_line, &handle);
        if (ret == ESP_OK) {
            calibrated = true;
        }
    }
#endif

    *out_handle = handle;
    if (calibrated) {
        ESP_LOGI(TAG, "Calibración para ADC Unit %d, Atten %d (%s) inicializada.", unit, atten, (ret == ESP_OK && handle) ? "EXITOSA" : "FALLIDA O NO SOPORTADA");
    } else {
        ESP_LOGW(TAG, "Calibración para ADC Unit %d, Atten %d NO disponible/falló.", unit, atten);
        if (handle) { // Si se creó un handle pero la calibración no se marcó como exitosa, liberar
            // Esto no debería pasar si ret != ESP_OK
            // adc_cali_delete_scheme_...(handle); // La función de delete depende del scheme
        }
    }
    return calibrated;
}

static void adc_init_new() { // Renombrada para evitar confusión con la antigua 'adc_init'
    ESP_LOGI(TAG, "Inicializando ADC1 (nuevo API)...");

    //-------------ADC1 Init---------------//
    adc_oneshot_unit_init_cfg_t init_config1 = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_config1, &g_adc1_handle));

    //-------------ADC1 Channel Config---------------//
    adc_oneshot_chan_cfg_t channel_config = {
        .bitwidth = g_adc_bitwidth,
        .atten = g_adc_atten,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(g_adc1_handle, ADC_INPUT_CHAN0, &channel_config));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(g_adc1_handle, ADC_INPUT_CHAN1, &channel_config));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(g_adc1_handle, ADC_INPUT_CHAN2, &channel_config));
    ESP_LOGI(TAG, "Canales ADC1 configurados con atenuación %d y %d bits.", g_adc_atten, g_adc_bitwidth);

    //-------------ADC1 Calibration---------------//
    // Intentar calibrar ADC1 para la atenuación y bitwidth configurados.
    // Si todos los canales usan la misma configuración, un solo handle de calibración es suficiente.
    g_adc1_calibrated = adc_calibration_init_scheme(ADC_UNIT_1, g_adc_atten, &g_adc1_cali_handle);
}

static int read_adc_voltage_mv_new(adc_channel_t channel) { // Renombrada
    int adc_raw_val;
    int voltage_mv_val;

    esp_err_t ret = adc_oneshot_read(g_adc1_handle, channel, &adc_raw_val);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Error adc_oneshot_read para canal %d: %s", channel, esp_err_to_name(ret));
        return -1; // Error de lectura
    }

    if (g_adc1_calibrated && g_adc1_cali_handle) {
        ret = adc_cali_raw_to_voltage(g_adc1_cali_handle, adc_raw_val, &voltage_mv_val);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "Error adc_cali_raw_to_voltage para canal %d (raw %d): %s.", channel, adc_raw_val, esp_err_to_name(ret));
            // Fallback: no hay buena forma de calcular sin calibración o Vref conocido
            // Devolver un valor que indique fallo de conversión
            return -2; 
        }
        // ESP_LOGD(TAG, "Canal %d: Raw=%d -> Calibrated Voltage=%d mV", channel, adc_raw_val, voltage_mv_val);
    } else {
        ESP_LOGW(TAG, "ADC para canal %d no calibrado o handle inválido. Devolviendo valor raw %d (¡IMPRECISO!).", channel, adc_raw_val);
        // Si no hay calibración, NO hay forma fiable de convertir a mV sin conocer el Vref exacto
        // y la linealidad. Podrías devolver el valor raw o un código de error.
        // Devolver raw puede ser confuso. Devolver un error es más seguro.
        return -3; // Indica que no hay calibración
    }
    return voltage_mv_val;
}
// =========================================================================
// ======================== FIN DE CAMBIOS ADC =============================
// =========================================================================

// --- Funciones Zigbee (Cluster Personalizado, Actualización) ---
// (Sin cambios aquí)
typedef struct {
    float current_sensor_1;
    float current_sensor_2;
    float current_sensor_3;
} esp_zb_custom_cluster_cfg_t;

esp_zb_attribute_list_t* esp_zb_custom_cluster_create(esp_zb_custom_cluster_cfg_t *cfg) {
    esp_zb_attribute_list_t *attr_list = esp_zb_zcl_attr_list_create(ZIGBEE_CUSTOM_CLUSTER_ID);
    if (!attr_list) { ESP_LOGE(TAG, "Fallo al crear lista atributos custom"); return NULL; }
    esp_err_t err;
    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_1, ESP_ZB_ZCL_ATTR_TYPE_SINGLE, ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_1));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo añadir S1");
    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_2, ESP_ZB_ZCL_ATTR_TYPE_SINGLE, ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_2));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo añadir S2");
    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_3, ESP_ZB_ZCL_ATTR_TYPE_SINGLE, ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_3));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo añadir S3");
    ESP_LOGI(TAG, "Cluster custom (ID: 0x%04X) creado.", ZIGBEE_CUSTOM_CLUSTER_ID);
    return attr_list;
}

static void update_sensor_currents(float current_1, float current_2, float current_3) {
    if (esp_zb_lock_acquire(portMAX_DELAY)) {
        esp_zb_zcl_status_t s1,s2,s3;
        s1 = esp_zb_zcl_set_attribute_val(ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ATTR_ID_CURRENT_SENSOR_1, &current_1, false);
        s2 = esp_zb_zcl_set_attribute_val(ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ATTR_ID_CURRENT_SENSOR_2, &current_2, false);
        s3 = esp_zb_zcl_set_attribute_val(ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE, ATTR_ID_CURRENT_SENSOR_3, &current_3, false);
        if (s1!=ESP_ZB_ZCL_STATUS_SUCCESS || s2!=ESP_ZB_ZCL_STATUS_SUCCESS || s3!=ESP_ZB_ZCL_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "Error actualizar corrientes: S1=%d,S2=%d,S3=%d",s1,s2,s3);
        } else { printf("Currents Updated: S1=%.2f, S2=%.2f, S3=%.2f A\n", current_1, current_2, current_3); }
        esp_zb_lock_release();
    } else { ESP_LOGE(TAG, "No se pudo adquirir lock Zigbee para actualizar corrientes"); }
}

// --- Manejador de Señales Zigbee ---
// (Sin cambios aquí)
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *p_sg_p = signal_struct->p_app_signal; esp_err_t err_status = signal_struct->esp_err_status; esp_zb_app_signal_type_t sig_type = *p_sg_p;
    if (g_is_rejoining && (sig_type == ESP_ZB_BDB_SIGNAL_STEERING_CANCELLED || sig_type == ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT || sig_type == ESP_ZB_ZDO_SIGNAL_LEAVE)) {
        ESP_LOGW(TAG, "Reintento en progreso, ignorando señal %s (0x%x)", esp_zb_zdo_signal_to_string(sig_type), sig_type); return;
    }
    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP: ESP_LOGI(TAG, "Stack Zigbee init, iniciando Network Steering..."); g_is_rejoining = false; led_set_state(LED_STATE_SEARCHING_BLINK); esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); break;
        case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START: case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
            led_set_state(LED_STATE_JOINING_BLINK);
            if (err_status == ESP_OK) { ESP_LOGI(TAG, "Dispositivo en red OK."); led_set_state(LED_STATE_CONNECTED_BLINK); g_is_rejoining = false; if (xSemaphoreGive(xZigbeeNetworkReadySemaphore) != pdTRUE) ESP_LOGD(TAG, "Semáforo ya tomado/no dado (normal en reinicio)."); else ESP_LOGI(TAG, "Semáforo dado: sensor_update_task puede iniciar."); }
            else { ESP_LOGE(TAG, "Fallo al establecer red: %s (0x%x)", esp_err_to_name(err_status), err_status); led_set_state(LED_STATE_ERROR_BLINK); g_is_rejoining = true; vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS)); led_set_state(LED_STATE_SEARCHING_BLINK); esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); g_is_rejoining = false; }
            break;
        case ESP_ZB_BDB_SIGNAL_STEERING:
             if (err_status == ESP_OK) { ESP_LOGI(TAG, "Network steering OK."); led_set_state(LED_STATE_CONNECTED_BLINK); g_is_rejoining = false; }
             else { ESP_LOGW(TAG, "Network steering falló/cancelado."); if (g_led_state != LED_STATE_ERROR_BLINK && g_led_state != LED_STATE_SEARCHING_BLINK) { led_set_state(LED_STATE_ERROR_BLINK); g_is_rejoining = true; vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS)); led_set_state(LED_STATE_SEARCHING_BLINK); esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); g_is_rejoining = false; } else ESP_LOGI(TAG,"Ya en error/búsqueda, reintento probablemente iniciado."); }
            break;
        case ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT:
            ESP_LOGW(TAG, "Señal 0x18: No enlaces activos."); if (g_led_state == LED_STATE_CONNECTED_BLINK) { led_set_state(LED_STATE_ERROR_BLINK); g_is_rejoining = true; vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS)); led_set_state(LED_STATE_SEARCHING_BLINK); esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); g_is_rejoining = false; } else ESP_LOGI(TAG, "Señal 0x18 recibida, pero no conectado. Ignorando.");
            break;
        case ESP_ZB_ZDO_SIGNAL_LEAVE: {
            esp_zb_zdo_signal_leave_params_t *leave_params = (esp_zb_zdo_signal_leave_params_t *)esp_zb_app_signal_get_params(p_sg_p); ESP_LOGW(TAG, "Dispositivo abandonó red (razón: %u)", leave_params->leave_type); led_set_state(LED_STATE_ERROR_BLINK); g_is_rejoining = true; vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS)); led_set_state(LED_STATE_SEARCHING_BLINK); esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING); g_is_rejoining = false; break;
        }
        default: ESP_LOGD(TAG, "Señal ZDO no manejada: %s (0x%x), status: %s (0x%x)", esp_zb_zdo_signal_to_string(sig_type), sig_type, esp_err_to_name(err_status), err_status); break;
    }
}

// --- Tarea principal de Zigbee ---
// (Sin cambios aquí)
static void esp_zb_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando esp_zb_task...");
    esp_zb_cfg_t zb_cfg; memset(&zb_cfg, 0, sizeof(esp_zb_cfg_t));
    zb_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ROUTER; zb_cfg.nwk_cfg.zczr_cfg.max_children = ZIGBEE_MAX_CHILDREN; zb_cfg.install_code_policy = false;
    ESP_LOGI(TAG, "Rol Zigbee: ROUTER (max_children=%d)", ZIGBEE_MAX_CHILDREN);
    esp_zb_init(&zb_cfg); ESP_LOGI(TAG, "Stack Zigbee inicializado.");
    // esp_zb_factory_reset(); // Descomentar para desarrollo si es necesario

    esp_zb_cluster_list_t *cluster_list = esp_zb_zcl_cluster_list_create();
    esp_zb_basic_cluster_cfg_t basic_cfg = {.zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE, .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE};
    esp_zb_attribute_list_t *basic_cluster = esp_zb_basic_cluster_create(&basic_cfg);
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(cluster_list, basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));
    esp_zb_identify_cluster_cfg_t identify_cfg = {.identify_time = 0};
    esp_zb_attribute_list_t *identify_cluster = esp_zb_identify_cluster_create(&identify_cfg);
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(cluster_list, identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));
    esp_zb_custom_cluster_cfg_t custom_cfg = {.current_sensor_1 = NAN, .current_sensor_2 = NAN, .current_sensor_3 = NAN};
    esp_zb_attribute_list_t *custom_cluster = esp_zb_custom_cluster_create(&custom_cfg);
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_custom_cluster(cluster_list, custom_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));
    ESP_LOGI(TAG, "Clusters Basic, Identify y Custom creados.");

    esp_zb_ep_list_t *ep_list = esp_zb_ep_list_create();
    esp_zb_endpoint_config_t ep_config = {.endpoint = ZIGBEE_ENDPOINT, .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID, .app_device_id = ESP_ZB_HA_SIMPLE_SENSOR_DEVICE_ID, .app_device_version = 0};
    ESP_ERROR_CHECK(esp_zb_ep_list_add_ep(ep_list, cluster_list, ep_config)); ESP_LOGI(TAG, "Endpoint añadido.");
    ESP_ERROR_CHECK(esp_zb_device_register(ep_list)); ESP_LOGI(TAG, "Dispositivo Zigbee registrado.");
    led_set_state(LED_STATE_INIT_BLINK);
    ESP_ERROR_CHECK(esp_zb_start(true)); ESP_LOGI(TAG, "Stack Zigbee arrancado. Entrando en bucle.");
    esp_zb_stack_main_loop();
    ESP_LOGW(TAG, "Saliendo de esp_zb_task (no debería ocurrir)."); vTaskDelete(NULL);
}

// --- Tarea del Sensor ---
static void sensor_update_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando sensor_update_task. Esperando red Zigbee...");
    if (xSemaphoreTake(xZigbeeNetworkReadySemaphore, portMAX_DELAY) == pdTRUE) {
        ESP_LOGI(TAG, "Red Zigbee lista! Iniciando lecturas ADC.");
        while (1) {
            // =========================================================================
            // ======================= CAMBIOS ADC ===========================
            // =========================================================================
            // Usar las nuevas funciones de lectura ADC
            int voltage_mv_1 = read_adc_voltage_mv_new(ADC_INPUT_CHAN0);
            int voltage_mv_2 = read_adc_voltage_mv_new(ADC_INPUT_CHAN1);
            int voltage_mv_3 = read_adc_voltage_mv_new(ADC_INPUT_CHAN2);
            // =========================================================================
            // ======================== CAMBIOS ADC =============================
            // =========================================================================

            float current_a_1 = (voltage_mv_1 >= 0) ? (((float)voltage_mv_1 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f;
            float current_a_2 = (voltage_mv_2 >= 0) ? (((float)voltage_mv_2 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f;
            float current_a_3 = (voltage_mv_3 >= 0) ? (((float)voltage_mv_3 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f;

            if (voltage_mv_1 < 0) ESP_LOGW(TAG, "Lectura ADC inválida/error Sensor 1 (código %d)", voltage_mv_1);
            if (voltage_mv_2 < 0) ESP_LOGW(TAG, "Lectura ADC inválida/error Sensor 2 (código %d)", voltage_mv_2);
            if (voltage_mv_3 < 0) ESP_LOGW(TAG, "Lectura ADC inválida/error Sensor 3 (código %d)", voltage_mv_3);

            update_sensor_currents(current_a_1, current_a_2, current_a_3);
            vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
        }
    } else {
        ESP_LOGE(TAG, "Timeout esperando semáforo red! Tarea sensor no iniciará.");
    }
    ESP_LOGW(TAG, "sensor_update_task terminando (no debería ocurrir).");
    vTaskDelete(NULL);
}

// --- Inicialización Plataforma ---
// (Sin cambios aquí)
void zigbee_platform_init() {
    esp_zb_platform_config_t config = { .radio_config = ESP_ZB_DEFAULT_RADIO_CONFIG(), .host_config = ESP_ZB_DEFAULT_HOST_CONFIG(), };
    ESP_LOGI(TAG, "1. Inicializando NVS...");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Problema NVS, borrando y reintentando...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret); ESP_LOGI(TAG, "NVS inicializado.");
    ESP_LOGI(TAG, "2. Configurando plataforma Zigbee...");
    ESP_ERROR_CHECK(esp_zb_platform_config(&config)); ESP_LOGI(TAG, "Plataforma Zigbee configurada.");
}

// --- Función Principal ---
void app_main(void) {
    ESP_LOGI(TAG, "--- Iniciando Router Zigbee con 3 Sensores de Corriente (LED RGB Pin %d) ---", RGB_LED_GPIO);

    xZigbeeNetworkReadySemaphore = xSemaphoreCreateBinary();
    if (xZigbeeNetworkReadySemaphore == NULL) { ESP_LOGE(TAG, "Fallo crear semáforo!"); return; }
    ESP_LOGI(TAG, "Semáforo creado.");

    led_init();
    adc_init_new(); // Inicializar ADC1 con la nueva API

    zigbee_platform_init();

    ESP_LOGI(TAG, "Creando tarea esp_zb_task...");
    xTaskCreate(esp_zb_task, "zigbee_task", 4096 * 2, NULL, 5, NULL);

    ESP_LOGI(TAG, "Creando tarea sensor_update_task...");
    xTaskCreate(sensor_update_task, "sensor_task", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "app_main: Inicialización completada.");
}