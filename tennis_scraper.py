"""
=============================================================
  TENNIS SCRAPER - Enrichit les données ATP + WTA 2025-2026
=============================================================

Source : tennis-data.co.uk (matchs ATP/WTA avec cotes)
- Télécharge tous les tournois 2025 + 2026 disponibles
- Circuit ATP  : /{year}/{tournament}.csv
- Circuit WTA  : /{year}w/{tournament}.csv
- Convertit le format vers celui de Jeff Sackmann
- Appende les nouvelles données sans doublons

UTILISATION :
    python tennis_scraper.py          -> ATP + WTA
    python tennis_scraper.py --atp    -> ATP uniquement
    python tennis_scraper.py --wta    -> WTA uniquement

PUIS relancer :
    python tennis_elo.py
    python tennis_modele.py
"""

import pandas as pd
import numpy as np
import requests
import os
import argparse
from io import StringIO
from datetime import datetime
import difflib

DOSSIER_DATA      = "tennis_data"
FICHIER_BRUTS     = os.path.join(DOSSIER_DATA, "atp_matchs_bruts.csv")
FICHIER_BRUTS_WTA = os.path.join(DOSSIER_DATA, "wta_matchs_bruts.csv")
FICHIER_BACKUP    = os.path.join(DOSSIER_DATA, "atp_matchs_bruts_backup.csv")

BASE_URL = "http://www.tennis-data.co.uk"

# Tous les tournois ATP disponibles sur tennis-data.co.uk
# Format : (année, slug, surface_hint)
TOURNOIS = [
    # 2025
    (2025, "ausopen",      "dur"),
    (2025, "doha",         "dur"),
    (2025, "rotterdam",    "dur"),
    (2025, "dubai",        "dur"),
    (2025, "acapulco",     "dur"),
    (2025, "indian_wells", "dur"),
    (2025, "miami",        "dur"),
    (2025, "montecarlo",   "terre"),
    (2025, "barcelona",    "terre"),
    (2025, "madrid",       "terre"),
    (2025, "rome",         "terre"),
    (2025, "frenchopen",   "terre"),
    (2025, "wimbledon",    "gazon"),
    (2025, "canada",       "dur"),
    (2025, "cincinnati",   "dur"),
    (2025, "usopen",       "dur"),
    (2025, "beijing",      "dur"),
    (2025, "shanghai",     "dur"),
    (2025, "paris",        "dur"),
    (2025, "brisbane",     "dur"),
    (2025, "auckland",     "dur"),
    (2025, "adelaide",     "dur"),
    # 2026
    (2026, "ausopen",      "dur"),
    (2026, "doha",         "dur"),
    (2026, "rotterdam",    "dur"),
    (2026, "dubai",        "dur"),
    (2026, "buenos_aires", "terre"),
    (2026, "acapulco",     "dur"),
]

# Tournois WTA — même slugs, URL avec suffixe 'w' : /{year}w/{slug}.csv
TOURNOIS_WTA = [
    # 2025
    (2025, "ausopen",      "dur"),
    (2025, "doha",         "dur"),
    (2025, "dubai",        "dur"),
    (2025, "indian_wells", "dur"),
    (2025, "miami",        "dur"),
    (2025, "charleston",   "terre"),
    (2025, "madrid",       "terre"),
    (2025, "rome",         "terre"),
    (2025, "frenchopen",   "terre"),
    (2025, "wimbledon",    "gazon"),
    (2025, "toronto",      "dur"),
    (2025, "cincinnati",   "dur"),
    (2025, "usopen",       "dur"),
    (2025, "beijing",      "dur"),
    (2025, "wuhan",        "dur"),
    (2025, "finals",       "dur"),
    # 2026
    (2026, "ausopen",      "dur"),
    (2026, "doha",         "dur"),
    (2026, "dubai",        "dur"),
    (2026, "abu_dhabi",    "dur"),
]

SURFACE_MAP = {
    "Hard":   "dur",
    "Clay":   "terre",
    "Grass":  "gazon",
    "Carpet": "dur",
    "Indoor Hard": "dur",
}

SERIES_MAP = {
    "Grand Slam":      "Grand Chelem",
    "Masters":         "Masters 1000",
    "Masters 1000":    "Masters 1000",
    "Masters Cup":     "Finals",
    "WTA Finals":      "Finals",
    "Premier Mandatory": "Masters 1000",
    "Premier 5":       "Masters 1000",
    "Premier":         "ATP 500/250",
    "ATP500":          "ATP 500/250",
    "ATP250":          "ATP 500/250",
    "WTA500":          "ATP 500/250",
    "WTA250":          "ATP 500/250",
    "International":   "ATP 500/250",
    "International Gold": "ATP 500/250",
}


# ─────────────────────────────────────────────
# 1. CONSTRUCTION DU DICTIONNAIRE DE NOMS
# ─────────────────────────────────────────────

def construire_dico_noms(df_existant):
    """
    Construit un mapping 'Last F.' → 'First Last'
    à partir des données Sackmann existantes.

    Ex: 'Sinner J.' → 'Jannik Sinner'
        'De Minaur A.' → 'Alex De Minaur'
    """
    noms_complets = set()
    for col in ['winner_name', 'loser_name']:
        if col in df_existant.columns:
            noms_complets.update(df_existant[col].dropna().unique())

    dico = {}
    for nom in noms_complets:
        parties = nom.strip().split()
        if len(parties) >= 2:
            # "Jannik Sinner" → "Sinner J."
            # "Alex De Minaur" → "De Minaur A."
            prenom = parties[0]
            # Le nom de famille peut être composé
            if len(parties) == 2:
                nom_famille = parties[1]
            else:
                # Essai : prénom = 1er mot, nom = reste
                nom_famille = " ".join(parties[1:])

            cle = f"{nom_famille} {prenom[0]}."
            dico[cle] = nom

            # Variantes avec prénom complet
            dico[nom.lower()] = nom

    return dico


def convertir_nom(nom_court, dico_noms, liste_noms):
    """
    Convertit 'Last F.' → 'First Last'.
    Utilise le dico d'abord, puis fuzzy matching en fallback.
    """
    if nom_court in dico_noms:
        return dico_noms[nom_court]

    # Essai normalisation (O'Connell → O Connell)
    norm = nom_court.replace("'", " ").replace("-", " ")
    if norm in dico_noms:
        return dico_noms[norm]

    # Fuzzy matching sur la liste complète
    matches = difflib.get_close_matches(nom_court, liste_noms, n=1, cutoff=0.7)
    if matches:
        return matches[0]

    # Reconstruction naïve : "Last F." → "F. Last"
    parties = nom_court.rsplit(" ", 1)
    if len(parties) == 2:
        nom_famille, initiale = parties
        initiale = initiale.rstrip(".")
        return f"{initiale}. {nom_famille}"

    return nom_court


# ─────────────────────────────────────────────
# 2. TÉLÉCHARGEMENT ET CONVERSION
# ─────────────────────────────────────────────

def telecharger_tournoi(annee, slug, circuit="atp"):
    """Télécharge les données d'un tournoi (ATP ou WTA)."""
    suffixe = "w" if circuit == "wta" else ""
    url = f"{BASE_URL}/{annee}{suffixe}/{slug}.csv"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        content = r.content.decode("utf-8-sig")
        df = pd.read_csv(StringIO(content))
        df["_circuit"] = circuit
        return df
    except Exception:
        return None


def convertir_format(df_td, annee, slug, surface_hint, dico_noms, liste_noms):
    """
    Convertit le format tennis-data.co.uk vers le format Sackmann.
    Les colonnes de stats de service seront NaN (non disponibles).
    """
    lignes = []

    for _, row in df_td.iterrows():
        # Ignorer les matchs sans résultat
        if pd.isna(row.get("Winner")) or pd.isna(row.get("Loser")):
            continue
        comment = str(row.get("Comment", "")).lower()
        if "walkover" in comment or "retired" in comment:
            # Garder les matchs terminés normalement ET les abandons
            # (abandon = résultat partiel, utile pour l'Elo)
            pass
        if "not played" in comment or "cancelled" in comment:
            continue

        # Date
        date_str = str(row.get("Date", ""))
        try:
            date_obj = datetime.strptime(date_str, "%d/%m/%Y")
            tourney_date_int = int(date_obj.strftime("%Y%m%d"))
            date_iso = date_obj.strftime("%Y-%m-%d")
        except Exception:
            continue

        # Surface
        surf_raw = str(row.get("Surface", surface_hint))
        surface_td = SURFACE_MAP.get(surf_raw, surface_hint)

        # Noms
        winner_court = str(row.get("Winner", "")).strip()
        loser_court  = str(row.get("Loser", "")).strip()
        winner_full  = convertir_nom(winner_court, dico_noms, liste_noms)
        loser_full   = convertir_nom(loser_court, dico_noms, liste_noms)

        # Classements
        try:
            w_rank = int(float(row.get("WRank", 999)))
        except (ValueError, TypeError):
            w_rank = 999
        try:
            l_rank = int(float(row.get("LRank", 999)))
        except (ValueError, TypeError):
            l_rank = 999

        # Niveau du tournoi
        series = str(row.get("Series", ""))
        niveau = SERIES_MAP.get(series, "ATP 500/250")

        # Tournoi
        tournament = str(row.get("Tournament", slug))

        lignes.append({
            "tourney_id":       f"{annee}-{slug}",
            "tourney_name":     tournament,
            "surface":          surf_raw,    # Hard/Clay/Grass (format original)
            "draw_size":        np.nan,
            "tourney_level":    "G" if "Grand Chelem" in niveau else ("M" if "Masters" in niveau else "A"),
            "tourney_date":     tourney_date_int,
            "match_num":        np.nan,
            "winner_id":        np.nan,
            "winner_seed":      np.nan,
            "winner_entry":     np.nan,
            "winner_name":      winner_full,
            "winner_hand":      np.nan,
            "winner_ht":        np.nan,
            "winner_ioc":       np.nan,
            "winner_age":       np.nan,
            "loser_id":         np.nan,
            "loser_seed":       np.nan,
            "loser_entry":      np.nan,
            "loser_name":       loser_full,
            "loser_hand":       np.nan,
            "loser_ht":         np.nan,
            "loser_ioc":        np.nan,
            "loser_age":        np.nan,
            "score":            np.nan,
            "best_of":          np.nan,
            "round":            row.get("Round", np.nan),
            "minutes":          np.nan,
            # Stats service - non disponibles dans tennis-data.co.uk
            "w_ace":     np.nan, "w_df":     np.nan,
            "w_svpt":    np.nan, "w_1stIn":  np.nan,
            "w_1stWon":  np.nan, "w_2ndWon": np.nan,
            "w_SvGms":   np.nan, "w_bpSaved":np.nan, "w_bpFaced":np.nan,
            "l_ace":     np.nan, "l_df":     np.nan,
            "l_svpt":    np.nan, "l_1stIn":  np.nan,
            "l_1stWon":  np.nan, "l_2ndWon": np.nan,
            "l_SvGms":   np.nan, "l_bpSaved":np.nan, "l_bpFaced":np.nan,
            "winner_rank":         w_rank,
            "winner_rank_points":  np.nan,
            "loser_rank":          l_rank,
            "loser_rank_points":   np.nan,
            "annee":       annee,
            "date":        date_iso,
            "niveau":      niveau,
            # Cotes bookmaker (bonus pour analyses futures)
            "b365_w":      row.get("B365W", np.nan),
            "b365_l":      row.get("B365L", np.nan),
            "ps_w":        row.get("PSW", np.nan),
            "ps_l":        row.get("PSL", np.nan),
            "avg_w":       row.get("AvgW", np.nan),
            "avg_l":       row.get("AvgL", np.nan),
            "source":      "tennis-data.co.uk",
        })

    return pd.DataFrame(lignes)


# ─────────────────────────────────────────────
# 3. FONCTION PRINCIPALE PAR CIRCUIT
# ─────────────────────────────────────────────

def traiter_circuit(circuit, tournois, fichier_bruts, dico_noms, liste_noms):
    """Télécharge et intègre les matchs d'un circuit (atp ou wta)."""
    print(f"\n{'ATP' if circuit == 'atp' else 'WTA'} — {fichier_bruts}")
    print("-" * 50)

    if not os.path.exists(fichier_bruts):
        if circuit == "wta":
            # Créer un fichier vide pour la WTA (pas de données historiques Sackmann)
            df_vide = pd.DataFrame(columns=[
                "tourney_id","tourney_name","surface","tourney_date","winner_name",
                "loser_name","winner_rank","loser_rank","annee","date","niveau",
                "w_ace","w_df","w_svpt","w_1stIn","w_1stWon","w_2ndWon",
                "w_bpSaved","w_bpFaced","l_ace","l_df","l_svpt","l_1stIn",
                "l_1stWon","l_2ndWon","l_bpSaved","l_bpFaced","source"
            ])
            df_vide.to_csv(fichier_bruts, index=False)
            print(f"  Fichier WTA créé : {fichier_bruts}")
            df_existant = df_vide
        else:
            print(f"❌ {fichier_bruts} introuvable — lance d'abord tennis_data_pipeline.py")
            return False
    else:
        df_existant = pd.read_csv(fichier_bruts, low_memory=False)
        print(f"  Données existantes : {len(df_existant)} matchs")

    # Clés de déduplication
    existants_keys = set()
    if "date" in df_existant.columns:
        for _, r in df_existant.iterrows():
            existants_keys.add((str(r.get("date",""))[:10],
                                str(r.get("winner_name","")),
                                str(r.get("loser_name",""))))

    nouveaux = []
    total_dl = total_skip = 0

    for annee, slug, surface_hint in tournois:
        df_td = telecharger_tournoi(annee, slug, circuit)
        if df_td is None or len(df_td) == 0:
            continue

        df_conv = convertir_format(df_td, annee, slug, surface_hint, dico_noms, liste_noms)
        df_conv["circuit"] = circuit

        nouveaux_matchs = []
        for _, row in df_conv.iterrows():
            key = (str(row.get("date",""))[:10],
                   str(row.get("winner_name","")),
                   str(row.get("loser_name","")))
            if key not in existants_keys:
                nouveaux_matchs.append(row)
                existants_keys.add(key)
                total_dl += 1
            else:
                total_skip += 1

        if nouveaux_matchs:
            nouveaux.append(pd.DataFrame(nouveaux_matchs))
            print(f"  ✅ {annee}/{slug:<14} : {len(nouveaux_matchs)} matchs")
        else:
            print(f"  ⏭️  {annee}/{slug:<14} : déjà présent")

    print(f"  Nouveaux : {total_dl} | Doublons ignorés : {total_skip}")

    if not nouveaux:
        return True

    df_nouveaux = pd.concat(nouveaux, ignore_index=True)

    for col in df_nouveaux.columns:
        if col not in df_existant.columns:
            df_existant[col] = np.nan
    for col in df_existant.columns:
        if col not in df_nouveaux.columns:
            df_nouveaux[col] = np.nan

    surf_map = {"Hard": "dur", "Clay": "terre", "Grass": "gazon", "Carpet": "dur"}
    df_nouveaux["surface"] = df_nouveaux["surface"].map(surf_map).fillna("dur")

    df_final = pd.concat([df_existant, df_nouveaux], ignore_index=True)
    df_final["date"] = pd.to_datetime(df_final["date"], errors="coerce")
    df_final = df_final.sort_values("date").reset_index(drop=True)
    df_final.to_csv(fichier_bruts, index=False)

    print(f"  Sauvegardé : {len(df_final)} matchs "
          f"({df_final['date'].min().date()} → {df_final['date'].max().date()})")
    return True


# ─────────────────────────────────────────────
# 4. PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tennis Scraper ATP + WTA")
    parser.add_argument("--atp", action="store_true", help="ATP uniquement")
    parser.add_argument("--wta", action="store_true", help="WTA uniquement")
    args = parser.parse_args()

    faire_atp = not args.wta
    faire_wta = not args.atp

    print("🎾 TENNIS SCRAPER - ATP + WTA 2025-2026")
    print("=" * 55)

    # Dictionnaire de noms depuis les données ATP existantes
    if os.path.exists(FICHIER_BRUTS):
        df_existant = pd.read_csv(FICHIER_BRUTS, low_memory=False)
        df_existant.to_csv(FICHIER_BACKUP, index=False)
        print(f"💾 Backup ATP : {FICHIER_BACKUP}")
    else:
        df_existant = pd.DataFrame()

    # Pour la WTA, charger aussi le fichier WTA existant si disponible
    df_wta_existant = pd.DataFrame()
    if os.path.exists(FICHIER_BRUTS_WTA):
        df_wta_existant = pd.read_csv(FICHIER_BRUTS_WTA, low_memory=False)

    dico_noms = construire_dico_noms(pd.concat([df_existant, df_wta_existant], ignore_index=True))
    liste_noms = list(dico_noms.values())
    print(f"📋 Dictionnaire de noms : {len(dico_noms)} entrées")

    if faire_atp:
        traiter_circuit("atp", TOURNOIS, FICHIER_BRUTS, dico_noms, liste_noms)
    if faire_wta:
        traiter_circuit("wta", TOURNOIS_WTA, FICHIER_BRUTS_WTA, dico_noms, liste_noms)

    print(f"""
═══════════════════════════════════════════════════════
  PROCHAINES ÉTAPES :
  1. python tennis_enrichissement.py  (rankings + absents)
  2. python tennis_elo.py             (recalcule les Elo)
  3. python tennis_modele.py          (ré-entraîne le modèle)
  4. python tennis_auto.py --analyse  (rapport value bets)
═══════════════════════════════════════════════════════
""")
