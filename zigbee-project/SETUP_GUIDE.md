# Guía de Configuración para el Proyecto Monitoreo Solar Zigbee

Este documento describe los pasos necesarios para configurar el entorno y ejecutar los scripts del proyecto Monitoreo Solar Zigbee, tanto en un sistema Windows como en una Raspberry Pi (o cualquier sistema Linux similar).

## Requisitos Previos Generales

1.  **Python:** Asegúrate de tener Python instalado. Se recomienda Python 3.9 o superior. Puedes descargarlo desde [python.org](https://www.python.org/).
2.  **Git:** Necesitarás Git para clonar este repositorio. Puedes descargarlo desde [git-scm.com](https://git-scm.com/).
3.  **Hardware Zigbee:**
    *   Un coordinador Zigbee compatible con `bellows` (por ejemplo, SONOFF ZBDongle-E, Elelabs Zigbee USB Adapter, etc.).
    *   Al menos un dispositivo router/sensor ESP32-H2 programado con el firmware correspondiente de este proyecto.

## 1. Clonar el Repositorio

Abre tu terminal o consola y clona el repositorio:
```bash
git clone https://github.com/julosnacar/Monitoreo_Solar_Zigbee.git
cd Monitoreo_Solar_Zigbee


## 2. Crear y Activar un Entorno Virtual (Recomendado)
Es altamente recomendable usar un entorno virtual para aislar las dependencias de este proyecto.
# Crear el entorno virtual (puedes nombrarlo como prefieras, ej: .venv)
python -m venv .venv 
# O un nombre específico si tienes varios entornos, ej:
# python -m venv .EFR32MG21

Bash
Activar el entorno virtual:
En Windows (CMD):
.\.venv\Scripts\activate
Use code with caution.
Cmd
o si usaste .EFR32MG21:
.\.EFR32MG21\Scripts\activate
Use code with caution.
Cmd
En Windows (PowerShell):
.venv\Scripts\Activate.ps1
# O si usaste .EFR32MG21:
.EFR32MG21\Scripts\Activate.ps1
# Si recibes un error de ejecución de scripts, puede que necesites ejecutar:
# Set-ExecutionPolicy Unrestricted -Scope Process

Powershell
En Linux (Raspberry Pi) o macOS (Bash/Zsh):
source .venv/bin/activate
# O si usaste .EFR32MG21:
source .EFR32MG21/bin/activate

Bash
Tu prompt de la terminal debería cambiar para indicar que el entorno virtual está activo.

## 3. Instalar Dependencias de Python
Con el entorno virtual activado, instala las librerías necesarias. Se proporciona un archivo requirements.txt (si lo tienes) o puedes instalarlas manualmente.

Bash
## Instalación manual de librerías clave (si no hay requirements.txt):
pip install bellows zigpy pyserial aiosqlite requests

Bash
bellows: Para la comunicación con coordinadores basados en EZSP (como el SONOFF ZBDongle-E).
zigpy: La librería fundamental de Zigbee.
pyserial (y pyserial-asyncio que se instala con bellows): Para la comunicación serie con el dongle.
aiosqlite: Para la base de datos de zigpy.
requests: (Opcional, si vas a enviar datos a una API HTTP como AWS).
Nota sobre sqlite3 en Linux:
En algunos sistemas Linux (como Raspberry Pi OS Lite), puede que necesites instalar las herramientas de desarrollo de sqlite3 si aiosqlite no se compila correctamente:
sudo apt-get update
sudo apt-get install libsqlite3-dev

Bash
Luego intenta pip install aiosqlite de nuevo.
## 4. Configuración Específica por Plataforma
## 4.1 Para Windows
Identificar el Puerto COM del Coordinador Zigbee:
Conecta tu dongle Zigbee.
Abre el "Administrador de dispositivos" en Windows.
Busca en "Puertos (COM & LPT)". Deberías ver tu dongle listado (ej. "Silicon Labs CP210x USB to UART Bridge (COM9)"). Anota el número de puerto COM (ej. COM9).
Es posible que necesites instalar drivers para el chip USB-serie de tu dongle (ej. CP210x para el ZBDongle-E). Generalmente se instalan automáticamente o puedes descargarlos del fabricante del chip (Silicon Labs).
Configurar el Script (Para_Windows/test_conect.py):
Abre el archivo Para_Windows/test_conect.py en un editor de texto.
Modifica la constante DEVICE_PATH al puerto COM que identificaste:
DEVICE_PATH = 'COM9'  # Reemplaza COM9 con tu puerto

Python
Asegúrate de que BAUDRATE (ej. 115200 para ZBDongle-E) y FLOW_CONTROL (generalmente None) son correctos para tu dongle.
Ejecutar el Script:
Asegúrate de que tu entorno virtual está activo en la terminal.
Navega a la carpeta Para_Windows:
cd Para_Windows

Bash
Ejecuta el script:
python test_conect.py

Bash
## 4.2 Para Raspberry Pi (o Linux)
Identificar el Puerto Serie del Coordinador Zigbee:
Conecta tu dongle Zigbee.
Abre una terminal.
Puedes usar dmesg -w y conectar el dongle para ver qué dispositivo tty se le asigna (ej. /dev/ttyACM0 o /dev/ttyUSB0).
Una forma más robusta es usar el enlace simbólico por ID:
ls -l /dev/serial/by-id/

Bash
Esto te dará un nombre más largo y estable, por ejemplo:
usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_xxxxxxxxxxxx-if00-port0
Este nombre es un enlace al dispositivo real (ej. ../../ttyUSB0).
Permisos del Puerto Serie (Importante):
Tu usuario necesita permisos para acceder al puerto serie. Usualmente, esto se logra añadiendo tu usuario al grupo dialout (o a veces tty).
sudo usermod -a -G dialout $USER

Bash
DEBES CERRAR SESIÓN Y VOLVER A INICIAR SESIÓN o REINICIAR la Raspberry Pi para que este cambio de grupo tenga efecto.
Puedes verificar si tu usuario está en el grupo con groups $USER.
Configurar el Script (Para_Raspberry/tu_script_raspberry.py):
Nota: Asume que tienes un script similar a test_conect.py en la carpeta Para_Raspberry.
Abre el script correspondiente en Para_Raspberry/ en un editor de texto.
Modifica la constante DEVICE_PATH. Se recomienda usar la ruta /dev/serial/by-id/... por su estabilidad:
DEVICE_PATH = '/dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_xxxxxxxxxxxx-if00-port0' 
# O el puerto directo si lo prefieres, ej:
# DEVICE_PATH = '/dev/ttyACM0'

Python
Asegúrate de que BAUDRATE y FLOW_CONTROL son correctos.
Ejecutar el Script:
Asegúrate de que tu entorno virtual está activo en la terminal.
Navega a la carpeta Para_Raspberry:
cd Para_Raspberry

Bash
Ejecuta el script:
python tu_script_raspberry.py

Bash
## 5. Funcionamiento Esperado del Script Principal (test_conect.py o similar)
El script intentará conectarse al coordinador Zigbee.
Si la red Zigbee no está formada, intentará crear una nueva en el canal especificado (o uno por defecto).
Una vez conectado y la red formada/cargada, abrirá la red para que dispositivos se unan durante un tiempo configurado (ej. 180 segundos).
Enciende tu dispositivo sensor/router ESP32-H2. Debería:
Parpadear en AZUL mientras busca la red.
Parpadear en NARANJA (o similar) mientras se une.
Parpadear en VERDE una vez conectado.
En la consola donde ejecutas el script Python, deberías empezar a ver mensajes:
*** LECTURA DE SENSOR: Sensor Corriente 1 (AttrID: 0x0001) = X.XX A ***
*** LECTURA DE SENSOR: Sensor Corriente 2 (AttrID: 0x0002) = Y.YY A ***
*** LECTURA DE SENSOR: Sensor Corriente 3 (AttrID: 0x0003) = Z.ZZ A ***

Estos mensajes indican que los datos de los sensores de corriente del ESP32-H2 están llegando correctamente al coordinador.
6. Solución de Problemas Comunes
Error de Permisos (Linux/Raspberry Pi): Si el script no puede abrir el puerto serie (PermissionError: [Errno 13] Permission denied: '/dev/ttyACM0'), asegúrate de que tu usuario está en el grupo dialout y has reiniciado/relogueado.
No se encuentra el Dongle: Verifica que el DEVICE_PATH sea correcto y que el dongle esté bien conectado y reconocido por el sistema operativo. Revisa dmesg (Linux) o el Administrador de Dispositivos (Windows).
Errores de bellows o zigpy:
Asegúrate de que las librerías están instaladas correctamente en el entorno virtual activo.
Verifica que tu dongle es compatible con bellows.
El ESP32-H2 no se une:
Asegúrate de que el script Python está en el estado de "permitir unión" (permit join).
Verifica los logs de la consola del ESP32-H2 para pistas sobre el proceso de unión.
Comprueba que el canal Zigbee en el que el coordinador forma la red es uno que el ESP32-H2 puede escanear.
Ningún mensaje de LECTURA DE SENSOR:
Verifica que el ESP32-H2 se haya unido correctamente (LED verde parpadeante).
Comprueba los logs del script Python para ver si el dispositivo ESP32-H2 es reconocido y si la configuración de reportes se completa sin errores.
Asegúrate de que los IDs de cluster (CUSTOM_CLUSTER_ID) y atributos (ATTR_ID_...) coinciden entre el firmware del ESP32-H2 y el script Python.
7. Notas Adicionales
La primera vez que se ejecuta el script con un dongle nuevo o reseteado, la formación de la red puede tardar unos segundos adicionales.
Se creará un archivo zigbee.db en el directorio desde donde se ejecuta el script. Este archivo almacena el estado de la red Zigbee.
Para detener el script, presiona Ctrl+C en la terminal.

