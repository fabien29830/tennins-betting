"""
=============================================================
  HEALTH CHECK — Diagnostic rapide de chaque composant
=============================================================

Teste chaque dépendance indépendamment et affiche un rapport
PASS / FAIL clair avec la cause exacte en cas d'échec.

Usage :
    python health_check.py            # rapport complet
    python health_check.py --fix      # tente de réparer ce qui peut l'être
    python health_check.py --json     # sortie JSON (pour CI)

Code retour :
    0 = tout OK
    1 = au moins un composant FAIL
"""

import os, sys, json, pickle, argparse, subprocess
from datetime import datetime

DOSSIER      = os.path.dirname(os.path.abspath(__file__))
DOSSIER_DATA = os.path.join(DOSSIER, "tennis_data")
LOG_FILE     = os.path.join(DOSSIER_DATA, "system.log")

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

class Resultat:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

def check(nom, fn):
    """Exécute un test et retourne (statut, détail)."""
    try:
        statut, detail = fn()
        return {"nom": nom, "statut": statut, "detail": detail}
    except Exception as e:
        return {"nom": nom, "statut": Resultat.FAIL, "detail": str(e)}

# ─────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────

def test_fichiers_modele():
    """Vérifie que les .pkl du modèle existent et se chargent."""
    fichiers = {
        "modele_tennis_calibre.pkl": "modèle calibré",
        "modele_tennis.pkl":         "modèle brut",
        "scaler.pkl":                "scaler",
        "colonnes.pkl":              "colonnes features",
    }
    manquants = []
    corrompus  = []
    for fname, label in fichiers.items():
        p = os.path.join(DOSSIER_DATA, fname)
        if not os.path.exists(p):
            manquants.append(label)
            continue
        try:
            with open(p, "rb") as f:
                pickle.load(f)
        except Exception as e:
            corrompus.append(f"{label} ({e})")

    if corrompus:
        return Resultat.FAIL, f"Fichiers corrompus : {', '.join(corrompus)}"
    if manquants:
        # modele_tennis_calibre.pkl manquant = juste pas calibré, pas critique
        if manquants == ["modèle calibré"]:
            return Resultat.WARN, "Modèle calibré absent — utilise le modèle brut"
        return Resultat.FAIL, f"Fichiers manquants : {', '.join(manquants)}"
    return Resultat.PASS, "Tous les fichiers modèle OK"


def test_fichiers_elo():
    """Vérifie les fichiers Elo ATP et WTA."""
    resultats = []
    for fname, label in [("elo_joueurs.csv", "Elo ATP"), ("wta_elo_joueurs.csv", "Elo WTA")]:
        p = os.path.join(DOSSIER_DATA, fname)
        if not os.path.exists(p):
            resultats.append(f"{label} manquant")
            continue
        try:
            import pandas as pd
            df = pd.read_csv(p)
            resultats.append(f"{label} OK ({len(df)} joueurs)")
        except Exception as e:
            resultats.append(f"{label} corrompu : {e}")

    echecs = [r for r in resultats if "manquant" in r or "corrompu" in r]
    if echecs:
        return Resultat.FAIL, " | ".join(echecs)
    return Resultat.PASS, " | ".join(resultats)


def test_fichiers_data():
    """Vérifie les CSV de stats, rankings, forme récente."""
    attendus = {
        "stats_joueurs.csv":   ("WARN", "stats de service absentes — features partielles"),
        "rankings_atp.csv":    ("WARN", "rankings ATP absents — rang affiché comme ?"),
        "rankings_wta.csv":    ("WARN", "rankings WTA absents — rang affiché comme ?"),
    }
    warns = []
    for fname, (niveau, msg) in attendus.items():
        p = os.path.join(DOSSIER_DATA, fname)
        if not os.path.exists(p):
            warns.append(msg)

    if warns:
        return Resultat.WARN, " | ".join(warns)
    return Resultat.PASS, "Stats + Rankings OK"


def test_api_odds():
    """Vérifie la clé API Odds et le quota restant."""
    try:
        import requests
        # Lire la clé depuis tennis_auto.py sans l'importer (évite les side effects)
        api_key = os.environ.get("ODDS_API_KEY", "")
        if not api_key:
            # Tenter de la lire directement depuis le fichier
            with open(os.path.join(DOSSIER, "tennis_auto.py"), encoding="utf-8") as f:
                for line in f:
                    if "ODDS_API_KEY" in line and '=' in line and '"' in line:
                        parts = line.split('"')
                        if len(parts) >= 2:
                            api_key = parts[-2]
                        break

        if not api_key or len(api_key) < 10:
            return Resultat.FAIL, "Clé API introuvable dans tennis_auto.py ni en variable d'env"

        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={"apiKey": api_key},
            timeout=10,
        )
        if r.status_code == 401:
            return Resultat.FAIL, "Clé API invalide (401)"
        if r.status_code == 429 or "OUT_OF_USAGE" in r.text:
            return Resultat.WARN, "Quota API dépassé — réessai le 1er du mois"
        if r.status_code != 200:
            return Resultat.FAIL, f"Statut HTTP inattendu : {r.status_code}"

        restantes = r.headers.get("x-requests-remaining", "?")
        return Resultat.PASS, f"API OK — {restantes} requêtes restantes ce mois"

    except Exception as e:
        return Resultat.FAIL, f"Impossible de joindre l'API : {e}"


def test_google_sheets():
    """Vérifie les credentials GSheet et la connexion."""
    creds_path  = os.path.join(DOSSIER_DATA, "gsheet_credentials.json")
    config_path = os.path.join(DOSSIER_DATA, "gsheet_config.json")

    if not os.path.exists(creds_path):
        return Resultat.FAIL, f"gsheet_credentials.json absent dans {DOSSIER_DATA}"
    if not os.path.exists(config_path):
        return Resultat.FAIL, f"gsheet_config.json absent dans {DOSSIER_DATA}"

    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        sheet_id = cfg.get("sheet_id", "")
        if not sheet_id:
            return Resultat.FAIL, "sheet_id manquant dans gsheet_config.json"
    except Exception as e:
        return Resultat.FAIL, f"gsheet_config.json illisible : {e}"

    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://spreadsheets.google.com/feeds",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        onglets = [ws.title for ws in sh.worksheets()]
        return Resultat.PASS, f"GSheet OK — onglets : {', '.join(onglets[:5])}"
    except ImportError:
        return Resultat.WARN, "gspread non installé (pip install gspread)"
    except Exception as e:
        return Resultat.FAIL, f"Connexion GSheet échouée : {e}"


def test_email():
    """Vérifie la config email (sans envoyer)."""
    try:
        import smtplib
        # Lire config depuis tennis_auto.py
        email_actif = True
        smtp_user   = ""
        smtp_pass   = ""
        smtp_host   = "smtp.gmail.com"
        smtp_port   = 587

        with open(os.path.join(DOSSIER, "tennis_auto.py"), encoding="utf-8") as f:
            for line in f:
                if "EMAIL_ACTIF" in line and "=" in line:
                    email_actif = "True" in line
                if "SMTP_USER" in line and '"' in line:
                    parts = line.split('"')
                    if len(parts) >= 2:
                        smtp_user = parts[1]
                if "SMTP_PASS" in line and "environ" in line:
                    smtp_pass = os.environ.get("EMAIL_PASSWORD", "")
                    if not smtp_pass and '"' in line:
                        parts = line.split('"')
                        smtp_pass = parts[-2] if len(parts) >= 2 else ""

        if not email_actif:
            return Resultat.WARN, "Email désactivé (EMAIL_ACTIF = False)"
        if not smtp_user:
            return Resultat.FAIL, "SMTP_USER non configuré"

        # Test de connexion SMTP sans envoyer
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            if smtp_pass:
                srv.login(smtp_user, smtp_pass)
                return Resultat.PASS, f"SMTP OK — connecté en tant que {smtp_user}"
            else:
                return Resultat.WARN, "SMTP joignable mais mot de passe absent (EMAIL_PASSWORD)"

    except smtplib.SMTPAuthenticationError:
        return Resultat.FAIL, "Authentification SMTP échouée — vérifier mot de passe d'application Gmail"
    except Exception as e:
        return Resultat.FAIL, f"SMTP inaccessible : {e}"


def test_cache_cotes():
    """Vérifie l'état du cache API."""
    cache_path = os.path.join(DOSSIER_DATA, "cache_cotes.json")
    if not os.path.exists(cache_path):
        return Resultat.WARN, "Pas de cache — premier run ou cache expiré"
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("timestamp")
        events = cache.get("events", [])
        nb_cotes = sum(1 for e in events if e.get("bookmakers"))
        if ts:
            from datetime import timezone
            age_min = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
            return Resultat.PASS, (
                f"Cache OK — {len(events)} matchs ({nb_cotes} avec cotes), "
                f"age {age_min:.0f} min"
            )
        return Resultat.WARN, "Cache présent mais timestamp absent"
    except Exception as e:
        return Resultat.FAIL, f"Cache illisible : {e}"


def test_dashboard_json():
    """Vérifie que docs/data.json existe et est valide."""
    p = os.path.join(DOSSIER, "docs", "data.json")
    if not os.path.exists(p):
        return Resultat.FAIL, "docs/data.json absent — dashboard ne fonctionnera pas"
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        stats = data.get("stats", {})
        updated = data.get("meta", {}).get("updated_at", "?")
        return Resultat.PASS, (
            f"Dashboard OK — ROI {stats.get('roi_global', '?')}% | "
            f"mis à jour le {updated}"
        )
    except Exception as e:
        return Resultat.FAIL, f"data.json illisible : {e}"


def test_git():
    """Vérifie que git peut pousser (pour dashboard GitHub Pages)."""
    try:
        r = subprocess.run(
            ["git", "remote", "-v"],
            capture_output=True, text=True, cwd=DOSSIER, timeout=10
        )
        if r.returncode != 0:
            return Resultat.FAIL, "Pas de remote git configuré"
        remote = r.stdout.strip().split("\n")[0] if r.stdout.strip() else ""
        return Resultat.PASS, f"Remote OK : {remote[:60]}"
    except FileNotFoundError:
        return Resultat.FAIL, "git non trouvé dans PATH"
    except Exception as e:
        return Resultat.FAIL, str(e)


def test_scheduler():
    """Vérifie que les tâches planifiées Windows sont actives."""
    if sys.platform != "win32":
        return Resultat.WARN, "Non-Windows — vérifie tes cron jobs manuellement"
    try:
        noms = ["TennisBetting_Matin", "TennisBetting_Midi",
                "TennisBetting_Soir", "TennisBetting_Pipeline"]
        manquantes = []
        for nom in noms:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", nom],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode != 0:
                manquantes.append(nom)
        if manquantes:
            return Resultat.WARN, f"Tâches absentes : {', '.join(manquantes)} — lance --setup"
        return Resultat.PASS, f"{len(noms)} tâches planifiées actives"
    except Exception as e:
        return Resultat.WARN, f"Impossible de vérifier schtasks : {e}"


# ─────────────────────────────────────────────────────────────
# RAPPORT
# ─────────────────────────────────────────────────────────────

TESTS = [
    ("Modele ML (pkl)",         test_fichiers_modele),
    ("Elo ATP + WTA",           test_fichiers_elo),
    ("Stats + Rankings",        test_fichiers_data),
    ("API The Odds",            test_api_odds),
    ("Google Sheets",           test_google_sheets),
    ("Email SMTP",              test_email),
    ("Cache cotes",             test_cache_cotes),
    ("Dashboard data.json",     test_dashboard_json),
    ("Git remote",              test_git),
    ("Scheduler Windows",       test_scheduler),
]

ICONES = {
    Resultat.PASS: "[OK]  ",
    Resultat.WARN: "[WARN]",
    Resultat.FAIL: "[FAIL]",
}


def run_checks():
    resultats = []
    for nom, fn in TESTS:
        r = check(nom, fn)
        resultats.append(r)
    return resultats


def afficher(resultats):
    print("\n" + "=" * 62)
    print("  HEALTH CHECK — Tennis Betting System")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 62)

    nb_fail = sum(1 for r in resultats if r["statut"] == Resultat.FAIL)
    nb_warn = sum(1 for r in resultats if r["statut"] == Resultat.WARN)
    nb_ok   = sum(1 for r in resultats if r["statut"] == Resultat.PASS)

    for r in resultats:
        icone = ICONES[r["statut"]]
        print(f"  {icone}  {r['nom']:<24}  {r['detail']}")

    print("-" * 62)
    print(f"  {nb_ok} OK  |  {nb_warn} WARN  |  {nb_fail} FAIL")

    if nb_fail == 0 and nb_warn == 0:
        print("  Systeme pret — aucune action requise.")
    elif nb_fail == 0:
        print("  Systeme operationnel avec avertissements mineurs.")
    else:
        print(f"  {nb_fail} probleme(s) bloquant(s) a corriger.")
        fails = [r for r in resultats if r["statut"] == Resultat.FAIL]
        print("\n  Actions requises :")
        for r in fails:
            print(f"    - {r['nom']} : {r['detail']}")

    print("=" * 62 + "\n")
    return nb_fail


def sauvegarder_log(resultats):
    """Ajoute une ligne dans system.log."""
    os.makedirs(DOSSIER_DATA, exist_ok=True)
    nb_fail = sum(1 for r in resultats if r["statut"] == Resultat.FAIL)
    nb_warn = sum(1 for r in resultats if r["statut"] == Resultat.WARN)
    statut_global = "FAIL" if nb_fail else ("WARN" if nb_warn else "OK")
    entree = {
        "ts":           datetime.now().isoformat(timespec="seconds"),
        "statut":       statut_global,
        "nb_fail":      nb_fail,
        "nb_warn":      nb_warn,
        "composants":   resultats,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Health check — Tennis Betting System")
    parser.add_argument("--json", action="store_true", help="Sortie JSON brute")
    parser.add_argument("--fix",  action="store_true", help="Tente de réparer (relance le pipeline)")
    args = parser.parse_args()

    resultats = run_checks()
    sauvegarder_log(resultats)

    if args.json:
        print(json.dumps(resultats, indent=2, ensure_ascii=False))
    else:
        nb_fail = afficher(resultats)

    if args.fix:
        fails = [r for r in resultats if r["statut"] == Resultat.FAIL]
        model_fail = any("pkl" in r["nom"].lower() or "elo" in r["nom"].lower() for r in fails)
        if model_fail:
            print("  [FIX] Relance du pipeline pour reconstruire les fichiers manquants...")
            subprocess.run([sys.executable, os.path.join(DOSSIER, "tennis_auto.py"),
                            "--pipeline"], cwd=DOSSIER)

    sys.exit(0 if all(r["statut"] != Resultat.FAIL for r in resultats) else 1)
