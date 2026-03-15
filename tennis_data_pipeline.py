"""
=============================================================
  PIPELINE TENNIS - Collecte & Préparation des données
  Pour débutants en data science
=============================================================

INSTALLATION (à faire une seule fois dans ton terminal) :
    pip install pandas numpy requests tqdm

SOURCES DE DONNÉES GRATUITES :
    - Jeff Sackmann GitHub : https://github.com/JeffSackmann/tennis_atp
    - Même repo pour WTA  : https://github.com/JeffSackmann/tennis_wta
    - Ces CSV couvrent ATP/WTA/Challenger/ITF depuis les années 1968

UTILISATION :
    python tennis_data_pipeline.py
"""

import pandas as pd
import numpy as np
import requests
import os
import datetime
from io import StringIO

# ─────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────

# Années à télécharger (automatiquement jusqu'à l'année en cours)
ANNEES = list(range(2020, datetime.date.today().year + 1))

# Circuit à analyser : "atp" ou "wta"
CIRCUIT = "atp"

# Dossier où sauvegarder les données
DOSSIER_DATA = "tennis_data"
os.makedirs(DOSSIER_DATA, exist_ok=True)


# ─────────────────────────────────────────────
# 2. TÉLÉCHARGEMENT DES DONNÉES BRUTES
# ─────────────────────────────────────────────

def telecharger_donnees(circuit: str, annees: list) -> pd.DataFrame:
    """
    Télécharge les données de matchs depuis le GitHub de Jeff Sackmann.
    Chaque fichier CSV contient TOUS les matchs d'une année.
    
    Colonnes importantes :
        tourney_date    : date du tournoi (YYYYMMDD)
        surface         : Clay / Grass / Hard / Carpet
        tourney_level   : G=Grand Chelem, M=Masters, A=ATP, C=Challenger, S=ITF
        winner_name     : nom du gagnant
        loser_name      : nom du perdant
        winner_rank     : classement ATP/WTA du gagnant
        loser_rank      : classement ATP/WTA du perdant
        w_svpt          : points de service joués par le gagnant
        w_1stIn         : 1res balles rentées (gagnant)
        w_1stWon        : points gagnés sur 1re balle (gagnant)
        w_bpFaced       : balles de break subies (gagnant)
        w_bpSaved       : balles de break sauvées (gagnant)
    """
    
    base_url = f"https://raw.githubusercontent.com/JeffSackmann/tennis_{circuit}/master"
    tous_les_matchs = []
    
    print(f"\n📥 Téléchargement des données {circuit.upper()} ({min(annees)}-{max(annees)})...")
    
    for annee in annees:
        url = f"{base_url}/{circuit}_matches_{annee}.csv"
        
        try:
            reponse = requests.get(url, timeout=15)
            reponse.raise_for_status()
            
            df_annee = pd.read_csv(StringIO(reponse.text), low_memory=False)
            df_annee["annee"] = annee
            tous_les_matchs.append(df_annee)
            print(f"  ✅ {annee} : {len(df_annee)} matchs téléchargés")
            
        except Exception as e:
            print(f"  ❌ {annee} : erreur - {e}")
    
    if not tous_les_matchs:
        raise ValueError("Aucune donnée téléchargée. Vérifie ta connexion internet.")
    
    df = pd.concat(tous_les_matchs, ignore_index=True)
    print(f"\n✅ Total : {len(df)} matchs sur {len(annees)} ans\n")
    return df


# ─────────────────────────────────────────────
# 3. NETTOYAGE DES DONNÉES
# ─────────────────────────────────────────────

def nettoyer_donnees(df: pd.DataFrame) -> pd.DataFrame:
    """
    Nettoie les données brutes :
    - Conversion des types
    - Gestion des valeurs manquantes
    - Filtrage des colonnes utiles
    """
    print("🧹 Nettoyage des données...")
    
    # Conversion de la date
    df["date"] = pd.to_datetime(df["tourney_date"].astype(str), format="%Y%m%d", errors="coerce")
    
    # Nettoyage des classements (certains sont manquants = joueurs non classés)
    df["winner_rank"] = pd.to_numeric(df["winner_rank"], errors="coerce")
    df["loser_rank"]  = pd.to_numeric(df["loser_rank"],  errors="coerce")
    
    # Remplacer les classements manquants par 999 (joueur non classé)
    df["winner_rank"] = df["winner_rank"].fillna(999)
    df["loser_rank"]  = df["loser_rank"].fillna(999)
    
    # Supprimer les matchs sans surface (données corrompues)
    df = df.dropna(subset=["surface", "winner_name", "loser_name"])
    
    # Standardiser les surfaces
    surface_map = {
        "Clay":   "terre",
        "Hard":   "dur",
        "Grass":  "gazon",
        "Carpet": "moquette"
    }
    df["surface"] = df["surface"].map(surface_map).fillna("inconnu")
    
    # Niveau du tournoi (lisible)
    niveau_map = {
        "G": "Grand Chelem",
        "M": "Masters 1000",
        "A": "ATP 500/250",
        "C": "Challenger",
        "S": "ITF/Satellite",
        "F": "Finals"
    }
    df["niveau"] = df["tourney_level"].map(niveau_map).fillna("Autre")
    
    # Trier chronologiquement
    df = df.sort_values("date").reset_index(drop=True)
    
    print(f"  ✅ {len(df)} matchs propres conservés")
    print(f"  📊 Surfaces : {df['surface'].value_counts().to_dict()}")
    print(f"  🏆 Niveaux  : {df['niveau'].value_counts().to_dict()}\n")
    
    return df


# ─────────────────────────────────────────────
# 4. CONSTRUCTION DES FEATURES (VARIABLES)
# ─────────────────────────────────────────────

def calculer_forme_recente(df: pd.DataFrame, fenetre: int = 10) -> dict:
    """
    Pour chaque joueur, calcule son taux de victoire
    sur ses N derniers matchs AVANT chaque rencontre.
    
    C'est une "rolling window" : on ne regarde que le passé,
    jamais le futur (sinon c'est de la triche !)
    """
    print(f"⚡ Calcul de la forme récente ({fenetre} derniers matchs)...")
    
    # On restructure : une ligne = un joueur dans un match (gagné ou perdu)
    gagnants = df[["date", "winner_name"]].copy()
    gagnants.columns = ["date", "joueur"]
    gagnants["victoire"] = 1
    
    perdants = df[["date", "loser_name"]].copy()
    perdants.columns = ["date", "joueur"]
    perdants["victoire"] = 0
    
    historique = pd.concat([gagnants, perdants]).sort_values("date")
    
    # Taux de victoire glissant par joueur
    forme = {}
    for joueur, groupe in historique.groupby("joueur"):
        groupe = groupe.sort_values("date")
        taux = groupe["victoire"].rolling(window=fenetre, min_periods=3).mean()
        # Associer chaque date au taux AVANT ce match
        for i, (idx, row) in enumerate(groupe.iterrows()):
            val = taux.shift(1).iloc[i]  # shift(1) = on prend la valeur précédente
            forme[(joueur, row["date"])] = val if not pd.isna(val) else 0.5
    
    return forme


def calculer_win_rate_surface(df: pd.DataFrame) -> dict:
    """
    Taux de victoire par joueur ET par surface,
    calculé sur tout l'historique disponible (approximation simple).
    """
    print("🏟️  Calcul du win rate par surface...")
    
    surface_stats = {}
    
    for surface, groupe in df.groupby("surface"):
        for joueur in pd.concat([groupe["winner_name"], groupe["loser_name"]]).unique():
            victoires = len(groupe[groupe["winner_name"] == joueur])
            defaites  = len(groupe[groupe["loser_name"]  == joueur])
            total     = victoires + defaites
            
            if total >= 5:  # minimum 5 matchs pour être significatif
                taux = victoires / total
            else:
                taux = 0.5  # valeur neutre si pas assez de données
            
            surface_stats[(joueur, surface)] = taux
    
    return surface_stats


def construire_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Assemble toutes les variables prédictives dans un DataFrame final.
    
    Chaque ligne représente un match avec :
    - Les features du joueur 1 (winner)
    - Les features du joueur 2 (loser)
    - La variable cible : 1 si joueur_1 gagne, 0 sinon
    
    IMPORTANT : on crée aussi des lignes "miroir" (joueur_2 vs joueur_1)
    pour éviter que le modèle apprenne juste "le joueur_1 gagne toujours"
    """
    print("🔧 Construction des features finales...")
    
    forme      = calculer_forme_recente(df)
    wr_surface = calculer_win_rate_surface(df)
    
    lignes = []
    
    for _, match in df.iterrows():
        surface = match["surface"]
        date    = match["date"]
        
        winner = match["winner_name"]
        loser  = match["loser_name"]
        
        # --- Features joueur gagnant ---
        forme_w       = forme.get((winner, date), 0.5)
        wr_surf_w     = wr_surface.get((winner, surface), 0.5)
        rank_w        = match["winner_rank"]
        
        # --- Features joueur perdant ---
        forme_l       = forme.get((loser, date), 0.5)
        wr_surf_l     = wr_surface.get((loser, surface), 0.5)
        rank_l        = match["loser_rank"]
        
        # --- Différences (très prédictives !) ---
        diff_rank     = rank_l - rank_w          # positif = winner mieux classé
        diff_forme    = forme_w - forme_l
        diff_surface  = wr_surf_w - wr_surf_l
        
        # Version 1 : winner = joueur_1 (label = 1)
        lignes.append({
            "date":              date,
            "surface":           surface,
            "niveau":            match["niveau"],
            "joueur_1":          winner,
            "joueur_2":          loser,
            "rank_j1":           rank_w,
            "rank_j2":           rank_l,
            "forme_j1":          forme_w,
            "forme_j2":          forme_l,
            "wr_surface_j1":     wr_surf_w,
            "wr_surface_j2":     wr_surf_l,
            "diff_rank":         diff_rank,
            "diff_forme":        diff_forme,
            "diff_wr_surface":   diff_surface,
            "victoire_j1":       1              # ← variable cible
        })
        
        # Version 2 : loser = joueur_1 (label = 0) — version miroir
        lignes.append({
            "date":              date,
            "surface":           surface,
            "niveau":            match["niveau"],
            "joueur_1":          loser,
            "joueur_2":          winner,
            "rank_j1":           rank_l,
            "rank_j2":           rank_w,
            "forme_j1":          forme_l,
            "forme_j2":          forme_w,
            "wr_surface_j1":     wr_surf_l,
            "wr_surface_j2":     wr_surf_w,
            "diff_rank":         -diff_rank,
            "diff_forme":        -diff_forme,
            "diff_wr_surface":   -diff_surface,
            "victoire_j1":       0              # ← variable cible
        })
    
    features_df = pd.DataFrame(lignes)
    print(f"  ✅ {len(features_df)} lignes générées ({len(df)} matchs × 2)\n")
    return features_df


# ─────────────────────────────────────────────
# 5. STATISTIQUES & APERÇU
# ─────────────────────────────────────────────

def afficher_apercu(df_raw: pd.DataFrame, df_features: pd.DataFrame):
    """Affiche un résumé des données pour vérification."""
    
    print("=" * 55)
    print("  APERÇU DES DONNÉES")
    print("=" * 55)
    
    print(f"\n📦 Données brutes :")
    print(f"   {len(df_raw)} matchs | {df_raw['annee'].nunique()} saisons")
    print(f"   Joueurs uniques : {pd.concat([df_raw['winner_name'], df_raw['loser_name']]).nunique()}")
    
    print(f"\n📊 Dataset features :")
    print(f"   {len(df_features)} lignes | {len(df_features.columns)} colonnes")
    
    print(f"\n🔢 Colonnes disponibles :")
    for col in df_features.columns:
        print(f"   - {col}")
    
    print(f"\n📈 Exemple (5 premières lignes) :")
    cols_affichage = ["date", "surface", "joueur_1", "joueur_2", 
                      "diff_rank", "diff_forme", "victoire_j1"]
    print(df_features[cols_affichage].head().to_string(index=False))
    
    print(f"\n📉 Valeurs manquantes :")
    manquants = df_features.isnull().sum()
    manquants = manquants[manquants > 0]
    if len(manquants) == 0:
        print("   Aucune ✅")
    else:
        print(manquants)


# ─────────────────────────────────────────────
# 6. PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

def traiter_circuit(circuit, annees, dossier):
    """Télécharge, nettoie, construit les features et sauvegarde pour un circuit."""
    print(f"\n{'='*55}")
    print(f"  CIRCUIT : {circuit.upper()}")
    print(f"{'='*55}")

    df_raw      = telecharger_donnees(circuit, annees)
    df_clean    = nettoyer_donnees(df_raw)
    df_features = construire_features(df_clean)
    afficher_apercu(df_raw, df_features)

    # Pour WTA : sauvegarde dans wta_matchs_historiques.csv
    # (séparé de wta_matchs_bruts.csv qui contient les données tennis-data.co.uk 2025-2026)
    if circuit == "wta":
        chemin_raw = os.path.join(dossier, "wta_matchs_historiques.csv")
    else:
        chemin_raw = os.path.join(dossier, f"{circuit}_matchs_bruts.csv")

    chemin_features = os.path.join(dossier, f"{circuit}_features.csv")
    df_clean.to_csv(chemin_raw, index=False)
    df_features.to_csv(chemin_features, index=False)

    print(f"\n  Fichiers sauvegardés :")
    print(f"   -> {chemin_raw}  ({len(df_clean)} matchs)")
    print(f"   -> {chemin_features}")
    return chemin_raw


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Tennis Data Pipeline")
    parser.add_argument("--circuit", choices=["atp", "wta", "both"],
                        default="both",
                        help="Circuit à télécharger (défaut : both)")
    args = parser.parse_args()

    print("TENNIS PREDICTION PIPELINE")
    print("=" * 55)

    circuits = ["atp", "wta"] if args.circuit == "both" else [args.circuit]
    for c in circuits:
        traiter_circuit(c, ANNEES, DOSSIER_DATA)

    print(f"""
=======================================================
  PROCHAINES ETAPES :
    python tennis_elo.py        -> Elo ATP
    python tennis_elo.py --wta  -> Elo WTA
=======================================================
""")
