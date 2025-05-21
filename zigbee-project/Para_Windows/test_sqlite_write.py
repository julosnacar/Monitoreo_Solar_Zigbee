# test_sqlite_write.py
import sqlite3
import os

db_file = "test_direct_sqlite.db"
print(f"Intentando crear/abrir la base de datos: {os.path.abspath(db_file)}")

try:
    # Intenta borrarla si existe para una prueba limpia
    if os.path.exists(db_file):
        os.remove(db_file)
        print(f"Archivo '{db_file}' existente borrado.")

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS test_table (id INTEGER, name TEXT)")
    cursor.execute("INSERT INTO test_table (id, name) VALUES (1, 'test_entry')")
    conn.commit()
    print(f"Base de datos '{db_file}' creada y datos insertados con éxito.")
    conn.close()

    # Verifica si el archivo realmente se creó
    if os.path.exists(db_file):
        print(f"CONFIRMADO: El archivo '{db_file}' existe en el disco.")
        print(f"Tamaño del archivo: {os.path.getsize(db_file)} bytes.")
    else:
        print(f"ERROR: El archivo '{db_file}' NO se encontró después de la operación.")

except Exception as e:
    print(f"Error al probar SQLite directamente: {e}")
    import traceback
    traceback.print_exc()

input("Presiona Enter para salir...") # Para que la ventana no se cierre si ejecutas con doble clic