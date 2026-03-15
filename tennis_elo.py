"""
=============================================================
  TENNIS ELO v2 - Elo par surface + fatigue
=============================================================

NOUVEAUTÉS :
    - Elo global ET Elo spécifique par surface (terre/dur/gazon)
    - Fatigue : nb de matchs joués dans les 14 derniers jours
    - H2H : historique direct entre les deux joueurs

UTILISATION :
    1. Lance d'abord tennis_data_pipeline.py
    2. Puis : python tennis_elo.py
"""

import pandas as pd
import numpy as np
import os
import argparse
from collections import defaultdict
from datetime import timedelta

# ── Sélection du circuit via --wta ────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--wta", action="store_true")
_args, _ = _parser.parse_known_args()
IS_WTA = _args.wta

DOSSIER_DATA = "tennis_data"

if IS_WTA:
    # WTA : utilise les données historiques Jeff Sackmann (2020-2024)
    # + on peut aussi inclure wta_matchs_bruts.csv (tennis-data.co.uk 2025-2026)
    FICHIER_MATCHS      = os.path.join(DOSSIER_DATA, "wta_matchs_historiques.csv")
    FICHIER_SORTIE      = os.path.join(DOSSIER_DATA, "wta_elo.csv")
    FICHIER_STATS       = os.path.join(DOSSIER_DATA, "stats_joueurs_wta.csv")
    FICHIER_ELO_JOUEURS = os.path.join(DOSSIER_DATA, "wta_elo_joueurs.csv")
    CIRCUIT_LABEL       = "WTA"
else:
    FICHIER_MATCHS      = os.path.join(DOSSIER_DATA, "atp_matchs_bruts.csv")
    FICHIER_SORTIE      = os.path.join(DOSSIER_DATA, "atp_elo.csv")
    FICHIER_STATS       = os.path.join(DOSSIER_DATA, "stats_joueurs.csv")
    FICHIER_ELO_JOUEURS = os.path.join(DOSSIER_DATA, "elo_joueurs.csv")
    CIRCUIT_LABEL       = "ATP"

ELO_DEPART    = 1500
K_GLOBAL      = 32
K_SURFACE     = 24   # un peu plus faible car moins de données par surface
FENETRE_STATS = 20   # nb de matchs pour la moyenne glissante des stats service


# ─────────────────────────────────────────────
# FONCTIONS ELO
# ─────────────────────────────────────────────

def prob_victoire(elo_a, elo_b):
    return 1 / (1 + 10 ** ((elo_b - elo_a) / 400))

def maj_elo(elo_w, elo_l, k=K_GLOBAL):
    p = prob_victoire(elo_w, elo_l)
    return elo_w + k * (1 - p), elo_l + k * (0 - (1 - p))


# ─────────────────────────────────────────────
# CALCUL FATIGUE
# ─────────────────────────────────────────────

def calculer_fatigue(df):
    """
    Pour chaque match, compte combien de matchs chaque joueur
    a joué dans les 14 jours précédents.
    Plus ce nombre est élevé, plus le joueur est fatigué.
    """
    print("😴 Calcul de la fatigue...")

    # Historique de tous les matchs par joueur
    historique = defaultdict(list)
    for _, row in df.iterrows():
        historique[row["winner_name"]].append(row["date"])
        historique[row["loser_name"]].append(row["date"])

    fatigue_winner = []
    fatigue_loser  = []

    for _, row in df.iterrows():
        date   = row["date"]
        limite = date - timedelta(days=14)

        # Matchs du winner dans les 14 derniers jours (avant ce match)
        mw = sum(1 for d in historique[row["winner_name"]] if limite <= d < date)
        ml = sum(1 for d in historique[row["loser_name"]]  if limite <= d < date)

        fatigue_winner.append(mw)
        fatigue_loser.append(ml)

    df["fatigue_winner"] = fatigue_winner
    df["fatigue_loser"]  = fatigue_loser
    print(f"  ✅ Fatigue calculée (moyenne : {np.mean(fatigue_winner):.1f} matchs/14j)\n")
    return df


# ─────────────────────────────────────────────
# CALCUL H2H
# ─────────────────────────────────────────────

def _stats_service_from_row(row, prefix):
    """Calcule les ratios de service à partir d'une ligne brute (w ou l)."""
    svpt = row.get(f"{prefix}_svpt", 0)
    if pd.isna(svpt) or svpt == 0:
        return None
    first_in  = row.get(f"{prefix}_1stIn", np.nan)
    first_won = row.get(f"{prefix}_1stWon", np.nan)
    second_in = svpt - first_in if not pd.isna(first_in) else 0
    bp_faced  = row.get(f"{prefix}_bpFaced", np.nan)
    bp_saved  = row.get(f"{prefix}_bpSaved", np.nan)
    return {
        "1st_in_pct":   first_in / svpt if not pd.isna(first_in) else np.nan,
        "1st_won_pct":  first_won / first_in if (not pd.isna(first_won) and first_in > 0) else np.nan,
        "2nd_won_pct":  row.get(f"{prefix}_2ndWon", np.nan) / second_in if second_in > 0 else np.nan,
        "bp_saved_pct": bp_saved / bp_faced if (not pd.isna(bp_faced) and bp_faced > 0) else np.nan,
        "ace_rate":     row.get(f"{prefix}_ace", np.nan) / svpt,
    }


# ─────────────────────────────────────────────
# CALCUL STATS SERVICE / RETOUR
# ─────────────────────────────────────────────

def calculer_stats_service(df):
    """
    Pour chaque match, calcule les moyennes glissantes des stats de service
    du winner et du loser sur leurs FENETRE_STATS derniers matchs PRÉCÉDENTS.

    Colonnes ajoutées :
        srv_w_*/srv_l_* : moyennes glissantes de chaque joueur
        diff_*          : différence (winner - loser)

    Retourne aussi un dict {joueur: {stat: valeur}} avec les stats actuelles.
    """
    print("🎾 Calcul des stats service/retour...")

    stat_keys = ["1st_in_pct", "1st_won_pct", "2nd_won_pct", "bp_saved_pct", "ace_rate"]

    if "w_svpt" not in df.columns:
        print("  ⚠️  Colonnes service absentes → stats ignorées")
        for k in stat_keys:
            df[f"srv_w_{k}"] = np.nan
            df[f"srv_l_{k}"] = np.nan
            df[f"diff_{k}"]  = np.nan
        return df, {}

    historique = defaultdict(list)   # {joueur: [dict_stats, ...]}
    rows_w, rows_l = [], []

    for _, row in df.iterrows():
        w = row["winner_name"]
        l = row["loser_name"]

        def rolling_avg(player):
            hist = historique[player][-FENETRE_STATS:]
            if len(hist) < 3:
                return {k: np.nan for k in stat_keys}
            return {k: np.nanmean([h[k] for h in hist if not np.isnan(h.get(k, np.nan))] or [np.nan])
                    for k in stat_keys}

        rows_w.append(rolling_avg(w))
        rows_l.append(rolling_avg(l))

        # Mise à jour de l'historique après le match
        sw = _stats_service_from_row(row, "w")
        sl = _stats_service_from_row(row, "l")
        if sw:
            historique[w].append(sw)
        if sl:
            historique[l].append(sl)

    for k in stat_keys:
        df[f"srv_w_{k}"] = [r[k] for r in rows_w]
        df[f"srv_l_{k}"] = [r[k] for r in rows_l]
        df[f"diff_{k}"]  = df[f"srv_w_{k}"] - df[f"srv_l_{k}"]

    n_ok = df["diff_1st_in_pct"].notna().sum()
    print(f"  ✅ Stats calculées pour {n_ok}/{len(df)} matchs ({n_ok * 100 // len(df)}%)\n")

    # Stats courantes par joueur (exportées vers stats_joueurs.csv)
    current_stats = {}
    for player, hist in historique.items():
        recent = hist[-FENETRE_STATS:]
        if len(recent) >= 3:
            current_stats[player] = {
                k: round(np.nanmean([h[k] for h in recent if not np.isnan(h.get(k, np.nan))] or [np.nan]), 4)
                for k in stat_keys
            }
    return df, current_stats


def calculer_h2h(df):
    """
    Pour chaque match, calcule le ratio de victoires du winner
    contre ce même adversaire dans les matchs PRÉCÉDENTS.
    """
    print("🤝 Calcul du head-to-head...")

    h2h = defaultdict(lambda: [0, 0])  # {(j1, j2): [victoires_j1, total]}
    h2h_winner = []
    h2h_loser  = []

    for _, row in df.iterrows():
        w = row["winner_name"]
        l = row["loser_name"]
        cle_w = tuple(sorted([w, l]))

        total    = h2h[cle_w][1]
        victoires_w = h2h[(w, l)][0] if (w, l) in h2h else 0

        if total >= 2:
            ratio_w = victoires_w / total
        else:
            ratio_w = 0.5  # neutre si pas assez de données

        h2h_winner.append(ratio_w)
        h2h_loser.append(1 - ratio_w)

        # Mise à jour après le match
        h2h[(w, l)][0] += 1
        h2h[cle_w][1]  += 1

    df["h2h_winner"] = h2h_winner
    df["h2h_loser"]  = h2h_loser
    print(f"  ✅ H2H calculé\n")
    return df


# ─────────────────────────────────────────────
# CALCUL ELO (GLOBAL + PAR SURFACE)
# ─────────────────────────────────────────────

def calculer_elo_complet(df):
    """
    Calcule :
    - Elo global (tous terrains)
    - Elo par surface (terre, dur, gazon)
    """
    print("⚡ Calcul des ratings Elo (global + par surface)...")

    elo_global  = {}
    elo_surface = defaultdict(dict)  # {surface: {joueur: elo}}
    resultats   = []

    for i, row in df.iterrows():
        w       = row["winner_name"]
        l       = row["loser_name"]
        surface = row["surface"]

        # Elo global avant match
        eg_w = elo_global.get(w, ELO_DEPART)
        eg_l = elo_global.get(l, ELO_DEPART)

        # Elo surface avant match
        es_w = elo_surface[surface].get(w, ELO_DEPART)
        es_l = elo_surface[surface].get(l, ELO_DEPART)

        # Probabilités
        prob_global  = prob_victoire(eg_w, eg_l)
        prob_surface = prob_victoire(es_w, es_l)

        # Probabilité combinée (60% surface, 40% global)
        prob_combinee = 0.6 * prob_surface + 0.4 * prob_global

        resultats.append({
            "date":             row["date"],
            "surface":          surface,
            "niveau":           row["niveau"],
            "winner":           w,
            "loser":            l,
            # Elo global
            "elo_w_global":     round(eg_w, 1),
            "elo_l_global":     round(eg_l, 1),
            "diff_elo_global":  round(eg_w - eg_l, 1),
            # Elo surface
            "elo_w_surface":    round(es_w, 1),
            "elo_l_surface":    round(es_l, 1),
            "diff_elo_surface": round(es_w - es_l, 1),
            # Probabilités
            "prob_global":      round(prob_global, 3),
            "prob_surface":     round(prob_surface, 3),
            "prob_combinee":    round(prob_combinee, 3),
            # Fatigue & H2H
            "fatigue_winner":   row.get("fatigue_winner", 0),
            "fatigue_loser":    row.get("fatigue_loser", 0),
            "diff_fatigue":     row.get("fatigue_loser", 0) - row.get("fatigue_winner", 0),
            "h2h_winner":       round(row.get("h2h_winner", 0.5), 3),
            "h2h_loser":        round(row.get("h2h_loser", 0.5), 3),
            # Stats service/retour (moyennes glissantes, NaN si pas assez d'historique)
            "diff_1st_in_pct":   row.get("diff_1st_in_pct",   np.nan),
            "diff_1st_won_pct":  row.get("diff_1st_won_pct",  np.nan),
            "diff_2nd_won_pct":  row.get("diff_2nd_won_pct",  np.nan),
            "diff_bp_saved_pct": row.get("diff_bp_saved_pct", np.nan),
            "diff_ace_rate":     row.get("diff_ace_rate",      np.nan),
        })

        # Mise à jour Elo
        new_eg_w, new_eg_l = maj_elo(eg_w, eg_l, K_GLOBAL)
        new_es_w, new_es_l = maj_elo(es_w, es_l, K_SURFACE)

        elo_global[w] = new_eg_w
        elo_global[l] = new_eg_l
        elo_surface[surface][w] = new_es_w
        elo_surface[surface][l] = new_es_l

        if i % 2000 == 0 and i > 0:
            print(f"  ... {i} matchs traités")

    print(f"  ✅ {len(resultats)} matchs traités\n")
    return pd.DataFrame(resultats), elo_global, elo_surface


# ─────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────

def afficher_classement(elo_global, elo_surface, n=15):
    print(f"\n🏆 TOP {n} - ELO GLOBAL")
    print("─" * 40)
    top = sorted(elo_global.items(), key=lambda x: x[1], reverse=True)[:n]
    for i, (j, e) in enumerate(top, 1):
        print(f"  {i:<3} {j:<25} {round(e)}")

    print(f"\n🏆 TOP 10 - ELO TERRE BATTUE")
    print("─" * 40)
    top_terre = sorted(elo_surface["terre"].items(), key=lambda x: x[1], reverse=True)[:10]
    for i, (j, e) in enumerate(top_terre, 1):
        print(f"  {i:<3} {j:<25} {round(e)}")

    print(f"\n🏆 TOP 10 - ELO SURFACE DURE")
    print("─" * 40)
    top_dur = sorted(elo_surface["dur"].items(), key=lambda x: x[1], reverse=True)[:10]
    for i, (j, e) in enumerate(top_dur, 1):
        print(f"  {i:<3} {j:<25} {round(e)}")


# ─────────────────────────────────────────────
# PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print(f"TENNIS ELO v2 — {CIRCUIT_LABEL} — Surface + Fatigue + H2H")
    print("=" * 55)

    if not os.path.exists(FICHIER_MATCHS):
        if IS_WTA:
            print(f"Fichier absent : {FICHIER_MATCHS}")
            print("Lance d'abord : python tennis_data_pipeline.py --circuit wta")
        else:
            print("Lance d'abord : python tennis_data_pipeline.py --circuit atp")
        exit(1)

    print("Chargement des donnees...")
    df = pd.read_csv(FICHIER_MATCHS, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  {len(df)} matchs charges ({CIRCUIT_LABEL})\n")

    df = calculer_fatigue(df)
    df = calculer_h2h(df)
    df, stats_joueurs = calculer_stats_service(df)
    df_elo, elo_global, elo_surface = calculer_elo_complet(df)

    # Précision modèle Elo combiné
    df_elo["correct"] = df_elo["prob_combinee"] > 0.5
    precision = df_elo["correct"].mean()
    print(f"📊 Précision Elo combiné : {precision*100:.1f}%")
    print(f"   (vs {df_elo['prob_global'].apply(lambda x: x>0.5).mean()*100:.1f}% avec Elo global seul)\n")

    afficher_classement(elo_global, elo_surface)

    df_elo.to_csv(FICHIER_SORTIE, index=False)
    print(f"\n💾 Sauvegardé : {FICHIER_SORTIE}")

    if stats_joueurs:
        df_stats = pd.DataFrame(stats_joueurs).T.reset_index()
        df_stats.columns = ["joueur"] + list(df_stats.columns[1:])
        df_stats.to_csv(FICHIER_STATS, index=False)
        print(f"💾 Stats joueurs : {FICHIER_STATS} ({len(df_stats)} joueurs)")

    # Elo actuel par joueur (toutes surfaces) → utilisé par tennis_auto.py
    rows_elo_j = []
    for player, eg in elo_global.items():
        rows_elo_j.append({
            "joueur":     player,
            "elo_global": round(eg, 1),
            "elo_dur":    round(elo_surface["dur"].get(player, eg), 1),
            "elo_terre":  round(elo_surface["terre"].get(player, eg), 1),
            "elo_gazon":  round(elo_surface["gazon"].get(player, eg), 1),
        })
    df_elo_j = pd.DataFrame(rows_elo_j).sort_values("elo_global", ascending=False)
    df_elo_j.to_csv(FICHIER_ELO_JOUEURS, index=False)
    print(f"💾 Elo joueurs   : {FICHIER_ELO_JOUEURS} ({len(df_elo_j)} joueurs)")
    print("""
═══════════════════════════════════════════════════════
  PROCHAINE ÉTAPE : python tennis_modele.py
═══════════════════════════════════════════════════════
""")
