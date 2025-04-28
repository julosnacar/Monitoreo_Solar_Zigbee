#include <esp_zigbee_core.h>
#include <esp_zigbee_cluster.h>
#include "zcl/esp_zigbee_zcl_analog_input.h" // Cabecera específica para el clúster Analog Input
#include "ha/esp_zigbee_ha_standard.h"


#define ESP_ZB_DEFAULT_RADIO_CONFIG()                           \
    {                                                           \
        .radio_mode = ZB_RADIO_MODE_NATIVE,                     \
    }

#define ESP_ZB_DEFAULT_HOST_CONFIG()                            \
    {                                                           \
        .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE,   \
    }