import asyncio
import logging
import signal
import json
import requests # Asegúrate de que esta librería esté instalada (pip install requests)
from datetime import datetime

# --- Importaciones Zigpy y Bellows ---
# (Sin cambios aquí)
from zigpy.application import ControllerApplication
import zigpy.config as zigpy_config
import zigpy.endpoint
import zigpy.profiles
import zigpy.types
import zigpy.zcl
import zigpy.zcl.foundation as zcl_f
import bellows.config as bellows_config
import bellows.ezsp

# --- Configuración ---
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# --- ¡¡¡ CONFIGURA ESTO !!! ---
SERIAL_PORT = '/dev/ttyACM0' # O el puerto correcto de tu Dongle
BAUD_RATE = 115200
DATABASE_PATH = 'zigbee.db'
# <--- CAMBIO: URL de la API actualizada
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

# --- Listener para eventos Zigpy ---

class ZigbeeListener:
    # ... (device_joined, device_left, device_removed) ...
    def device_joined(self, device):
        _LOGGER.info("Dispositivo unido: %s", device)

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

    # --- Callback principal para recibir datos ---
    def attribute_updated(self, device, profile_id, cluster_id, endpoint_id, attrid, value):
        """Se llama cuando un atributo ZCL es actualizado (p.ej., por un reporte)."""
        # (Lógica interna sin cambios: debug log, filtro por cluster/attr, almacenar en caché, llamar a check_and_send)
        _LOGGER.debug(
            "Actualización de atributo recibida de %s (Endpoint: %d): Cluster: 0x%04x, Atributo: 0x%04x, Valor: %s",
            device.ieee, endpoint_id, cluster_id, attrid, value
        )

        if cluster_id == CUSTOM_CLUSTER_ID:
            if attrid in [ATTR_ID_CURRENT_SENSOR_1, ATTR_ID_CURRENT_SENSOR_2, ATTR_ID_CURRENT_SENSOR_3]:
                _LOGGER.info(
                    "Dato de corriente recibido de %s: Attr 0x%04x = %.3f A",
                    device.ieee, attrid, value
                )
                if device.ieee not in sensor_data_cache:
                    sensor_data_cache[device.ieee] = {}
                sensor_data_cache[device.ieee][attrid] = value
                check_and_send_data(device)


# --- Función para enviar datos a AWS ---

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

    _LOGGER.info("Enviando a AWS: %s", json.dumps(payload_clean))

    # Headers solo con Content-Type
    headers = {
        'Content-Type': 'application/json'
    }

    try:
        # Se usa la variable AWS_API_ENDPOINT actualizada
        response = requests.post(AWS_API_ENDPOINT, headers=headers, json=payload_clean, timeout=10)
        response.raise_for_status()
        _LOGGER.info("Datos enviados a AWS exitosamente (Status: %d)", response.status_code)
        last_send_time[device_ieee] = datetime.utcnow()

    except requests.exceptions.RequestException as e:
        _LOGGER.error("Error al enviar datos a AWS: %s", e)
    except Exception as e:
        _LOGGER.error("Error inesperado durante envío a AWS: %s", e)


# --- Función para verificar caché y enviar ---
def check_and_send_data(device):
    # ... (lógica sin cambios: verificar 3 attrs, verificar intervalo, llamar a send_to_aws) ...
    global sensor_data_cache, last_send_time
    dev_ieee = device.ieee
    if dev_ieee in sensor_data_cache and \
       ATTR_ID_CURRENT_SENSOR_1 in sensor_data_cache[dev_ieee] and \
       ATTR_ID_CURRENT_SENSOR_2 in sensor_data_cache[dev_ieee]