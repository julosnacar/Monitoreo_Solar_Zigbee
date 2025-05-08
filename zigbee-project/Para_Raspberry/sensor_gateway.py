import asyncio
import logging
import signal
import json
import requests
from datetime import datetime, timedelta
import time
import sys
import serial
import serial.tools.list_ports

# --- Importaciones Zigpy y ZNP ---
from zigpy.application import ControllerApplication
import zigpy.config as zigpy_config
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
import zigpy.device

try:
    import zigpy_znp.zigbee.application
    import zigpy_znp.config as znp_config
    RADIO_LIBRARIES_AVAILABLE = True
except ImportError:
    RADIO_LIBRARIES_AVAILABLE = False
    print("Advertencia: Biblioteca zigpy-znp no instalada. Instalar con: pip install zigpy-znp")

# --- Configuración ---
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# SERIAL_PORT será detectado automáticamente.
# En Raspberry Pi, los puertos suelen ser /dev/ttyUSB0, /dev/ttyACM0, etc.
BAUD_RATE = 115200
DATABASE_PATH = 'zigbee.db' # Considera una ruta absoluta si lo ejecutas como servicio, ej: '/home/pi/project-zigbee/zigbee.db'
AWS_API_ENDPOINT = 'https://lbbcoc4xnd.execute-api.ap-southeast-2.amazonaws.com/dev/reading/save'

# --- Constantes del Cluster Personalizado ---
CUSTOM_CLUSTER_ID = 0xFC01
ATTR_ID_CURRENT_SENSOR_1 = 0x0001
ATTR_ID_CURRENT_SENSOR_2 = 0x0002
ATTR_ID_CURRENT_SENSOR_3 = 0x0003

# --- Estado Global ---
app = None
sensor_data_cache = {}
last_send_time = {}
SEND_INTERVAL_SECONDS = 5

class ZigbeeListener:
    def device_joined(self, device):
        _LOGGER.info("Dispositivo unido: %s", device)
        if device.ieee not in sensor_data_cache:
            sensor_data_cache[device.ieee] = {}
        if device.ieee not in last_send_time:
            last_send_time[device.ieee] = datetime.min

    def device_left(self, device):
        _LOGGER.info("Dispositivo abandonó: %s", device)
        if device.ieee in sensor_data_cache:
            del sensor_data_cache[device.ieee]
        if device.ieee in last_send_time:
            del last_send_time[device.ieee]

    def device_removed(self, device):
        _LOGGER.info("Dispositivo eliminado: %s", device)
        if device.ieee in sensor_data_cache:
            del sensor_data_cache[device.ieee]
        if device.ieee in last_send_time:
            del last_send_time[device.ieee]

    def attribute_updated(self, device, profile_id, cluster_id, endpoint_id, attrid, value):
        _LOGGER.debug(
            "Actualización de atributo recibida de %s (Endpoint: %d): Cluster: 0x%04x, Atributo: 0x%04x, Valor: %s",
            device.ieee, endpoint_id, cluster_id, attrid, value
        )

        if cluster_id == CUSTOM_CLUSTER_ID:
            if attrid in [ATTR_ID_CURRENT_SENSOR_1, ATTR_ID_CURRENT_SENSOR_2, ATTR_ID_CURRENT_SENSOR_3]:
                if device.ieee not in sensor_data_cache:
                    sensor_data_cache[device.ieee] = {}
                if device.ieee not in last_send_time:
                    last_send_time[device.ieee] = datetime.min
                try:
                    float_value = float(value)
                    _LOGGER.info(
                        "Dato de corriente recibido de %s: Attr 0x%04x = %.3f A",
                        device.ieee, attrid, float_value
                    )
                    sensor_data_cache[device.ieee][attrid] = float_value
                    check_and_send_data(device)
                except ValueError:
                    _LOGGER.warning(
                        "Valor no numérico recibido de %s: Attr 0x%04x = %s",
                        device.ieee, attrid, value
                    )

def send_to_aws(device_ieee, data):
    global last_send_time
    payload = {
        "sensor_mac": str(device_ieee),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "current_1": data.get(ATTR_ID_CURRENT_SENSOR_1, None),
        "current_2": data.get(ATTR_ID_CURRENT_SENSOR_2, None),
        "current_3": data.get(ATTR_ID_CURRENT_SENSOR_3, None)
    }
    payload_clean = {k: v for k, v in payload.items() if v is not None}
    _LOGGER.info("Preparando para enviar a AWS: %s", json.dumps(payload_clean))
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(AWS_API_ENDPOINT, headers=headers, json=payload_clean, timeout=10)
        response.raise_for_status()
        _LOGGER.info("Datos enviados a AWS exitosamente para %s (Status: %d)", device_ieee, response.status_code)
        last_send_time[device_ieee] = datetime.utcnow()
    except requests.exceptions.Timeout:
        _LOGGER.error("Error de Timeout al enviar datos a AWS para %s", device_ieee)
    except requests.exceptions.HTTPError as http_err:
        _LOGGER.error("Error HTTP al enviar datos a AWS para %s: %s - Respuesta: %s", device_ieee, http_err, response.text)
    except requests.exceptions.RequestException as e:
        _LOGGER.error("Error de Red/Conexión al enviar datos a AWS para %s: %s", device_ieee, e)
    except Exception as e:
        _LOGGER.error("Error inesperado durante envío a AWS para %s: %s", device_ieee, e)

def check_and_send_data(device):
    global sensor_data_cache, last_send_time, SEND_INTERVAL_SECONDS
    dev_ieee = device.ieee
    if dev_ieee not in sensor_data_cache:
        _LOGGER.debug("check_and_send_data: Dispositivo %s no encontrado en caché.", dev_ieee)
        return
    cached_data = sensor_data_cache[dev_ieee]
    if (ATTR_ID_CURRENT_SENSOR_1 in cached_data and
            ATTR_ID_CURRENT_SENSOR_2 in cached_data and
            ATTR_ID_CURRENT_SENSOR_3 in cached_data):
        _LOGGER.debug("check_and_send_data: Los 3 atributos recibidos para %s.", dev_ieee)
        now = datetime.utcnow()
        last_sent = last_send_time.get(dev_ieee, datetime.min)
        time_since_last_send = now - last_sent
        if time_since_last_send >= timedelta(seconds=SEND_INTERVAL_SECONDS):
            _LOGGER.info("check_and_send_data: Intervalo cumplido para %s. Enviando datos.", dev_ieee)
            send_to_aws(dev_ieee, cached_data)
        else:
            _LOGGER.debug("check_and_send_data: Aún no ha pasado el intervalo para %s. Esperando.", dev_ieee)
    else:
        _LOGGER.debug("check_and_send_data: Faltan atributos para %s. Datos actuales: %s", dev_ieee, cached_data)

# --- MODIFICADO: Detección de puerto para Linux (Raspberry Pi) ---
def find_sonoff_dongle_port():
    """Intenta encontrar el puerto serie del Sonoff Dongle automáticamente."""
    # VID y PID típicos para el puente CP210x (usado en ZBDongle-E y P)
    SONOFF_VID = 0x10C4
    SONOFF_PID = 0xEA60

    ports = serial.tools.list_ports.comports()
    _LOGGER.info("Buscando puertos serie disponibles...")
    sonoff_port = None
    for port in ports:
        vid_str = f"{port.vid:04X}" if port.vid is not None else "N/A"
        pid_str = f"{port.pid:04X}" if port.pid is not None else "N/A"
        _LOGGER.debug(
            f"Puerto detectado: {port.device} - {port.description} "
            f"[VID:{vid_str} PID:{pid_str} SER:{port.serial_number} HWID:{port.hwid}]"
        )
        # En Linux, VID y PID pueden ser None si el usuario no tiene permisos para acceder a toda la info de USB.
        # A menudo, la descripción es más útil en Linux si VID/PID fallan.
        if port.vid == SONOFF_VID and port.pid == SONOFF_PID:
            _LOGGER.info(f"Dongle Sonoff (CP210x/CP2102N) encontrado por VID/PID en: {port.device} (Descripción: {port.description})")
            sonoff_port = port.device
            break
        # Fallback a la descripción si VID/PID no están disponibles o no coinciden (más robusto en Linux)
        elif "CP210x" in port.description or "Silicon Labs CP210x" in port.description:
             _LOGGER.info(f"Dongle Sonoff (CP210x/CP2102N) encontrado por descripción en: {port.device} (Descripción: {port.description})")
             if sonoff_port is None: # Solo tomar si no se encontró por VID/PID
                 sonoff_port = port.device
                 # No hacemos break aquí para dar prioridad a la detección por VID/PID si aparece después

    if sonoff_port:
        # --- Asegurar permisos en Linux ---
        # Esto no cambia el permiso permanentemente, solo es una nota.
        # El usuario necesita ser parte del grupo 'dialout' o usar udev rules.
        _LOGGER.info(f"Puerto seleccionado: {sonoff_port}. Asegúrate de tener permisos de lectura/escritura.")
        _LOGGER.info("En Linux, añade tu usuario al grupo 'dialout': sudo usermod -a -G dialout $USER")
        _LOGGER.info("Y reinicia la sesión o el sistema. O usa 'sudo python sensor_gateway.py' (no recomendado para largo plazo).")
        return sonoff_port
    else:
        _LOGGER.error("No se pudo encontrar automáticamente un dongle Sonoff Zigbee compatible.")
        _LOGGER.warning("Asegúrate de que el dongle (modelo ZBDongle-E o ZBDongle-P) esté conectado,")
        _LOGGER.warning("y que no esté siendo utilizado por otra aplicación (ej. Zigbee2MQTT, ZHA).")
        _LOGGER.warning("Puertos listados:")
        for port in ports:
            _LOGGER.warning(f"  - {port.device}: {port.description}")
        return None
# --- FIN DE MODIFICACIÓN ---

async def main():
    global app

    if not RADIO_LIBRARIES_AVAILABLE:
        _LOGGER.error("La biblioteca de radio requerida (zigpy-znp) no está instalada. Por favor, instálala primero.")
        return

    logging.getLogger('zigpy_znp').setLevel(logging.DEBUG) # Para más detalles si hay problemas
    logging.getLogger('zigpy.application').setLevel(logging.DEBUG)


    SERIAL_PORT = find_sonoff_dongle_port()
    if not SERIAL_PORT:
        _LOGGER.error("No se pudo determinar el puerto del dongle Zigbee. Abortando.")
        return
    _LOGGER.info(f"Usando el puerto serie detectado: {SERIAL_PORT}")

    # Configuración para zigpy-znp
    # Para el ZBDongle-E, prueba con rtscts=True si el firmware lo espera,
    # o sin él si causa problemas.
    znp_device_config = {
        'path': SERIAL_PORT,
        'baudrate': BAUD_RATE,
        # 'rtscts': True, # Descomenta si tu firmware flasheado lo requiere y los DIPs (si los tuviera) están ON
                         # Para el ZBDongle-E sin DIPs, prueba sin esto primero.
    }

    app_config = {
        zigpy_config.CONF_DATABASE: DATABASE_PATH,
        zigpy_config.CONF_DEVICE: znp_device_config,
    }

    _LOGGER.info("Intentando crear la aplicación del controlador...")
    _LOGGER.debug(f"Configuración de la aplicación para zigpy-znp: {app_config}")

    try:
        app = await zigpy_znp.zigbee.application.ControllerApplication.new(
            config=app_config,
            auto_form=True
        )
        _LOGGER.info("Instancia de ControllerApplication (zigpy-znp) creada exitosamente.")

        listener = ZigbeeListener()
        app.add_listener(listener)

        _LOGGER.info("Iniciando la aplicación del controlador Zigbee (startup)...")
        await app.startup(auto_form=True)
        _LOGGER.info("Controlador Zigbee iniciado exitosamente. Esperando dispositivos y datos...")

        # Mantener el script corriendo
        # En un escenario real, podrías tener aquí un loop que haga otras tareas
        # o simplemente esperar a ser interrumpido.
        stop_event = asyncio.Event()
        # Configurar manejadores de señales para llamar a stop_event.set()
        loop = asyncio.get_running_loop()
        
        # --- MODIFICADO: Manejo de señales para asyncio.Event ---
        signals_to_handle = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT) # SIGHUP puede no estar en Windows, pero sí en RPi
        for sig in signals_to_handle:
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(signal_handler(s, stop_event)))
            except (NotImplementedError, AttributeError, RuntimeError):
                 _LOGGER.warning(f"No se pudo registrar el manejador para la señal {sig.name}. Puede no estar disponible en este SO.")
        # --- FIN DE MODIFICACIÓN ---

        await stop_event.wait()

    except serial.SerialException as se:
        _LOGGER.error(f"Error del puerto serial: {se}")
        _LOGGER.error(f"No se puede comunicar con el adaptador Zigbee en {SERIAL_PORT}.")
        _LOGGER.error("Verifica las conexiones y permisos (ej. ser miembro del grupo 'dialout').")
    except Exception as radio_init_error:
        _LOGGER.error(f"Error de inicialización de radio o configuración: {radio_init_error}", exc_info=True)
        _LOGGER.error("Esto puede indicar un problema de comunicación con el adaptador Zigbee,")
        _LOGGER.error(f"un problema con el firmware del dongle ({SERIAL_PORT}),")
        _LOGGER.error("o un error en la configuración de zigpy-znp.")
        _LOGGER.debug(f"app_config utilizada: {app_config}")
    finally:
        _LOGGER.info("Iniciando proceso de cierre de la aplicación del controlador Zigbee...")
        if app and hasattr(app, 'shutdown'):
            try:
                if hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
                    _LOGGER.info("Llamando a app.shutdown()...")
                    await app.shutdown()
                    _LOGGER.info("app.shutdown() completado.")
                else:
                    _LOGGER.info("La aplicación no estaba en estado RUNNING o ya se cerró, omitiendo shutdown.")
            except Exception as e_shutdown:
                _LOGGER.exception("Error durante app.shutdown(): %s", e_shutdown)
        _LOGGER.info("Proceso de cierre finalizado.")

# --- MODIFICADO: Rutina de manejo de señal para asyncio.Event ---
async def signal_handler(sig, stop_event):
    _LOGGER.info("Recibida señal de salida %s. Iniciando cierre...", sig.name)
    # No necesitamos llamar a app.shutdown() aquí, se hará en el finally de main()
    # cuando stop_event.wait() se desbloquee.
    # Simplemente activamos el evento para que main() pueda salir de su espera.
    if not stop_event.is_set():
        stop_event.set()
# --- FIN DE MODIFICACIÓN ---


# --- MODIFICADO: El antiguo 'shutdown' ahora es parte del flujo de 'main' y 'signal_handler' ---
# El código de manejo de señales y el loop de eventos se simplifica usando asyncio.run()

if __name__ == "__main__":
    if not RADIO_LIBRARIES_AVAILABLE: # Chequeo temprano
        sys.exit(1)

    try:
        # Para Python 3.7+ se recomienda asyncio.run()
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt (Ctrl+C) recibido. El cierre es manejado por la señal SIGINT.")
    except Exception as e:
        _LOGGER.exception("Excepción no controlada en el nivel superior: %s", e)
    finally:
        _LOGGER.info("Programa finalizado.")
