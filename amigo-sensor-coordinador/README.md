Este proyecto implementa un Coordinador Zigbee (ZC) utilizando un microcontrolador ESP32-H2. Su función principal es formar y gestionar una red Zigbee, permitir que dispositivos se unan (como los sensores ESP32-H2 configurados como Routers) y recibir datos enviados por estos dispositivos. Específicamente, está configurado para escuchar y procesar reportes de atributos del clúster Analog Input (Basic), mostrando en la consola la dirección del sensor y el valor analógico recibido. Este código sirve como base para una puerta de enlace (Gateway) que podría reenviar estos datos a otros sistemas (ej. Raspberry Pi, AWS).
Requisitos y Dependencias
Hardware: ESP32-H2 (ej. ESP32-H2-DEV-KIT-N4)
Software: SDK ESP-IDF (v5.1 o superior recomendada, compatible con la v5.4.1 usada en los logs), Toolchain de desarrollo C.
Componentes ESP-IDF: nvs_flash, esp_log, freertos, esp_system, esp_idf_lib_helpers (implícita), esp_zigbee_core, esp_zigbee_zcl (implícitas al habilitar Zigbee en menuconfig).
Configuración: Habilitar el componente esp_zigbee en menuconfig.
Estructura del Código
Archivo Principal: main.c (Contiene toda la lógica del coordinador).
Headers Clave Incluidos:
esp_zigbee_core.h: Funciones principales del stack Zigbee (inicialización, arranque, señales, BDB).
esp_zigbee_zcl_command.h: Estructuras y funciones para manejar comandos ZCL (incluyendo reportes).
esp_zigbee_ha_standard.h: Definiciones estándar del perfil Home Automation (IDs de dispositivos, perfiles).
zcl/esp_zigbee_zcl_common.h: Tipos y constantes comunes de ZCL.
zcl/esp_zigbee_zcl_analog_input.h: Definiciones específicas del clúster Analog Input.
zcl/esp_zigbee_zcl_basic.h, zcl/esp_zigbee_zcl_identify.h: Para crear los clústeres básicos del coordinador.
Otros headers estándar de ESP-IDF (stdio.h, string.h, freertos/*, nvs_flash.h, esp_log.h, etc.).
Funciones Principales:
esp_zb_zcl_attr_handler(const esp_zb_zcl_cmd_info_t *cmd_info, const void *user_data): Callback invocado por el stack Zigbee cuando llega un comando ZCL dirigido a un endpoint/cluster de este dispositivo. Es responsable de procesar los reportes de atributos entrantes. (Nota: No se registra explícitamente con una función tipo register_attr_handler, su invocación depende de la correcta configuración de los clústeres cliente en el endpoint).
esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct): Callback que gestiona señales asíncronas del stack Zigbee (estado de la red, unión/salida de dispositivos, errores BDB).
esp_zb_task(void *pvParameters): Tarea principal de FreeRTOS que inicializa la configuración Zigbee (esp_zb_cfg_t como Coordinador), define los clústeres y endpoints del coordinador, registra el dispositivo, inicia el stack (esp_zb_start) y entra en el bucle principal (esp_zb_stack_main_loop).
zigbee_platform_init(): Inicializa NVS y configura los parámetros de la plataforma Zigbee (radio, host).
app_main(): Punto de entrada de la aplicación. Inicializa la plataforma y crea la tarea esp_zb_task. Establece el nivel de log para Zigbee.

## Troubleshooting

For any technical queries, please open an [issue](https://github.com/espressif/esp-idf/issues) on GitHub. We will get back to you soon
