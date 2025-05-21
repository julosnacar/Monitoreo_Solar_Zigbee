#include <esp_zigbee_core.h>
#include <esp_zigbee_cluster.h>
#include "zcl/esp_zigbee_zcl_analog_input.h" // Cabecera específica para el clúster Analog Input
#include "ha/esp_zigbee_ha_standard.h"
#include "zdo/esp_zigbee_zdo_common.h" // Para definiciones de señales como ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT
#include "zcl/esp_zigbee_zcl_common.h"    // Funciones comunes ZCL como set_attribute_val
#include "zcl/esp_zigbee_zcl_basic.h"     // IDs de atributos del Cluster Basic

// ------------------------------
/* Basic manufacturer information */
//#define ESP_MANUFACTURER_NAME_STR "\x09""ESPRESSIF\0"
//#define ESP_MODEL_IDENTIFIER_STR  "\x07"CONFIG_IDF_TARGET"\0" // CONFIG_IDF_TARGET debe ser una cadena como "esp32h2"

#define ESP_ZB_DEFAULT_RADIO_CONFIG()                           \
    {                                                           \
        .radio_mode = ZB_RADIO_MODE_NATIVE,                     \
    }

#define ESP_ZB_DEFAULT_HOST_CONFIG()                            \
    {                                                           \
        .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE,   \
    }