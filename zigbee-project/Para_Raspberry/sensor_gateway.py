import asyncio
import logging
import random
import signal
from typing import Dict, Any

# --- Configuración ---
DEVICE_PATH = '/dev/ttyUSB0'
BAUDRATE = 115200
FLOW_CONTROL = None
PERMIT_JOIN_DURATION_ON_STARTUP = 180

ESP32_H2_ENDPOINT_ID = 1
CUSTOM_CLUSTER_ID = 0xFC01
ATTR_ID_CURRENT_SENSOR_1 = 0x0001
ATTR_ID_CURRENT_SENSOR_2 = 0x0002
ATTR_ID_CURRENT_SENSOR_3 = 0x0003

REPORTING_MIN_INTERVAL = 10
REPORTING_MAX_INTERVAL = 60
REPORTABLE_CURRENT_CHANGE = 0.05

import zigpy.config as zigpy_config
import zigpy.exceptions
import zigpy.device as zigpy_dev
import zigpy.endpoint as zigpy_ep
import zigpy.zcl.foundation as zcl_f
from zigpy.zcl import Cluster
import zigpy.types as t

try:
    from bellows.zigbee.application import ControllerApplication as BellowsApplication
except ImportError:
    print("Error: La biblioteca 'bellows' no está instalada.")
    BellowsApplication = None

class CustomPowerSensorCluster(Cluster):
    cluster_id = CUSTOM_CLUSTER_ID
    attributes = {
        ATTR_ID_CURRENT_SENSOR_1: ("current_sensor_1", t.Single),
        ATTR_ID_CURRENT_SENSOR_2: ("current_sensor_2", t.Single),
        ATTR_ID_CURRENT_SENSOR_3: ("current_sensor_3", t.Single),
    }

if CUSTOM_CLUSTER_ID not in Cluster._registry:
     Cluster._registry[CUSTOM_CLUSTER_ID] = CustomPowerSensorCluster
else:
    if Cluster._registry[CUSTOM_CLUSTER_ID] is not CustomPowerSensorCluster:
        logging.warning(
            f"Cluster ID {CUSTOM_CLUSTER_ID:#06x} ya estaba en Cluster.registry "
            f"pero con una clase diferente ({Cluster._registry[CUSTOM_CLUSTER_ID].__name__}). "
            f"Sobrescribiendo con CustomPowerSensorCluster."
        )
        Cluster._registry[CUSTOM_CLUSTER_ID] = CustomPowerSensorCluster
    else:
        logging.info(f"Cluster ID {CUSTOM_CLUSTER_ID:#06x} (CustomPowerSensorCluster) ya estaba en Cluster.registry.")

shutdown_event = asyncio.Event()

class SensorAttributeListener:
    def __init__(self, device_ieee: t.EUI64):
        self.device_ieee = device_ieee
        self._last_values: Dict[int, float] = {}

    def attribute_updated(self, cluster: Cluster, attribute_id: int, value: Any, timestamp):
        print(f"DEBUG: Se recibió una actualización para cluster {cluster.cluster_id:#06x}, atributo {attribute_id:#06x}, valor {value}")
        if cluster.endpoint.device.ieee != self.device_ieee or cluster.cluster_id != CUSTOM_CLUSTER_ID:
            return

        self._last_values[attribute_id] = value
        sensor_name = "Desconocido"
        if attribute_id == ATTR_ID_CURRENT_SENSOR_1:
            sensor_name = "Sensor Corriente 1"
        elif attribute_id == ATTR_ID_CURRENT_SENSOR_2:
            sensor_name = "Sensor Corriente 2"
        elif attribute_id == ATTR_ID_CURRENT_SENSOR_3:
            sensor_name = "Sensor Corriente 3"

        # Usar print en lugar de logging para asegurar que se vea
        print(f"*** LECTURA DE SENSOR: {sensor_name} (AttrID: {attribute_id:#06x}) = {value:.2f} A ***")
        logging.info(f"ACTUALIZACIÓN SENSOR ({cluster.endpoint.device.nwk:#06x}): "
                     f"{sensor_name} (AttrID: {attribute_id:#06x}) = {value:.2f} A")


class MyEventListener:
    def __init__(self, app_controller):
        self._app = app_controller
        self._sensor_listeners: Dict[t.EUI64, SensorAttributeListener] = {}

    def device_joined(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO UNIDO (info básica): {device.nwk:#06x} / {device.ieee}")

    def raw_device_initialized(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO RAW INICIALIZADO (endpoints leídos): {device.nwk:#06x} / {device.ieee}")

    async def configure_device_reporting(self, device: zigpy_dev.Device):
        if ESP32_H2_ENDPOINT_ID not in device.endpoints:
            logging.warning(f"Dispositivo {device.ieee} no tiene el endpoint {ESP32_H2_ENDPOINT_ID}")
            return

        endpoint = device.endpoints[ESP32_H2_ENDPOINT_ID]

        if CUSTOM_CLUSTER_ID not in endpoint.in_clusters:
            logging.error(
                f"Dispositivo {device.ieee} NO TIENE el cluster custom {CUSTOM_CLUSTER_ID:#06x} "
                f"en el endpoint {ESP32_H2_ENDPOINT_ID} después de la inicialización. "
                f"Clusters en input: {list(endpoint.in_clusters.keys())}"
            )
            return
        
        custom_cluster = endpoint.in_clusters[CUSTOM_CLUSTER_ID]
        if not isinstance(custom_cluster, CustomPowerSensorCluster):
            logging.error(f"Cluster {CUSTOM_CLUSTER_ID:#06x} en {device.ieee} no es del tipo CustomPowerSensorCluster esperado. "
                          f"Tipo actual: {type(custom_cluster)}. El registro del cluster puede haber fallado o sido sobrescrito.")
            return

        attributes_to_configure = {
            ATTR_ID_CURRENT_SENSOR_1: "Corriente Sensor 1",
            ATTR_ID_CURRENT_SENSOR_2: "Corriente Sensor 2",
            ATTR_ID_CURRENT_SENSOR_3: "Corriente Sensor 3",
        }

        logging.info(f"Configurando reporte de atributos para {device.ieee} en cluster {custom_cluster!r}...")
        for attr_id, attr_name in attributes_to_configure.items():
            try:
                if not custom_cluster.find_attribute(attr_id):
                    logging.error(f"  Atributo {attr_id:#06x} ({attr_name}) NO ENCONTRADO en la definición de CustomPowerSensorCluster.")
                    continue

                
                response_tuple = await custom_cluster.configure_reporting(
                    attr_id,
                    REPORTING_MIN_INTERVAL,
                    REPORTING_MAX_INTERVAL,
                    REPORTABLE_CURRENT_CHANGE,
                )
                
                if response_tuple and len(response_tuple) == 2:
                    # res_header = response_tuple[0] # No la usamos directamente aquí
                    res_payload_args = response_tuple[1]

                    if hasattr(res_payload_args, 'status_records') and res_payload_args.status_records:
                        all_attr_successful = True
                        for record in res_payload_args.status_records:
                            if record.status != zcl_f.Status.SUCCESS:
                                all_attr_successful = False
                                logging.error(
                                    f"  Fallo al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}) "
                                    f"en registro específico: Status={record.status.name}, AttrID={record.attrid}, Direction={record.direction}"
                                )
                        
                        if all_attr_successful:
                            if len(res_payload_args.status_records) == 1 and res_payload_args.status_records[0].attrid is None:
                                logging.info(f"  Configurado reporte para {attr_name} (AttrID: {attr_id:#06x}) exitosamente (respuesta SUCCESS global).")
                            else:
                                logging.info(f"  Configurado reporte para {attr_name} (AttrID: {attr_id:#06x}) con registros de estado específicos: {res_payload_args.status_records}")
                    else:
                        logging.warning(f"  Respuesta sin status_records para {attr_name} (AttrID: {attr_id:#06x}): PAYLOAD_ARGS={res_payload_args}")
                elif response_tuple is None:
                    logging.error(f"  No se recibió respuesta (None) al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}). Timeout o no se esperaba respuesta?")
                else:
                    logging.warning(f"  Respuesta con formato diferente al esperado para {attr_name} (AttrID: {attr_id:#06x}): {response_tuple}")

            except ValueError as ve:
                logging.error(f"  ValueError (posiblemente atributo desconocido internamente por zigpy) al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}): {ve}")
            except Exception as e:
                logging.error(f"  Excepción general al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}): {type(e).__name__} - {e}", exc_info=True) # Poner True para traceback completo
        
        if device.ieee not in self._sensor_listeners:
            sensor_listener = SensorAttributeListener(device.ieee)
            custom_cluster.add_listener(sensor_listener)
            self._sensor_listeners[device.ieee] = sensor_listener
            logging.info(f"Listener de atributos añadido para el cluster custom de {device.ieee}")

    def device_initialized(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO COMPLETAMENTE INICIALIZADO: {device}")
        if device.nwk != 0x0000: # No configurar el propio coordinador
            # Crear una tarea para no bloquear el callback del listener
            asyncio.create_task(self.configure_device_reporting(device))

    def device_left(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO ABANDONÓ LA RED: {device}")
        if device.ieee in self._sensor_listeners and ESP32_H2_ENDPOINT_ID in device.endpoints:
            endpoint = device.endpoints[ESP32_H2_ENDPOINT_ID]
            if CUSTOM_CLUSTER_ID in endpoint.in_clusters:
                custom_cluster = endpoint.in_clusters[CUSTOM_CLUSTER_ID]
                try:
                    # Verificar si el listener está realmente en la lista antes de intentar removerlo
                    # El acceso a _listeners es interno, pero para un listener que nosotros añadimos, debería estar bien.
                    if hasattr(custom_cluster, '_listeners') and \
                       self._sensor_listeners[device.ieee] in custom_cluster._listeners.values():
                        custom_cluster.remove_listener(self._sensor_listeners[device.ieee])
                except Exception as e: 
                    logging.warning(f"Error al remover listener de {custom_cluster}: {e}")
            del self._sensor_listeners[device.ieee]
            logging.info(f"Listener de atributos removido para {device.ieee}")


    def connection_lost(self, exc: Exception):
        logging.error(f"CONEXIÓN PERDIDA con el coordinador: {exc}")
        logging.info("Intentando detener la aplicación debido a la pérdida de conexión...")
        if not shutdown_event.is_set():
            shutdown_event.set()

async def main():
    if BellowsApplication is None: return
    log_format = "%(asctime)s %(levelname)s [%(name)s] [%(module)s:%(lineno)d] %(funcName)s: %(message)s"
    logging.getLogger().setLevel(logging.DEBUG)  # Cambiar de INFO a DEBUG
    logging.basicConfig(level=logging.INFO, format=log_format)
    print(f"Intentando conectar al coordinador en: {DEVICE_PATH} a {BAUDRATE} baudios con control de flujo: {FLOW_CONTROL}.")
    app = None
    try:
        bellows_specific_config = { zigpy_config.CONF_DEVICE_PATH: DEVICE_PATH, zigpy_config.CONF_DEVICE_BAUDRATE: BAUDRATE, }
        if FLOW_CONTROL is not None: bellows_specific_config[zigpy_config.CONF_DEVICE_FLOW_CONTROL] = FLOW_CONTROL
        network_config = {}
        zigpy_general_config = { zigpy_config.CONF_DATABASE: "zigbee.db", zigpy_config.CONF_NWK_BACKUP_ENABLED: True, zigpy_config.CONF_NWK: network_config, zigpy_config.CONF_OTA: { zigpy_config.CONF_OTA_ENABLED: False, zigpy_config.CONF_OTA_PROVIDERS: [], } }
        config_para_schema = { zigpy_config.CONF_DEVICE: bellows_specific_config, **zigpy_general_config }
        final_app_config = BellowsApplication.SCHEMA(config_para_schema)
        app = BellowsApplication(config=final_app_config)
        listener = MyEventListener(app_controller=app)
        app.add_listener(listener)
        print("Iniciando aplicación del controlador Zigbee...")
        try:
            await app.startup(auto_form=False)
            print("Aplicación iniciada. Red ya estaba formada o fue cargada de la BD/dongle.")
        except zigpy.exceptions.NetworkNotFormed:
            print("La red no está formada. Intentando formar una nueva red...")
            await app.form_network()
            print("Nueva red formada. Reiniciando la aplicación para usar la nueva red...")
            await app.shutdown(db=False)
            app = BellowsApplication(config=final_app_config)
            app.add_listener(listener)
            await app.startup(auto_form=False)
            print("Aplicación reiniciada con la nueva red formada.")
        print("¡Controlador Zigbee listo y operando!")
        node_info = app.state.node_info
        network_info = app.state.network_info
        if node_info: print(f"  Coordinador IEEE: {node_info.ieee}, NWK: 0x{node_info.nwk:04x}")
        if network_info: print(f"  Red EPID: {network_info.extended_pan_id}, PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")
        if PERMIT_JOIN_DURATION_ON_STARTUP > 0:
            print(f"\nAbriendo la red para uniones durante {PERMIT_JOIN_DURATION_ON_STARTUP} segundos...")
            await app.permit(PERMIT_JOIN_DURATION_ON_STARTUP)
            print(f"La red está abierta para uniones. Se cerrará automáticamente en {PERMIT_JOIN_DURATION_ON_STARTUP}s.")
        else: print("\nLa red NO se abrirá automáticamente para uniones al inicio.")
        print("\nLa aplicación Zigbee está en funcionamiento. Presiona Ctrl+C para detener.")
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logging.info("\nInterrupción por teclado detectada. Iniciando cierre...")
        if not shutdown_event.is_set(): shutdown_event.set()
    except Exception as e:
        logging.error(f"Error general en la aplicación: {type(e).__name__}: {e}", exc_info=True)
        if not shutdown_event.is_set(): shutdown_event.set()
    finally:
        pending_tasks_in_finally = []
        if app is not None:
            logging.info("\nIniciando proceso de cierre de la aplicación Zigbee...")
            connection_ezsp_active = False
            if hasattr(app, '_ezsp') and app._ezsp is not None:
                try:
                    if app._ezsp.is_connected: connection_ezsp_active = True
                except AttributeError: logging.warning("app._ezsp.is_connected no encontrado durante el cierre.")
                except Exception as e_check_conn: logging.warning(f"Error al verificar app._ezsp.is_connected: {e_check_conn}")
            if connection_ezsp_active:
                try:
                    logging.info("Cerrando permiso de unión (permit(0))...")
                    await app.permit(0)
                except Exception as e_permit: logging.warning(f"No se pudo cerrar el permiso de unión durante el cierre: {e_permit}")
            else: logging.info("La conexión EZSP no parece activa, omitiendo app.permit(0) explícito.")
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    current_main_task = asyncio.current_task(loop=loop)
                    for task in asyncio.all_tasks(loop=loop):
                        if task is not current_main_task and not task.done(): pending_tasks_in_finally.append(task)
            except RuntimeError: logging.warning("No se pudo obtener el bucle de eventos en finally para cancelar tareas.")
            if pending_tasks_in_finally:
                logging.info(f"Cancelando {len(pending_tasks_in_finally)} tareas pendientes antes de app.shutdown()...")
                for task in pending_tasks_in_finally: task.cancel()
                try:
                    await asyncio.gather(*pending_tasks_in_finally, return_exceptions=True)
                    logging.info("Cancelación de tareas pendientes (o finalización) completada.")
                except Exception as e_gather: logging.warning(f"Error durante gather de tareas canceladas: {e_gather}")
            else: logging.info("No hay tareas pendientes activas para cancelar.")
            logging.info("Llamando a app.shutdown()...")
            try:
                await app.shutdown()
                logging.info("Proceso de cierre del controlador completado.")
            except Exception as e_shutdown: logging.error(f"Error durante app.shutdown(): {type(e_shutdown).__name__}: {e_shutdown}", exc_info=True)
        else: logging.info("\nLa instancia de la aplicación no fue creada o la conexión inicial falló.")
        logging.info("Fin del script.")

def signal_handler(sig, frame):
    logging.info(f"Señal {signal.Signals(sig).name} recibida, estableciendo evento de cierre...")
    if not shutdown_event.is_set(): shutdown_event.set()
    else: logging.warning("Evento de cierre ya establecido. Múltiples señales de interrupción recibidas.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    asyncio.run(main())