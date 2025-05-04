import asyncio
import logging
import signal
import json
import requests # Asegúrate de que esta librería esté instalada (pip install requests)
from datetime import datetime, timedelta
import time
import sys
# --- MODIFICADO: Añadida importación de serial ---
import serial

# --- Importaciones Zigpy y Bellows ---
from zigpy.application import ControllerApplication
import zigpy.config as zigpy_config
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
# --- MODIFICADO: Importaciones específicas de bellows ---
try:
    import bellows.zigbee.application
    from bellows.exception import EzspError
    RADIO_LIBRARIES_AVAILABLE = True
except ImportError:
    RADIO_LIBRARIES_AVAILABLE = False
    print("Advertencia: Biblioteca Bellows no instalada. Instalar con: pip install bellows")
import zigpy.device

# --- Configuración ---
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# Probablemente sea /dev/ttyACM0 o /dev/ttyUSB0. ¡¡¡VERIFICA CUÁL ES EN TU PI!!!
SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 115200 # Esta velocidad suele ser correcta para el Sonoff Dongle Plus
DATABASE_PATH = 'zigbee.db'
AWS_API_ENDPOINT = 'https://lbbcoc4xnd.execute-api.ap-southeast-2.amazonaws.com/dev/reading/save'

# --- Constantes del Cluster Personalizado ---
CUSTOM_CLUSTER_ID = 0xFC01
ATTR_ID_CURRENT_SENSOR_1 = 0x0001
ATTR_ID_CURRENT_SENSOR_2 = 0x0002
ATTR_ID_CURRENT_SENSOR_3 = 0x0003

# --- Estado Global ---
app = None # La aplicación del controlador Zigbee
sensor_data_cache = {} # Guarda los últimos valores recibidos por sensor {ieee: {attr_id: value}}
last_send_time = {} # Guarda cuándo se envió por última vez para cada sensor {ieee: timestamp}
SEND_INTERVAL_SECONDS = 5 # Intervalo mínimo entre envíos por sensor (en segundos)

# --- Listener para eventos Zigpy ---
class ZigbeeListener:
    # --- Métodos device_joined, device_left, device_removed, attribute_updated ---
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

# --- Función send_to_aws ---
def send_to_aws(device_ieee, data):
    """Formatea y envía los datos recolectados a la API de AWS."""
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

# --- Función check_and_send_data ---
def check_and_send_data(device):
    """Verifica si tenemos los 3 datos de un sensor y si ha pasado el intervalo para enviar."""
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

# --- Función principal Asíncrona ---
async def main():
    # --- MODIFICADO: Sección completamente reescrita ---
    global app # Hacemos referencia a la variable global 'app'

    # Verificar si las bibliotecas requeridas están instaladas
    if not RADIO_LIBRARIES_AVAILABLE:
        _LOGGER.error("Las bibliotecas de radio requeridas (bellows) no están instaladas. Por favor, instálalas primero.")
        return

    # Configuración del dispositivo (puerto, velocidad y control de flujo)
    device_config = {
        'path': SERIAL_PORT,
        'baudrate': BAUD_RATE,
        'flow_control': 'software', # 'software' (xonxoff) o 'hardware' (rtscts)
    }

    # Configuración general de la aplicación Zigpy
    app_config = {
        'database_path': DATABASE_PATH,
        'device': device_config,
    }

    _LOGGER.info("Intentando crear la aplicación del controlador...")
    
    try:
        # Primero, verificar si el puerto serial está disponible
        try:
            # Intentar abrir el puerto serial para verificar si existe
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            ser.close()
            _LOGGER.info(f"El puerto serial {SERIAL_PORT} está disponible")
        except serial.SerialException as se:
            _LOGGER.error(f"No se puede abrir el puerto serial {SERIAL_PORT}: {se}")
            _LOGGER.error("La antena Zigbee Sonoff parece estar desconectada o no configurada correctamente")
            return

        # Ahora intentar crear la aplicación del controlador usando bellows específicamente
        app = await bellows.zigbee.application.ControllerApplication.new(config=app_config)
        _LOGGER.info("Instancia de ControllerApplication creada exitosamente.")

        # Creamos y añadimos nuestro listener
        listener = ZigbeeListener()
        app.add_listener(listener)

        # Iniciamos la aplicación (esto intenta conectar con la radio, inicializa la red, etc.)
        _LOGGER.info("Iniciando la aplicación del controlador Zigbee (startup)...")
        await app.startup(auto_form=True)
        _LOGGER.info("Controlador Zigbee iniciado exitosamente. Esperando dispositivos y datos...")

        # Mantenemos el script corriendo indefinidamente
        stop_event = asyncio.Event()
        await stop_event.wait() # Espera hasta que stop_event.set() sea llamado (o el script sea interrumpido)

    except EzspError as ezsp_err:
        _LOGGER.error(f"Error EZSP: {ezsp_err}")
        _LOGGER.error("Esto generalmente indica un problema de comunicación con el adaptador Zigbee")
        _LOGGER.error("Verifica que el Sonoff Dongle Plus esté conectado correctamente y no esté siendo utilizado por otra aplicación")
    except serial.SerialException as se:
        _LOGGER.error(f"Error del puerto serial: {se}")
        _LOGGER.error("No se puede comunicar con el adaptador Zigbee. Verifica las conexiones y permisos.")
    except Exception as e:
        # Capturamos errores durante startup o la espera principal
        _LOGGER.exception("Error durante la inicialización (startup) o ejecución principal: %s", e)
    finally:
        _LOGGER.info("Iniciando proceso de cierre de la aplicación del controlador Zigbee...")
        # Verificamos si app se inicializó y si está en un estado que permita shutdown
        if app and hasattr(app, 'shutdown'):
             try:
                 # Comprobamos si está corriendo antes de intentar cerrar para evitar errores
                 if hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
                     _LOGGER.info("Llamando a app.shutdown()...")
                     await app.shutdown()
                     _LOGGER.info("app.shutdown() completado.")
                 else:
                     _LOGGER.info("La aplicación no estaba en estado RUNNING, omitiendo shutdown.")
             except Exception as e_shutdown:
                 _LOGGER.exception("Error durante app.shutdown(): %s", e_shutdown)
        elif app:
             _LOGGER.warning("La instancia 'app' existe pero no parece tener método shutdown o estado adecuado.")
        else:
             _LOGGER.info("La instancia de la aplicación ('app') no fue creada o asignada exitosamente.")
        _LOGGER.info("Proceso de cierre finalizado.")
    # --- FIN DE SECCIÓN MODIFICADA ---

# --- Punto de entrada y manejo de cierre ---
async def shutdown(sig, loop):
    """Rutina de cierre ordenado."""
    _LOGGER.info("Recibida señal de salida %s. Iniciando cierre...", sig.name)
    # Detener el bucle principal si está esperando en stop_event.wait()
    # Buscamos el evento stop_event si existe en las variables locales/globales.
    # Esto es un poco más complejo, una forma simple es parar el loop directamente.
    # Pero cancelar tareas es más limpio.

    tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not asyncio.current_task()]
    if tasks:
        _LOGGER.info("Cancelando %d tareas pendientes...", len(tasks))
        [task.cancel() for task in tasks]
        try:
            # Esperamos a que las tareas terminen (o lancen CancelledError)
            await asyncio.gather(*tasks, return_exceptions=True)
            _LOGGER.info("Tareas canceladas.")
        except asyncio.CancelledError:
            _LOGGER.info("Excepción CancelledError manejada durante gather.") # Esperado

    # Intentar cerrar la aplicación Zigbee explícitamente si existe y está corriendo
    # (Aunque el bloque finally en main ya lo intenta, hacerlo aquí asegura que se intente al recibir la señal)
    if app and hasattr(app, 'shutdown') and hasattr(app, 'state') and app.state == zigpy.application.ControllerApplication.State.RUNNING:
        _LOGGER.info("Llamando a app.shutdown() desde la rutina de cierre...")
        try:
            await app.shutdown()
            _LOGGER.info("app.shutdown() completado desde la rutina de cierre.")
        except Exception as e_shutdown:
            _LOGGER.exception("Error durante app.shutdown() en rutina de cierre: %s", e_shutdown)

    # Detener el bucle de eventos
    if loop.is_running():
        _LOGGER.info("Deteniendo el bucle de eventos...")
        loop.stop()
    _LOGGER.info("Cierre iniciado por señal %s completado.", sig.name)


if __name__ == "__main__":
    # --- MODIFICADO: Añadida verificación de pyserial ---
    try:
        # Verificar si pyserial está instalado
        if 'serial' not in sys.modules:
            print("Error: módulo pyserial no encontrado. Instalar con: pip install pyserial")
            sys.exit(1)
            
        # Configurar manejo de señales
        loop = asyncio.get_event_loop()
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(
                s, lambda s=s: asyncio.create_task(shutdown(s, loop))
            )
            
        # Ejecutar la función principal
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt recibido. El cierre debería manejarse en 'finally' o 'shutdown'.")
    except Exception as e:
        _LOGGER.exception("Excepción no controlada en el nivel superior: %s", e)
    finally:
        _LOGGER.info("Programa finalizado.")