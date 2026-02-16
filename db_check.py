#!/usr/bin/env python3
"""
Script para verificar la conexión a PostgreSQL.
Uso: python db_check.py
"""

from dotenv import load_dotenv
from os import getenv
import sys

load_dotenv()

# Importar después de cargar .env
from main import DatabaseHelper


def main():
    # Verificar variables requeridas
    required_vars = ['DB_HOST', 'DB_USER', 'DB_PASS', 'DB_NAME']
    missing = [var for var in required_vars if not getenv(var)]
    if missing:
        print(f"❌ Variables de entorno faltantes: {', '.join(missing)}")
        print("   Asegurate de tener un archivo .env con:")
        print("   DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME")
        sys.exit(1)

    db_config = {
        'host': getenv('DB_HOST'),
        'port': int(getenv('DB_PORT', '5432')),
        'user': getenv('DB_USER'),
        'password': getenv('DB_PASS'),
        'dbname': getenv('DB_NAME')
    }

    print("=" * 50)
    print("🔍 Verificación de Base de Datos PostgreSQL")
    print("=" * 50)
    print(f"\n📌 Configuración:")
    print(f"   Host: {db_config['host']}")
    print(f"   Puerto: {db_config['port']}")
    print(f"   Usuario: {db_config['user']}")
    print(f"   Base de datos: {db_config['dbname']}")

    helper = DatabaseHelper(db_config)

    # 1. Verificar conexión
    print(f"\n🔌 Verificando conexión...")
    conn_ok, conn_msg = helper.check_connection()
    icon = "✅" if conn_ok else "❌"
    print(f"   {icon} {conn_msg}")

    if not conn_ok:
        print("\n❌ No se pudo conectar. Verifica:")
        print("   - Que PostgreSQL esté corriendo")
        print("   - Que las credenciales sean correctas")
        print("   - Que el host sea accesible desde esta máquina")
        sys.exit(1)

    # 2. Verificar tablas
    print(f"\n📋 Verificando tablas...")
    tables_ok, missing = helper.check_tables_exist()
    icon = "✅" if tables_ok else "❌"
    if tables_ok:
        print(f"   {icon} Todas las tablas existen")
    else:
        print(f"   {icon} Faltan tablas: {missing}")

    # 3. Health check completo
    print(f"\n🏥 Health check completo...")
    health = helper.health_check()
    icon = "✅" if health['healthy'] else "❌"
    print(f"   {icon} Estado: {'Saludable' if health['healthy'] else 'Con problemas'}")

    # 4. Estadísticas
    print(f"\n📊 Estadísticas:")
    stats = helper.get_db_stats()
    if stats:
        print(f"   💾 Tamaño DB: {stats.get('db_size', 'N/A')}")
        print(f"   👥 Suscripciones: {stats.get('subscriptions_count', 'N/A')}")
        print(f"   📈 Registros de tasas: {stats.get('rate_history_count', 'N/A')}")
        print(f"   💬 Sugerencias: {stats.get('suggestions_count', 'N/A')}")

    print("\n" + "=" * 50)
    if health['healthy']:
        print("✅ Todo OK - La base de datos está lista")
        sys.exit(0)
    else:
        print("⚠️  Hay problemas con la base de datos")
        sys.exit(1)


if __name__ == '__main__':
    main()
