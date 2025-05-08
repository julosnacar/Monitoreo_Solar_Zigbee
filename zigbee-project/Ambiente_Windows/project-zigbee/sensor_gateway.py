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
import zigpy.config as zigpy_config
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
import zigpy.device

# --- MODIFICADO: Cambiar de Bellows a zigpy-znp ---
try:
    import zigpy_znp.zigbee.application
    import zigpy_znp.config as znp_config # Para configuraciones específicas de ZNP si son necesarias
    # from zigpy_znp.api import ZNPNotRunningError # Ejemplo de error específico de ZNP
    RADIO_LIBRARIES_AVAILABLE = True
except ImportError:
    RADIO_LIBRARIES_AVAILABLE = False
    print("Advertencia: Biblioteca zigpy-znp no instalada. Instalar con: pip install zigpy-znp")
# --- FIN DE MODIFICACIÓN ---


# --- Configuración ---
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# SERIAL_PORT será detectado automáticamente.
BAUD_RATE = 115200 # Esta velocidad suele ser correcta para el Sonoff Dongle Plus y E
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
    """Intenta encontrar el puerto serie del Sonoff Dongle automáticamente."""
    # VID y PID típicos para el puente CP210x usado en muchos dongles Sonoff
    # Silicon Labs (VID=10C4), CP210X UART Bridge (PID=EA60)
    # Este VID/PID es usado tanto por ZBDongle-P (con EFR32) como por ZBDongle-E (con CC2652P)
    # ya que ambos usan un chip CP210x o variante (CP2102N para el Dongle-E) como puente USB.
    SONOFF_VID = 0x10C4
    SONOFF_PID = 0xEA60

    ports = serial.tools.list_ports.comports()
    _LOGGER.info("Buscando puertos serie disponibles...")
    sonoff_port = None
    for port in ports:
        _LOGGER.debug(
            f"Puerto detectado: {port.device} - {port.description} "
            f"[VID:{port.vid:04X} PID:{port.pid:04X} SER:{port.serial_number} HWID:{port.hwid}]"
        )
        if port.vid == SONOFF_VID and port.pid == SONOFF_PID:
            # Esta detección es para el chip puente USB-UART, no para el chip Zigbee en sí.
            # Funcionará para Dongle-P y Dongle-E. La biblioteca (bellows/znp) se encarga del resto.
            _LOGGER.info(f"Dongle Sonoff (con chip CP210x/CP2102N) encontrado en: {port.device} (Descripción: {port.description})")
            sonoff_port = port.device
            break

    if sonoff_port:
        return sonoff_port
    else:
        _LOGGER.error("No se pudo encontrar automáticamente un dongle Sonoff Zigbee con puente CP210x/CP2102N.")
        _LOGGER.warning("Asegúrate de que el dongle (modelo ZBDongle-P o ZBDongle-E) esté conectado,")
        _LOGGER.warning("los controladores CP210x/CP2102N estén instalados (especialmente en Windows),")
        _LOGGER.warning("y que no esté siendo utilizado por otra aplicación.")
        _LOGGER.warning("Puedes intentar especificar el puerto manualmente si conoces el puerto COM o /dev/tty*.")
        return None

async def main():
    global app

    if not RADIO_LIBRARIES_AVAILABLE:
        _LOGGER.error("La biblioteca de radio requerida (zigpy-znp) no está instalada. Por favor, instálala primero.")
        return

    SERIAL_PORT = find_sonoff_dongle_port()
    if not SERIAL_PORT:
        _LOGGER.error("No se pudo determinar el puerto del dongle Zigbee. Abortando.")
        return
    _LOGGER.info(f"Usando el puerto serie detectado: {SERIAL_PORT}")

    # --- MODIFICADO: Configuración para zigpy-znp ---
    # Para zigpy-znp, el control de flujo ('flow_control') a menudo no es necesario o se prefiere 'software'.
    # Si se omite 'flow_control', zigpy-znp usará su valor predeterminado.
    # Para CC2652P, 'software' rtscts (si es soportado por el firmware del dongle) puede ser una opción,
    # pero a menudo no es necesario configurar explícitamente.
    # Probemos sin 'flow_control' primero, o con 'software'.
    znp_device_config = {
        'path': SERIAL_PORT,
        'baudrate': BAUD_RATE,
        #'rtscts': True  # Habilita el control de flujo por hardware
        #'flow_control': 'software', # Podrías probar con 'software' o dejarlo comentado para el default de ZNP
                                    # Para los dongles CC2652P de Sonoff, a menudo no se necesita especificar flow_control
    }

    app_config = {
        zigpy_config.CONF_DATABASE: DATABASE_PATH,
        zigpy_config.CONF_DEVICE: znp_device_config,
        # Opcional: configuraciones específicas de ZNP si las necesitas, por ejemplo:
        # znp_config.CONF_ZNP_NVRAM_BACKUP: True, # Para hacer backup de NVRAM
    }
    # --- FIN DE MODIFICACIÓN ---
    
    logging.getLogger('zigpy_znp').setLevel(logging.DEBUG)
    logging.getLogger('zigpy.application').setLevel(logging.DEBUG)
    logging.getLogger('zigpy_znp.api').setLevel(logging.DEBUG)
    logging.getLogger('zigpy_znp.uart').setLevel(logging.DEBUG)
    _LOGGER.info("Intentando crear la aplicación del controlador...")
    _LOGGER.debug(f"Configuración de la aplicación para zigpy-znp: {app_config}")

    try:
        # --- MODIFICADO: Usar zigpy_znp.zigbee.application ---
        app = await zigpy_znp.zigbee.application.ControllerApplication.new(
            config=app_config,
            auto_form=True
        )
        # --- FIN DE MODIFICACIÓN ---
        _LOGGER.info("Instancia de ControllerApplication (zigpy-znp) creada exitosamente.")

        listener = ZigbeeListener()
        app.add_listener(listener)

        _LOGGER.info("Iniciando la aplicación del controlador Zigbee (startup)...")
        await app.startup(auto_form=True) # auto_form=True intentará formar una nueva red si es necesario
        _LOGGER.info("Controlador Zigbee iniciado exitosamente. Esperando dispositivos y datos...")

        stop_event = asyncio.Event()
        await stop_event.wait()

    # --- MODIFICADO: Cambiar el tipo de error específico de radio ---
    # EzspError es específico de bellows. Usar una excepción más general o una específica de ZNP si la conoces.
    # Por ahora, Exception es suficiente para capturar errores de inicialización de radio.
    except serial.SerialException as se:
        _LOGGER.error(f"Error del puerto serial: {se}")
        _LOGGER.error(f"No se puede comunicar con el adaptador Zigbee en {SERIAL_PORT}.")
        _LOGGER.error("Verifica las conexiones, permisos y que los controladores (CP210x/CP2102N) estén instalados.")
    except Exception as radio_init_error: # Captura general para errores de radio o configuración
        _LOGGER.error(f"Error de inicialización de radio o configuración: {radio_init_error}")
        _LOGGER.error("Esto puede indicar un problema de comunicación con el adaptador Zigbee,")
        _LOGGER.error(f"un problema con el firmware del dongle ({SERIAL_PORT}),")
        _LOGGER.error("o un error en la configuración de zigpy-znp.")
        _LOGGER.debug(f"app_config utilizada: {app_config}")
    # except Exception as e: # Ya estaba este más general
    #     _LOGGER.exception("Error durante la inicialización (startup) o ejecución principal: %s", e)
    # --- FIN DE MODIFICACIÓN ---
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
        elif app:
            _LOGGER.warning("La instancia 'app' existe pero no parece tener método shutdown o estado adecuado.")
        else:
            _LOGGER.info("La instancia de la aplicación ('app') no fue creada o asignada exitosamente.")
        _LOGGER.info("Proceso de cierre finalizado.")


async def shutdown(sig, loop):
    _LOGGER.info("Recibida señal de salida %s. Iniciando cierre...", sig.name)
    global app
    if app and hasattr(app, 'shutdown') and hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
        _LOGGER.info("Llamando a app.shutdown() desde la rutina de cierre...")
        try:
            await app.shutdown()
            _LOGGER.info("app.shutdown() completado desde la rutina de cierre.")
        except Exception as e_shutdown:
            _LOGGER.exception("Error durante app.shutdown() en rutina de cierre: %s", e_shutdown)

    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task()]
    if tasks:
        _LOGGER.info("Cancelando %d tareas pendientes...", len(tasks))
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            _LOGGER.info("Tareas canceladas.")
        except asyncio.CancelledError:
            _LOGGER.info("Excepción CancelledError manejada durante gather (esperado).")

    if loop.is_running():
        _LOGGER.info("Deteniendo el bucle de eventos...")
        loop.stop()
    _LOGGER.info("Cierre iniciado por señal %s completado.", sig.name)


if __name__ == "__main__":
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
            
        loop = asyncio.get_event_loop() # Sigue generando DeprecationWarning en Python >= 3.10
                                        # La forma moderna es asyncio.run(main()) y manejar señales de otra manera
                                        # o usar loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
                                        # pero para este script, get_event_loop() aún funciona.

        signals_to_handle = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, 'SIGHUP'):
            signals_to_handle.append(signal.SIGHUP)
        
        for s in signals_to_handle:
            try:
                loop.add_signal_handler(
                    s, lambda sig=s: asyncio.create_task(shutdown(sig, loop))
                )
            except (NotImplementedError, AttributeError, RuntimeError): # RuntimeError puede ocurrir en Windows con ProactorEventLoop
                _LOGGER.warning(f"No se pudo registrar el manejador para la señal {s.name}. Puede no estar disponible en este SO o con el loop actual.")
            
        loop.run_until_complete(main())

    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt recibido. El cierre debería ser manejado por la señal SIGINT.")
        if loop.is_running():
            loop.stop()
    except Exception as e:
        _LOGGER.exception("Excepción no controlada en el nivel superior: %s", e)
    finally:
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            _LOGGER.info("Cerrando el bucle de eventos...")
            remaining_tasks = asyncio.all_tasks(loop=loop)
            if remaining_tasks:
                _LOGGER.info(f"Cancelando {len(remaining_tasks)} tareas restantes antes de cerrar el bucle.")
                for task in remaining_tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*remaining_tasks, return_exceptions=True))
            loop.close()
            _LOGGER.info("Bucle de eventos cerrado.")
        _LOGGER.info("Programa finalizado.")