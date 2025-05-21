# Monitoreo Solar Zigbee con ESP32-H2 y Coordinador Python

Este proyecto implementa un sistema de monitoreo de corriente, ideal para aplicaciones de energía solar o consumo general, utilizando la tecnología Zigbee. Consiste en un dispositivo sensor/router basado en ESP32-H2 y un script coordinador en Python que gestiona la red y recibe los datos.

## Descripción General

El sistema se compone de dos partes principales:

1.  **Dispositivo Sensor/Router ESP32-H2:**
    *   Utiliza un microcontrolador ESP32-H2 programado con ESP-IDF.
    *   Funciona como un router Zigbee, ayudando a extender el alcance de la red.
    *   Lee datos de hasta tres sensores de corriente analógicos (por ejemplo, HSTS016L).
    *   Reporta las mediciones de corriente a través de un clúster Zigbee ZCL personalizado.
    *   Incluye un indicador LED RGB para mostrar el estado de la conexión Zigbee.

2.  **Coordinador Zigbee Python:**
    *   Un script Python que se ejecuta en un PC con un dongle Zigbee (por ejemplo, Sonoff ZBDongle-E).
    *   Utiliza las bibliotecas `zigpy` y `bellows` para actuar como el coordinador de la red Zigbee.
    *   Gestiona la formación de la red, permite que los dispositivos se unan, y configura el reporte de atributos.
    *   Recibe y muestra los datos de corriente enviados por los dispositivos ESP32-H2.

## Estructura del Repositorio

*   `amigo-sensor-coordinador/`
    *   **Descripción:** Contiene el código y los archivos necesarios para el coordinador Zigbee basado en Python.
    *   **Contenido Clave:**
        *   `test_conect.py`: El script principal del coordinador Python.
        *   `requirements.txt` (Sugerido): Lista de dependencias Python (ej. `bellows`, `zigpy`).
        *   `zigbee.db` (Opcional): Archivo de base de datos SQLite utilizado por `zigpy` para almacenar el estado de la red y los dispositivos. Puede ser útil para restaurar una red existente o como ejemplo.

*   `amigo-sensor-zigbee/`
    *   **Descripción:** Contiene el código fuente del firmware para el dispositivo sensor/router ESP32-H2.
    *   **Contenido Clave:**
        *   `main/`: Directorio principal del código fuente del firmware.
            *   `main.c`: Lógica principal del firmware, incluyendo inicialización de ADC, gestión de Zigbee, clúster personalizado y control del LED.
            *   `main.h`: Archivos de cabecera.
            *   `CMakeLists.txt`: Script de compilación para el directorio `main`.
        *   `CMakeLists.txt`: Script de compilación principal del proyecto ESP-IDF.
        *   `sdkconfig`: Archivo de configuración del proyecto ESP-IDF (define características del hardware, componentes del SDK, etc.).
        *   `partitions.csv` (Si existe): Define la tabla de particiones de la flash del ESP32.

*   `zigbee-project/`
    *   **Descripción:** _(Por favor, describe qué contiene esta carpeta si decides mantenerla. Si es un proyecto de ejemplo o una versión anterior, indícalo. Si es redundante, considera eliminarla para mayor claridad)._
    *   **Contenido Clave:** _(Lista los archivos importantes aquí)_

*   `.gitignore`
    *   **Descripción:** Especifica los archivos y directorios que Git debe ignorar y no incluir en el control de versiones (ej. archivos de compilación, entornos virtuales).

*   `README.md`
    *   **Descripción:** Este archivo, proporcionando una visión general del proyecto.

## Requisitos Previos

### Para el Dispositivo Sensor/Router ESP32-H2:
*   Hardware:
    *   Placa de desarrollo ESP32-H2.
    *   Sensores de corriente analógicos (ej. HSTS016L).
    *   LED RGB (si se usa la indicación visual).
*   Software:
    *   ESP-IDF (Espressif IoT Development Framework) versión X.Y.Z (especifica la versión que usaste, ej. v5.1).
    *   Toolchain de compilación para ESP32.

### Para el Coordinador Zigbee Python:
*   Hardware:
    *   PC con Python instalado.
    *   Dongle USB Zigbee compatible con `bellows` (ej. Sonoff ZBDongle-E, basado en EFR32MG21).
*   Software:
    *   Python (versión 3.x recomendada, especifica si es necesario, ej. 3.8+).
    *   Bibliotecas Python: `bellows`, `zigpy`, `pyserial` (pueden instalarse con `pip install -r requirements.txt` si provees el archivo).

## Guía de Configuración y Uso

### 1. Firmware del ESP32-H2 (`amigo-sensor-zigbee/`)
1.  Clona este repositorio.
2.  Navega al directorio `amigo-sensor-zigbee/`.
3.  Configura tu entorno ESP-IDF.
4.  Ajusta la configuración del proyecto si es necesario usando `idf.py menuconfig` (por ejemplo, pines GPIO para los sensores o el LED).
5.  Compila y flashea el firmware en tu dispositivo ESP32-H2:
    ```bash
    idf.py build
    idf.py -p /dev/ttyUSB0 flash monitor # Reemplaza /dev/ttyUSB0 con tu puerto serial
    ```

### 2. Coordinador Python (`amigo-sensor-coordinador/`)
1.  Navega al directorio `amigo-sensor-coordinador/`.
2.  (Recomendado) Crea y activa un entorno virtual Python:
    ```bash
    python -m venv venv
    source venv/bin/activate  # En Linux/macOS
    # venv\Scripts\activate    # En Windows
    ```
3.  Instala las dependencias (si tienes un `requirements.txt`):
    ```bash
    pip install -r requirements.txt
    ```
    O instala manualmente:
    ```bash
    pip install bellows zigpy pyserial
    ```
4.  Conecta tu dongle Zigbee al PC.
5.  Modifica `test_conect.py` para apuntar al puerto serial correcto de tu dongle (variable `DEVICE_PATH`).
6.  Ejecuta el script del coordinador:
    ```bash
    python test_conect.py
