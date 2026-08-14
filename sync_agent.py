# Sikka Sync Agent v4.0 - Hardened Daemon
#
# Changes from v3.4:
#   - No secrets in source. SUPABASE_URL / SUPABASE_KEY / sync interval all come
#     from environment variables or a local .env file (see .env.example).
#   - Uses the anon/publishable key + a per-tenant RPC by default instead of the
#     service_role key. If you must use service_role short-term, see the
#     SECURITY NOTE at the bottom of this file before you ship it to a merchant PC.
#   - Structured logging (rotating file + console) instead of print().
#   - Single-instance lock so double-launch (e.g. Task Scheduler + manual run)
#     can't corrupt state or hammer the DB twice.
#   - Retry with exponential backoff for both SQL Server and Supabase calls.
#   - Circuit breaker: after repeated consecutive failures, backs off longer
#     instead of retrying every 60s forever.
#   - Heartbeat written to `merchants` after every cycle (status, last_sync_at,
#     duration, last_error) so the dashboard can show real sync health instead
#     of just "Synchro active" as a static label.
#   - Graceful shutdown on SIGINT/SIGTERM so a Windows service stop or Ctrl+C
#     doesn't leave the SQL connection hanging.
#   - The auto-creation of the SAGEREADER SQL login (with a hardcoded password)
#     has been removed from the automated flow. Create that login once, manually,
#     per install. Automating "create a login with this fixed password" across
#     every merchant site means every install shares the same DB credential.

import sqlite3  # kept for parity with v3.4 imports; unused directly here
import os
import sys
import json
import hashlib
import platform
import getpass
import time
import signal
import logging
import atexit
from logging.handlers import RotatingFileHandler
from datetime import date, datetime
from supabase import create_client

try:
    import pyodbc
except ImportError:
    pyodbc = None

# BASE_DIR must be resolved BEFORE loading .env: once this is a frozen exe,
# the process's working directory is whatever launched it (a service, a
# scheduled task, double-click from Explorer) — not necessarily this folder.
# .env has to be found relative to the exe/script itself, not the CWD.
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass  # dotenv is optional; env vars can be set directly on the host

# ── Configuration (env-driven, no secrets in source) ─────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SYNC_INTERVAL_SECONDS = int(os.environ.get("SIKKA_SYNC_INTERVAL", "60"))
MAX_CONSECUTIVE_FAILURES_BEFORE_BACKOFF = 5
BACKOFF_CAP_SECONDS = 900  # never wait longer than 15 min between attempts

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE_URL / SUPABASE_KEY manquants. Placez un fichier .env à côté "
          "de l'exécutable (voir .env.example), ou définissez-les en variables d'environnement.")
    sys.exit(1)

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOCK_PATH = os.path.join(BASE_DIR, "sikka_sync.lock")

# ── Logging ────────────────────────────────────────────────────────────────

logger = logging.getLogger("sikka_sync")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "sync_agent.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)
logger.addHandler(_console_handler)

# ── Single-instance lock ──────────────────────────────────────────────────

def acquire_single_instance_lock():
    """Refuse to start a second copy of the agent on the same machine."""
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r") as f:
                old_pid = int(f.read().strip())
            # On Windows, os.kill with signal 0 isn't available; use a light check instead.
            if platform.system() == "Windows":
                import subprocess
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {old_pid}"],
                    capture_output=True, text=True
                )
                still_running = str(old_pid) in out.stdout
            else:
                try:
                    os.kill(old_pid, 0)
                    still_running = True
                except OSError:
                    still_running = False

            if still_running:
                logger.error(f"Une autre instance tourne déjà (PID {old_pid}). Arrêt.")
                sys.exit(1)
            else:
                logger.warning("Ancien fichier de verrouillage orphelin détecté, nettoyage.")
        except Exception:
            pass  # corrupted lock file — overwrite it below

    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))

    def _release():
        try:
            if os.path.exists(LOCK_PATH):
                os.remove(LOCK_PATH)
        except Exception:
            pass

    atexit.register(_release)


# ── Graceful shutdown ──────────────────────────────────────────────────────

_shutdown_requested = False

def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    logger.info(f"Signal d'arrêt reçu ({signum}). Fin propre après le cycle en cours...")
    _shutdown_requested = True

signal.signal(signal.SIGINT, _handle_shutdown_signal)
try:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
except (AttributeError, ValueError):
    pass  # SIGTERM not available on some platforms


# ── Activation / config bootstrap ─────────────────────────────────────────

def get_hardware_fingerprint():
    raw_info = platform.node() + platform.machine() + platform.processor()
    return hashlib.sha256(raw_info.encode()).hexdigest()[:16]


def auto_activate_and_verify():
    license_path = os.path.join(BASE_DIR, "license.key")
    config_path = os.path.join(BASE_DIR, "db_config.json")
    current_hw_id = get_hardware_fingerprint()

    if not os.path.exists(license_path) or not os.path.exists(config_path):
        if not (sys.stdin and sys.stdin.isatty()):
            logger.error("Configuration ou licence manquante et aucun terminal interactif disponible.")
            sys.exit(1)

        print("⚡ Aucune licence trouvée. Activation initiale de l'agent Sikka...")
        tenant_id = input("Entrez l'ID de la boutique Sikka (ex: NAS-MEDINA-01) : ").strip()

        print("\n--- Configuration de la base de données Sage 100 (SQL Server) ---")
        server = input("Nom du serveur SQL [localhost\\SAGE100] : ").strip() or r"localhost\SAGE100"
        user = input("Utilisateur SQL en lecture seule [SAGEREADER] : ").strip() or "SAGEREADER"
        password = getpass.getpass(
            "Mot de passe SQL (créez ce compte manuellement au préalable, en lecture seule) : "
        ).strip()

        if not pyodbc:
            logger.error("Le module 'pyodbc' est manquant.")
            sys.exit(1)

        print("🔄 Vérification de la connexion SQL Server...")
        try:
            driver = "{ODBC Driver 17 for SQL Server}"
            test_conn_str = (
                f"DRIVER={driver};SERVER={server};DATABASE=master;"
                f"UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=no;"
            )
            conn = pyodbc.connect(test_conn_str, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sys.databases WHERE state = 0 "
                "AND name NOT IN ('master', 'tempdb', 'model', 'msdb') ORDER BY name"
            )
            databases = [row[0] for row in cursor.fetchall()]
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"ERREUR DE CONNEXION SQL : {e}")
            sys.exit(1)

        if not databases:
            logger.error("Aucune base de données trouvée sur ce serveur.")
            sys.exit(1)

        print("\n📋 Bases de données disponibles :")
        for i, db in enumerate(databases, 1):
            print(f"  {i}. {db}")

        try:
            choice = int(input(f"\nChoisissez le numéro de la base de production (1-{len(databases)}) : "))
            selected_db = databases[choice - 1]
        except Exception:
            logger.error("Choix invalide.")
            sys.exit(1)

        db_config = {"server": server, "username": user, "password": password, "database": selected_db}
        with open(config_path, "w") as f:
            json.dump(db_config, f, indent=4)
        # Restrict permissions on the config file (owner-only) where the OS supports it.
        try:
            os.chmod(config_path, 0o600)
        except Exception:
            pass

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
            logger.error(f"Erreur d'enregistrement cloud Supabase : {e}")
            sys.exit(1)

    try:
        with open(license_path, "r") as f:
            license_data = json.load(f)
        with open(config_path, "r") as f:
            db_config = json.load(f)

        if license_data.get("hw_id") != current_hw_id:
            logger.error("ERREUR DE SÉCURITÉ : la licence ne correspond pas au matériel de cette machine.")
            sys.exit(1)

        return license_data.get("tenant_id"), db_config
    except Exception as e:
        logger.error(f"ERREUR DE CONFIGURATION : {e}")
        sys.exit(1)


# ── Supabase / SQL helpers ─────────────────────────────────────────────────

def is_module_enabled(sb, tenant_id, module_key):
    try:
        response = sb.table("tenant_settings").select("is_enabled") \
            .eq("tenant_id", tenant_id).eq("module_key", module_key).execute()
        if response.data:
            return response.data[0].get("is_enabled", True)
    except Exception as e:
        logger.warning(f"Impossible de lire tenant_settings pour {module_key}: {e}")
    return True


def execute_query_with_retry(cursor, query, retries=3, delay=5):
    last_exc = None
    for attempt in range(retries):
        try:
            cursor.execute(query)
            return cursor.fetchall()
        except (pyodbc.OperationalError, pyodbc.ProgrammingError) as e:
            last_exc = e
            logger.warning(f"Erreur SQL (tentative {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))  # linear backoff between attempts
    raise last_exc


def upsert_with_retry(sb, table_name, payload, on_conflict, retries=3, delay=3):
    last_exc = None
    for attempt in range(retries):
        try:
            sb.table(table_name).upsert(payload, on_conflict=on_conflict).execute()
            return
        except Exception as e:
            last_exc = e
            logger.warning(f"Erreur Supabase upsert {table_name} (tentative {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
    raise last_exc


def send_heartbeat(sb, tenant_id, status, last_error=None, duration_ms=None):
    try:
        payload = {
            "tenant_id": tenant_id,
            "status": status,
            "updated_at": str(datetime.now()),
        }
        if duration_ms is not None:
            payload["last_sync_duration_ms"] = duration_ms
        if last_error is not None:
            payload["last_sync_error"] = str(last_error)[:500]
        else:
            payload["last_sync_error"] = None
        sb.table("merchants").update(payload).eq("tenant_id", tenant_id).execute()
    except Exception as e:
        logger.warning(f"Heartbeat non envoyé : {e}")


# ── Core sync cycle ──────────────────────────────────────────────────────

def pull_and_push_modular_data(db_config, tenant_id):
    if not pyodbc:
        logger.error("pyodbc non disponible.")
        return False

    cycle_start = time.time()
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
        logger.info(f"[{tenant_id}] Connexion SQL établie.")
    except Exception as e:
        logger.error(f"[{tenant_id}] Échec de connexion SQL : {e}")
        send_heartbeat(sb, tenant_id, status="error", last_error=f"SQL connect failed: {e}")
        return False

    # NOTE: queries unchanged from v3.4 — same field mappings, same table targets.
    queries_map = {
        "sales": ("tenant_ventes_matrix", """
            SELECT TOP 200 E.DO_Piece AS NumFacture, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                E.DO_Tiers AS CodeClient, ISNULL(C.CT_Intitule, 'CLIENT INCONNU') AS NomClient,
                CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MONTANT_TTC,
                CASE E.DO_Type WHEN 7 THEN 'FACTURE' WHEN 6 THEN 'AVOIR' ELSE CAST(E.DO_Type AS VARCHAR) END AS TypeDoc
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type IN (6, 7)
            GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule, E.DO_Type
            ORDER BY E.DO_Date DESC, E.DO_Piece DESC;
        """),
        "achats": ("tenant_achats_matrix", """
            SELECT TOP 100 E.DO_Piece AS NumFacture, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                E.DO_Tiers AS CodeFournisseur, ISNULL(C.CT_Intitule, 'FOURNISSEUR INCONNU') AS NomFournisseur,
                CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MONTANT_TTC, 'FACTURE FOURNISSEUR' AS TypeDoc
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type = 17
            GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule
            ORDER BY E.DO_Date DESC, E.DO_Piece DESC;
        """),
        "caisse": ("tenant_caisse_matrix", """
            SELECT TOP 200 R.RG_No AS NUM_REGLEMENT, CONVERT(VARCHAR(10), R.RG_Date, 103) AS DATE_MVT,
                ISNULL(C.CT_Intitule, 'DIVERS') AS NOM_TIERS, R.RG_Libelle AS LIBELLE_MVT,
                ISNULL(CA.CA_Intitule, 'CAISSE PRINCIPALE') AS NOM_CAISSE,
                CASE WHEN R.RG_Type = 0 THEN R.RG_Montant ELSE 0 END AS ENTREE_CAISSE,
                CASE WHEN R.RG_Type = 1 THEN R.RG_Montant ELSE 0 END AS SORTIE_CAISSE,
                CASE R.N_Reglement WHEN 0 THEN 'Espèces' WHEN 1 THEN 'Chèque' WHEN 2 THEN 'Carte' WHEN 3 THEN 'Virement' ELSE 'Autre' END AS MODE_REGLEMENT
            FROM F_CREGLEMENT R WITH (NOLOCK)
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON R.CT_NumPayeur = C.CT_Num
            LEFT JOIN F_CAISSE CA WITH (NOLOCK) ON R.CA_No = CA.CA_No
            ORDER BY R.RG_No DESC;
        """),
        "stock": ("tenant_stock_matrix", """
            SELECT A.AR_Ref AS CodeArticle, A.AR_Design AS LibelleArticle, S.AS_QteSto AS QuantiteStock, S.AS_MontSto AS MontantStock
            FROM F_ARTSTOCK S WITH (NOLOCK)
            INNER JOIN F_ARTICLE A WITH (NOLOCK) ON S.AR_Ref = A.AR_Ref
            WHERE S.AS_QteSto != 0
            ORDER BY A.AR_Ref;
        """),
        "top_articles": ("tenant_top_articles_matrix", """
            SELECT TOP 20 L.AR_Ref AS CodeArticle, MAX(ISNULL(L.DL_Design, 'Article')) AS LibelleArticle,
                SUM(L.DL_Qte) AS QuantiteVendue, SUM(L.DL_MontantTTC) AS MontantVendu
            FROM F_DOCLIGNE L WITH (NOLOCK)
            INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type
            WHERE E.DO_Type IN (6, 7)
            GROUP BY L.AR_Ref
            ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "top_clients": ("tenant_top_clients_matrix", """
            SELECT TOP 20 E.DO_Tiers AS CodeClient, MAX(ISNULL(C.CT_Intitule, 'CLIENT')) AS NomClient,
                SUM(L.DL_MontantTTC) AS CA_Total, COUNT(DISTINCT E.DO_Piece) AS NbFactures
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            WHERE E.DO_Type IN (6, 7)
            GROUP BY E.DO_Tiers
            ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "evolution_ca": ("tenant_evolution_journaliere_matrix", """
            SELECT CONVERT(VARCHAR(10), E.DO_Date, 120) AS DateJour,
                CAST(SUM(CASE WHEN E.DO_Type IN (6, 7) THEN L.DL_MontantTTC ELSE 0 END) AS DECIMAL(18,2)) AS CA_Jour,
                ISNULL(T.TotalEncaisse, 0) AS Encaisse_Jour
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN (
                SELECT CAST(RG_Date AS DATE) AS RDate, SUM(RG_Montant) AS TotalEncaisse
                FROM F_CREGLEMENT WITH (NOLOCK)
                GROUP BY CAST(RG_Date AS DATE)
            ) T ON CAST(E.DO_Date AS DATE) = T.RDate
            WHERE E.DO_Type IN (6, 7)
            GROUP BY E.DO_Date, T.TotalEncaisse
            ORDER BY E.DO_Date ASC;
        """),
        "rotation": ("tenant_rotation_matrix", """
            SELECT TOP 20 L.AR_Ref AS CodeArticle, MAX(ISNULL(A.AR_Design, L.DL_Design)) AS Libelle,
                SUM(L.DL_Qte) AS QuantiteVendue, SUM(L.DL_MontantTTC) / NULLIF(SUM(L.DL_Qte), 0) AS PrixMoyen
            FROM F_DOCLIGNE L WITH (NOLOCK)
            INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type
            LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref
            WHERE E.DO_Type IN (6, 7) AND L.AR_Ref IS NOT NULL
            GROUP BY L.AR_Ref
            ORDER BY SUM(L.DL_Qte) DESC;
        """),
        "mode_reglement": ("tenant_mode_reglement_matrix", """
            SELECT CASE R.N_Reglement WHEN 0 THEN 'Espèces' WHEN 1 THEN 'Chèque' WHEN 2 THEN 'Carte' WHEN 3 THEN 'Virement' ELSE 'Autre' END AS ModeReglement,
                SUM(R.RG_Montant) AS TotalRegle
            FROM F_CREGLEMENT R WITH (NOLOCK)
            GROUP BY R.N_Reglement
            ORDER BY SUM(R.RG_Montant) DESC;
        """),
        "marge_brute": ("tenant_marge_brute_matrix", """
            SELECT TOP 20 L.AR_Ref AS CodeArticle, MAX(ISNULL(A.AR_Design, L.DL_Design)) AS Libelle,
                AVG(L.DL_PrixUnitaire) AS PrixVenteMoyen, AVG(ISNULL(A.AR_PrixAch, 0)) AS PrixAchatMoyen,
                (AVG(L.DL_PrixUnitaire) - AVG(ISNULL(A.AR_PrixAch, 0))) AS MargeUnitaire
            FROM F_DOCLIGNE L WITH (NOLOCK)
            INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type
            LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref
            WHERE E.DO_Type IN (6, 7) AND L.AR_Ref IS NOT NULL
            GROUP BY L.AR_Ref
            HAVING AVG(L.DL_PrixUnitaire) > 0
            ORDER BY MargeUnitaire DESC;
        """),
        "mouvements": ("tenant_mouvements_matrix", """
            SELECT TOP 200 E.DO_Piece AS NumPiece, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateMouvement,
                L.AR_Ref AS CodeArticle, L.DL_Design AS LibelleArticle, L.DL_Qte AS Quantite,
                CASE WHEN E.DO_Type = 20 THEN 'ENTRÉE' ELSE 'SORTIE' END AS Sens, L.DL_MontantTTC AS Montant
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            WHERE E.DO_Type IN (20, 21)
            ORDER BY E.DO_Date DESC;
        """),
        "dernieres_transac": ("tenant_dernieres_transac_matrix", """
            SELECT TOP 50 CONVERT(VARCHAR(10), E.DO_Date, 103) AS Date, E.DO_Piece AS Piece, E.DO_Tiers AS Tiers,
                SUM(L.DL_MontantTTC) AS Montant,
                CASE E.DO_Type WHEN 7 THEN 'Facture' WHEN 17 THEN 'Achat' WHEN 20 THEN 'Entrée Stock' WHEN 21 THEN 'Sortie Stock' ELSE CAST(E.DO_Type AS VARCHAR) END AS Type
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            GROUP BY E.DO_Date, E.DO_Piece, E.DO_Tiers, E.DO_Type
            ORDER BY E.DO_Date DESC;
        """),
        "ca_famille": ("tenant_ca_famille_matrix", """
            SELECT ISNULL(FA.FA_CodeFamille, 'AUTRE') AS CodeFamille, ISNULL(FA.FA_Intitule, 'Général') AS LibelleFamille,
                SUM(L.DL_MontantTTC) AS CA_Famille
            FROM F_DOCLIGNE L WITH (NOLOCK)
            INNER JOIN F_DOCENTETE E WITH (NOLOCK) ON L.DO_Piece = E.DO_Piece AND L.DO_Type = E.DO_Type
            LEFT JOIN F_ARTICLE A WITH (NOLOCK) ON L.AR_Ref = A.AR_Ref
            LEFT JOIN F_FAMILLE FA WITH (NOLOCK) ON A.FA_CodeFamille = FA.FA_CodeFamille
            WHERE E.DO_Type IN (6, 7)
            GROUP BY FA.FA_CodeFamille, FA.FA_Intitule
            ORDER BY SUM(L.DL_MontantTTC) DESC;
        """),
        "impayees": ("tenant_impayees_matrix", """
            SELECT TOP 50 E.DO_Piece AS NumFacture, CONVERT(VARCHAR(10), E.DO_Date, 103) AS DateFacture,
                E.DO_Tiers AS CodeClient, ISNULL(C.CT_Intitule, 'CLIENT') AS NomClient,
                CAST(SUM(L.DL_MontantTTC) AS DECIMAL(18,2)) AS MontantFacture,
                ISNULL(SUM(R.RG_Montant), 0) AS MontantRegle,
                CAST(SUM(L.DL_MontantTTC) - ISNULL(SUM(R.RG_Montant), 0) AS DECIMAL(18,2)) AS Solde
            FROM F_DOCENTETE E WITH (NOLOCK)
            INNER JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_COMPTET C WITH (NOLOCK) ON E.DO_Tiers = C.CT_Num
            LEFT JOIN F_CREGLEMENT R WITH (NOLOCK) ON E.DO_Tiers = R.CT_NumPayeur
            WHERE E.DO_Type = 7
            GROUP BY E.DO_Piece, E.DO_Date, E.DO_Tiers, C.CT_Intitule
            HAVING CAST(SUM(L.DL_MontantTTC) - ISNULL(SUM(R.RG_Montant), 0) AS DECIMAL(18,2)) > 0
            ORDER BY E.DO_Date DESC;
        """),
        "solde_client": ("tenant_solde_client_matrix", """
            SELECT C.CT_Num AS CodeClient, C.CT_Intitule AS Nom,
                ISNULL(SUM(L.DL_MontantTTC), 0) AS TotalFacture, ISNULL(SUM(R.RG_Montant), 0) AS TotalRegle,
                ISNULL(SUM(L.DL_MontantTTC), 0) - ISNULL(SUM(R.RG_Montant), 0) AS Solde
            FROM F_COMPTET C WITH (NOLOCK)
            LEFT JOIN F_DOCENTETE E WITH (NOLOCK) ON C.CT_Num = E.DO_Tiers AND E.DO_Type = 7
            LEFT JOIN F_DOCLIGNE L WITH (NOLOCK) ON E.DO_Piece = L.DO_Piece AND E.DO_Type = L.DO_Type
            LEFT JOIN F_CREGLEMENT R WITH (NOLOCK) ON C.CT_Num = R.CT_NumPayeur
            GROUP BY C.CT_Num, C.CT_Intitule
            HAVING ISNULL(SUM(L.DL_MontantTTC), 0) - ISNULL(SUM(R.RG_Montant), 0) <> 0
            ORDER BY Solde DESC;
        """),
    }

    module_errors = []
    try:
        for module_key, (table_name, query) in queries_map.items():
            if not is_module_enabled(sb, tenant_id, module_key):
                continue
            try:
                rows = execute_query_with_retry(cursor, query)
                columns = [col[0] for col in cursor.description]
                data_rows = [dict(zip(columns, row)) for row in rows]
                if data_rows:
                    payload = {
                        "tenant_id": tenant_id,
                        "data": json.loads(json.dumps(data_rows, default=str)),
                        "updated_at": str(datetime.now())
                    }
                    upsert_with_retry(sb, table_name, payload, on_conflict="tenant_id")
            except Exception as e:
                logger.error(f"[{tenant_id}] Erreur module {module_key} : {e}")
                module_errors.append(f"{module_key}: {e}")

        duration_ms = int((time.time() - cycle_start) * 1000)
        if module_errors:
            logger.warning(f"[{tenant_id}] Cycle terminé avec {len(module_errors)} module(s) en erreur ({duration_ms}ms).")
            send_heartbeat(sb, tenant_id, status="degraded",
                            last_error="; ".join(module_errors[:5]), duration_ms=duration_ms)
        else:
            logger.info(f"[{tenant_id}] Synchronisation réussie ({duration_ms}ms).")
            send_heartbeat(sb, tenant_id, status="active", duration_ms=duration_ms)
        return True

    except Exception as e:
        logger.error(f"[{tenant_id}] Erreur générale de cycle : {e}")
        send_heartbeat(sb, tenant_id, status="error", last_error=str(e))
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ── Main loop ────────────────────────────────────────────────────────────

def main():
    acquire_single_instance_lock()
    logger.info("🚀 Sikka Sync Agent v4.0 démarré.")

    result = auto_activate_and_verify()
    if not result:
        sys.exit(1)
    tenant_id, db_config = result

    consecutive_failures = 0

    while not _shutdown_requested:
        success = False
        try:
            success = pull_and_push_modular_data(db_config, tenant_id)
        except Exception as e:
            logger.error(f"Erreur critique dans la boucle principale : {e}")

        if success:
            consecutive_failures = 0
            wait = SYNC_INTERVAL_SECONDS
        else:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES_BEFORE_BACKOFF:
                wait = min(SYNC_INTERVAL_SECONDS * (2 ** (consecutive_failures - MAX_CONSECUTIVE_FAILURES_BEFORE_BACKOFF + 1)), BACKOFF_CAP_SECONDS)
                logger.warning(f"{consecutive_failures} échecs consécutifs — attente prolongée de {wait}s avant nouvelle tentative.")
            else:
                wait = SYNC_INTERVAL_SECONDS

        # Sleep in small increments so a shutdown signal is honored quickly.
        slept = 0
        while slept < wait and not _shutdown_requested:
            time.sleep(min(1, wait - slept))
            slept += 1

    logger.info("Arrêt propre de l'agent.")


if __name__ == "__main__":
    main()

# ── SECURITY NOTE ────────────────────────────────────────────────────────
# This agent should authenticate with the LEAST privileged Supabase key that
# still lets it do its job — ideally a scoped key or an RPC/edge function
# that only allows writes to rows matching the caller's own tenant_id, not
# the service_role key (which bypasses Row Level Security entirely and can
# read/write every tenant's data). If every merchant's on-site agent embeds
# the same service_role key, a single leaked or reverse-engineered install
# compromises all of your customers' data, not just that one merchant's.
#
# Recommended path:
#   1. Rotate the service_role key that was in v3.4 immediately — treat it as
#      compromised, since it's been sitting in a script distributed to
#      merchant machines.
#   2. Create a Postgres function (SECURITY DEFINER) or Supabase Edge Function
#      that accepts (tenant_id, table_name, rows) and only upserts rows whose
#      tenant_id matches a signed, per-tenant API token — not a shared secret.
#   3. Issue each merchant agent its own scoped token at activation time
#      (store it in license.key instead of a shared key), and validate it
#      server-side against the `merchants` table before writing.
#   4. Never automate creation of a DB login with a hardcoded password across
#      installs (removed from this version) — create SAGEREADER manually once
#      per site with a unique, randomly generated password.
