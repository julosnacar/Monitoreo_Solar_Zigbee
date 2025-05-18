# --- START OF FILE ---
import asyncio
import logging
import random
import signal
from typing import Dict, Any

# --- Configuración ---
DEVICE_PATH = 'COM9'
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
        
        print(f"DEBUG: Se recibió una actualización para cluster {self.owning_cluster.cluster_id:#06x}, atributo {attribute_id:#06x}, valor {value}")
        
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

        print(f"*** LECTURA DE SENSOR: {sensor_name} (AttrID: {attribute_id:#06x}) = {value:.2f} A ***")
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
                from zigpy import types as t 
                # zdo_types ya debería estar importado como zigpy.zdo.types
                # import zigpy.zdo.types as zdo_types # Si no está ya globalmente
                # zcl_f ya debería estar importado como zigpy.zcl.foundation
                # import zigpy.zcl.foundation as zcl_f # Si no está ya globalmente

                src_ieee_bind = device.ieee
                src_ep_bind = endpoint.endpoint_id
                cluster_id_bind = CUSTOM_CLUSTER_ID

                dst_multi_addr = zdo_types.MultiAddress()
                dst_multi_addr.addrmode = t.AddrMode.IEEE 
                dst_multi_addr.ieee = coordinator_device.ieee
                dst_multi_addr.endpoint = 1 

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
                # La respuesta zdo_resp_payload para Bind_rsp es a menudo una lista con un solo elemento de estado
                if isinstance(zdo_resp_payload, list) and len(zdo_resp_payload) > 0:
                    # Tomar el primer elemento si es una lista
                    payload_item = zdo_resp_payload[0]
                    if hasattr(payload_item, 'status'): # Si el item es un objeto contenedor
                        status_val = payload_item.status
                    elif isinstance(payload_item, (zdo_types.Status, zcl_f.Status)): # Si el item es directamente el estado
                        status_val = payload_item
                elif hasattr(zdo_resp_payload, 'status'): # Si la respuesta es un objeto con .status
                    status_val = zdo_resp_payload.status
                elif isinstance(zdo_resp_payload, (zdo_types.Status, zcl_f.Status)): # Si la respuesta es solo el estado
                    status_val = zdo_resp_payload
                
                # Comparar con zdo_types.Status.SUCCESS o zcl_f.Status.SUCCESS
                # Es más seguro usar el numérico 0x00 si no estamos seguros de cuál tipo de Status es.
                # O verificar ambos:
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
                # ... (el resto de tu lógica de configure_reporting para cada atributo sigue aquí) ...
                # ... (sin cambios desde tu última versión funcional de esta parte) ...
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
        
        # El listener de atributos ya lo añades fuera de este bucle, lo cual está bien.
        if device.ieee not in self._sensor_listeners:
            sensor_listener = SensorAttributeListener(device.ieee, custom_cluster) # DESPUÉS, pasar custom_cluster
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
    if BellowsApplication is None: 
        return
    log_format = "%(asctime)s %(levelname)s [%(name)s]: %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    logging.getLogger("bellows").setLevel(logging.WARNING)
    logging.getLogger("zigpy").setLevel(logging.WARNING)
    logging.getLogger("zigpy.zcl").setLevel(logging.WARNING) # Añadir para silenciar logs ZCL si son muy verbosos
    logging.getLogger("zigpy.device").setLevel(logging.WARNING) # Añadir para silenciar logs de dispositivo

    # ---LOGGING DETALLADO ---
    # Configurar niveles de logging específicos ANTES de basicConfig
    # Esto te dará mucha más información sobre lo que está sucediendo internamente.
    # Puedes ajustar los niveles (DEBUG, INFO, WARNING) según sea necesario.
    # DEBUG es el más verboso.
    ##logging.getLogger("bellows").setLevel(logging.DEBUG)  # Para ver la comunicación con el dongle
    ##logging.getLogger("zigpy").setLevel(logging.DEBUG)    # Para zigpy en general
    ##logging.getLogger("zigpy.application").setLevel(logging.DEBUG) # Para el manejo de la aplicación y dispositivos
    ##logging.getLogger("zigpy.device").setLevel(logging.DEBUG)      # Para el proceso de "entrevista" del dispositivo
    ##logging.getLogger("zigpy.zdo").setLevel(logging.DEBUG)         # Para mensajes ZDO (comandos de gestión de red y dispositivos)
    ##logging.getLogger("zigpy.zcl").setLevel(logging.DEBUG)         # Para mensajes ZCL (comandos de clusters)
    # logging.getLogger("zigpy.serial").setLevel(logging.DEBUG) # Descomenta si usas serial y quieres ver ese nivel
    # logging.getLogger("bellows.ezsp").setLevel(logging.DEBUG) # Para detalles específicos de EZSP si usas bellows

    # Formato de log mejorado que incluye el nombre del logger y el número de línea
    # Esto ayuda mucho a rastrear de dónde viene cada mensaje de log.
    ##log_format = "%(asctime)s %(levelname)-8s [%(name)s:%(lineno)d] %(message)s"
    
    # Configuración básica del logging para la raíz y la salida por defecto.
    # El nivel aquí (logging.INFO) afectará a los loggers que NO hayas configurado explícitamente arriba.
    # Como hemos configurado zigpy y bellows a DEBUG, esos usarán DEBUG.
    # Otros loggers (si los hubiera) usarían INFO.
    ##logging.basicConfig(level=logging.INFO, format=log_format, force=True) # force=True puede ser útil si se reconfigura
    # --- FIN LOGGING DETALLADO ---

    print(f"Intentando conectar al coordinador en: {DEVICE_PATH} a {BAUDRATE} baudios con control de flujo: {FLOW_CONTROL}.")
    app = None
    
    DESIRED_CHANNEL = 15 # <--- DEFINE EL CANAL

    try:
        bellows_specific_config = { zigpy_config.CONF_DEVICE_PATH: DEVICE_PATH, zigpy_config.CONF_DEVICE_BAUDRATE: BAUDRATE, }
        if FLOW_CONTROL is not None: bellows_specific_config[zigpy_config.CONF_DEVICE_FLOW_CONTROL] = FLOW_CONTROL
        
        # Modificar la configuración de red para PREFERIR el canal deseado AL FORMAR la red
        # Esto solo aplica si la red NO está formada y se va a auto_form=True o se llama a app.form_network()
        network_config = {
            zigpy_config.CONF_NWK_CHANNEL: DESIRED_CHANNEL, # Canal preferido si se forma una nueva red
            zigpy_config.CONF_NWK_CHANNELS: [DESIRED_CHANNEL], # Solo permitir este canal al formar
        }
        
        zigpy_general_config = { 
            zigpy_config.CONF_DATABASE: "zigbee.db", 
            zigpy_config.CONF_NWK_BACKUP_ENABLED: True, 
            zigpy_config.CONF_NWK: network_config, # Aplicar la config de red
            zigpy_config.CONF_OTA: { 
                zigpy_config.CONF_OTA_ENABLED: False, 
                zigpy_config.CONF_OTA_PROVIDERS: [], 
            } 
        }
        config_para_schema = { zigpy_config.CONF_DEVICE: bellows_specific_config, **zigpy_general_config }
        
        # Validar la configuración completa con el esquema de la aplicación
        # Esto asegura que todos los campos requeridos estén presentes antes de pasarlos a BellowsApplication
        # Si BellowsApplication.SCHEMA no maneja bien los defaults internos de CONF_NWK,
        # podríamos necesitar ser más explícitos con todos los campos de CONF_NWK.
        final_app_config = BellowsApplication.SCHEMA(config_para_schema)

        app = BellowsApplication(config=final_app_config)
        listener = MyEventListener(app_controller=app)
        app.add_listener(listener)
        
        print("Iniciando aplicación del controlador Zigbee...")
        try:
            # auto_form=False para manejar la formación manualmente y poder establecer el canal
            await app.startup(auto_form=False) 
            print("Aplicación iniciada. Red ya estaba formada o fue cargada de la BD/dongle.")

        except zigpy.exceptions.NetworkNotFormed:
            print(f"La red no está formada. Intentando formar una nueva red en el canal {DESIRED_CHANNEL}...")
            # app.form_network() usará la configuración de canal de final_app_config
            await app.form_network() 
            print(f"Nueva red formada. Reiniciando la aplicación para usar la nueva red...")
            # Es crucial reiniciar la app después de form_network para que cargue la nueva configuración.
            await app.shutdown(db=False) # No guardar BD aquí, se hará después
            app = BellowsApplication(config=final_app_config) # Recrear con la misma config
            app.add_listener(listener)
            await app.startup(auto_form=False) # Iniciar de nuevo
            print("Aplicación reiniciada con la nueva red formada.")

        print("¡Controlador Zigbee listo y operando!")
        node_info = app.state.node_info
        network_info = app.state.network_info
        if node_info: print(f"  Coordinador IEEE: {node_info.ieee}, NWK: 0x{node_info.nwk:04x}")
        if network_info: print(f"  Red EPID: {network_info.extended_pan_id}, PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")

        # --- BLOQUE PARA CAMBIAR DE CANAL SI LA RED YA ESTÁ FORMADA EN OTRO CANAL ---
        if network_info and network_info.channel != DESIRED_CHANNEL:
            print(f"\nLa red está actualmente en el canal {network_info.channel}. Intentando migrar al canal {DESIRED_CHANNEL}...")
            try:
                # Primero, realiza un escaneo de energía para asegurar que el canal es viable (opcional pero recomendado)
                # Podrías querer una lógica más sofisticada para elegir el mejor canal si DESIRED_CHANNEL no es bueno.
                # energy_scan_results = await app.energy_scan(channels=t.Channels.ALL_CHANNELS, duration_exp=3, count=1)
                # print(f"Resultado del escaneo de energía: {energy_scan_results}")
                # if energy_scan_results.get(DESIRED_CHANNEL, 255) > (0.5 * 255): # Si el canal está muy ocupado
                #     print(f"ADVERTENCIA: El canal deseado {DESIRED_CHANNEL} parece tener mucha interferencia. Procediendo de todas formas.")

                await app.move_network_to_channel(DESIRED_CHANNEL)
                print(f"Solicitud de migración de canal a {DESIRED_CHANNEL} enviada. La red puede tardar en estabilizarse.")
                # Actualizar la info de red después del cambio
                await app.load_network_info(load_devices=False) # Recargar info de red
                network_info = app.state.network_info # Actualizar variable local
                if network_info: print(f"  Nuevo estado de red: PAN ID: 0x{network_info.pan_id:04x}, Canal: {network_info.channel}")

            except Exception as e_chn_chg:
                print(f"Error durante el intento de cambio de canal: {e_chn_chg}")
        # --- FIN DEL BLOQUE DE CAMBIO DE CANAL ---

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