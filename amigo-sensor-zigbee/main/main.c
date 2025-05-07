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
#define ZIGBEE_REJOIN_DELAY_MS 5000 // Tiempo de espera antes de intentar reunirse

// --- Configuración Cluster Personalizado ---
#define ZIGBEE_CUSTOM_CLUSTER_ID        0xFC01
#define ATTR_ID_CURRENT_SENSOR_1        0x0001
#define ATTR_ID_CURRENT_SENSOR_2        0x0002
#define ATTR_ID_CURRENT_SENSOR_3        0x0003

// --- Configuración LED RGB ---
#define RGB_LED_GPIO 8 //CONFIG_BLINK_GPIO
#define BLINK_PERIOD_MS 1000 // Periodo de parpadeo total (500ms ON, 500ms OFF)

//Enum para los estados del LED RGB
typedef enum {
    LED_STATE_OFF = 0,            // Estado inicial o apagado explícito
    LED_STATE_INIT_BLINK,         // Amarillo parpadeante (Stack iniciado, antes de buscar)
    LED_STATE_SEARCHING_BLINK,    // Azul tenue parpadeante (Buscando red)
    LED_STATE_JOINING_BLINK,      // Naranja parpadeante (Intentando unirse/autenticarse)
    LED_STATE_CONNECTED_BLINK,    // Verde parpadeante (Conectado y operacional)
    LED_STATE_ERROR_BLINK,        // Rojo parpadeante (Error de conexión / Dejado la red / Sin enlace activo)
} led_state_t;

typedef struct {
    uint8_t r;
    uint8_t g;
    uint8_t b;
} rgb_color_t;

//Definición de colores para cada estado (valores bajos para no saturar)
const rgb_color_t COLOR_OFF      = {0, 0, 0};
const rgb_color_t COLOR_YELLOW   = {16, 16, 0}; // Amarillo
const rgb_color_t COLOR_BLUE     = {0, 0, 16};  // Azul tenue
const rgb_color_t COLOR_ORANGE   = {30, 10, 0}; // Naranja (Ajustado para diferenciarse de amarillo/rojo)
const rgb_color_t COLOR_GREEN    = {16, 0, 0};  // Verde
const rgb_color_t COLOR_RED      = {0, 16, 0};  // Rojo

static led_strip_handle_t g_led_strip = NULL; //Handle para el LED strip
static volatile led_state_t g_led_state = LED_STATE_OFF; // Volatile puede ser útil si se accede desde ISR, aunque aquí no es el caso.
static TimerHandle_t blink_timer = NULL;
static bool g_led_physical_state_on = false;
static rgb_color_t g_current_color = {0, 0, 0};

// --- Variables Globales ---
static SemaphoreHandle_t xZigbeeNetworkReadySemaphore = NULL;
// Flag para evitar reintentos múltiples muy rápidos
static volatile bool g_is_rejoining = false;

// --- Funciones LED RGB ---
static void led_init() {
    ESP_LOGI(TAG, "Configurando LED RGB en GPIO%d", RGB_LED_GPIO);
    led_strip_config_t strip_config = {
        .strip_gpio_num = RGB_LED_GPIO,
        .max_leds = 1,
        // --- NUEVO: Configuración de backend ---
        .led_pixel_format = LED_PIXEL_FORMAT_GRB, // Ajusta según tu LED (GRB es común)
        .led_model = LED_MODEL_WS2812,          // Ajusta según tu LED
        .flags.invert_out = false,              // Usualmente false
    };
    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,         // Usa reloj por defecto
        .resolution_hz = 10 * 1000 * 1000,      // 10MHz
        .flags.with_dma = false,                // DMA usualmente no necesario para 1 LED
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &g_led_strip));
    if (g_led_strip) {
        led_strip_clear(g_led_strip);
        ESP_LOGI(TAG, "LED RGB inicializado.");
    } else {
        ESP_LOGE(TAG, "Fallo al inicializar LED RGB!");
    }
}

static void led_set_rgb(uint8_t r, uint8_t g, uint8_t b) {
    if (g_led_strip) {
        // Asegurarse de que el estado físico se actualice *antes* de cambiar el LED
        g_led_physical_state_on = (r > 0 || g > 0 || b > 0);
        esp_err_t err = led_strip_set_pixel(g_led_strip, 0, r, g, b);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Error led_strip_set_pixel: %s", esp_err_to_name(err));
        }
        err = led_strip_refresh(g_led_strip);
        if (err != ESP_OK) {
             ESP_LOGE(TAG, "Error led_strip_refresh: %s", esp_err_to_name(err));
        }
    }
}

static void led_off() {
    if (g_led_strip) {
        g_led_physical_state_on = false; // Actualizar estado físico
        esp_err_t err = led_strip_clear(g_led_strip);
         if (err != ESP_OK) {
            ESP_LOGE(TAG, "Error led_strip_clear: %s", esp_err_to_name(err));
         }
         // No es necesario refrescar aquí si clear lo hace, pero no hace daño
         err = led_strip_refresh(g_led_strip);
         if (err != ESP_OK) {
             ESP_LOGE(TAG, "Error led_strip_refresh after clear: %s", esp_err_to_name(err));
         }
    }
}

static void blink_timer_callback(TimerHandle_t xTimer) {
    // Leer el estado actual de forma segura
    led_state_t current_state = g_led_state;

    if (current_state == LED_STATE_INIT_BLINK ||
        current_state == LED_STATE_SEARCHING_BLINK ||
        current_state == LED_STATE_JOINING_BLINK ||
        current_state == LED_STATE_CONNECTED_BLINK ||
        current_state == LED_STATE_ERROR_BLINK)
    {
        if (g_led_physical_state_on) {
            led_off();
        } else {
            led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
        }
    }
    // No necesitamos detener el timer aquí, led_set_state se encarga si el estado cambia
}

static void led_set_state(led_state_t new_state) {
    // Solo proceder si el estado realmente cambia
    if (g_led_state == new_state) {
        return;
    }

    ESP_LOGI(TAG, "Cambiando estado del LED de %d a %d", g_led_state, new_state);

    // Detener el timer si estaba activo antes de cambiar el estado
    if (blink_timer && xTimerIsTimerActive(blink_timer)) {
        xTimerStop(blink_timer, 0);
        ESP_LOGI(TAG, "Timer de parpadeo detenido para cambio de estado.");
    }
    // Apagar el LED momentáneamente para una transición limpia
    led_off();
    //vTaskDelay(pdMS_TO_TICKS(50)); // Pausa muy corta

    g_led_state = new_state; // Actualizar el estado global

    // Asignar el color base para el nuevo estado
    switch (new_state) {
        case LED_STATE_INIT_BLINK:      g_current_color = COLOR_YELLOW; break;
        case LED_STATE_SEARCHING_BLINK: g_current_color = COLOR_BLUE;   break;
        case LED_STATE_JOINING_BLINK:   g_current_color = COLOR_ORANGE; break;
        case LED_STATE_CONNECTED_BLINK: g_current_color = COLOR_GREEN;  break;
        case LED_STATE_ERROR_BLINK:     g_current_color = COLOR_RED;    break;
        case LED_STATE_OFF:
        default:                        g_current_color = COLOR_OFF;    break;
    }

    // Si el nuevo estado es de parpadeo, (re)iniciar el timer
    if (new_state >= LED_STATE_INIT_BLINK && new_state <= LED_STATE_ERROR_BLINK) {
        if (blink_timer == NULL) { // Crear el timer si no existe
            blink_timer = xTimerCreate("BlinkTimer", pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), pdTRUE, (void *)0, blink_timer_callback);
            if (!blink_timer) {
                ESP_LOGE(TAG, "Fallo al crear el timer de parpadeo!");
                g_led_state = LED_STATE_OFF; // Volver a OFF si falla
                return;
            }
             ESP_LOGI(TAG, "Timer de parpadeo creado.");
        }
        // Asegurar que el periodo sea el correcto (medio ciclo)
        xTimerChangePeriod(blink_timer, pdMS_TO_TICKS(BLINK_PERIOD_MS / 2), 0);
        // Encender el LED con el nuevo color inmediatamente
        led_set_rgb(g_current_color.r, g_current_color.g, g_current_color.b);
        // Iniciar el timer para que parpadee
        if (xTimerStart(blink_timer, 0) != pdPASS) {
             ESP_LOGE(TAG, "Fallo al iniciar el timer de parpadeo!");
             led_off(); // Apagar si no se pudo iniciar el parpadeo
             g_led_state = LED_STATE_OFF;
        } else {
            ESP_LOGI(TAG, "Timer de parpadeo iniciado para estado %d.", new_state);
        }
    } else { // Si el nuevo estado es OFF
        led_off(); // Asegurar que el LED esté apagado
        ESP_LOGI(TAG, "LED apagado para estado %d.", new_state);
    }
}


// --- Funciones ADC ---
static void adc_init() {
    ESP_LOGI(TAG, "Inicializando ADC1...");
    ESP_ERROR_CHECK(adc1_config_width(ADC_WIDTH));
    ESP_LOGI(TAG, "Configurando Sensor 1 (ADC1_CHANNEL_%d -> GPIO1) con atenuación %d", ADC_CHANNEL_1, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_1, ADC_ATTEN));
    ESP_LOGI(TAG, "Configurando Sensor 2 (ADC1_CHANNEL_%d -> GPIO2) con atenuación %d", ADC_CHANNEL_2, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_2, ADC_ATTEN));
    ESP_LOGI(TAG, "Configurando Sensor 3 (ADC1_CHANNEL_%d -> GPIO3) con atenuación %d", ADC_CHANNEL_3, ADC_ATTEN);
    ESP_ERROR_CHECK(adc1_config_channel_atten(ADC_CHANNEL_3, ADC_ATTEN));
    ESP_LOGI(TAG, "ADC1 inicializado y canales configurados.");
}

static int read_adc_voltage_mv(adc1_channel_t channel) {
    int adc_raw = adc1_get_raw(channel);
    if (adc_raw == -1) { // adc1_get_raw devuelve -1 en error
        ESP_LOGE(TAG, "Error al leer ADC del canal %d", channel);
        return -1; // Devuelve -1 para indicar error
    }
    // Conversión a milivoltios
    // Nota: La relación lineal exacta puede depender de la calibración si se usa.
    // Esta es una aproximación común sin calibración específica de eFuse Vref.
    uint32_t voltage_mv = (uint32_t)(((float)adc_raw / (float)((1 << ADC_WIDTH) - 1)) * ADC_VREF_MV);

    //ESP_LOGD(TAG, "Canal ADC %d: Raw=%d -> Voltage=%d mV", channel, adc_raw, voltage_mv);
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
    // Añadir atributos como SINGLE (float)
    esp_err_t err;
    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_1, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                                  ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_1));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo al añadir atributo S1");

    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_2, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                                  ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_2));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo al añadir atributo S2");

    err = esp_zb_cluster_add_attr(attr_list, ZIGBEE_CUSTOM_CLUSTER_ID, ATTR_ID_CURRENT_SENSOR_3, ESP_ZB_ZCL_ATTR_TYPE_SINGLE,
                                  ESP_ZB_ZCL_ATTR_ACCESS_READ_ONLY | ESP_ZB_ZCL_ATTR_ACCESS_REPORTING, &(cfg->current_sensor_3));
    ESP_RETURN_ON_FALSE(err == ESP_OK, NULL, TAG, "Fallo al añadir atributo S3");

    ESP_LOGI(TAG, "Cluster personalizado (ID: 0x%04X) creado con 3 atributos float (Single)", ZIGBEE_CUSTOM_CLUSTER_ID);
    return attr_list;
}

static void update_sensor_currents(float current_1, float current_2, float current_3) {
    if (esp_zb_lock_acquire(portMAX_DELAY)) {
        esp_zb_zcl_status_t state1 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_1, &current_1, false); // false: no chequear acceso (ya es Read Only)
        esp_zb_zcl_status_t state2 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_2, &current_2, false);
        esp_zb_zcl_status_t state3 = esp_zb_zcl_set_attribute_val(
            ZIGBEE_ENDPOINT, ZIGBEE_CUSTOM_CLUSTER_ID, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ATTR_ID_CURRENT_SENSOR_3, &current_3, false);

        if (state1 != ESP_ZB_ZCL_STATUS_SUCCESS || state2 != ESP_ZB_ZCL_STATUS_SUCCESS || state3 != ESP_ZB_ZCL_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "Error al actualizar atributos de corriente: S1=%d, S2=%d, S3=%d", state1, state2, state3);
        } else {
            // ESP_LOGI(TAG, "Zigbee: Atributos Cluster Custom actualizados: S1=%.2f A, S2=%.2f A, S3=%.2f A", current_1, current_2, current_3);
             // Log más conciso para evitar llenar la consola
             printf("Currents Updated: S1=%.2f, S2=%.2f, S3=%.2f A\n", current_1, current_2, current_3);
        }
        esp_zb_lock_release();
    } else {
        ESP_LOGE(TAG, "No se pudo adquirir el lock de Zigbee para actualizar atributos de corriente");
    }
}

// --- Manejador de Señales Zigbee ---
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *p_sg_p       = signal_struct->p_app_signal;
    esp_err_t err_status = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig_type = *p_sg_p;

    // Evitar reintentos concurrentes
    if (g_is_rejoining && (sig_type == ESP_ZB_BDB_SIGNAL_STEERING_CANCELLED ||
                           sig_type == ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT ||
                           sig_type == ESP_ZB_ZDO_SIGNAL_LEAVE)) {
        ESP_LOGW(TAG, "Reintento ya en progreso, ignorando señal %s (0x%x)", esp_zb_zdo_signal_to_string(sig_type), sig_type);
        return;
    }

    switch (sig_type) {
        case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
            ESP_LOGI(TAG, "Stack Zigbee inicializado, iniciando Network Steering...");
            g_is_rejoining = false; // Resetear flag
            led_set_state(LED_STATE_SEARCHING_BLINK); // Azul parpadeante: Buscando red
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            break;

        case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
        case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
            // El dispositivo intenta unirse o reunirse
            led_set_state(LED_STATE_JOINING_BLINK); // Naranja parpadeante: Intentando unirse
            if (err_status == ESP_OK) {
                ESP_LOGI(TAG, "Dispositivo establecido en la red con éxito.");
                led_set_state(LED_STATE_CONNECTED_BLINK); // Verde parpadeante: Conectado
                g_is_rejoining = false; // Conectado, resetear flag
                // Dar semáforo SOLO si la tarea del sensor aún no ha empezado
                // (Para reinicios, podría ya estar corriendo)
                if (xSemaphoreGive(xZigbeeNetworkReadySemaphore) != pdTRUE) {
                    // Esto es normal si el semáforo ya fue tomado
                    ESP_LOGD(TAG, "Semáforo ya tomado o no se pudo dar (puede ser normal en reinicio).");
                } else {
                     ESP_LOGI(TAG, "Semáforo dado: sensor_update_task puede iniciar.");
                }
            } else {
                ESP_LOGE(TAG, "Fallo al establecer la red: %s (0x%x)", esp_err_to_name(err_status), err_status);
                led_set_state(LED_STATE_ERROR_BLINK); // Rojo parpadeante: Error
                g_is_rejoining = true; // Marcar que estamos en proceso de reintento
                vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS));
                led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
                g_is_rejoining = false; // Permitir futuros reintentos después del inicio
            }
            break;

        case ESP_ZB_BDB_SIGNAL_STEERING:
             if (err_status == ESP_OK) {
                ESP_LOGI(TAG, "Network steering completado con éxito.");
                // El estado del LED ya debería ser CONNECTED por la señal DEVICE_REBOOT/FIRST_START
                 led_set_state(LED_STATE_CONNECTED_BLINK); // Asegurar verde
                 g_is_rejoining = false;
            } else {
                ESP_LOGW(TAG, "Network steering falló o fue cancelado.");
                // Si no estamos ya en un estado de error/búsqueda, indicar error y reintentar
                if (g_led_state != LED_STATE_ERROR_BLINK && g_led_state != LED_STATE_SEARCHING_BLINK) {
                    led_set_state(LED_STATE_ERROR_BLINK); // Rojo parpadeante: Error
                    g_is_rejoining = true;
                    vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS));
                    led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
                    esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
                    g_is_rejoining = false;
                } else {
                    ESP_LOGI(TAG,"Ya en estado de error/búsqueda, reintento probablemente ya iniciado.");
                }
            }
            break;

        // *** NUEVO CASO ***
        case ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT:
            ESP_LOGW(TAG, "Señal 0x18: No quedan enlaces activos, probable desconexión del padre.");
            // Solo actuar si estábamos conectados
            if (g_led_state == LED_STATE_CONNECTED_BLINK) {
                led_set_state(LED_STATE_ERROR_BLINK); // Rojo parpadeante
                g_is_rejoining = true;
                vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS)); // Esperar antes de reintentar
                led_set_state(LED_STATE_SEARCHING_BLINK); // Azul parpadeante
                esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
                g_is_rejoining = false;
            } else {
                ESP_LOGI(TAG, "Señal 0x18 recibida, pero no estábamos en estado conectado. Ignorando acción de reintento.");
            }
            break;

        case ESP_ZB_ZDO_SIGNAL_LEAVE:
            // Este caso es cuando el *propio* dispositivo recibe una orden de Leave o decide irse.
            esp_zb_zdo_signal_leave_params_t *leave_params = (esp_zb_zdo_signal_leave_params_t *)esp_zb_app_signal_get_params(p_sg_p);
            ESP_LOGW(TAG, "Dispositivo abandonó la red (razón: %u)", leave_params->leave_type);
            led_set_state(LED_STATE_ERROR_BLINK); // Rojo parpadeante
            g_is_rejoining = true;
            // No dar el semáforo aquí, la tarea del sensor debe detenerse o manejar errores.
            vTaskDelay(pdMS_TO_TICKS(ZIGBEE_REJOIN_DELAY_MS));
            led_set_state(LED_STATE_SEARCHING_BLINK); // Volver a azul antes de reintentar
            esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
            g_is_rejoining = false;
            break;

        // casos relevantes si son necesarios, por ejemplo, para errores específicos.
        // case ESP_ZB_ZDO_SIGNAL_DEVICE_ANNCE:
             // Un nuevo dispositivo se unió A TRAVÉS de este router (si es ZC/ZR)
             // No afecta el estado LED de *este* dispositivo.
             // break;
        // case ESP_ZB_ZDO_SIGNAL_LEAVE_INDICATION:
             // Un dispositivo hijo de este router se fue.
             // No afecta el estado LED de *este* dispositivo.
             // break;

        default:
            ESP_LOGD(TAG, "Señal ZDO no manejada explícitamente: %s (0x%x), status: %s (0x%x)",
                     esp_zb_zdo_signal_to_string(sig_type), sig_type,
                     esp_err_to_name(err_status), err_status);
            // No cambiar el estado del LED para señales no manejadas explícitamente
            break;
    }
}

// --- Tarea principal de Zigbee ---
static void esp_zb_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando la tarea esp_zb_task...");
    esp_zb_cfg_t zb_cfg;
    memset(&zb_cfg, 0, sizeof(esp_zb_cfg_t));
    zb_cfg.esp_zb_role = ESP_ZB_DEVICE_TYPE_ROUTER;
    zb_cfg.nwk_cfg.zczr_cfg.max_children = ZIGBEE_MAX_CHILDREN;
    zb_cfg.install_code_policy = false;
    ESP_LOGI(TAG, "Rol Zigbee establecido como ROUTER (max_children=%d)", ZIGBEE_MAX_CHILDREN);

    esp_zb_init(&zb_cfg);
    ESP_LOGI(TAG, "Stack Zigbee inicializado.");

    esp_zb_cluster_list_t *esp_zb_cluster_list = esp_zb_zcl_cluster_list_create();
    if (!esp_zb_cluster_list) { ESP_LOGE(TAG, "¡Fallo al crear lista de clusters!"); vTaskDelete(NULL); return; }

    esp_zb_basic_cluster_cfg_t basic_cluster_cfg = {
        .zcl_version = ESP_ZB_ZCL_BASIC_ZCL_VERSION_DEFAULT_VALUE,
        .power_source = ESP_ZB_ZCL_BASIC_POWER_SOURCE_DEFAULT_VALUE
    };
    esp_zb_attribute_list_t *esp_zb_basic_cluster = esp_zb_basic_cluster_create(&basic_cluster_cfg);
    if (!esp_zb_basic_cluster) { ESP_LOGE(TAG, "¡Fallo al crear clúster Basic!"); free(esp_zb_cluster_list); vTaskDelete(NULL); return; }
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_basic_cluster(esp_zb_cluster_list, esp_zb_basic_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    esp_zb_identify_cluster_cfg_t identify_cluster_cfg = { .identify_time = 0 };
    esp_zb_attribute_list_t *esp_zb_identify_cluster = esp_zb_identify_cluster_create(&identify_cluster_cfg);
    if (!esp_zb_identify_cluster) { ESP_LOGE(TAG, "¡Fallo al crear clúster Identify!"); /* Limpieza...*/ vTaskDelete(NULL); return; }
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_identify_cluster(esp_zb_cluster_list, esp_zb_identify_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));

    // Clúster personalizado para corrientes
    esp_zb_custom_cluster_cfg_t custom_cluster_cfg = {
        .current_sensor_1 = 0.0f / 0.0f, // Usar NaN como valor inicial "inválido"
        .current_sensor_2 = 0.0f / 0.0f,
        .current_sensor_3 = 0.0f / 0.0f
    };
    esp_zb_attribute_list_t *esp_zb_custom_cluster = esp_zb_custom_cluster_create(&custom_cluster_cfg);
    if (!esp_zb_custom_cluster) { ESP_LOGE(TAG, "¡Fallo al crear clúster Custom!"); /* Limpieza...*/ vTaskDelete(NULL); return; }
    ESP_ERROR_CHECK(esp_zb_cluster_list_add_custom_cluster(esp_zb_cluster_list, esp_zb_custom_cluster, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE));
    ESP_LOGI(TAG, "Clústeres Basic, Identify y Custom creados y añadidos.");

    esp_zb_ep_list_t *esp_zb_ep_list = esp_zb_ep_list_create();
    if (!esp_zb_ep_list) { ESP_LOGE(TAG, "¡Fallo al crear lista de endpoints!"); /* Limpieza...*/ vTaskDelete(NULL); return; }

    esp_zb_endpoint_config_t ep_config = {
        .endpoint = ZIGBEE_ENDPOINT,
        .app_profile_id = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id = ESP_ZB_HA_SIMPLE_SENSOR_DEVICE_ID, // Podría ser otro ID si este no encaja
        .app_device_version = 0
    };
    ESP_LOGI(TAG, "Configuración de Endpoint: ID=%d, Profile=%04X, DeviceID=%04X",
             ep_config.endpoint, ep_config.app_profile_id, ep_config.app_device_id);

    esp_err_t add_ep_status = esp_zb_ep_list_add_ep(esp_zb_ep_list, esp_zb_cluster_list, ep_config);
    if (add_ep_status != ESP_OK) {
        ESP_LOGE(TAG, "¡Fallo al añadir endpoint a la lista! Error: %s", esp_err_to_name(add_ep_status));
        // Limpieza más robusta sería deseable aquí
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "Endpoint añadido exitosamente.");

    ESP_ERROR_CHECK(esp_zb_device_register(esp_zb_ep_list));
    ESP_LOGI(TAG, "Dispositivo Zigbee (Endpoint y Clusters) registrado.");

    // Establecer estado LED a amarillo parpadeante *antes* de llamar a esp_zb_start
    led_set_state(LED_STATE_INIT_BLINK);

    ESP_LOGI(TAG, "Arrancando el stack Zigbee (auto-start)...");
    // Usamos autostart=true, lo que iniciará BDB automáticamente según el estado (factory new o no)
    // La señal SKIP_STARTUP no se generará en este caso.
    // Se generará BDB_SIGNAL_DEVICE_FIRST_START o BDB_SIGNAL_DEVICE_REBOOT.
    ESP_ERROR_CHECK(esp_zb_start(true));

    ESP_LOGI(TAG, "Stack Zigbee arrancado. Entrando en el bucle principal.");
    // El bucle principal maneja eventos y callbacks, incluyendo esp_zb_app_signal_handler
    esp_zb_stack_main_loop();

    // Código después de esp_zb_stack_main_loop() no se alcanzará normalmente
    ESP_LOGW(TAG, "Saliendo de esp_zb_task (esto no debería ocurrir en operación normal).");
    vTaskDelete(NULL);
}

// --- Tarea del Sensor ---
static void sensor_update_task(void *pvParameters) {
    ESP_LOGI(TAG, "Iniciando sensor_update_task. Esperando a que la red Zigbee esté lista (semáforo)...");

    // Esperar a que la red esté lista (señalado por esp_zb_app_signal_handler)
    if (xSemaphoreTake(xZigbeeNetworkReadySemaphore, portMAX_DELAY) == pdTRUE) {
        ESP_LOGI(TAG, "Semáforo tomado. ¡Red Zigbee lista! Iniciando lecturas ADC y actualizaciones de corriente.");

        while (1) {
            int voltage_mv_1 = read_adc_voltage_mv(ADC_CHANNEL_1);
            int voltage_mv_2 = read_adc_voltage_mv(ADC_CHANNEL_2);
            int voltage_mv_3 = read_adc_voltage_mv(ADC_CHANNEL_3);

            // Calcular corriente incluso si la lectura ADC falló (resultará en corriente errónea pero evita NaN)
            float current_a_1 = (voltage_mv_1 >= 0) ? (((float)voltage_mv_1 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f; // Valor sentinela
            float current_a_2 = (voltage_mv_2 >= 0) ? (((float)voltage_mv_2 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f;
            float current_a_3 = (voltage_mv_3 >= 0) ? (((float)voltage_mv_3 - SENSOR_ZERO_CURRENT_VOLTAGE_MV) / SENSOR_SENSITIVITY_MV_PER_A) : -999.9f;

             if (voltage_mv_1 < 0) ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 1 (GPIO1)");
             if (voltage_mv_2 < 0) ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 2 (GPIO2)");
             if (voltage_mv_3 < 0) ESP_LOGW(TAG, "Lectura ADC inválida para Sensor 3 (GPIO3)");

            // Actualizar atributos Zigbee solo si la red está conectada (implícito por el semáforo inicial)
            // Si la red se cae después, update_sensor_currents intentará enviar pero podría fallar.
             update_sensor_currents(current_a_1, current_a_2, current_a_3);

             vTaskDelay(pdMS_TO_TICKS(SEND_INTERVAL_MS));
        }
    } else {
        ESP_LOGE(TAG, "¡Timeout esperando el semáforo de red lista! La tarea del sensor no iniciará.");
        // El estado del LED lo maneja esp_zb_app_signal_handler en caso de fallo de unión.
    }

    ESP_LOGW(TAG, "sensor_update_task terminando (esto no debería ocurrir en operación normal).");
    vTaskDelete(NULL);
}

// --- Inicialización Plataforma ---
void zigbee_platform_init() {
    esp_zb_platform_config_t config = {
        .radio_config = ESP_ZB_DEFAULT_RADIO_CONFIG(),
        .host_config = ESP_ZB_DEFAULT_HOST_CONFIG(),
    };
    ESP_LOGI(TAG, "1. Inicializando NVS...");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "Problema con NVS, borrando y reintentando...");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS inicializado.");
    ESP_LOGI(TAG, "2. Configurando plataforma Zigbee...");
    ESP_ERROR_CHECK(esp_zb_platform_config(&config));
    ESP_LOGI(TAG, "Plataforma Zigbee configurada.");
}

// --- Función Principal ---
void app_main(void) {
    ESP_LOGI(TAG, "--- Iniciando Router Zigbee con 3 Sensores de Corriente (LED RGB Pin %d) ---", RGB_LED_GPIO);

    // Crear semáforo ANTES de iniciar tareas que dependan de él
    xZigbeeNetworkReadySemaphore = xSemaphoreCreateBinary();
    if (xZigbeeNetworkReadySemaphore == NULL) {
        ESP_LOGE(TAG, "¡Fallo al crear el semáforo!");
        // Considerar reiniciar o detenerse aquí
        return;
    }
    ESP_LOGI(TAG, "Semáforo creado.");

    // Inicializar periféricos primero
    led_init();
    adc_init();

    // Inicializar la plataforma Zigbee (incluye NVS)
    zigbee_platform_init();

    // Crear tareas
    ESP_LOGI(TAG, "Creando la tarea esp_zb_task...");
    xTaskCreate(esp_zb_task, "zigbee_task", 4096 * 2, NULL, 5, NULL);

    ESP_LOGI(TAG, "Creando la tarea sensor_update_task...");
    xTaskCreate(sensor_update_task, "sensor_task", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "app_main: Inicialización completada. Tareas creadas y sistema en marcha.");
    // No hacer nada más aquí, las tareas se encargarán
}