# Sikka Secure Auto-Activating Sync Agent (Sage SQL Bulletproof Edition)
import sqlite3
import os
import sys
import json
import hashlib
import platform
import getpass
from datetime import date, datetime
from supabase import create_client

try:
    import pyodbc
except ImportError:
    pyodbc = None

SUPABASE_URL = "https://pednybdwhfgosfxbrmvf.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBlZG55YmR3aGZnb3NmeGJybXZmIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjIwMjY3MCwiZXhwIjoyMTAxNzc4NjcwfQ.lMnWE-0sPmdtxjy2Spt_aDnHPncw9xzCrZVpT16rgKk"

def pause_on_exit(msg="Appuyez sur Entrée pour fermer..."):
    input(f"\n{msg}")
    sys.exit(1)

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return "."

def get_hardware_fingerprint():
    raw_info = platform.node() + platform.machine() + platform.processor()
    return hashlib.sha256(raw_info.encode()).hexdigest()[:16]

def auto_activate_and_verify():
    license_path = os.path.join(get_base_dir(), "license.key")
    config_path = os.path.join(get_base_dir(), "db_config.json")
    current_hw_id = get_hardware_fingerprint()

    if not os.path.exists(license_path):
        print("⚡ No license found. Initiating Sikka auto-activation & POS discovery...")
        
        tenant_id = input("Entrez l'ID de la boutique Sikka (ex: NAS-MEDINA-01) : ").strip()
        
        print("\n--- Configuration de la base de données Sage (SQL Server) ---")
        server = input("Nom du serveur SQL [localhost\\SAGE100] : ").strip() or r"localhost\SAGE100"
        user = input("Utilisateur SQL [SAGEREADER] : ").strip() or "SAGEREADER"
        password = getpass.getpass("Mot de passe SQL : ").strip()

        if not pyodbc:
            print("❌ Erreur critique : Le module 'pyodbc' est manquant dans l'exécutable.")
            pause_on_exit()

        # Test connection & auto-create SAGEREADER user if using sa/admin rights
        print("🔄 Connexion au serveur SQL Server en cours...")
        try:
            driver = "{ODBC Driver 17 for SQL Server}"
            # Step 1: Connect to master to check/create user and list databases
            master_conn_str = f"DRIVER={driver};SERVER={server};DATABASE=master;UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;"
            conn = pyodbc.connect(master_conn_str, timeout=5)
            conn.autocommit = True
            cursor = conn.cursor()

            # Automatically run your brother's setup logic if permissions allow
            try:
                cursor.execute("IF NOT LOGINPROPERTY('SAGEREADER', 'CreateDate') IS NULL PRINT 'Exists'")
                # Auto-provision SAGEREADER if not present and we have admin rights
                cursor.execute("""
                    IF NOT EXISTS (SELECT * FROM sys.server_principals WHERE name = 'SAGEREADER')
                    BEGIN
                        CREATE LOGIN SAGEREADER WITH PASSWORD = 'S@ndaga3615', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF;
                        GRANT CONNECT SQL TO SAGEREADER;
                    END
                """)
            except Exception:
                pass # If user doesn't have sysadmin rights, skip auto-creation and proceed with provided credentials

            # List accessible production databases
            cursor.execute("SELECT name FROM sys.databases WHERE state = 0 AND name NOT IN ('master', 'tempdb', 'model', 'msdb') ORDER BY name")
            databases = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"\n❌ ERREUR DE CONNEXION SQL : {e}")
            print("💡 Conseil : Vérifiez que SQL Server accepte l'authentification mixte et que le nom du serveur est correct.")
            pause_on_exit()

        if not databases:
            print("❌ Aucune base de données de production trouvée sur ce serveur.")
            pause_on_exit()

        print("\n📋 Bases de données disponibles :")
        for i, db in enumerate(databases, 1):
            print(f"  {i}. {db}")
        
        try:
            choice = int(input(f"\nChoisissez le numéro de la base de production (1-{len(databases)}) : "))
            selected_db = databases[choice - 1]
        except Exception:
            print("❌ Choix invalide.")
            pause_on_exit()

        print(f"📌 Base sélectionnée : {selected_db}")

        # Save config locally
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
            sb.table("merchants").upsert({
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "status": "active",
                "updated_at": str(datetime.now())
            }).execute()
            
            license_data = {
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "expires_at": "2027-08-08"
            }
            with open(license_path, "w") as f:
                json.dump(license_data, f, indent=4)
                
            print(f"✅ Activation réussie ! Licence liée à la boutique : {tenant_id}")
            return tenant_id, db_config
            
        except Exception as e:
            print(f"❌ Erreur d'enregistrement cloud Supabase : {e}")
            pause_on_exit()

    try:
        with open(license_path, "r") as f:
            license_data = json.load(f)
        with open(config_path, "r") as f:
            db_config = json.load(f)
            
        if license_data.get("hw_id") != current_hw_id:
            print("ERREUR DE SÉCURITÉ : La licence ne correspond pas au matériel de cette machine.")
            pause_on_exit()
            
        return license_data.get("tenant_id"), db_config
    except Exception as e:
        print(f"ERREUR DE CONFIGURATION : {e}")
        pause_on_exit()

def pull_sage_metrics(db_config):
    if not pyodbc:
        return (0.0, 0.0, 0.0)
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
        
        # Pulling total sales from Sage table structure
        cursor.execute("SELECT SUM(CA_Vente) FROM F_DOCENTETE WHERE DO_Date >= DATEADD(day, -1, GETDATE())")
        row = cursor.fetchone()
        total_sales = float(row[0]) if row and row[0] else 0.0
        
        cursor.close()
        conn.close()
        return (total_sales, 0.0, 0.0)
    except Exception as e:
        print(f"⚠️ Avertissement lecture Sage : {e}")
        return (0.0, 0.0, 0.0)

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
        print(f"[{tenant_id}] Synchronisation réussie -> Ventes : {sales} FCFA")
    except Exception as e:
        print(f"Erreur de synchronisation cloud : {e}")

if __name__ == "__main__":
    sync()
    input("\nOpération terminée. Appuyez sur Entrée pour quitter...")
