"""
=============================================================
  TENNIS ENRICHISSEMENT - Données complémentaires
=============================================================

Génère 3 fichiers de données enrichies dans tennis_data/ :

  rankings_atp.csv   — Classement ATP actuel (extrait de nos matchs)
  rankings_wta.csv   — Classement WTA actuel
  absents.csv        — Joueurs absents / potentiellement blessés
  stats_service_2025.csv — Stats de service 2025 (ATP tour officiel)

UTILISATION :
    python tennis_enrichissement.py

Ces données sont utilisées automatiquement par tennis_auto.py :
  - Affiche le rang ATP/WTA à côté de chaque joueur
  - Marque les joueurs absents avec un avertissement ⚠️
  - Enrichit le rapport avec les stats de service récentes
"""

import pandas as pd
import numpy as np
import requests
import os
from datetime import datetime, timedelta

DOSSIER_DATA       = "tennis_data"
FICHIER_ATP        = os.path.join(DOSSIER_DATA, "atp_matchs_bruts.csv")
FICHIER_WTA        = os.path.join(DOSSIER_DATA, "wta_matchs_bruts.csv")
FICHIER_RANK_ATP   = os.path.join(DOSSIER_DATA, "rankings_atp.csv")
FICHIER_RANK_WTA   = os.path.join(DOSSIER_DATA, "rankings_wta.csv")
FICHIER_ABSENTS    = os.path.join(DOSSIER_DATA, "absents.csv")
FICHIER_STATS_2025 = os.path.join(DOSSIER_DATA, "stats_service_2025.csv")

# Joueur absent si pas de match dans les N derniers jours
SEUIL_ABSENCE_JOURS = 28


# ─────────────────────────────────────────────
# 1. CLASSEMENTS — extraits de nos données
# ─────────────────────────────────────────────

def extraire_classements(fichier_matchs, circuit="ATP"):
    """
    Extrait le classement approximatif depuis les données de matchs.
    Pour chaque joueur, on prend le rang ATP/WTA le plus récent vu dans
    les données (winner_rank ou loser_rank).
    Retourne un DataFrame trié par rang avec : joueur, rang, date_derniere_obs.
    """
    if not os.path.exists(fichier_matchs):
        print(f"  ⚠️  {circuit} : fichier absent ({fichier_matchs})")
        return pd.DataFrame(columns=["joueur", "rang", "date"])

    df = pd.read_csv(fichier_matchs, low_memory=False)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")

    rang_par_joueur = {}  # joueur → (rang, date)

    for _, row in df.iterrows():
        date = row["date"]
        for col_name, col_rank in [("winner_name", "winner_rank"),
                                    ("loser_name",  "loser_rank")]:
            nom  = str(row.get(col_name, "")).strip()
            rang = row.get(col_rank, np.nan)
            if nom and not pd.isna(rang):
                try:
                    rang_int = int(float(rang))
                    if rang_int > 0 and rang_int < 2000:
                        # Mettre à jour si date plus récente
                        if nom not in rang_par_joueur or date >= rang_par_joueur[nom][1]:
                            rang_par_joueur[nom] = (rang_int, date)
                except (ValueError, TypeError):
                    pass

    if not rang_par_joueur:
        return pd.DataFrame(columns=["joueur", "rang", "date"])

    lignes = [{"joueur": j, "rang": r, "date": d.date()}
              for j, (r, d) in rang_par_joueur.items()]
    df_ranks = pd.DataFrame(lignes).sort_values("rang").reset_index(drop=True)
    print(f"  {circuit} rankings : {len(df_ranks)} joueurs "
          f"| Top 5 : {list(df_ranks['joueur'].head(5))}")
    return df_ranks


# ─────────────────────────────────────────────
# 2. ABSENTS / BLESSÉS
# ─────────────────────────────────────────────

def detecter_absents(fichier_matchs, circuit="ATP", seuil_jours=SEUIL_ABSENCE_JOURS):
    """
    Détecte les joueurs actifs qui n'ont pas joué depuis plus de N jours.
    'Actif' = apparu dans au moins 5 matchs au total dans nos données.
    """
    if not os.path.exists(fichier_matchs):
        return pd.DataFrame(columns=["joueur", "circuit", "derniere_date",
                                      "jours_absence", "statut"])

    df = pd.read_csv(fichier_matchs, low_memory=False)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    date_limite = pd.Timestamp(datetime.now() - timedelta(days=seuil_jours))
    date_ref    = pd.Timestamp(datetime.now())

    derniere_date = {}  # joueur → dernière date vue
    nb_matchs     = {}  # joueur → nombre de matchs total

    for _, row in df.iterrows():
        for col in ["winner_name", "loser_name"]:
            nom = str(row.get(col, "")).strip()
            if nom:
                derniere_date[nom] = max(derniere_date.get(nom, row["date"]), row["date"])
                nb_matchs[nom] = nb_matchs.get(nom, 0) + 1

    absents = []
    for joueur, derniere in derniere_date.items():
        if nb_matchs.get(joueur, 0) < 5:
            continue  # Ignorer les joueurs peu actifs
        if derniere < date_limite:
            jours = (date_ref - derniere).days
            absents.append({
                "joueur":        joueur,
                "circuit":       circuit,
                "derniere_date": derniere.date(),
                "jours_absence": jours,
                "statut":        "Absent >4 sem." if jours > 28 else "Absent >2 sem."
            })

    df_abs = pd.DataFrame(absents).sort_values("jours_absence", ascending=False)
    print(f"  {circuit} absents : {len(df_abs)} joueurs "
          f"sans match depuis >{seuil_jours}j")
    if len(df_abs) > 0:
        exemples = list(df_abs.head(5)["joueur"])
        print(f"    Exemples : {exemples}")
    return df_abs


# ─────────────────────────────────────────────
# 3. STATS DE SERVICE 2025 — ATP Tour officiel
# ─────────────────────────────────────────────

def fetch_stats_service_officielles():
    """
    Tente de récupérer les stats de service 2025 depuis la page officielle
    ATP Tour. L'API non-officielle retourne des tableaux HTML parsables.
    En cas d'échec, retourne un DataFrame vide.
    """
    stats_categories = {
        "aces":           "https://www.atptour.com/en/stats/aces/2025/all/all/",
        "first_serve_pct":"https://www.atptour.com/en/stats/first-serve/2025/all/all/",
        "first_serve_won":"https://www.atptour.com/en/stats/first-serve-points-won/2025/all/all/",
        "second_serve_won":"https://www.atptour.com/en/stats/second-serve-points-won/2025/all/all/",
        "bp_saved":       "https://www.atptour.com/en/stats/break-points-saved/2025/all/all/",
    }

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }

    dfs = {}
    for stat_name, url in stats_categories.items():
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            tables = pd.read_html(r.text)
            if not tables:
                continue
            # La première table contient généralement le classement + stat
            df_t = tables[0]
            # Chercher colonne joueur et stat principale
            # Les noms de colonnes varient, on prend les 2 premières colonnes utiles
            cols = list(df_t.columns)
            # Trouver colonne "Player" ou "Name"
            nom_col = next((c for c in cols if "player" in str(c).lower()
                            or "name" in str(c).lower()), None)
            if nom_col is None and len(cols) >= 2:
                nom_col = cols[1]  # Fallback : 2e colonne

            # Trouver colonne stat (dernière colonne numérique souvent)
            stat_col = None
            for c in reversed(cols):
                if df_t[c].dtype in [float, "float64"] or "%" in str(c):
                    stat_col = c
                    break

            if nom_col and stat_col:
                df_sub = df_t[[nom_col, stat_col]].copy()
                df_sub.columns = ["joueur", stat_name]
                df_sub = df_sub.dropna()
                df_sub["joueur"] = df_sub["joueur"].astype(str).str.strip()
                dfs[stat_name] = df_sub
                print(f"    {stat_name:<22} : {len(df_sub)} joueurs")
        except Exception as e:
            print(f"    {stat_name:<22} : échec ({type(e).__name__})")

    if not dfs:
        return pd.DataFrame()

    # Fusion de toutes les stats
    df_stats = None
    for stat_name, df_s in dfs.items():
        if df_stats is None:
            df_stats = df_s
        else:
            df_stats = df_stats.merge(df_s, on="joueur", how="outer")

    return df_stats if df_stats is not None else pd.DataFrame()


# ─────────────────────────────────────────────
# 4. FUSION AVEC stats_joueurs.csv
# ─────────────────────────────────────────────

def fusionner_stats_service(df_atp_officiel):
    """
    Fusionne les stats de service officielle ATP avec notre stats_joueurs.csv.
    Les nouvelles données 2025 ont priorité sur les moyennes glissantes 2020-2024.
    """
    fichier_stats = os.path.join(DOSSIER_DATA, "stats_joueurs.csv")
    if not os.path.exists(fichier_stats):
        print(f"  ⚠️  {fichier_stats} absent — skip fusion")
        return

    if df_atp_officiel.empty:
        print("  ⚠️  Pas de stats officielles à fusionner")
        return

    df_base = pd.read_csv(fichier_stats)
    nb_avant = len(df_base)

    # Mapping colonnes ATP officiel → notre format
    col_map = {
        "aces":            "ace_rate",
        "first_serve_pct": "1st_in_pct",
        "first_serve_won": "1st_won_pct",
        "second_serve_won":"2nd_won_pct",
        "bp_saved":        "bp_saved_pct",
    }

    # Normaliser les valeurs en ratios (0-1) si elles sont en pourcentages
    df_off = df_atp_officiel.copy()
    for col_off, col_nous in col_map.items():
        if col_off in df_off.columns:
            vals = pd.to_numeric(df_off[col_off].astype(str).str.replace("%",""), errors="coerce")
            # Si valeurs > 1, elles sont en %, convertir en ratio
            if vals.median() > 1:
                vals = vals / 100
            df_off[col_nous] = vals

    df_off_clean = df_off[["joueur"] + [v for v in col_map.values()
                                         if v in df_off.columns]].copy()

    # Merge : les joueurs dans les stats officielles remplacent les valeurs existantes
    df_merged = df_base.set_index("joueur")
    for _, row_off in df_off_clean.iterrows():
        joueur = row_off["joueur"]
        if joueur in df_merged.index:
            for col in [v for v in col_map.values() if v in row_off.index]:
                val = row_off[col]
                if not pd.isna(val):
                    df_merged.at[joueur, col] = val
        else:
            # Nouveau joueur : l'ajouter
            new_row = {c: np.nan for c in df_merged.columns}
            for col in [v for v in col_map.values() if v in row_off.index]:
                new_row[col] = row_off[col]
            df_merged.loc[joueur] = new_row

    df_merged.reset_index().rename(columns={"index":"joueur"}).to_csv(fichier_stats, index=False)
    print(f"  stats_joueurs.csv mis à jour : {nb_avant} → {len(df_merged)} joueurs")


# ─────────────────────────────────────────────
# 5. PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("🎾 TENNIS ENRICHISSEMENT - Rankings + Absents + Stats 2025")
    print("=" * 60)
    os.makedirs(DOSSIER_DATA, exist_ok=True)

    # ── Rankings ─────────────────────────────────────────────
    print("\n📊 Extraction des classements...")
    df_rank_atp = extraire_classements(FICHIER_ATP, "ATP")
    df_rank_wta = extraire_classements(FICHIER_WTA, "WTA")
    df_rank_atp.to_csv(FICHIER_RANK_ATP, index=False)
    df_rank_wta.to_csv(FICHIER_RANK_WTA, index=False)
    print(f"  Sauvegardés : {FICHIER_RANK_ATP}, {FICHIER_RANK_WTA}")

    # ── Absents ───────────────────────────────────────────────
    print(f"\n🏥 Détection des absences (>{SEUIL_ABSENCE_JOURS} jours)...")
    df_abs_atp = detecter_absents(FICHIER_ATP, "ATP")
    df_abs_wta = detecter_absents(FICHIER_WTA, "WTA")
    df_absents = pd.concat([df_abs_atp, df_abs_wta], ignore_index=True)
    df_absents.to_csv(FICHIER_ABSENTS, index=False)
    print(f"  Sauvegardé : {FICHIER_ABSENTS} ({len(df_absents)} joueurs)")

    # ── Stats de service ATP officiel (2025) ──────────────────
    print("\n📈 Récupération stats de service 2025 (atptour.com)...")
    df_stats_off = fetch_stats_service_officielles()

    if not df_stats_off.empty:
        df_stats_off.to_csv(FICHIER_STATS_2025, index=False)
        print(f"  Sauvegardé : {FICHIER_STATS_2025} ({len(df_stats_off)} joueurs)")
        fusionner_stats_service(df_stats_off)
    else:
        print("  ⚠️  Stats ATP officielles non disponibles (site JS ou hors ligne)")
        print("     → Les stats rolling 2020-2024 restent utilisées")

    # ── Résumé final ──────────────────────────────────────────
    print(f"""
═══════════════════════════════════════════════════════
  FICHIERS GÉNÉRÉS :
  - {FICHIER_RANK_ATP}
  - {FICHIER_RANK_WTA}
  - {FICHIER_ABSENTS}
  {"- " + FICHIER_STATS_2025 if not df_stats_off.empty else "  (stats 2025 non dispo)"}

  CLASSEMENT ATP TOP 10 :
""")
    for i, row in df_rank_atp.head(10).iterrows():
        print(f"  {row['rang']:>3}.  {row['joueur']}")

    print(f"""
  Ces données sont utilisées automatiquement par :
    python tennis_auto.py --analyse
═══════════════════════════════════════════════════════
""")
