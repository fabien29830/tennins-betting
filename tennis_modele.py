"""
=============================================================
  TENNIS MODELE v2 - Avec Elo surface + fatigue + H2H
=============================================================

NOUVEAUTÉS :
    - Utilise le nouvel Elo par surface
    - Intègre la fatigue des joueurs
    - Intègre le head-to-head
    - XGBoost en option (pip install xgboost)

UTILISATION :
    python tennis_modele.py
"""

import pandas as pd
import numpy as np
import os
import pickle
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

DOSSIER_DATA        = "tennis_data"
FICHIER_ELO         = os.path.join(DOSSIER_DATA, "atp_elo.csv")
FICHIER_MODELE      = os.path.join(DOSSIER_DATA, "modele_tennis.pkl")
FICHIER_MODELE_CAL  = os.path.join(DOSSIER_DATA, "modele_tennis_calibre.pkl")
FICHIER_SCALER      = os.path.join(DOSSIER_DATA, "scaler.pkl")
FICHIER_COLS        = os.path.join(DOSSIER_DATA, "colonnes.pkl")

# Toutes les features disponibles
FEATURES = [
    "diff_elo_global",    # différence Elo global
    "diff_elo_surface",   # différence Elo sur cette surface
    "prob_global",        # probabilité Elo global
    "prob_surface",       # probabilité Elo surface
    "prob_combinee",      # probabilité combinée (meilleure)
    "diff_fatigue",       # différence de fatigue (loser - winner)
    "h2h_winner",         # ratio H2H du winner
    # Stats service/retour (moyennes glissantes sur 20 matchs)
    "diff_1st_in_pct",    # diff % 1ère balle rentée
    "diff_1st_won_pct",   # diff % points gagnés sur 1ère balle
    "diff_2nd_won_pct",   # diff % points gagnés sur 2ème balle
    "diff_bp_saved_pct",  # diff % balles de break sauvées
    "diff_ace_rate",      # diff taux d'aces
]


def charger_modele():
    """
    Charge le modèle calibré s'il existe, sinon le modèle original.
    Retourne (modele, scaler, colonnes).
    """
    if os.path.exists(FICHIER_MODELE_CAL):
        with open(FICHIER_MODELE_CAL, "rb") as f:
            modele = pickle.load(f)
        print(f"✅ Modèle calibré chargé: {FICHIER_MODELE_CAL}")
    else:
        with open(FICHIER_MODELE, "rb") as f:
            modele = pickle.load(f)
        print(f"⚠️  Modèle original chargé (pas de calibré): {FICHIER_MODELE}")

    with open(FICHIER_SCALER, "rb") as f:
        scaler = pickle.load(f)
    with open(FICHIER_COLS, "rb") as f:
        colonnes = pickle.load(f)

    return modele, scaler, colonnes


def preparer_donnees(df_elo):
    print("🔧 Préparation des données...")
    lignes = []

    stat_keys = ["diff_1st_in_pct", "diff_1st_won_pct", "diff_2nd_won_pct",
                 "diff_bp_saved_pct", "diff_ace_rate"]

    for _, row in df_elo.iterrows():
        srv = {k: row.get(k, 0.0) if not pd.isna(row.get(k, np.nan)) else 0.0
               for k in stat_keys}
        # Version 1 : winner = joueur A (label 1)
        lignes.append({
            "diff_elo_global":   row["diff_elo_global"],
            "diff_elo_surface":  row["diff_elo_surface"],
            "prob_global":       row["prob_global"],
            "prob_surface":      row["prob_surface"],
            "prob_combinee":     row["prob_combinee"],
            "diff_fatigue":      row["diff_fatigue"],
            "h2h_winner":        row["h2h_winner"],
            **srv,
            "label":             1
        })
        # Version 2 : loser = joueur A (label 0, tout inversé)
        lignes.append({
            "diff_elo_global":   -row["diff_elo_global"],
            "diff_elo_surface":  -row["diff_elo_surface"],
            "prob_global":       1 - row["prob_global"],
            "prob_surface":      1 - row["prob_surface"],
            "prob_combinee":     1 - row["prob_combinee"],
            "diff_fatigue":      -row["diff_fatigue"],
            "h2h_winner":        row["h2h_loser"],
            **{k: -v for k, v in srv.items()},
            "label":             0
        })

    df = pd.DataFrame(lignes)
    print(f"  ✅ {len(df)} exemples générés\n")
    return df


def entrainer_modeles(df):
    print("🤖 Entraînement des modèles...")
    X = df[FEATURES]
    y = df["label"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )
    print(f"  Train: {len(X_train)} | Test: {len(X_test)}\n")

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s  = scaler.transform(X_test)

    resultats = {}

    print("  📈 Régression Logistique...")
    lr = LogisticRegression(max_iter=1000)
    lr.fit(X_train_s, y_train)
    acc = accuracy_score(y_test, lr.predict(X_test_s))
    print(f"     Précision : {acc*100:.1f}%")
    resultats["Régression Logistique"] = (lr, acc)

    print("  🌲 Random Forest...")
    rf = RandomForestClassifier(n_estimators=200, max_depth=6, random_state=42)
    rf.fit(X_train_s, y_train)
    acc = accuracy_score(y_test, rf.predict(X_test_s))
    print(f"     Précision : {acc*100:.1f}%")
    resultats["Random Forest"] = (rf, acc)

    print("  🚀 Gradient Boosting...")
    gb = GradientBoostingClassifier(n_estimators=200, max_depth=4,
                                     learning_rate=0.05, random_state=42)
    gb.fit(X_train_s, y_train)
    acc = accuracy_score(y_test, gb.predict(X_test_s))
    print(f"     Précision : {acc*100:.1f}%")
    resultats["Gradient Boosting"] = (gb, acc)

    # Essayer XGBoost si installé
    try:
        from xgboost import XGBClassifier
        print("  ⚡ XGBoost...")
        xgb = XGBClassifier(n_estimators=200, max_depth=4,
                             learning_rate=0.05, random_state=42,
                             eval_metric="logloss", verbosity=0)
        xgb.fit(X_train_s, y_train)
        acc = accuracy_score(y_test, xgb.predict(X_test_s))
        print(f"     Précision : {acc*100:.1f}%")
        resultats["XGBoost"] = (xgb, acc)
    except ImportError:
        print("  ⚡ XGBoost non installé (optionnel)")
        print("     Pour l'activer : pip install xgboost")

    meilleur_nom = max(resultats, key=lambda k: resultats[k][1])
    meilleur, acc = resultats[meilleur_nom]
    print(f"\n  🏆 Meilleur modèle : {meilleur_nom} ({acc*100:.1f}%)")

    # Importance des features (si disponible)
    if hasattr(meilleur, "feature_importances_"):
        print(f"\n  📊 Importance des variables :")
        importances = sorted(zip(FEATURES, meilleur.feature_importances_),
                             key=lambda x: x[1], reverse=True)
        for feat, imp in importances:
            barre = "█" * int(imp * 40)
            print(f"     {feat:<22} {barre} {imp*100:.1f}%")

    return meilleur, scaler


def predire_match(modele, scaler,
                   elo_w_global, elo_l_global,
                   elo_w_surface, elo_l_surface,
                   fatigue_w=3, fatigue_l=3,
                   h2h_w=0.5):
    """Prédit la probabilité de victoire du joueur 1."""
    def pv(a, b): return 1 / (1 + 10 ** ((b - a) / 400))

    prob_g = pv(elo_w_global, elo_l_global)
    prob_s = pv(elo_w_surface, elo_l_surface)
    prob_c = 0.6 * prob_s + 0.4 * prob_g

    exemple = pd.DataFrame([{
        "diff_elo_global":   elo_w_global - elo_l_global,
        "diff_elo_surface":  elo_w_surface - elo_l_surface,
        "prob_global":       prob_g,
        "prob_surface":      prob_s,
        "prob_combinee":     prob_c,
        "diff_fatigue":      fatigue_l - fatigue_w,
        "h2h_winner":        h2h_w,
        "diff_1st_in_pct":   0.0,
        "diff_1st_won_pct":  0.0,
        "diff_2nd_won_pct":  0.0,
        "diff_bp_saved_pct": 0.0,
        "diff_ace_rate":     0.0,
    }])[FEATURES]
    return modele.predict_proba(scaler.transform(exemple))[0][1]


if __name__ == "__main__":
    print("🎾 TENNIS - MODÈLE v2 (Surface + Fatigue + H2H)")
    print("=" * 55)

    if not os.path.exists(FICHIER_ELO):
        print("❌ Lance d'abord tennis_elo.py")
        exit(1)

    df_elo = pd.read_csv(FICHIER_ELO, parse_dates=["date"])
    df_elo = df_elo.sort_values("date").reset_index(drop=True)
    print(f"📂 {len(df_elo)} matchs chargés\n")

    df_train = preparer_donnees(df_elo)
    modele, scaler = entrainer_modeles(df_train)

    with open(FICHIER_MODELE, "wb") as f:
        pickle.dump(modele, f)
    with open(FICHIER_SCALER, "wb") as f:
        pickle.dump(scaler, f)
    with open(FICHIER_COLS, "wb") as f:
        pickle.dump(FEATURES, f)

    print(f"\n💾 Modèle sauvegardé")

    # Exemples de prédictions
    print("\n🎾 EXEMPLES DE PRÉDICTIONS")
    print("=" * 55)

    matchs = [
        # (nom1, elo_global1, elo_surface1, nom2, elo_global2, elo_surface2, surface)
        ("Jannik Sinner",   2131, 2050, "Carlos Alcaraz",     2089, 2100, "dur"),
        ("Novak Djokovic",  2012, 1980, "Daniil Medvedev",    1923, 1950, "dur"),
        ("Rafael Nadal",    1744, 2100, "Holger Rune",        1821, 1780, "terre"),
    ]

    for n1, eg1, es1, n2, eg2, es2, surf in matchs:
        p = predire_match(modele, scaler, eg1, eg2, es1, es2)
        favori = n1 if p > 0.5 else n2
        print(f"\n  {n1} vs {n2} ({surf})")
        print(f"  → {n1}: {p*100:.1f}%  |  {n2}: {(1-p)*100:.1f}%")
        print(f"  → Favori : {favori}")

    print("""
═══════════════════════════════════════════════════════
  PROCHAINE ÉTAPE : python tennis_paris.py
═══════════════════════════════════════════════════════
""")
