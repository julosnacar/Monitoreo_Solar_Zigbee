Este proyecto implementa un dispositivo Zigbee multifunción en un ESP32-H2. Actúa como:
Router Zigbee (ZR): Se une a una red Zigbee existente (formada por un Coordinador) y ayuda a extender el alcance de la red permitiendo que otros dispositivos se conecten a través de él (funcionalidad mesh).
Sensor Analógico: Lee continuamente un valor de un sensor conectado a un pin ADC.
Reportador de Datos: Envía el valor leído del sensor a través de la red Zigbee utilizando el clúster estándar Analog Input (Basic) como servidor, actualizando el atributo PresentValue.
Indicador de Estado: Utiliza un LED conectado a un único pin (GPIO8) para indicar su estado de conexión a la red (parpadea cuando está conectado, apagado si no lo está).
Requisitos y Dependencias
Hardware: ESP32-H2 (ej. ESP32-H2-DEV-KIT-N4), Sensor analógico (ej. Efecto Hall) conectado al pin ADC configurado, LED conectado a GPIO8.
Software: SDK ESP-IDF (v5.1 o superior recomendada, compatible con la v5.4.1 usada en los logs), Toolchain de desarrollo C.
Componentes ESP-IDF: nvs_flash, esp_log, freertos (Tasks, Semaphores, Timers), driver/adc, driver/gpio, esp_system, esp_idf_lib_helpers, esp_zigbee_core, esp_zigbee_zcl.
Configuración: Habilitar el componente esp_zigbee en menuconfig.
Estructura del Código
Archivo Principal: main.c
Headers Clave Incluidos:
esp_zigbee_core.h: Funciones base del stack, señales, BDB.
esp_zigbee_zcl_command.h: Estructuras ZCL (usadas implícitamente para esp_zb_zcl_set_attribute_val).
esp_zigbee_ha_standard.h: Perfil Home Automation.
zcl/esp_zigbee_zcl_common.h: Tipos comunes ZCL.
zcl/esp_zigbee_zcl_analog_input.h: Definiciones del clúster Analog Input.
zcl/esp_zigbee_zcl_basic.h, zcl/esp_zigbee_zcl_identify.h: Para clústeres obligatorios.
driver/adc.h, driver/gpio.h: Control de periféricos.
freertos/timers.h: Para el parpadeo del LED.
Otros estándar (stdio.h, string.h, freertos/*, nvs_flash.h, esp_log.h, etc.).
Funciones Principales:
led_init(), led_set_physical(), blink_timer_callback(), led_set_state(): Gestionan el estado y parpadeo del LED en GPIO8.
adc_init(), read_adc(): Configuran y leen el valor del conversor Analógico-Digital.
update_zigbee_present_value(): Actualiza el valor del atributo PresentValue en el clúster Analog Input para que pueda ser leído o reportado.
esp_zb_app_signal_handler(): Callback que gestiona señales del stack Zigbee (inicio, conexión exitosa/fallida, desconexión) y controla el estado del LED (LED_STATE_OFF o LED_STATE_CONNECTED_BLINKING). Libera el semáforo xZigbeeNetworkReadySemaphore al conectarse.
esp_zb_task(): Tarea FreeRTOS que configura el rol como Router, define clústeres (Basic S, Identify S, Analog Input S), crea el endpoint, registra el dispositivo e inicia el stack con esp_zb_start(true) (autostart para intentar unirse inmediatamente). Entra en esp_zb_stack_main_loop().
sensor_update_task(): Tarea FreeRTOS que espera el semáforo xZigbeeNetworkReadySemaphore (liberado por esp_zb_app_signal_handler al conectarse). Una vez conectada, entra en un bucle que lee el ADC, escala el valor, llama a update_zigbee_present_value(), y espera SEND_INTERVAL_MS.
zigbee_platform_init(): Inicializa NVS y plataforma Zigbee.
app_main(): Punto de entrada. Inicializa LED, NVS, plataforma, ADC, crea el semáforo y las tareas esp_zb_task y sensor_update_task.
