# --- START OF FILE ---
import asyncio
import logging
import random
import signal
from typing import Dict, Any

# --- Configuración ---
DEVICE_PATH = '/dev/ttyUSB0'
BAUDRATE = 115200
FLOW_CONTROL = None

# Configuración para la tarea de permiso de unión periódico
# PERMIT_JOIN_DURATION_ON_STARTUP ahora será la duración de cada ventana de unión
PERMIT_JOIN_DURATION_ON_STARTUP = 180 # Duración de cada ventana de unión (ej. 3 minutos)
REOPEN_JOIN_INTERVAL_SECONDS = 150    # Reabrir cada 150 segundos (2.5 minutos)
                                      # Asegúrate REOPEN_JOIN_INTERVAL_SECONDS < PERMIT_JOIN_DURATION_ON_STARTUP

ESP32_H2_ENDPOINT_ID = 1
CUSTOM_CLUSTER_ID = 0xFC01
ATTR_ID_CURRENT_SENSOR_1 = 0x0001
ATTR_ID_CURRENT_SENSOR_2 = 0x0002
ATTR_ID_CURRENT_SENSOR_3 = 0x0003

REPORTING_MIN_INTERVAL = 10
REPORTING_MAX_INTERVAL = 60
REPORTABLE_CURRENT_CHANGE = 0.05
import zigpy.backups
import zigpy.config as zigpy_config
import zigpy.exceptions
import zigpy.device as zigpy_dev
import zigpy.endpoint as zigpy_ep
import zigpy.zcl.foundation as zcl_f
from zigpy.zcl import Cluster
import zigpy.types as t
import zigpy.zdo.types as zdo_types
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
    def __init__(self, device_ieee: t.EUI64, owning_cluster: Cluster): # Renombrar para claridad
        self.device_ieee = device_ieee
        self._last_values: Dict[int, float] = {}
        self.owning_cluster = owning_cluster # El cluster al que este listener pertenece

    def attribute_updated(self, attribute_id: int, value: Any, timestamp: Any):

        # Comentado para reducir el ruido, pero útil para depuración avanzada si es necesario
        # print(f"DEBUG: Se recibió una actualización para cluster {self.owning_cluster.cluster_id:#06x}, atributo {attribute_id:#06x}, valor {value}")

        if self.owning_cluster.endpoint.device.ieee != self.device_ieee or self.owning_cluster.cluster_id != CUSTOM_CLUSTER_ID:
            return

        self._last_values[attribute_id] = value
        sensor_name = "Desconocido"
        if attribute_id == ATTR_ID_CURRENT_SENSOR_1:
            sensor_name = "Sensor Corriente 1"
        elif attribute_id == ATTR_ID_CURRENT_SENSOR_2:
            sensor_name = "Sensor Corriente 2"
        elif attribute_id == ATTR_ID_CURRENT_SENSOR_3:
            sensor_name = "Sensor Corriente 3"

        device_identifier = str(self.owning_cluster.endpoint.device.ieee) # Usamos el del cluster actual
        print(f"*** LECTURA DE SENSOR [{device_identifier}] : {sensor_name} (AttrID: {attribute_id:#06x}) = {value:.2f} A ***")
        logging.info(f"ACTUALIZACIÓN SENSOR ({self.owning_cluster.endpoint.device.nwk:#06x}): "
                     f"Sensor (AttrID: {attribute_id:#06x}) = {value} a las {timestamp}")


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

        custom_cluster = endpoint.in_clusters[CUSTOM_CLUSTER_ID] # Esta es la instancia del cluster en el dispositivo ESP32
        if not isinstance(custom_cluster, CustomPowerSensorCluster):
            logging.error(f"Cluster {CUSTOM_CLUSTER_ID:#06x} en {device.ieee} no es del tipo CustomPowerSensorCluster esperado. "
                          f"Tipo actual: {type(custom_cluster)}. El registro del cluster puede haber fallado o sido sobrescrito.")
            return
# --- INICIO DEL BLOQUE DE BINDING EXPLÍCITO (OPCIÓN 2.E - ZDO REQUEST - CORREGIDO) ---
        try:
            coordinator_device = self._app.get_device(nwk=0x0000)
            if coordinator_device:
                # from zigpy import types as t # Ya importado globalmente
                # zdo_types ya debería estar importado como zigpy.zdo.types
                # zcl_f ya debería estar importado como zigpy.zcl.foundation

                src_ieee_bind = device.ieee
                src_ep_bind = endpoint.endpoint_id
                cluster_id_bind = CUSTOM_CLUSTER_ID

                dst_multi_addr = zdo_types.MultiAddress()
                dst_multi_addr.addrmode = t.AddrMode.IEEE
                dst_multi_addr.ieee = coordinator_device.ieee
                dst_multi_addr.endpoint = 1 # Asumiendo que el coordinador escucha en el endpoint 1

                logging.info(
                    f"Intentando binding explícito (device.zdo.Bind_req) para Dispositivo: {src_ieee_bind} "
                    f"SrcEP: {src_ep_bind} ClusterID: {cluster_id_bind:#06x} "
                    f"hacia Coordinador (MultiAddress): {dst_multi_addr}"
                )

                zdo_resp_payload = await device.zdo.Bind_req(
                    src_ieee_bind,
                    src_ep_bind,
                    cluster_id_bind,
                    dst_multi_addr
                )

                status_val = None
                if isinstance(zdo_resp_payload, list) and len(zdo_resp_payload) > 0:
                    payload_item = zdo_resp_payload[0]
                    if hasattr(payload_item, 'status'):
                        status_val = payload_item.status
                    elif isinstance(payload_item, (zdo_types.Status, zcl_f.Status)):
                        status_val = payload_item
                elif hasattr(zdo_resp_payload, 'status'):
                    status_val = zdo_resp_payload.status
                elif isinstance(zdo_resp_payload, (zdo_types.Status, zcl_f.Status)):
                    status_val = zdo_resp_payload

                is_success = (isinstance(status_val, (zdo_types.Status, zcl_f.Status)) and status_val == zcl_f.Status.SUCCESS) or \
                             (isinstance(status_val, int) and status_val == 0x00)


                if is_success:
                    logging.info(f"Binding explícito (ZDO Bind_req) para {CUSTOM_CLUSTER_ID:#06x} en {device.ieee} exitoso. Respuesta: {zdo_resp_payload}")
                else:
                    logging.warning(f"Binding explícito (ZDO Bind_req) para {CUSTOM_CLUSTER_ID:#06x} en {device.ieee} con respuesta: {zdo_resp_payload} (Status interpretado: {status_val})")
            else:
                logging.warning("No se pudo obtener el objeto del dispositivo coordinador para el binding.")

        except AttributeError as ae:
             logging.error(f"AttributeError durante el binding explícito (ZDO Bind_req): {ae}.", exc_info=True)
        except ValueError as ve:
            logging.error(f"ValueError durante el binding explícito (ZDO Bind_req): {ve}", exc_info=True)
        except Exception as e_bind:
            logging.error(f"Excepción general durante el binding explícito (ZDO Bind_req): {type(e_bind).__name__} - {e_bind}", exc_info=True)
        # --- FIN DEL BLOQUE DE BINDING EXPLÍCITO ---

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

                response_from_configure = await custom_cluster.configure_reporting(
                    attr_id,
                    REPORTING_MIN_INTERVAL,
                    REPORTING_MAX_INTERVAL,
                    REPORTABLE_CURRENT_CHANGE,
                )

                res_payload_args = None
                if response_from_configure is None:
                    logging.error(f"  No se recibió respuesta (None) al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}). Timeout o no se esperaba respuesta?")
                elif isinstance(response_from_configure, tuple) and len(response_from_configure) == 2:
                    res_header = response_from_configure[0]
                    res_payload_args = response_from_configure[1]
                    logging.info(f"  Respuesta (tupla) al configurar reporte para {attr_name}: Header={res_header}, PayloadArgs={res_payload_args}")
                elif hasattr(response_from_configure, 'status_records'):
                    res_payload_args = response_from_configure
                    logging.info(f"  Respuesta (directa) al configurar reporte para {attr_name}: PayloadArgs={res_payload_args}")
                else:
                    logging.warning(f"  Respuesta con formato desconocido para {attr_name} (AttrID: {attr_id:#06x}): {response_from_configure}")
                    continue

                if res_payload_args:
                    if hasattr(res_payload_args, 'status_records') and res_payload_args.status_records:
                        all_attr_successful = True
                        for record in res_payload_args.status_records:
                            if record.status != zcl_f.Status.SUCCESS:
                                all_attr_successful = False
                                logging.error(
                                    f"  Fallo al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}) "
                                    f"en registro específico: Status={record.status.name}, AttrID={getattr(record, 'attrid', 'N/A')}, Direction={getattr(record, 'direction', 'N/A')}"
                                )

                        if all_attr_successful:
                            if len(res_payload_args.status_records) == 1 and getattr(res_payload_args.status_records[0], 'attrid', None) is None and res_payload_args.status_records[0].status == zcl_f.Status.SUCCESS :
                                logging.info(f"  Configurado reporte para {attr_name} (AttrID: {attr_id:#06x}) exitosamente (respuesta SUCCESS global).")
                            else:
                                logging.info(f"  Configurado reporte para {attr_name} (AttrID: {attr_id:#06x}) con registros de estado específico: {res_payload_args.status_records}")
                    elif hasattr(res_payload_args, 'status') and res_payload_args.status == zcl_f.Status.SUCCESS:
                         logging.info(f"  Configurado reporte para {attr_name} (AttrID: {attr_id:#06x}) exitosamente (respuesta SUCCESS directa).")
                    else:
                        logging.warning(f"  Respuesta sin status_records válidos o status SUCCESS para {attr_name} (AttrID: {attr_id:#06x}): PAYLOAD_ARGS={res_payload_args}")

            except ValueError as ve:
                logging.error(f"  ValueError (posiblemente atributo desconocido internamente por zigpy) al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}): {ve}")
            except zigpy.exceptions.ZigbeeException as ze:
                logging.error(f"  ZigbeeException al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}): {ze}")
            except Exception as e:
                logging.error(f"  Excepción general al configurar reporte para {attr_name} (AttrID: {attr_id:#06x}): {type(e).__name__} - {e}", exc_info=True)

        if device.ieee not in self._sensor_listeners:
            sensor_listener = SensorAttributeListener(device.ieee, custom_cluster)
            custom_cluster.add_listener(sensor_listener)
            self._sensor_listeners[device.ieee] = sensor_listener
            logging.info(f"Listener de atributos añadido para el cluster custom de {device.ieee}")

    def device_initialized(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO COMPLETAMENTE INICIALIZADO: {device}")
        if device.nwk != 0x0000: # No configurar el propio coordinador
            asyncio.create_task(self.configure_device_reporting(device))

    def device_left(self, device: zigpy_dev.Device):
        logging.info(f"DISPOSITIVO ABANDONÓ LA RED: {device}")
        if device.ieee in self._sensor_listeners and ESP32_H2_ENDPOINT_ID in device.endpoints:
            endpoint = device.endpoints[ESP32_H2_ENDPOINT_ID]
            if CUSTOM_CLUSTER_ID in endpoint.in_clusters:
                custom_cluster = endpoint.in_clusters[CUSTOM_CLUSTER_ID]
                try:
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


async def periodic_permit_join_task(app_controller, shutdown_evt):
    """Tarea que periódicamente reabre la red para uniones."""
    logging.info("Iniciando tarea de permiso de unión periódico.")
    try:
        while not shutdown_evt.is_set():
            try:
                logging.info(f"Abriendo la red para uniones durante {PERMIT_JOIN_DURATION_ON_STARTUP} segundos...")
                await app_controller.permit(PERMIT_JOIN_DURATION_ON_STARTUP)
                logging.info(f"La red está abierta. Se reabrirá en {REOPEN_JOIN_INTERVAL_SECONDS}s (aprox).")
            except Exception as e:
                logging.error(f"Error en la tarea de permiso de unión periódico al llamar a app.permit: {e}")

            try:
                await asyncio.wait_for(shutdown_evt.wait(), timeout=REOPEN_JOIN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass # Esto es esperado, significa que el timeout se alcanzó y debemos reabrir
            except Exception as e_wait:
                logging.error(f"Error esperando en la tarea de permiso de unión: {e_wait}")
                await asyncio.sleep(5) # Pequeña pausa antes de reintentar en caso de error en wait

    except asyncio.CancelledError:
        logging.info("Tarea de permiso de unión periódico cancelada.")
    finally:
        # Intentar cerrar el permiso de unión si la app sigue conectada
        if app_controller and hasattr(app_controller, 'permit') and \
           hasattr(app_controller, '_ezsp') and app_controller._ezsp and app_controller._ezsp.is_connected:
            try:
                logging.info("Cerrando permiso de unión desde la tarea periódica al finalizar...")
                await app_controller.permit(0)
            except Exception as e_close:
                logging.warning(f"Error al cerrar permiso de unión en la tarea periódica: {e_close}")
        logging.info("Tarea de permiso de unión periódico finalizada.")

async def main():

    if BellowsApplication is None:
        return
    log_format = "%(asctime)s %(levelname)s [%(name)s]: %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    logging.getLogger("bellows").setLevel(logging.WARNING)
    logging.getLogger("zigpy").setLevel(logging.WARNING)

    #logging.getLogger("bellows").setLevel(logging.DEBUG)
    #logging.getLogger("zigpy").setLevel(logging.DEBUG)

    app = None # app se inicializará dentro del bucle de reintento
    listener = None # listener también se inicializará con app
    permit_join_task_handle = None
    DESIRED_CHANNEL = 15

    print(f"Intentando conectar al coordinador en: {DEVICE_PATH} a {BAUDRATE} baudios con control de flujo: {FLOW_CONTROL}.")

    ### INICIO DEL BLOQUE DE REINTENTO DE CONEXIÓN ###
    MAX_CONNECT_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 5

    for attempt in range(MAX_CONNECT_ATTEMPTS):
        try:
            bellows_specific_config = {
                zigpy_config.CONF_DEVICE_PATH: DEVICE_PATH,
                zigpy_config.CONF_DEVICE_BAUDRATE: BAUDRATE,
            }
            if FLOW_CONTROL is not None:
                bellows_specific_config[zigpy_config.CONF_DEVICE_FLOW_CONTROL] = FLOW_CONTROL

            network_config = {
                zigpy_config.CONF_NWK_CHANNEL: DESIRED_CHANNEL,
                zigpy_config.CONF_NWK_CHANNELS: [DESIRED_CHANNEL],
            }

            zigpy_general_config = {
                zigpy_config.CONF_DATABASE: "zigbee.db",
                zigpy_config.CONF_NWK_BACKUP_ENABLED: True,
                zigpy_config.CONF_NWK: network_config,
                zigpy_config.CONF_OTA: {
                    zigpy_config.CONF_OTA_ENABLED: False,
                    zigpy_config.CONF_OTA_PROVIDERS: [],
                }
            }
            config_para_schema = { zigpy_config.CONF_DEVICE: bellows_specific_config, **zigpy_general_config }
            final_app_config = BellowsApplication.SCHEMA(config_para_schema)

            # Recrear la instancia de la app y el listener si es necesario
            #print antes de crear la app
            print(f"Creando instancia de la aplicación del controlador Zigbee...")
            app = BellowsApplication(config=final_app_config)
            #print después de crear la app
            print(f"Instancia de la aplicación creada correctamente.")
            # Listener se crea aquí para asegurarse de que está vinculado a la nueva instancia de app
            if listener is None:
                listener = MyEventListener(app_controller=app)
            else:
                # Si el listener ya existe, solo actualizamos su referencia a la nueva instancia de app
                listener._app = app # Asumiendo que listener tiene un atributo _app
            app.add_listener(listener)

            print(f"Iniciando aplicación del controlador Zigbee (Intento {attempt + 1}/{MAX_CONNECT_ATTEMPTS})...")
            await app.startup(auto_form=False)
            print("Aplicación iniciada correctamente.")
            break # Si tiene éxito, sal del bucle
        except TimeoutError as e_timeout:
            logging.error(f"TimeoutError en el intento {attempt + 1} de conexión: {e_timeout}")
            if app and hasattr(app, 'shutdown'):
                try:
                    logging.info("Intentando cerrar la aplicación después del TimeoutError...")
                    await app.shutdown()
                except Exception as e_shutdown_fail:
                    logging.warning(f"Error al cerrar la app después del TimeoutError: {e_shutdown_fail}")
            app = None # Asegurar que se recree
            if attempt < MAX_CONNECT_ATTEMPTS - 1:
                logging.info(f"Reintentando en {RETRY_DELAY_SECONDS} segundos...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error("Máximo número de intentos de conexión (TimeoutError) alcanzado.")
        except Exception as e_connect: # Captura otras excepciones durante la conexión/startup
            logging.error(f"Error inesperado en el intento {attempt + 1} de conexión/startup: {type(e_connect).__name__} - {e_connect}", exc_info=True)
            if app and hasattr(app, 'shutdown'):
                try:
                    logging.info("Intentando cerrar la aplicación después de un error inesperado...")
                    await app.shutdown()
                except Exception as e_shutdown_fail:
                    logging.warning(f"Error al cerrar la app después de un error inesperado: {e_shutdown_fail}")
            app = None
            if attempt < MAX_CONNECT_ATTEMPTS - 1:
                logging.info(f"Reintentando en {RETRY_DELAY_SECONDS} segundos...")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                logging.error("Máximo número de intentos de conexión (Error inesperado) alcanzado.")

    # Verificar si la aplicación se inició correctamente después de los intentos
    if not (app and hasattr(app, 'state') and app.state and hasattr(app.state, 'node_info') and app.state.node_info):
        logging.critical("La aplicación Zigbee no pudo iniciarse después de varios intentos. Saliendo.")
        # Asegurarse de que si app existe pero está mal, se intente un shutdown final
        if app and hasattr(app, 'shutdown'):
            try: await app.shutdown()
            except: pass
        return # Salir de main si la conexión falló persistentemente
    ### FIN DEL BLOQUE DE REINTENTO DE CONEXIÓN ###

    # A partir de aquí, 'app' DEBERÍA estar inicializada y conectada si el bucle tuvo éxito.
    # NO volvemos a llamar a app.startup() ni a crear la instancia de app.

    try:
        # Esta sección ahora asume que 'app' está lista y conectada desde el bucle anterior.
        print("¡Controlador Zigbee listo y operando!")
        node_info = app.state.node_info
        network_info = app.state.network_info
        if node_info: print(f"  Coordinador IEEE: {node_info.ieee}, NWK: 0x{node_info.nwk:04x}")
        if network_info: 
            print(f"  Red EPID: {network_info.extended_pan_id}, PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")


        # Comprobar si la red está formada y formarla si es necesario
        # Esta lógica es importante DESPUÉS de que 'app' se haya conectado al dongle
        if not network_info or not network_info.pan_id:
             logging.info(f"La red no parece estar formada (PAN ID: {network_info.pan_id if network_info else 'N/A'}). Intentando formar una nueva red en el canal {DESIRED_CHANNEL}...")
             try:
                 await app.form_network()
                 logging.info(f"Nueva red formada. Recargando información de la red...")
                 await app.load_network_info(load_devices=True)
                 node_info = app.state.node_info # Actualizar variables locales
                 network_info = app.state.network_info # Actualizar variables locales
                 if node_info: 
                     logging.info(f"  Nuevo Coordinador IEEE: {node_info.ieee}, NWK: 0x{node_info.nwk:04x}")
                 if network_info: 
                     logging.info(f"  Nueva Red EPID: {network_info.extended_pan_id}, PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")
             except Exception as e_form:
                 logging.error(f"Error al intentar formar la red: {e_form}", exc_info=True)
                 # Podrías querer salir aquí si la formación de red es crítica y falla
                 if app and hasattr(app, 'shutdown'): await app.shutdown()
                 return


        if network_info and network_info.channel != DESIRED_CHANNEL:
            print(f"\nLa red está actualmente en el canal {network_info.channel}. Intentando migrar al canal {DESIRED_CHANNEL}...")
            try:
                await app.move_network_to_channel(DESIRED_CHANNEL)
                print(f"Solicitud de migración de canal a {DESIRED_CHANNEL} enviada.")
                await app.load_network_info(load_devices=False)
                network_info = app.state.network_info # Actualizar
                if network_info: print(f"  Nuevo estado de red: PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")
            except Exception as e_chn_chg:
                print(f"Error durante el intento de cambio de canal: {e_chn_chg}")


        permit_join_task_handle = asyncio.create_task(periodic_permit_join_task(app, shutdown_event))

        print("\nLa aplicación Zigbee está en funcionamiento. Presiona Ctrl+C para detener.")
        await shutdown_event.wait()

    except KeyboardInterrupt:
        logging.info("\nInterrupción por teclado detectada. Iniciando cierre...")
        if not shutdown_event.is_set(): shutdown_event.set()
    except Exception as e:
        logging.error(f"Error general en la aplicación (fuera del bucle de conexión): {type(e).__name__}: {e}", exc_info=True)
        if not shutdown_event.is_set(): shutdown_event.set()
    finally:
        if permit_join_task_handle and not permit_join_task_handle.done():
            logging.info("Cancelando la tarea de permiso de unión periódico...")
            permit_join_task_handle.cancel()
            try:
                await permit_join_task_handle
            except asyncio.CancelledError:
                logging.info("La tarea de permiso de unión fue cancelada como se esperaba.")
            except Exception as e_task_cancel:
                logging.error(f"Error esperando la cancelación de la tarea de permiso: {e_task_cancel}")

        pending_tasks_in_finally = []
        if app is not None:
            logging.info("\nIniciando proceso de cierre de la aplicación Zigbee...")
            connection_ezsp_active = False
            if hasattr(app, '_ezsp') and app._ezsp is not None:
                try:
                    if app._ezsp.is_connected: connection_ezsp_active = True
                except AttributeError: logging.warning("app._ezsp.is_connected no encontrado.")
                except Exception as e_check_conn: logging.warning(f"Error al verificar app._ezsp.is_connected: {e_check_conn}")

            if connection_ezsp_active and hasattr(app, 'permit'):
                try:
                    logging.info("Cerrando permiso de unión explícitamente en finally (app.permit(0))...")
                    await app.permit(0)
                except Exception as e_permit_final:
                    logging.warning(f"No se pudo cerrar el permiso de unión en el bloque finally principal: {e_permit_final}")

            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    current_main_task = asyncio.current_task(loop=loop)
                    for task in asyncio.all_tasks(loop=loop):
                        if task is not current_main_task and task is not permit_join_task_handle and not task.done():
                            pending_tasks_in_finally.append(task)
            except RuntimeError: logging.warning("No se pudo obtener el bucle de eventos en finally.")

            if pending_tasks_in_finally:
                logging.info(f"Cancelando {len(pending_tasks_in_finally)} tareas pendientes adicionales...")
                for task in pending_tasks_in_finally: task.cancel()
                try:
                    await asyncio.gather(*pending_tasks_in_finally, return_exceptions=True)
                except Exception as e_gather: logging.warning(f"Error durante gather de tareas canceladas adicionales: {e_gather}")

            logging.info("Llamando a app.shutdown()...")
            try:
                await app.shutdown()
                logging.info("Proceso de cierre del controlador completado.")
            except Exception as e_shutdown:
                logging.error(f"Error durante app.shutdown(): {type(e_shutdown).__name__}: {e_shutdown}", exc_info=True)
        else:
            logging.info("\nLa instancia de la aplicación no fue creada o la conexión inicial falló persistentemente.")
        logging.info("Fin del script.")


def signal_handler(sig, frame):
    # ... (tu signal_handler)
    logging.info(f"Señal {signal.Signals(sig).name} recibida, estableciendo evento de cierre...")
    if not shutdown_event.is_set():
        shutdown_event.set()
    else:
        logging.warning("Evento de cierre ya establecido. Múltiples señales de interrupción recibidas.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    asyncio.run(main())