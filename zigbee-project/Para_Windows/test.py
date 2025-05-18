import asyncio
import logging
import bellows
import zigpy
import sys

# --- Configuración ---
DEVICE_PATH = 'COM9'        # Puerto para Windows
BAUDRATE = 115200           # Para ZBDongle-E (EFR32MG21)
FLOW_CONTROL = None         # Control de flujo

# Importar las clases necesarias
import zigpy.config as zigpy_config
import bellows.config as bellows_config
import zigpy.exceptions
import bellows.ezsp
try:
    from bellows.zigbee.application import ControllerApplication as BellowsApplication
except ImportError:
    print("Error: La biblioteca 'bellows' no está instalada o no se encuentra bellows.zigbee.application.")
    print("Por favor, instálala con: pip install bellows")
    sys.exit(1)

async def main():
    # Configurar logging
    logging.basicConfig(level=logging.INFO)
    
    print(f"Intentando conectar al coordinador en: {DEVICE_PATH} a {BAUDRATE} baudios con control de flujo: {FLOW_CONTROL}.")
    print("Este proceso puede tardar unos segundos...")

    # Preparar la configuración
    bellows_specific_config = {
        bellows_config.CONF_DEVICE_PATH: DEVICE_PATH,
        bellows_config.CONF_DEVICE_BAUDRATE: BAUDRATE,
    }
    if FLOW_CONTROL is not None:
        bellows_specific_config[zigpy_config.CONF_DEVICE_FLOW_CONTROL] = FLOW_CONTROL

    # Configuración general de zigpy
    zigpy_general_config = {
        zigpy_config.CONF_DATABASE: "zigbee.db",
        zigpy_config.CONF_NWK_BACKUP_ENABLED: True,
        zigpy_config.CONF_OTA: {
            zigpy_config.CONF_OTA_ENABLED: False,
            zigpy_config.CONF_OTA_PROVIDERS: [],
        }
    }

    # Combinar la configuración
    config_para_schema = BellowsApplication.SCHEMA({
        zigpy_config.CONF_DEVICE: bellows_specific_config,
        **zigpy_general_config
    })

    # Crear instancia de la aplicación
    app = BellowsApplication(config=config_para_schema)
    
    # Lista para almacenar tareas pendientes que necesitaremos cancelar
    pending_tasks = []

    try:
        # Conectar al coordinador
        print("Conectando al adaptador Zigbee e inicializando...")
        await app.startup(auto_form=True)
        print("¡Conexión e inicialización básicas exitosas!")

        # Mostrar características del coordinador
        node_info = app.state.node_info
        print("\n--- Características del Coordinador (NodeInfo) ---")
        if node_info:
            print(f"  Puerto utilizado (configurado): {DEVICE_PATH}")
            print(f"  IEEE Address (MAC): {node_info.ieee}")
            print(f"  NWK Address: 0x{node_info.nwk:04x}")
            print(f"  Logical Type: {node_info.logical_type.name if node_info.logical_type else 'N/A'}")
            print(f"  Manufacturer: {node_info.manufacturer if node_info.manufacturer else 'N/A'}")
            print(f"  Model: {node_info.model if node_info.model else 'N/A'}")
            print(f"  Versión de Firmware EZSP (reportada por bellows): {node_info.version if node_info.version else 'N/A'}")
            print(f"    (Nota: 'version' aquí es la versión del firmware EZSP, no necesariamente del chip completo)")

            # Información adicional del Node Descriptor
            if app._device and app._device.node_desc:
                nd = app._device.node_desc
                print(f"  Node Descriptor:")
                print(f"    Tipo Lógico: {nd.logical_type.name}")
                print(f"    Código de Fabricante: 0x{nd.manufacturer_code:04x}")
                print(f"    Flags de Capacidad MAC: {nd.mac_capability_flags}")
                print(f"    Tamaño Máximo de Buffer: {nd.maximum_buffer_size}")
                print(f"    Banda de Frecuencia: {nd.frequency_band}")
            else:
                print("  Node Descriptor no disponible.")
        else:
            print("  No se pudo obtener NodeInfo del coordinador.")

        # Mostrar información de la red
        network_info = app.state.network_info
        print("\n--- Información de la Red (NetworkInfo) ---")
        if network_info:
            print(f"  Extended PAN ID: {network_info.extended_pan_id}")
            print(f"  PAN ID: 0x{network_info.pan_id:04x}")
            print(f"  Channel: {network_info.channel}")
            print(f"  NWK Update ID: {network_info.nwk_update_id}")
            print(f"  Security Level: {network_info.security_level}")
            if network_info.pan_id == 0xFFFF or network_info.channel == 0:
                print("  ¡Advertencia: La red parece no estar formada o no se ha unido a una!")
        else:
            print("  No se pudo obtener NetworkInfo del coordinador.")

    except Exception as e:
        print(f"\nError durante la conexión o lectura de información: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Obtener todas las tareas pendientes antes del shutdown
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                pending_tasks.append(task)
        
        print("\nIntentando cerrar la conexión con el adaptador...")
        try:
            # SOLUCIÓN: Cancelar todas las tareas pendientes antes del shutdown
            print(f"Cancelando {len(pending_tasks)} tareas pendientes...")
            for task in pending_tasks:
                if not task.done():
                    task.cancel()
            
            # Esperar a que todas las tareas se cancelen
            if pending_tasks:
                await asyncio.wait(pending_tasks, timeout=2.0)
            
            # Ahora es seguro hacer shutdown
            await app.shutdown()
            print("Proceso de cierre completado correctamente.")
        except Exception as e_shutdown:
            print(f"Error durante el shutdown: {type(e_shutdown).__name__}: {e_shutdown}")
            import traceback
            traceback.print_exc()
        
        print("Fin del script.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nPrograma interrumpido por el usuario.")