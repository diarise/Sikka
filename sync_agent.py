# Sikka Secure Auto-Activating Sync Agent
import sqlite3
import os
import sys
import json
import hashlib
import platform
from datetime import date, datetime
from supabase import create_client

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
    """Checks for a license; if missing, auto-registers with Supabase using the store ID."""
    license_path = os.path.join(get_base_dir(), "license.key")
    current_hw_id = get_hardware_fingerprint()

    # IF LICENSE IS MISSING: Auto-activation routine
    if not os.path.exists(license_path):
        print("⚡ No license found. Initiating auto-activation...")
        
        # Prompt the user (merchant) for their Store ID (Tenant ID) created from your admin panel
        tenant_id = input("Enter your Sikka Store ID (e.g., boutique-dakar-02): ").strip()
        
        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            
            # Register hardware ID to this tenant in Supabase
            response = sb.table("merchants").upsert({
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "status": "active",
                "updated_at": str(datetime.now())
            }).execute()
            
            # Create the license file locally on their machine
            license_data = {
                "tenant_id": tenant_id,
                "hw_id": current_hw_id,
                "expires_at": "2027-08-08"  # Or calculate 1 year out
            }
            
            with open(license_path, "w") as f:
                json.dump(license_data, f, indent=4)
                
            print(f"✅ Activation successful! License locked to store: {tenant_id}")
            return tenant_id
            
        except Exception as e:
            print(f"❌ Activation failed: {e}")
            sys.exit(1)

    # STANDARD LICENSE CHECK (If license file already exists)
    try:
        with open(license_path, "r") as f:
            license_data = json.load(f)
            
        # Check Hardware Lock
        if license_data.get("hw_id") != current_hw_id:
            print("SECURITY ERROR: License key does not match this computer hardware.")
            sys.exit(1)
            
        return license_data.get("tenant_id")
    except Exception as e:
        print(f"SECURITY ERROR: Invalid license format. Details: {e}")
        sys.exit(1)

def pull_local_metrics():
    local_db = os.path.join(get_base_dir(), "pos_local.db")
    if not os.path.exists(local_db):
        return (0, 0, 0)
    try:
        con = sqlite3.connect(local_db)
        cur = con.cursor()
        row = cur.execute("""
            SELECT total_sales, cash_in_drawer, bank_balance
            FROM daily_totals WHERE date = ?
        """, (str(date.today()),)).fetchone()
        con.close()
        return row or (0, 0, 0)
    except Exception:
        return (0, 0, 0)

def sync():
    tenant_id = auto_activate_and_verify()
    if not tenant_id:
        return
        
    sales, cash, bank = pull_local_metrics()
    
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb.table("business_metrics").insert({
            "tenant_id": tenant_id,
            "date": str(date.today()),
            "total_sales": sales,
            "cash_in_drawer": cash,
            "bank_balance": bank,
        }).execute()
        print(f"[{tenant_id}] Secure sync successful for {date.today()} -> Sales: {sales}")
    except Exception as e:
        print(f"Sync error: {e}")

if __name__ == "__main__":
    sync()