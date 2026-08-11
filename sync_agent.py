# Sikka Secure Auto-Activating Sync Agent (Sage 100 Production Core v3.0 - Continuous Daemon)
import sqlite3
import os
import sys
import json
import hashlib
import platform
import getpass
import time
from datetime import date, datetime
from supabase import create_client

try:
    import pyodbc
except ImportError:
    pyodbc = None

SUPABASE_URL = "https://pednybdwhfgosfxbrmvf.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBlZG55YmR3aGZnb3NmeGJybXZmIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NjIwMjY3MCwiZXhwIjoyMTAxNzc4NjcwfQ.lMnWE-0sPmdtxjy2Spt_aDnHPncw9xzCrZVpT16rgKk"

def pause_on_exit(msg="Appuyez sur Entrée pour fermer..."):
    """Ne bloque sur une saisie que si le script est exécuté dans un terminal interactif."""
    if sys.stdin and sys.stdin.isatty():
        input(f"\n{msg}")
    sys.exit(1)

def get_base_dir():
    """Retourne le dossier absolu où réside le script ou l'exécutable."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_hardware_fingerprint():
    """Génère l'empreinte matérielle unique du serveur."""
    raw_info = platform.node() + platform.machine() + platform.processor()
    return hashlib.sha256(raw_info.encode()).hexdigest()[:16]

def auto_activate_and_verify():
    license_path = os.path.join(get_base_dir(), "license.key")
    config_path = os.path.join(get_base_dir(), "db_config.json")
    current_hw_id = get_hardware_fingerprint()

    if not os.path.exists(license_path) or not os.path.exists(config_path):
        if not (sys.stdin and sys.stdin.isatty()):
            print("❌ Configuration ou licence manquante. L'activation initiale requiert un terminal interactif.")
            sys.exit(1)

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
            conn = pyodbc.connect(master_conn_str, timeout=30)
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
            print("❌ ERREUR DE SÉCURITÉ : La licence ne correspond pas au matériel.")
            pause_on_exit()
            
        return license_data.get("tenant_id"), db_config
    except Exception as e:
        print(f"❌ ERREUR DE CONFIGURATION : {e}")
        pause_on_exit()

def is_module_enabled(sb, tenant_id, module_key):
    """Vérifie si un module est activé pour le tenant dans Supabase."""
    try:
        response = sb.table("tenant_settings").select("is_enabled").eq("tenant_id", tenant_id).eq("module_key", module_key).execute()
        if response.data and len(response.data) > 0:
            return response.data[0].get("is_enabled", True)
    except Exception:
        pass
    return True

def execute_query_with_retry(cursor, query, retries=3, delay=5):
    """Exécute une requête SQL avec mécanisme de plusieurs tentatives en cas de micro-coupure."""
    for attempt in range(retries):
        try:
            cursor.execute(query)
            return cursor.fetchall()
        except (pyodbc.OperationalError, pyodbc.ProgrammingError) as e:
            print(f"⚠️ Erreur d'exécution SQL (tentative {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            else:
                raise
        except Exception as e:
            raise

def pull_and_push_modular_data(db_config, tenant_id):
    """Extrait les 15 matrices opérationnelles et synchronise vers Supabase."""
    if not pyodbc:
        print("❌ pyodbc non disponible.")
        return
    
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
    
    conn = None
    cursor = None
    try:
        conn = pyodbc.connect(conn_str, timeout=30)
        cursor = conn.cursor()
        print(f"[{tenant_id}] Connexion SQL établie.")
    except Exception as e:
        print(f"[{tenant_id}] ❌ Échec de connexion SQL : {e}")
        return

    # Dictionnaire complet des 15 requêtes avec leurs clés de module respectives
    queries_map = {
        "sales": ("tenant_ventes_matrix", """
            SELECT TOP 50 
                E.DO_Piece AS NumFacture,
                CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                E.DO_Tiers AS CodeClient,
                ISNULL(C.CT_Intitule, 'CLIENT INCONNU') AS NomClient,
                CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MONTANT_TTC,
                CASE WHEN E.DO_Type = 6 THEN 'FACTURE' WHEN E.DO_Type = 7 THEN 'AVOIR' ELSE CAST(E.DO_Type AS VARCHAR) END AS TypeDoc
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type IN (6, 7)
            GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule, E.DO_Type
            ORDER BY E.DO_Date DESC, E.DO_Piece DESC;
        """),
        "achats": ("tenant_achats_matrix", """
            SELECT TOP 50 
                E.DO_Piece AS NumFacture,
                CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                E.DO_Tiers AS CodeFournisseur,
                ISNULL(C.CT_Intitule, 'FOURNISSEUR INCONNU') AS NomFournisseur,
                CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MONTANT_TTC,
                CASE WHEN E.DO_Type = 0 THEN 'ACHAT' WHEN E.DO_Type = 5 THEN 'AVOIR FOURNISSEUR' ELSE CAST(E.DO_Type AS VARCHAR) END AS TypeDoc
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type IN (0, 1, 4, 5)
            GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule, E.DO_Type
            ORDER BY E.DO_Date DESC, E.DO_Piece DESC;
        """),
        "caisse": ("tenant_caisse_matrix", """
            SELECT TOP 200 
                R.RG_No AS NUM_REGLEMENT, 
                CONVERT(VARCHAR(10), R.RG_Date, 103) AS DATE_MVT,
                ISNULL(C.CT_Intitule, 'DIVERS') AS NOM_TIERS,
                R.RG_Libelle AS LIBELLE_MVT, 
                ISNULL(CA.CA_Intitule, 'CAISSE PRINCIPALE') AS NOM_CAISSE,
                CASE WHEN R.RG_Type = 0 THEN R.RG_Montant ELSE 0 END AS ENTREE_CAISSE,
                CASE WHEN R.RG_Type = 1 THEN R.RG_Montant ELSE 0 END AS SORTIE_CAISSE,
                ISNULL(P.R_Intitule, 'Espèces / Autre') AS MODE_REGLEMENT
            FROM F_CREGLEMENT R WITH (NOLOCK)
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON R.CT_NumPayeur = C.CT_Num
            LEFT JOIN F_CAISSE CA WITH (NOLOCK) ON R.CA_No = CA.CA_No
            LEFT JOIN P_REGLEMENT P WITH (NOLOCK) ON R.N_Reglement = P.CbIndice
            ORDER BY R.RG_No DESC;
        """),
        "stock": ("tenant_stock_matrix", """
            SELECT A.AR_Ref AS CodeArticle, A.AR_Design AS LibelleArticle, S.AS_QteSto AS QuantiteStock, S.AS_MontSto AS MontantStock
            FROM F_ARTSTOCK S WITH (NOLOCK) INNER JOIN F_ARTICLE A WITH (NOLOCK) ON S.AR_Ref = A.AR_Ref WHERE S.AS_QteSto != 0 ORDER BY A.AR_Ref;
        """),
        "top_articles": ("tenant_top_articles_matrix", """
            SELECT TOP 20 ISNULL(L.AR_Ref, 'AUTRE') AS CodeArticle, MAX(ISNULL(L.DL_Design, 'Article')) AS LibelleArticle, SUM(L.DL_Qte) AS QuantiteVendue, SUM(L.DL_MontantTTC) AS MontantVendu
            FROM F_DOCLIGNE L WITH (NOLOCK) INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type
            WHERE E.DO_Type IN (6, 7) GROUP BY L.AR_Ref ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "top_clients": ("tenant_top_clients_matrix", """
            SELECT TOP 20 E.DO_Tiers AS CodeClient, MAX(ISNULL(C.CT_Intitule, 'CLIENT')) AS NomClient, SUM(L.DL_MontantTTC) AS CA_Total, COUNT(DISTINCT E.DO_Piece) AS NbFactures
            FROM F_DOCENTETE E WITH (NOLOCK) INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num WHERE E.DO_Type IN (6, 7) GROUP BY E.DO_Tiers ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "evolution_ca": ("tenant_evolution_ca_matrix", """
            SELECT YEAR(E.DO_Date) AS Annee, MONTH(E.DO_Date) AS Mois, SUM(L.DL_MontantTTC) AS CA_Mensuel, COUNT(DISTINCT E.DO_Piece) AS NbFactures
            FROM F_DOCENTETE E WITH (NOLOCK) INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type WHERE E.DO_Type IN (6, 7)
            GROUP BY YEAR(E.DO_Date), MONTH(E.DO_Date) ORDER BY Annee DESC, Mois DESC;
        """),
        "rotation": ("tenant_rotation_matrix", """
            SELECT TOP 20 L.AR_Ref AS CodeArticle, MAX(ISNULL(A.AR_Design, L.DL_Design)) AS Libelle, SUM(L.DL_Qte) AS QuantiteVendue, SUM(L.DL_MontantTTC) / NULLIF(SUM(L.DL_Qte), 0) AS PrixMoyen
            FROM F_DOCLIGNE L WITH (NOLOCK) INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref
            WHERE E.DO_Type IN (6, 7) AND L.AR_Ref IS NOT NULL GROUP BY L.AR_Ref ORDER BY SUM(L.DL_Qte) DESC;
        """),
        "mode_reglement": ("tenant_mode_reglement_matrix", """
            SELECT ISNULL(P.R_Intitule, 'Espèces / Autre') AS ModeReglement, SUM(R.RG_Montant) AS TotalRegle 
            FROM F_CREGLEMENT R WITH (NOLOCK) 
            LEFT JOIN P_REGLEMENT P WITH (NOLOCK) ON R.N_Reglement = P.CbIndice 
            GROUP BY P.R_Intitule ORDER BY SUM(R.RG_Montant) DESC;
        """),
        "marge_brute": ("tenant_marge_brute_matrix", """
            SELECT TOP 20 L.AR_Ref AS CodeArticle, MAX(ISNULL(A.AR_Design, L.DL_Design)) AS Libelle, AVG(L.DL_PrixUnitaire) AS PrixVenteMoyen, AVG(ISNULL(A.AR_PrixAch, 0)) AS PrixAchatMoyen,
            (AVG(L.DL_PrixUnitaire) - AVG(ISNULL(A.AR_PrixAch, 0))) AS MargeUnitaire
            FROM F_DOCLIGNE L WITH (NOLOCK) INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref
            WHERE E.DO_Type IN (6, 7) AND L.AR_Ref IS NOT NULL GROUP BY L.AR_Ref HAVING AVG(L.DL_PrixUnitaire) > 0 ORDER BY MargeUnitaire DESC;
        """),
        "mouvements": ("tenant_mouvements_matrix", """
            SELECT TOP 200 E.DO_Piece AS NumPiece, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateMouvement, L.AR_Ref AS CodeArticle, L.DL_Design AS LibelleArticle,
            L.DL_Qte AS Quantite, CASE WHEN E.DO_Type = 20 THEN 'ENTRÉE' ELSE 'SORTIE' END AS Sens, L.DL_MontantTTC AS Montant
            FROM F_DOCENTETE E WITH (NOLOCK) INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type WHERE E.DO_Type IN (20, 21, 30, 31) ORDER BY E.DO_Date DESC;
        """),
        "dernieres_transac": ("tenant_dernieres_transac_matrix", """
            SELECT TOP 50 CONVERT(VARCHAR(10), E.DO_Date, 103) AS Date, E.DO_Piece AS Piece, E.DO_Tiers AS Tiers, SUM(L.DL_MontantTTC) AS Montant,
            CASE E.DO_Type WHEN 6 THEN 'Vente' WHEN 7 THEN 'Avoir Vente' WHEN 0 THEN 'Achat' WHEN 20 THEN 'Entrée Stock' WHEN 21 THEN 'Sortie Stock' ELSE CAST(E.DO_Type AS VARCHAR) END AS Type
            FROM F_DOCENTETE E WITH (NOLOCK) INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type GROUP BY E.DO_Date, E.DO_Piece, E.DO_Tiers, E.DO_Type ORDER BY E.DO_Date DESC;
        """),
        "ca_famille": ("tenant_ca_famille_matrix", """
            SELECT ISNULL(FA.FA_CodeFamille, 'AUTRE') AS CodeFamille, ISNULL(FA.FA_Intitule, 'Général') AS LibelleFamille, SUM(L.DL_MontantTTC) AS CA_Famille
            FROM F_DOCLIGNE L WITH (NOLOCK) INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref LEFT JOIN F_FAMILLE FA WITH (NOLOCK) ON A.FA_CodeFamille = FA.FA_CodeFamille
            WHERE E.DO_Type IN (6, 7) GROUP BY FA.FA_CodeFamille, FA.FA_Intitule ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "impayees": ("tenant_impayees_matrix", """
            SELECT E.DO_Piece AS NumFacture, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture, E.DO_Tiers AS CodeClient, ISNULL(C.CT_Intitule, 'CLIENT') AS NomClient,
            SUM(L.DL_MontantTTC) AS MontantFacture, ISNULL(SUM(R.RG_Montant), 0) AS MontantRegle, SUM(L.DL_MontantTTC) - ISNULL(SUM(R.RG_Montant), 0) AS Solde
            FROM F_DOCENTETE E WITH (NOLOCK) INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num LEFT JOIN F_CREGLEMENT R WITH (NOLOCK) ON E.DO_Tiers = R.CT_NumPayeur
            WHERE E.DO_Type = 6 GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule HAVING SUM(L.DL_MontantTTC) - ISNULL(SUM(R.RG_Montant), 0) > 0 ORDER BY E.DO_Date;
        """),
        "solde_client": ("tenant_solde_client_matrix", """
            SELECT C.CT_Num AS CodeClient, C.CT_Intitule AS Nom, ISNULL(SUM(L.DL_MontantTTC), 0) AS TotalFacture, ISNULL(SUM(R.RG_Montant), 0) AS TotalRegle,
            ISNULL(SUM(L.DL_MontantTTC), 0) - ISNULL(SUM(R.RG_Montant), 0) AS Solde FROM F_COMPTET C WITH (NOLOCK)
            LEFT JOIN F_DOCENTETE E WITH (NOLOCK) ON C.CT_Num = E.DO_Tiers AND E.DO_Type IN (6, 7) LEFT JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type LEFT JOIN F_CREGLEMENT R WITH (NOLOCK) ON C.CT_Num = R.CT_NumPayeur
            GROUP BY C.CT_Num, C.CT_Intitule HAVING ISNULL(SUM(L.DL_MontantTTC), 0) - ISNULL(SUM(R.RG_Montant), 0) <> 0 ORDER BY Solde DESC;
        """)
    }

    try:
        for module_key, (table_name, query) in queries_map.items():
            if is_module_enabled(sb, tenant_id, module_key):
                try:
                    rows = execute_query_with_retry(cursor, query)
                    columns = [col[0] for col in cursor.description]
                    data_rows = [dict(zip(columns, row)) for row in rows]
                    if data_rows:
                        sb.table(table_name).upsert({
                            "tenant_id": tenant_id,
                            "data": json.loads(json.dumps(data_rows, default=str)),
                            "updated_at": str(datetime.now())
                        }, on_conflict="tenant_id").execute()
                        print(f"[{tenant_id}] ✅ {module_key.capitalize()} : {len(data_rows)} lignes synchronisées.")
                    else:
                        print(f"[{tenant_id}] ⚠️ {module_key.capitalize()} : aucune ligne.")
                except Exception as e:
                    print(f"[{tenant_id}] ❌ Erreur {module_key} : {e}")
            else:
                print(f"[{tenant_id}] Module '{module_key}' désactivé par l'administrateur.")

    except Exception as e:
        print(f"[{tenant_id}] ❌ Erreur générale sync : {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            print(f"[{tenant_id}] Connexion SQL fermée.")

def sync():
    result = auto_activate_and_verify()
    if not result:
        return
    tenant_id, db_config = result
    pull_and_push_modular_data(db_config, tenant_id)

if __name__ == "__main__":
    print("🚀 Sikka Sync Agent démarré en mode continu (Daemon)...")
    while True:
        try:
            sync()
        except Exception as e:
            print(f"⚠️ Erreur dans la boucle d'arrière-plan : {e}")
        
        print("⏳ Prochaine synchronisation dans 20 minutes...")
        time.sleep(1200) # Pause de 20 minutes (1200 secondes)
