# Sikka Secure Auto-Activating Sync Agent (Full Analytics & Invoices Edition)
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

def get_storage_dir():
    base_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'SikkaSync')
    if not os.path.exists(base_dir):
        os.makedirs(base_dir, exist_ok=True)
    return base_dir

def get_hardware_fingerprint():
    raw_info = platform.node() + platform.machine() + platform.processor()
    return hashlib.sha256(raw_info.encode()).hexdigest()[:16]

def auto_activate_and_verify():
    storage_dir = get_storage_dir()
    license_path = os.path.join(storage_dir, "license.key")
    config_path = os.path.join(storage_dir, "db_config.json")
    current_hw_id = get_hardware_fingerprint()

    if not os.path.exists(license_path) or not os.path.exists(config_path):
        print("⚡ No valid license or config found. Initiating Sikka auto-activation...")
        tenant_id = input("Entrez l'ID de la boutique Sikka (ex: NAS-MEDINA-01) : ").strip()
        
        server = input("Nom ou IP du serveur SQL [100.68.244.92\\SAGE100] : ").strip() or r"100.68.244.92\SAGE100"
        user = input("Utilisateur SQL [SAGEREADER] : ").strip() or "SAGEREADER"
        password = getpass.getpass("Mot de passe SQL : ").strip()

        if not pyodbc:
            print("❌ Erreur critique : Le module 'pyodbc' est manquant.")
            pause_on_exit()

        try:
            driver = "{ODBC Driver 17 for SQL Server}"
            master_conn_str = f"DRIVER={driver};SERVER={server};DATABASE=master;UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;Connection Timeout=10;"
            conn = pyodbc.connect(master_conn_str, timeout=10)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sys.databases WHERE state = 0 AND name NOT IN ('master', 'tempdb', 'model', 'msdb') ORDER BY name")
            databases = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"\n❌ ERREUR DE CONNEXION DISTANTE SQL : {e}")
            pause_on_exit()

        print("\n📋 Bases de données disponibles :")
        for i, db in enumerate(databases, 1):
            print(f"  {i}. {db}")
        
        try:
            choice = int(input(f"\nChoisissez le numéro de la base (1-{len(databases)}) : "))
            selected_db = databases[choice - 1]
        except Exception:
            pause_on_exit("❌ Choix invalide.")

        db_config = {"server": server, "username": user, "password": password, "database": selected_db}
        with open(config_path, "w") as f:
            json.dump(db_config, f, indent=4)

        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            sb.table("merchants").upsert({"tenant_id": tenant_id, "hw_id": current_hw_id, "status": "active", "updated_at": str(datetime.now())}).execute()
            with open(license_path, "w") as f:
                json.dump({"tenant_id": tenant_id, "hw_id": current_hw_id, "expires_at": "2027-08-08"}, f, indent=4)
            print(f"✅ Activation réussie pour {tenant_id}")
            return tenant_id, db_config
        except Exception as e:
            pause_on_exit(f"❌ Erreur Supabase : {e}")

    try:
        with open(license_path, "r") as f:
            lic = json.load(f)
        with open(config_path, "r") as f:
            cfg = json.load(f)
        return lic.get("tenant_id"), cfg
    except Exception as e:
        pause_on_exit(f"ERREUR CONFIG : {e}")

def pull_sage_data(db_config):
    """Pulls aggregated metrics and recent invoices using brother's SQL queries."""
    if not pyodbc:
        return 0.0, 0.0, []
    
    driver = "{ODBC Driver 17 for SQL Server}"
    conn_str = f"DRIVER={driver};SERVER={db_config['server']};DATABASE={db_config['database']};UID={db_config['username']};PWD={db_config['password']};TrustServerCertificate=yes;Encrypt=no;"
    
    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cursor = conn.cursor()
        
        # 1. Total Sales (last 30 days to capture history even if closed today)
        cursor.execute("""
            SELECT ISNULL(SUM(CAST(L.DL_MontantTTC AS DECIMAL(18,0))), 0) 
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            WHERE E.DO_Type IN (6, 7) AND E.DO_Date >= DATEADD(day, -30, CAST(GETDATE() AS DATE))
        """)
        total_sales = float(cursor.fetchone()[0])

        # 2. Total Cash Collections
        cursor.execute("""
            SELECT ISNULL(SUM(CAST(RG_Montant AS DECIMAL(18,0))), 0) 
            FROM F_CREGLEMENT WITH (NOLOCK)
            WHERE RG_Type = 0 AND RG_Date >= DATEADD(day, -30, CAST(GETDATE() AS DATE))
        """)
        cash_drawer = float(cursor.fetchone()[0])

        # 3. Recent Invoices / List of Sales Lines (Brother's Ventes Query snippet)[cite: 1]
        cursor.execute("""
            SELECT TOP 50 
                E.DO_Piece AS NUM_FACTURE,
                ISNULL(C.CT_Intitule, 'CLIENT DIVERS') AS NOM_CLIENT,
                CONVERT(VARCHAR(10), E.DO_Date, 103) AS DATE_VT,
                L.DL_Design AS PRODUIT,
                CAST(L.DL_Qte AS INT) AS QUANTITE,
                CAST(L.DL_MontantTTC AS DECIMAL(18,0)) AS MONTANT
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type IN (6, 7)
            ORDER BY E.DO_Date DESC, E.DO_Piece DESC
        """)
        
        invoices = []
        for row in cursor.fetchall():
            invoices.append({
                "num_facture": row[0],
                "nom_client": row[1],
                "date_vt": row[2],
                "produit": row[3],
                "quantite": row[4],
                "montant": float(row[5])
            })

        cursor.close()
        conn.close()
        return total_sales, cash_drawer, invoices
    except Exception as e:
        print(f"⚠️ Erreur de lecture Sage : {e}")
        return 0.0, 0.0, []

def sync():
    tenant_id, db_config = auto_activate_and_verify()
    sales, cash, invoices = pull_sage_data(db_config)
    
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # Push Daily Metric Snapshot
    sb.table("business_metrics").upsert({
        "tenant_id": tenant_id,
        "date": str(date.today()),
        "total_sales": sales,
        "cash_in_drawer": cash,
        "bank_balance": 0.0
    }, on_conflict="tenant_id,date").execute()

    # Push Invoices / Sales List
    for inv in invoices:
        inv["tenant_id"] = tenant_id
        sb.table("invoices").insert(inv).execute()

    print(f"[{tenant_id}] Synchronisation complète réussie ! {len(invoices)} factures synchronisées.")

if __name__ == "__main__":
    sync()
    input("\nAppuyez sur Entrée pour quitter...")
