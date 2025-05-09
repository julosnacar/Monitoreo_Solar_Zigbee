#--- START OF FILE sensor_gateway_ezsp.py ---
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

# --- Importaciones Zigpy y Bellows (para EZSP) ---
from zigpy.application import ControllerApplication
import zigpy.config as zigpy_config
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
import zigpy.device

try:
    import bellows.ezsp # Para verificar que bellows está disponible
    import bellows.config as bellows_config # Para configuraciones específicas de bellows/EZSP
    import bellows.zigbee.application as bellows_app # Para la aplicación de Zigbee de bellows
    RADIO_LIBRARIES_AVAILABLE = True
except ImportError:
    RADIO_LIBRARIES_AVAILABLE = False
    print("Advertencia: Biblioteca 'bellows' no instalada. Instalar con: pip install bellows")

# --- Configuración ---
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

BAUD_RATE = 115200 # ZBDongle-E usa 115200 baudios por defecto
DATABASE_PATH = 'zigbee.db'
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

def find_sonoff_dongle_port():
    """Intenta encontrar el puerto serie del Sonoff ZBDongle-E automáticamente."""
    # VID y PID típicos para el puente CP210x (usado en ZBDongle-E y P si este último usa CP210x)
    CP210X_VID = 0x10C4
    CP210X_PID = 0xEA60

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
        if port.vid == CP210X_VID and port.pid == CP210X_PID:
            _LOGGER.info(f"Dongle con chip CP210x (como Sonoff ZBDongle-E) encontrado por VID/PID en: {port.device} (Descripción: {port.description})")
            sonoff_port = port.device
            break # Encontrado el más probable por VID/PID
        elif port.description and ("CP210x" in port.description or "Silicon Labs CP210x" in port.description):
             _LOGGER.info(f"Posible Dongle con chip CP210x encontrado por descripción en: {port.device} (Descripción: {port.description})")
             if not sonoff_port: # Tomar si no se ha encontrado uno mejor
                 sonoff_port = port.device
        # Podríamos añadir aquí detección para TI si se quisiera un script más genérico,
        # pero como este es para EZSP/bellows, nos centramos en CP210x.

    if sonoff_port:
        _LOGGER.info(f"Puerto seleccionado para EZSP (bellows): {sonoff_port}. Asegúrate de tener permisos de lectura/escritura.")
        _LOGGER.info("En Linux, añade tu usuario al grupo 'dialout': sudo usermod -a -G dialout $USER")
        _LOGGER.info("Y reinicia la sesión o el sistema. O usa 'sudo python ...' (no recomendado para largo plazo).")
        return sonoff_port
    else:
        _LOGGER.error("No se pudo encontrar automáticamente un dongle Zigbee con chip CP210x (como Sonoff ZBDongle-E).")
        _LOGGER.warning("Asegúrate de que el dongle ZBDongle-E esté conectado y no esté siendo utilizado por otra aplicación.")
        _LOGGER.warning("Si estás usando un Sonoff ZBDongle-P (chip TI), necesitas usar la biblioteca 'zigpy-znp' y un script adaptado para ella.")
        _LOGGER.warning("Puertos listados:")
        for p in ports:
            _LOGGER.warning(f"  - {p.device}: {p.description} [VID:{p.vid:04X if p.vid else 'N/A'} PID:{p.pid:04X if p.pid else 'N/A'}]")
        return None

async def main():
    global app

    if not RADIO_LIBRARIES_AVAILABLE:
        _LOGGER.error("La biblioteca de radio requerida ('bellows') no está instalada. Por favor, instálala primero con 'pip install bellows'.")
        return

    # Habilitar logs de depuración para las bibliotecas clave
    logging.getLogger('bellows').setLevel(logging.DEBUG)
    logging.getLogger('bellows.uart').setLevel(logging.DEBUG) # Para ver la comunicación serial cruda
    logging.getLogger('bellows.ezsp').setLevel(logging.DEBUG) # Para el protocolo EZSP
    logging.getLogger('zigpy.application').setLevel(logging.DEBUG)

    SERIAL_PORT = find_sonoff_dongle_port()
    if not SERIAL_PORT:
        _LOGGER.error("No se pudo determinar el puerto del dongle Zigbee EZSP. Abortando.")
        return
    _LOGGER.info(f"Usando el puerto serie detectado para EZSP: {SERIAL_PORT}")

    # --- CONFIGURACIÓN PARA BELLOWS (EZSP) ---
    device_config_ezsp = {
        # bellows_config.CONF_DEVICE_PATH es la forma "oficial" pero una cadena también funciona.
        # Usaremos la cadena para consistencia con cómo se manejan otras claves.
        'path': SERIAL_PORT,
        'baudrate': BAUD_RATE,
        # Para ZBDongle-E (EZSP), el control de flujo por hardware es CRUCIAL.
        # Bellows espera 'hardware', 'software', o None.
        'flow_control': 'hardware', # RTS/CTS
    }

    app_config = {
        zigpy_config.CONF_DATABASE: DATABASE_PATH,
        zigpy_config.CONF_DEVICE: device_config_ezsp, # Aquí va la config específica del radio
        # zigpy_config.CONF_DEVICE_TYPE: 'ezsp', # No es estrictamente necesario, zigpy lo infiere
                                                 # si bellows está instalado y configurado.
    }
    # --- FIN DE CONFIGURACIÓN PARA BELLOWS ---

    _LOGGER.info("Intentando crear la aplicación del controlador (bellows para EZSP)...")
    _LOGGER.debug(f"Configuración de la aplicación para EZSP (bellows): {json.dumps(app_config, default=str)}")

    # **RECOMENDACIÓN:** Antes de cada intento, especialmente después de un fallo,
    # elimina manualmente el archivo 'zigbee.db'.
    _LOGGER.info(f"Asegúrate de que el archivo '{DATABASE_PATH}' se elimine si estás solucionando problemas de inicio.")

    try:
        # Usar la clase ControllerApplication de bellows.zigbee.application
        app = await bellows_app.ControllerApplication.new(
            config=app_config,
            auto_form=True # Intentará formar una red si no existe o restaurar desde DB
        )
        _LOGGER.info("Instancia de ControllerApplication (EZSP/bellows) creada exitosamente.")

        listener = ZigbeeListener()
        app.add_listener(listener)

        _LOGGER.info("Iniciando la aplicación del controlador Zigbee (startup)...")
        await app.startup(auto_form=True)
        _LOGGER.info("Controlador Zigbee iniciado exitosamente. Esperando dispositivos y datos...")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        signals_to_handle = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, 'SIGHUP'): # SIGHUP no existe en Windows
            signals_to_handle.append(signal.SIGHUP)

        for sig in signals_to_handle:
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(signal_handler_async(s, stop_event)))
            except (NotImplementedError, AttributeError, RuntimeError):
                 _LOGGER.warning(f"No se pudo registrar el manejador para la señal {sig.name}. Puede no estar disponible en este SO.")

        await stop_event.wait()

    except serial.SerialException as se:
        _LOGGER.error(f"Error del puerto serial: {se}")
        _LOGGER.error(f"No se puede comunicar con el adaptador Zigbee en {SERIAL_PORT}.")
        _LOGGER.error("Verifica las conexiones y permisos (ej. ser miembro del grupo 'dialout' en Linux).")
    except Exception as radio_init_error: # Captura más genérica para otros errores de bellows/zigpy
        _LOGGER.error(f"Error de inicialización de radio o configuración (EZSP/bellows): {radio_init_error}", exc_info=True)
        _LOGGER.error("Esto puede indicar un problema de comunicación con el adaptador Zigbee,")
        _LOGGER.error(f"un problema con el firmware del dongle ({SERIAL_PORT}),")
        _LOGGER.error("o un error en la configuración de bellows.")
        _LOGGER.debug(f"app_config utilizada: {json.dumps(app_config, default=str)}")
    finally:
        _LOGGER.info("Iniciando proceso de cierre de la aplicación del controlador Zigbee...")
        if app and hasattr(app, 'shutdown'):
            try:
                # ControllerApplication general de zigpy usa app.state
                if hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
                    _LOGGER.info("Llamando a app.shutdown()...")
                    await app.shutdown()
                    _LOGGER.info("app.shutdown() completado.")
                else:
                    _LOGGER.info(f"La aplicación no estaba en estado RUNNING (estado actual: {app.state if hasattr(app, 'state') else 'desconocido'}) o ya se cerró, omitiendo shutdown explícito aquí.")
            except Exception as e_shutdown:
                _LOGGER.exception("Error durante app.shutdown(): %s", e_shutdown)
        elif app:
            _LOGGER.warning("La instancia 'app' existe pero no parece tener método shutdown o estado adecuado.")
        else:
            _LOGGER.info("La instancia de la aplicación ('app') no fue creada o asignada exitosamente.")
        _LOGGER.info("Proceso de cierre finalizado.")

async def signal_handler_async(sig, stop_event):
    _LOGGER.info("Recibida señal de salida %s. Iniciando cierre...", sig.name)
    if not stop_event.is_set():
        stop_event.set()

if __name__ == "__main__":
    if not RADIO_LIBRARIES_AVAILABLE:
        sys.exit(1)

    # Verificar dependencias de pyserial
    try:
        if 'serial' not in sys.modules:
            print("Error: módulo pyserial no encontrado. Instalar con: pip install pyserial")
            sys.exit(1)
        try:
            if not hasattr(serial.tools.list_ports, 'comports'):
                raise ImportError("serial.tools.list_ports.comports no encontrado.")
        except ImportError:
            print("Error: serial.tools.list_ports.comports no encontrado. Asegúrate de tener pyserial >= 2.6.")
            print("Instalar o actualizar con: pip install --upgrade pyserial")
            sys.exit(1)
    except Exception as dep_check_error:
        _LOGGER.error(f"Error durante la verificación de dependencias: {dep_check_error}")
        sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt (Ctrl+C) recibido. El cierre es manejado por la señal SIGINT o el finally de main.")
    except Exception as e:
        _LOGGER.exception("Excepción no controlada en el nivel superior del script: %s", e)
    finally:
        _LOGGER.info("Programa finalizado desde el bloque __main__.")

#--- END OF FILE sensor_gateway_ezsp.py ---
