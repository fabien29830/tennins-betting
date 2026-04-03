"""
=============================================================
  TENNIS VALUE BETTING v2 - Compatible modèle amélioré
=============================================================

UTILISATION :
    python tennis_paris.py
"""

import pandas as pd
import numpy as np
import os
import pickle
from tennis_modele import charger_modele

DOSSIER_DATA    = "tennis_data"
FICHIER_STATS   = os.path.join(DOSSIER_DATA, "stats_joueurs.csv")

BANKROLL_DEPART = 1000
SEUIL_VALUE     = 0.05
FRACTION_KELLY  = 0.5
MISE_MAX_PCT    = 0.05


def predire_probabilite(modele, scaler, features,
                          elo_w_g, elo_l_g, elo_w_s, elo_l_s,
                          fatigue_w=3, fatigue_l=3, h2h_w=0.5,
                          stats_w=None, stats_l=None):
    def pv(a, b): return 1 / (1 + 10 ** ((b - a) / 400))
    prob_g = pv(elo_w_g, elo_l_g)
    prob_s = pv(elo_w_s, elo_l_s)
    prob_c = 0.6 * prob_s + 0.4 * prob_g

    data = {
        "diff_elo_global":   elo_w_g - elo_l_g,
        "diff_elo_surface":  elo_w_s - elo_l_s,
        "prob_global":       prob_g,
        "prob_surface":      prob_s,
        "prob_combinee":     prob_c,
        "diff_fatigue":      fatigue_l - fatigue_w,
        "h2h_winner":        h2h_w,
        # Stats service : 0 par défaut (neutre) si inconnues
        "diff_1st_in_pct":   0.0,
        "diff_1st_won_pct":  0.0,
        "diff_2nd_won_pct":  0.0,
        "diff_bp_saved_pct": 0.0,
        "diff_ace_rate":     0.0,
    }
    if stats_w and stats_l:
        for k in ["1st_in_pct", "1st_won_pct", "2nd_won_pct", "bp_saved_pct", "ace_rate"]:
            vw = stats_w.get(k, np.nan)
            vl = stats_l.get(k, np.nan)
            if not (np.isnan(vw) or np.isnan(vl)):
                data[f"diff_{k}"] = vw - vl

    exemple = pd.DataFrame([{f: data.get(f, 0.0) for f in features}])
    return modele.predict_proba(scaler.transform(exemple))[0][1]


def normaliser_cotes(cote_j1, cote_j2):
    """Retire la marge bookmaker : renvoie les probas implicites normalisées."""
    p1_brute = 1 / cote_j1
    p2_brute = 1 / cote_j2
    total = p1_brute + p2_brute  # > 1.0 à cause de la marge
    return p1_brute / total, p2_brute / total

def calculer_value(prob_modele, prob_implicite_normalisee):
    """EV = prob_modele / prob_implicite_normalisee - 1 (sans marge bookmaker)."""
    return (prob_modele / prob_implicite_normalisee) - 1

def calculer_mise(prob, cote, bankroll):
    kelly = ((prob * cote) - 1) / (cote - 1)
    if kelly <= 0:
        return 0
    return min(kelly * FRACTION_KELLY * bankroll, bankroll * MISE_MAX_PCT)


def analyser_pari(match, modele, scaler, features, bankroll, stats_lookup=None):
    nom_j1, elo_g1, elo_s1, cote_j1, nom_j2, elo_g2, elo_s2, cote_j2, \
        fatigue_j1, fatigue_j2, h2h_j1, surface = match

    stats_j1 = stats_lookup.get(nom_j1) if stats_lookup else None
    stats_j2 = stats_lookup.get(nom_j2) if stats_lookup else None

    prob_j1 = predire_probabilite(modele, scaler, features,
                                   elo_g1, elo_g2, elo_s1, elo_s2,
                                   fatigue_j1, fatigue_j2, h2h_j1,
                                   stats_j1, stats_j2)
    prob_j2 = 1 - prob_j1
    impl_j1, impl_j2 = normaliser_cotes(cote_j1, cote_j2)
    ev_j1   = calculer_value(prob_j1, impl_j1)
    ev_j2   = calculer_value(prob_j2, impl_j2)
    mise_j1 = calculer_mise(prob_j1, cote_j1, bankroll)
    mise_j2 = calculer_mise(prob_j2, cote_j2, bankroll)

    print(f"\n{'═'*58}")
    print(f"  {nom_j1}  vs  {nom_j2}  [{surface}]")
    print(f"{'═'*58}")
    print(f"  {'':28} {nom_j1:<14} {nom_j2}")
    print(f"  {'Elo global':28} {round(elo_g1):<14} {round(elo_g2)}")
    print(f"  {'Elo surface':28} {round(elo_s1):<14} {round(elo_s2)}")
    print(f"  {'Fatigue (matchs/14j)':28} {fatigue_j1:<14} {fatigue_j2}")
    print(f"  {'H2H':28} {h2h_j1*100:.0f}%          {(1-h2h_j1)*100:.0f}%")
    if stats_j1 and stats_j2:
        print(f"  {'─'*56}")
        print(f"  {'Stats service (moy 20m)':28} {nom_j1:<14} {nom_j2}")
        print(f"  {'  1re balle %':28} {stats_j1.get('1st_in_pct',0)*100:.1f}%         {stats_j2.get('1st_in_pct',0)*100:.1f}%")
        print(f"  {'  Pts gagnés/1re balle':28} {stats_j1.get('1st_won_pct',0)*100:.1f}%         {stats_j2.get('1st_won_pct',0)*100:.1f}%")
        print(f"  {'  Pts gagnés/2e balle':28} {stats_j1.get('2nd_won_pct',0)*100:.1f}%         {stats_j2.get('2nd_won_pct',0)*100:.1f}%")
        print(f"  {'  Break pts sauvés %':28} {stats_j1.get('bp_saved_pct',0)*100:.1f}%         {stats_j2.get('bp_saved_pct',0)*100:.1f}%")
        print(f"  {'  Taux d aces':28} {stats_j1.get('ace_rate',0)*100:.1f}%         {stats_j2.get('ace_rate',0)*100:.1f}%")
    print(f"  {'─'*56}")
    marge = (1/cote_j1 + 1/cote_j2 - 1) * 100
    print(f"  {'Prob. modèle':28} {prob_j1*100:.1f}%         {prob_j2*100:.1f}%")
    print(f"  {'Cote bookmaker':28} {cote_j1:<14} {cote_j2}")
    print(f"  {'Prob. implicite (brute)':28} {100/cote_j1:.1f}%         {100/cote_j2:.1f}%")
    print(f"  {'Marge bookmaker':28} {marge:.1f}%")
    print(f"  {'Prob. implicite (nette)':28} {impl_j1*100:.1f}%         {impl_j2*100:.1f}%")
    print(f"  {'Expected Value (net)':28} {ev_j1*100:+.1f}%         {ev_j2*100:+.1f}%")
    print(f"{'─'*58}")

    recommandations = []
    if ev_j1 >= SEUIL_VALUE:
        recommandations.append((nom_j1, cote_j1, ev_j1, mise_j1, prob_j1))
    if ev_j2 >= SEUIL_VALUE:
        recommandations.append((nom_j2, cote_j2, ev_j2, mise_j2, prob_j2))

    if recommandations:
        print(f"\n  ✅ PARI(S) À VALEUR POSITIVE !")
        for nom, cote, ev, mise, prob in recommandations:
            print(f"\n  → Parier sur   : {nom}")
            print(f"     Cote         : {cote}")
            print(f"     Prob. modèle : {prob*100:.1f}%")
            print(f"     Value (EV)   : +{ev*100:.1f}%")
            print(f"     Mise Kelly   : {round(mise, 2)}€")
            print(f"     Gain si win  : +{round(mise*(cote-1), 2)}€")
    else:
        print(f"\n  ❌ Pas de valeur → Ne pas parier.")

    print(f"{'═'*58}")


if __name__ == "__main__":
    print("🎾 TENNIS - VALUE BETTING v2")
    print("=" * 58)

    modele, scaler, features = charger_modele()

    # Chargement des stats de service (optionnel, généré par tennis_elo.py)
    stats_lookup = {}
    if os.path.exists(FICHIER_STATS):
        df_stats = pd.read_csv(FICHIER_STATS).set_index("joueur")
        stats_lookup = df_stats.to_dict(orient="index")
        print(f"📋 Stats service chargées : {len(stats_lookup)} joueurs")
    else:
        print("⚠️  stats_joueurs.csv absent → relance tennis_elo.py pour l'activer")

    print(f"\n💰 Bankroll      : {BANKROLL_DEPART}€")
    print(f"📊 Seuil value   : {SEUIL_VALUE*100}% minimum")
    print(f"🎯 Fraction Kelly: {FRACTION_KELLY*100}%")

    # ════════════════════════════════════════════════════════
    # MODIFIE CES MATCHS AVEC LES VRAIS MATCHS DU WEEK-END
    #
    # Format de chaque match :
    # (nom_j1, elo_global_j1, elo_surface_j1, cote_j1,
    #  nom_j2, elo_global_j2, elo_surface_j2, cote_j2,
    #  fatigue_j1, fatigue_j2,   ← nb matchs joués dans les 14 derniers jours
    #  h2h_j1,                   ← ratio H2H de j1 contre j2 (0.5 si inconnu)
    #  surface)                  ← "dur", "terre", "gazon"
    #
    # Les Elo se trouvent dans tennis_data/atp_elo.csv
    # Les cotes viennent de ton bookmaker
    # ════════════════════════════════════════════════════════
    matchs = [
        # Elo calculés sur données nov. 2024 — relancer tennis_elo.py pour màj 2026
        ("Jannik Sinner",    2131, 2059, 1.17,
         "Alexander Zverev", 1918, 1838, 5.20,
         3, 3, 0.5, "dur"),
    ]

    print("\n\n🎾 ANALYSE DES MATCHS")
    for match in matchs:
        analyser_pari(match, modele, scaler, features, BANKROLL_DEPART, stats_lookup)

    print(f"""
═══════════════════════════════════════════════════════
  ROUTINE HEBDOMADAIRE :

  1. python tennis_data_pipeline.py  (màj données)
  2. python tennis_elo.py            (màj Elo)
  3. python tennis_modele.py         (màj modèle)
  4. Modifie les matchs ci-dessus avec :
     - Les vrais matchs du week-end
     - Les Elo dans tennis_data/atp_elo.csv
     - Les vraies cotes de ton bookmaker
     - La fatigue (nb matchs joués les 14 derniers jours)
     - Le H2H (0.5 si tu ne sais pas)
  5. python tennis_paris.py → décision de pari !

  ⚠️  Ne mise jamais plus que tu peux perdre.
      Rentabilité visible après 200+ paris minimum.
═══════════════════════════════════════════════════════
""")
