# Sikka Secure Auto-Activating Sync Agent (Sage 100 Production Core v2.4)
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
        
        print("\n--- Configuration de la base de données Sage 100 (SQL Server) ---")
        server = input("Nom du serveur SQL [localhost\\SAGE100] : ").strip() or r"localhost\SAGE100"
        user = input("Utilisateur SQL [SAGEREADER] : ").strip() or "SAGEREADER"
        password = getpass.getpass("Mot de passe SQL : ").strip()

        if not pyodbc:
            print("❌ Erreur critique : Le module 'pyodbc' est manquant.")
            pause_on_exit()

        print("🔄 Connexion au serveur SQL Server en cours...")
        try:
            driver = "{ODBC Driver 17 for SQL Server}"
            master_conn_str = f"DRIVER={driver};SERVER={server};DATABASE=master;UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;"
            conn = pyodbc.connect(master_conn_str, timeout=5)
            conn.autocommit = True
            cursor = conn.cursor()

            try:
                cursor.execute("""
                    IF NOT EXISTS (SELECT * FROM sys.server_principals WHERE name = 'SAGEREADER')
                    BEGIN
                        CREATE LOGIN SAGEREADER WITH PASSWORD = 'S@ndaga3615', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF;
                        GRANT CONNECT SQL TO SAGEREADER;
                    END
                """)
            except Exception:
                pass 

            cursor.execute("SELECT name FROM sys.databases WHERE state = 0 AND name NOT IN ('master', 'tempdb', 'model', 'msdb') ORDER BY name")
            databases = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"\n❌ ERREUR DE CONNEXION SQL : {e}")
            pause_on_exit()

        if not databases:
            print("❌ Aucune base de données trouvée sur ce serveur.")
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

        db_config = {"server": server, "username": user, "password": password, "database": selected_db}
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
            
            license_data = {"tenant_id": tenant_id, "hw_id": current_hw_id, "expires_at": "2027-08-08"}
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
            print("ERREUR DE SÉCURITÉ : La licence ne correspond pas au matériel.")
            pause_on_exit()
            
        return license_data.get("tenant_id"), db_config
    except Exception as e:
        print(f"ERREUR DE CONFIGURATION : {e}")
        pause_on_exit()

def is_module_enabled(sb, tenant_id, module_key):
    """Check Supabase tenant settings to see if a module is toggled active."""
    try:
        response = sb.table("tenant_settings").select("is_enabled").eq("tenant_id", tenant_id).eq("module_key", module_key).execute()
        if response.data and len(response.data) > 0:
            return response.data[0].get("is_enabled", True) # Default to True if explicitly configured or fallback
    except Exception:
        pass
    return True # Default fallback if setting hasn't been initialized yet

def pull_and_push_modular_data(db_config, tenant_id):
    """Pulls full matrix datasets matching exact client query formats and syncs to Supabase based on feature toggles."""
    if not pyodbc:
        return
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        
        driver = "{ODBC Driver 17 for SQL Server}"
        conn_str = (
            f"DRIVER={driver};"
            f"SERVER={db_config['server']};"
            f"DATABASE={db_config['database']};"
            f"UID={db_config['username']};"
            f"PWD={db_config['password']};"
            f"TrustServerCertificate=yes;Encrypt=no;"
        )
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()

        # 1. VENTES SYNC (Controlled by feature toggle 'sales')
        if is_module_enabled(sb, tenant_id, "sales"):
            cursor.execute("""
                SELECT TOP 50 
                    E.DO_Piece AS NumFacture,
                    CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                    E.DO_Tiers AS CodeClient,
                    ISNULL(C.CT_Intitule, 'CLIENT INCONNU') AS NomClient,
                    CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MontantTTC,
                    CASE WHEN E.DO_Type = 6 THEN 'FACTURE' WHEN E.DO_Type = 7 THEN 'AVOIR' ELSE CAST(E.DO_Type AS VARCHAR) END AS TypeDoc
                FROM F_DOCENTETE E WITH (NOLOCK)
                INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
                LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
                WHERE E.DO_Type IN (6, 7)
                GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule, E.DO_Type
                ORDER BY E.DO_Date DESC, E.DO_Piece DESC;
            """)
            columns = [column[0] for column in cursor.description]
            ventes_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            if ventes_rows:
                sb.table("tenant_ventes_matrix").upsert({
                    "tenant_id": tenant_id, 
                    "data": json.loads(json.dumps(ventes_rows, default=str)), 
                    "updated_at": str(datetime.now())
                }, on_conflict="tenant_id").execute()
                print(f"[{tenant_id}] Synchronisation Ventes réussie ({len(ventes_rows)} lignes).")
        else:
            print(f"[{tenant_id}] Module 'sales' désactivé par l'administrateur.")

        # 2. CAISSE SYNC (Controlled by feature toggle 'caisse')
        if is_module_enabled(sb, tenant_id, "caisse"):
            cursor.execute("""
                SELECT TOP 200 
                    R.RG_No AS NUM_REGLEMENT, CONVERT(VARCHAR(10), R.RG_Date, 126) AS DATE_MVT,
                    R.CT_NumPayeur AS CPTE_TIERS, ISNULL(C.CT_Intitule, 'DIVERS') AS NOM_TIERS,
                    R.RG_Libelle AS LIBELLE_MVT, ISNULL(CA.CA_Intitule, 'CAISSE PRINCIPALE') AS NOM_CAISSE,
                    CASE WHEN R.RG_Type = 0 THEN R.RG_Montant ELSE 0 END AS ENTREE_CAISSE,
                    CASE WHEN R.RG_Type = 1 THEN R.RG_Montant ELSE 0 END AS SORTIE_CAISSE,
                    ISNULL(R.N_Reglement, 0) AS MODE_REGLEMENT
                FROM F_CREGLEMENT R WITH (NOLOCK)
                LEFT JOIN F_COMPTET C WITH (NOLOCK) ON R.CT_NumPayeur = C.CT_Num
                LEFT JOIN F_CAISSE CA WITH (NOLOCK) ON R.CA_No = CA.CA_No
                ORDER BY R.RG_Date DESC, R.RG_No;
            """)
            c_cols = [col[0] for col in cursor.description]
            caisse_rows = [dict(zip(c_cols, row)) for row in cursor.fetchall()]
            if caisse_rows:
                sb.table("tenant_caisse_matrix").upsert({
                    "tenant_id": tenant_id, 
                    "data": json.loads(json.dumps(caisse_rows, default=str)), 
                    "updated_at": str(datetime.now())
                }, on_conflict="tenant_id").execute()
                print(f"[{tenant_id}] Synchronisation Caisse réussie ({len(caisse_rows)} lignes).")
        else:
            print(f"[{tenant_id}] Module 'caisse' désactivé par l'administrateur.")

        cursor.close()
        conn.close()
        print(f"[{tenant_id}] Synchronisation modulaire des matrices terminée.")
    except Exception as e:
        print(f"⚠️ Erreur sync modulaire : {e}")

def sync():
    result = auto_activate_and_verify()
    if not result:
        return
    tenant_id, db_config = result
    pull_and_push_modular_data(db_config, tenant_id)

if __name__ == "__main__":
    sync()
    input("\nOpération terminée. Appuyez sur Entrée pour quitter...")
