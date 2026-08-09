# Sikka Secure Auto-Activating Sync Agent (Sage SQL Edition)
import sqlite3
import os
import sys
import json
import hashlib
import platform
from datetime import date, datetime
from supabase import create_client

# Try importing pyodbc for SQL Server connection
try:
    import pyodbc
except ImportError:
    pyodbc = None

SUPABASE_URL = "https://pednybdwhfgosfxbrmvf.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBlZG55YmR3aGZnb3NmeGJybXZmIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjIwMjY3MCwiZXhwIjoyMTAxNzc4NjcwfQ.lMnWE-0sPmdtxjy2Spt_aDnHPncw9xzCrZVpT16rgKk"

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return "."

def get_hardware_fingerprint():
    """Generates a unique hardware signature for this specific computer."""
    raw_info = platform.node() + platform.machine() + platform.processor()
    return hashlib.sha256(raw_info.encode()).hexdigest()[:16]

def auto_activate_and_verify():
    """Checks for a license; if missing, auto-registers with Supabase and configures SQL Server."""
    license_path = os.path.join(get_base_dir(), "license.key")
    config_path = os.path.join(get_base_dir(), "db_config.json")
    current_hw_id = get_hardware_fingerprint()

    # IF LICENSE IS MISSING: Auto-activation & SQL Discovery Routine
    if not os.path.exists(license_path):
        print("⚡ No license found. Initiating Sikka auto-activation & POS discovery...")
        
        tenant_id = input("Enter your Sikka Store ID (e.g., NAS-MEDINA-01): ").strip()
        
        # SQL Server Interactive Discovery (Your brother's workaround)
        print("\n--- Configuration de la base de données Sage (SQL Server) ---")
        server = input("Nom du serveur SQL (ex: 127.0.0.1\\SAGE100 ou localhost): ").strip()
        user = input("Utilisateur SQL [SAGEREADER]: ").strip() or "SAGEREADER"
        import getpass
        password = getpass.getpass("Mot de passe SQL : ").strip()

        # Test connection and list databases
        try:
            driver = "{ODBC Driver 17 for SQL Server}"
            conn_str = f"DRIVER={driver};SERVER={server};DATABASE=master;UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;"
            conn = pyodbc.connect(conn_str, timeout=5)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sys.databases WHERE state = 0 ORDER BY name")
            databases = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"❌ Erreur de connexion au serveur SQL: {e}")
            sys.exit(1)

        print("\n📋 Bases de données disponibles sur ce POS :")
        for i, db in enumerate(databases, 1):
            print(f"  {i}. {db}")
        
        choice = int(input(f"\nChoisissez la base de production (1-{len(databases)}) : "))
        selected_db = databases[choice - 1]
        print(f"📌 Base sélectionnée : {selected_db}")

        # Save SQL config locally
        db_config = {
            "server": server,
            "username": user,
            "password": password,
            "database": selected_db
        }
        with open(config_path, "w") as f:
            json.dump(db_config, f, indent=4)

        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            
            # Register hardware ID to this tenant in Supabase
            sb.table("merchants").upsert({
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "status": "active",
                "updated_at": str(datetime.now())
            }).execute()
            
            # Create the license file locally
            license_data = {
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "expires_at": "2027-08-08"
            }
            
            with open(license_path, "w") as f:
                json.dump(license_data, f, indent=4)
                
            print(f"✅ Activation successful! License locked to store: {tenant_id}")
            return tenant_id, db_config
            
        except Exception as e:
            print(f"❌ Cloud activation failed: {e}")
            sys.exit(1)

    # STANDARD CHECK (If license file already exists)
    try:
        with open(license_path, "r") as f:
            license_data = json.load(f)
        with open(config_path, "r") as f:
            db_config = json.load(f)
            
        if license_data.get("hw_id") != current_hw_id:
            print("SECURITY ERROR: License key does not match this computer hardware.")
            sys.exit(1)
            
        return license_data.get("tenant_id"), db_config
    except Exception as e:
        print(f"SECURITY ERROR: Invalid license or config format. Details: {e}")
        sys.exit(1)

def pull_sage_metrics(db_config):
    """Pulls live business metrics directly from the Sage SQL database."""
    if not pyodbc:
        print("❌ pyodbc module missing.")
        return (0, 0, 0)
    
    try:
        driver = "{ODBC Driver 17 for SQL Server}"
        conn_str = (
            f"DRIVER={driver};"
            f"SERVER={db_config['server']};"
            f"DATABASE={db_config['database']};"
            f"UID={db_config['username']};"
            f"PWD={db_config['password']};"
            f"TrustServerCertificate=yes;Encrypt=no;"
        )
        conn = pyodbc.connect(conn_str, timeout=5)
        cursor = conn.cursor()
        
        # Example query adapting to Sage tables (adjust table/columns based on Sage schema schema)
        # Pulling total sales for today or latest entry
        cursor.execute("SELECT SUM(<code>CA_Vente</code>) FROM <code>F_DOCENTETE</code> WHERE <code>DO_Date</code> = CAST(GETDATE() AS DATE)")
        row = cursor.fetchone()
        total_sales = float(row[0]) if row and row[0] else 0.0
        
        cursor.close()
        conn.close()
        return (total_sales, 0.0, 0.0) # Cash and bank can be mapped similarly from Sage tables
    except Exception as e:
        print(f"⚠️ Sage SQL read warning: {e}")
        return (0, 0, 0)

def sync():
    result = auto_activate_and_verify()
    if not result:
        return
    tenant_id, db_config = result
        
    sales, cash, bank = pull_sage_metrics(db_config)
    
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb.table("business_metrics").insert({
            "tenant_id": tenant_id,
            "date": str(date.today()),
            "total_sales": sales,
            "cash_in_drawer": cash,
            "bank_balance": bank,
        }).execute()
        print(f"[{tenant_id}] Secure sync successful for {date.today()} -> Sales: {sales} FCFA")
    except Exception as e:
        print(f"Sync error: {e}")

if __name__ == "__main__":
    sync()
