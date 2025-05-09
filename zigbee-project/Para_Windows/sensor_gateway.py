# --- START OF CORRECTED SCRIPT sensor_gateway.py ---
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

# --- Importaciones Zigpy ---
from zigpy.application import ControllerApplication
import zigpy.application
import zigpy.config as zigpy_config # zigpy.config es general
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
import zigpy.device

# --- Biblioteca de Radio (Bellows para EZSP) ---
try:
    import bellows.ezsp # Para verificar que bellows está disponible y su submódulo principal
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

app = None
sensor_data_cache = {}
last_send_time = {}
SEND_INTERVAL_SECONDS = 5

class ZigbeeListener:
    # ... (el contenido de la clase ZigbeeListener no cambia) ...
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
    # ... (el contenido de send_to_aws no cambia) ...
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
    # ... (el contenido de check_and_send_data no cambia) ...
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
    # ... (el contenido de find_sonoff_dongle_port no cambia, es genérico para CP210x) ...
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
        if port.vid == SONOFF_VID and port.pid == SONOFF_PID:
            _LOGGER.info(f"Dongle Sonoff (con chip puente CP210x/CP2102N) encontrado en: {port.device} (Descripción: {port.description})")
            sonoff_port = port.device
            break
        elif port.description and ("CP2102N" in port.description or "Silicon Labs CP210x" in port.description):
            _LOGGER.info(f"Posible Dongle Sonoff encontrado por descripción en: {port.device} (Descripción: {port.description})")
            if not sonoff_port:
                sonoff_port = port.device
    if sonoff_port:
        return sonoff_port
    else:
        _LOGGER.error("No se pudo encontrar automáticamente un dongle Sonoff Zigbee (ZBDongle-E o ZBDongle-P) con puente CP210x/CP2102N.")
        # ... (resto de mensajes de error) ...
        return None

async def main():
    global app

    if not RADIO_LIBRARIES_AVAILABLE:
        _LOGGER.error("La biblioteca de radio requerida ('bellows') no está instalada. Por favor, instálala primero con 'pip install bellows'.")
        return

    SERIAL_PORT = find_sonoff_dongle_port()
    if not SERIAL_PORT:
        _LOGGER.error("No se pudo determinar el puerto del dongle Zigbee. Abortando.")
        return
    _LOGGER.info(f"Usando el puerto serie detectado: {SERIAL_PORT}")

    # --- CONFIGURACIÓN PARA BELLOWS (EZSP) ---
    # zigpy necesita saber qué tipo de radio usar. Esto se hace a través de la configuración del dispositivo.
    # La configuración del dispositivo para bellows se pasa directamente.
    device_config_ezsp = {
        bellows_config.CONF_DEVICE_PATH: SERIAL_PORT,
        "baudrate": BAUD_RATE,
        "flow_control": "hardware",
        # No suelen necesitarse 'flow_control' explícito para EZSP con ZBDongle-E
    }

    # La configuración general de la aplicación zigpy
    app_config = {
        # zigpy_config.CONF_DEVICE_TYPE: 'ezsp', # Ya no es necesario especificar explícitamente, zigpy lo deduce si bellows está.
        zigpy_config.CONF_DATABASE: DATABASE_PATH, # Corregido el nombre de la clave
        zigpy_config.CONF_DEVICE: device_config_ezsp,
        # Puedes añadir configuraciones específicas de bellows/ezsp aquí si son necesarias
        # por ejemplo, algunos firmwares antiguos podrían necesitar:
        # bellows_config.CONF_EZSP_RESET_METHOD: bellows_config.EZSP_RESET_METHOD.RESET_SW,
    }
    # --- FIN DE CONFIGURACIÓN PARA BELLOWS ---

    logging.getLogger('bellows').setLevel(logging.DEBUG) # Para la capa de bajo nivel EZSP (bellows)
    logging.getLogger('zigpy.application').setLevel(logging.DEBUG)
    # logging.getLogger('bellows.uart').setLevel(logging.DEBUG) # Para depuración UART muy detallada

    _LOGGER.info("Intentando crear la aplicación del controlador (usando bellows para EZSP)...")
    _LOGGER.debug(f"Configuración de la aplicación para EZSP (bellows): {app_config}")

    try:
        # --- USAR ControllerApplication GENERAL DE ZIGPY ---
        # zigpy buscará automáticamente el tipo de radio adecuado (bellows en este caso)
        # basado en la configuración proporcionada y las bibliotecas instaladas.
        app = await bellows_app.ControllerApplication.new(
            config=app_config,
            auto_form=True
        )
        # --- FIN DE MODIFICACIÓN ---
        _LOGGER.info("Instancia de ControllerApplication (EZSP/bellows) creada exitosamente.")

        listener = ZigbeeListener()
        app.add_listener(listener)

        _LOGGER.info("Iniciando la aplicación del controlador Zigbee (startup)...")
        await app.startup(auto_form=True)
        _LOGGER.info("Controlador Zigbee iniciado exitosamente. Esperando dispositivos y datos...")

        stop_event_main = asyncio.Event()
        await stop_event_main.wait()

    except serial.SerialException as se:
        _LOGGER.error(f"Error del puerto serial: {se}")
        # ... (resto del manejo de errores) ...
    except Exception as radio_init_error:
        _LOGGER.error(f"Error de inicialización de radio o configuración (EZSP/bellows): {radio_init_error}", exc_info=True)
        # ... (resto del manejo de errores) ...
    finally:
        # ... (el bloque finally no cambia significativamente en su lógica) ...
        _LOGGER.info("Iniciando proceso de cierre de la aplicación del controlador Zigbee (desde el finally de main)...")
        if app and hasattr(app, 'shutdown'):
            try:
                if hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
                    _LOGGER.info("Llamando a app.shutdown()...")
                    await app.shutdown()
                    _LOGGER.info("app.shutdown() completado.")
                else:
                    _LOGGER.info(f"La aplicación no estaba en estado RUNNING (estado actual: {app.state if hasattr(app, 'state') else 'desconocido'}) o ya se cerró, omitiendo shutdown explícito aquí.")
            except Exception as e_shutdown:
                _LOGGER.exception("Error durante app.shutdown() en el 'finally' de main: %s", e_shutdown)
        elif app:
            _LOGGER.warning("La instancia 'app' existe pero no parece tener método shutdown o estado adecuado.")
        else:
            _LOGGER.info("La instancia de la aplicación ('app') no fue creada o asignada exitosamente.")
        _LOGGER.info("Proceso de cierre finalizado (desde el finally de main).")

# ... (El resto del script: shutdown(), if __name__ == "__main__" se mantiene similar en estructura) ...
# Asegúrate de que la función shutdown y el bloque if __name__ == "__main__" estén
# como en la versión anterior, ya que manejan el ciclo de vida de asyncio y las señales.
# Por brevedad, no lo repito aquí, pero no deberían necesitar cambios drásticos por usar bellows.

async def shutdown(sig, loop, stop_event_main_ref=None):
    _LOGGER.info("Recibida señal de salida %s. Iniciando cierre (rutina shutdown)...", sig.name)
    global app
    for s_to_clear in [signal.SIGTERM, signal.SIGINT] + ([signal.SIGHUP] if hasattr(signal, 'SIGHUP') else []):
        try:
            loop.remove_signal_handler(s_to_clear)
        except Exception:
            pass
    if stop_event_main_ref and not stop_event_main_ref.is_set():
        _LOGGER.info("Activando stop_event_main para desbloquear la función main.")
        stop_event_main_ref.set()
    if loop.is_running():
        _LOGGER.info("Deteniendo el bucle de eventos desde la rutina de cierre (shutdown)...")
        loop.stop()
    _LOGGER.info("Cierre iniciado por señal %s completado (rutina shutdown).", sig.name)


if __name__ == "__main__":
    if not RADIO_LIBRARIES_AVAILABLE:
        sys.exit(1)
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

    loop = asyncio.get_event_loop()
    stop_event_for_main_task = asyncio.Event()
    signals_to_handle = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, 'SIGHUP'):
        signals_to_handle.append(signal.SIGHUP)
    for s in signals_to_handle:
        try:
            loop.add_signal_handler(
                s, lambda sig=s: asyncio.create_task(shutdown(sig, loop, stop_event_for_main_task))
            )
        except (NotImplementedError, AttributeError, RuntimeError):
            _LOGGER.warning(f"No se pudo registrar el manejador para la señal {s.name}.")
    
    main_task = None
    try:
        _LOGGER.info("Iniciando la tarea principal (main)...")
        main_task = loop.create_task(main())
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt (Ctrl+C) recibido.")
        if not stop_event_for_main_task.is_set():
             stop_event_for_main_task.set()
        if main_task and not main_task.done():
            main_task.cancel()
        if loop.is_running():
            loop.stop()
    except asyncio.CancelledError:
        _LOGGER.info("Tarea principal cancelada.")
    except Exception as e:
        _LOGGER.exception("Excepción no controlada en el nivel superior: %s", e)
    finally:
        _LOGGER.info("Bloque 'finally' principal alcanzado.")
        if loop.is_running():
            loop.stop()
        if main_task and not main_task.done():
            try:
                loop.run_until_complete(main_task)
            except asyncio.CancelledError:
                _LOGGER.info("Tarea principal finalizada por cancelación.")
            except Exception as e_main_task:
                _LOGGER.error(f"La tarea principal finalizó con error: {e_main_task}")
        _LOGGER.info("Recopilando tareas restantes...")
        remaining_tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task(loop=loop)]
        if remaining_tasks:
            _LOGGER.info(f"Cancelando {len(remaining_tasks)} tareas restantes...")
            for task in remaining_tasks:
                task.cancel()
            try:
                loop.run_until_complete(asyncio.gather(*remaining_tasks, return_exceptions=True))
                _LOGGER.info("Tareas restantes procesadas.")
            except Exception as e_gather:
                 _LOGGER.error(f"Error durante gather de tareas restantes: {e_gather}")
        if not loop.is_closed():
            _LOGGER.info("Cerrando el bucle de eventos...")
            loop.close()
            _LOGGER.info("Bucle de eventos cerrado.")
        else:
            _LOGGER.info("El bucle de eventos ya estaba cerrado.")
        _LOGGER.info("Programa finalizado.")
# --- END OF CORRECTED SCRIPT ---