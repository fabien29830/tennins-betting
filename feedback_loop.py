"""
=============================================================
  FEEDBACK LOOP — Calibration, dérive ROI, logs décisions
=============================================================

Ce module analyse les paris résolus pour détecter :
  1. Dérive ROI (rolling 10 et 20 paris)
  2. Calibration du modèle (prob prédite vs win rate réel)
  3. Brier score (qualité des probabilités)
  4. Alertes de recalibration si dérive > seuil

Fichiers lus :
  - tennis_data/decisions.jsonl   -> log structuré de chaque décision
  - docs/data.json                -> résultats résolus + bankroll

Fichier produit :
  - tennis_data/feedback_report.json  -> rapport complet
  - tennis_data/feedback_alerte.txt   -> alerte texte si dérive

Usage :
  python feedback_loop.py              # rapport complet
  python feedback_loop.py --check      # code retour 1 si recalibration nécessaire
"""

import json
import os
import math
import argparse
from datetime import datetime, date
from collections import defaultdict

# -------------------------------------------------------------
# CHEMINS
# -------------------------------------------------------------
DOSSIER      = os.path.dirname(os.path.abspath(__file__))
DOSSIER_DATA = os.path.join(DOSSIER, "tennis_data")
DECISIONS_FILE  = os.path.join(DOSSIER_DATA, "decisions.jsonl")
DATA_JSON       = os.path.join(DOSSIER, "docs", "data.json")
RAPPORT_FILE    = os.path.join(DOSSIER_DATA, "feedback_report.json")
ALERTE_FILE     = os.path.join(DOSSIER_DATA, "feedback_alerte.txt")

# -------------------------------------------------------------
# SEUILS D'ALERTE
# -------------------------------------------------------------
ROI_DRIFT_SEUIL        = -0.10   # ROI rolling < -10% -> alerte
CALIBRATION_ECART_MAX  = 0.10    # |prob_pred - win_rate| > 10% -> alerte
BRIER_SCORE_MAX        = 0.30    # Brier score > 0.30 -> modèle faible
MIN_PARIS_ANALYSE      = 10      # Minimum de paris résolus pour analyser


# -------------------------------------------------------------
# CHARGEMENT DES DONNÉES
# -------------------------------------------------------------

def charger_decisions() -> list[dict]:
    """Charge les logs structurés de decisions.jsonl."""
    if not os.path.exists(DECISIONS_FILE):
        return []
    decisions = []
    with open(DECISIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    decisions.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return decisions


def charger_paris_resolus() -> list[dict]:
    """
    Charge les paris résolus depuis docs/data.json.
    Retourne une liste de dicts avec : date, match, joueur, cote, mise,
    prob_modele, ev, resultat (Gagné/Perdu), gain_perte.
    """
    if not os.path.exists(DATA_JSON):
        return []
    with open(DATA_JSON, encoding="utf-8") as f:
        data = json.load(f)

    paris = []
    for b in data.get("paris_recents", []):
        if b.get("resultat") in ("Gagné", "Perdu"):
            paris.append(b)
    return paris


# -------------------------------------------------------------
# ANALYSES
# -------------------------------------------------------------

def analyser_roi_rolling(paris: list[dict], fenetre: int = 10) -> dict:
    """
    Calcule le ROI sur les N derniers paris (fenêtre glissante).
    Retourne {roi_rolling_10, roi_rolling_20, roi_global, tendance}.
    """
    if not paris:
        return {}

    resolus = [b for b in paris if b.get("resultat") in ("Gagné", "Perdu")]
    if not resolus:
        return {}

    # Trier par date (les paris_recents sont déjà en ordre décroissant)
    resolus_chrono = list(reversed(resolus))

    def roi_sur(bets):
        gain = sum(b["gain_perte"] for b in bets)
        mise = sum(b["mise"] for b in bets)
        return gain / mise if mise > 0 else 0.0

    roi_global = roi_sur(resolus_chrono)

    last_10 = resolus_chrono[-10:] if len(resolus_chrono) >= 10 else resolus_chrono
    last_20 = resolus_chrono[-20:] if len(resolus_chrono) >= 20 else resolus_chrono

    roi_10 = roi_sur(last_10)
    roi_20 = roi_sur(last_20)

    # Tendance : comparaison roi_10 vs roi_global
    if len(resolus_chrono) >= 10:
        tendance = "hausse" if roi_10 > roi_global else "baisse"
    else:
        tendance = "insuffisant"

    # Série en cours (win streak / lose streak)
    serie = 0
    dernier = None
    for b in reversed(resolus_chrono):
        r = b["resultat"]
        if dernier is None:
            dernier = r
            serie = 1
        elif r == dernier:
            serie += 1
        else:
            break

    return {
        "roi_global":    round(roi_global, 4),
        "roi_rolling_10": round(roi_10, 4),
        "roi_rolling_20": round(roi_20, 4),
        "tendance":      tendance,
        "serie_en_cours": f"{serie}x {dernier}",
        "nb_paris_resolus": len(resolus_chrono),
    }


def analyser_calibration(decisions: list[dict], paris: list[dict]) -> dict:
    """
    Compare prob_modele prédite vs win rate réel, par bucket de probabilité.

    Utilise decisions.jsonl si disponible (données les plus riches),
    sinon repart sur paris_recents de data.json.
    """
    # Assembler les données : on joint decisions aux résultats
    # Format decisions : {match, joueur, prob_modele, ev, cote, accepted, ...}
    # Format paris_recents : {match, joueur, prob_modele (si présent), resultat}

    echantillon = []

    # Priorité : données de decisions.jsonl enrichies avec résultats
    if decisions:
        paris_index = {
            (b.get("match", ""), b.get("joueur", "")): b
            for b in paris
        }
        for d in decisions:
            if not d.get("accepted"):
                continue
            key = (d.get("match", ""), d.get("joueur", ""))
            pari = paris_index.get(key)
            if pari and pari.get("resultat") in ("Gagné", "Perdu"):
                echantillon.append({
                    "prob": d.get("prob_modele", 0.5),
                    "gagne": 1 if pari["resultat"] == "Gagné" else 0,
                    "cote": d.get("cote", 0),
                    "ev": d.get("ev", 0),
                })
    else:
        # Fallback : paris_recents sans prob_modele explicite
        # On estime prob_modele depuis ev et cote : prob = (ev+1)/cote
        for b in paris:
            if b.get("resultat") not in ("Gagné", "Perdu"):
                continue
            cote = b.get("cote", 0)
            ev = b.get("ev", 0) / 100  # stocké en % dans data.json
            if cote > 1:
                prob = (1 + ev) / cote
                echantillon.append({
                    "prob": min(max(prob, 0.01), 0.99),
                    "gagne": 1 if b["resultat"] == "Gagné" else 0,
                    "cote": cote,
                    "ev": ev,
                })

    if len(echantillon) < MIN_PARIS_ANALYSE:
        return {"statut": "insuffisant", "nb": len(echantillon)}

    # Brier score : MSE des probabilités
    brier = sum((e["prob"] - e["gagne"]) ** 2 for e in echantillon) / len(echantillon)

    # Calibration par bucket (0-40%, 40-55%, 55-70%, 70%+)
    buckets = [
        (0.00, 0.40, "< 40%"),
        (0.40, 0.55, "40-55%"),
        (0.55, 0.70, "55-70%"),
        (0.70, 1.01, "> 70%"),
    ]
    calibration = []
    for lo, hi, label in buckets:
        subset = [e for e in echantillon if lo <= e["prob"] < hi]
        if not subset:
            continue
        prob_moy  = sum(e["prob"] for e in subset) / len(subset)
        win_rate  = sum(e["gagne"] for e in subset) / len(subset)
        ecart     = win_rate - prob_moy
        calibration.append({
            "bucket":    label,
            "n":         len(subset),
            "prob_pred": round(prob_moy, 3),
            "win_rate":  round(win_rate, 3),
            "ecart":     round(ecart, 3),
            "alerte":    abs(ecart) > CALIBRATION_ECART_MAX,
        })

    return {
        "statut":      "ok",
        "nb":          len(echantillon),
        "brier_score": round(brier, 4),
        "brier_alerte": brier > BRIER_SCORE_MAX,
        "buckets":     calibration,
    }


def analyser_profils(paris: list[dict]) -> dict:
    """
    ROI par profil : surface, tranche de cote, circuit.
    """
    resolus = [b for b in paris if b.get("resultat") in ("Gagné", "Perdu")]
    if not resolus:
        return {}

    def roi_groupe(items):
        gain = sum(b["gain_perte"] for b in items)
        mise = sum(b["mise"] for b in items)
        wins = sum(1 for b in items if b["resultat"] == "Gagné")
        n = len(items)
        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 3) if n else 0,
            "roi": round(gain / mise, 4) if mise else 0,
            "gain": round(gain, 2),
        }

    # Par tranche de cote
    tranches_cote = {
        "favori (<=1.5)":   [b for b in resolus if b["cote"] <= 1.5],
        "leger fav (1.5-2)": [b for b in resolus if 1.5 < b["cote"] <= 2.0],
        "equilibre (2-3)":  [b for b in resolus if 2.0 < b["cote"] <= 3.0],
        "outsider (>3)":    [b for b in resolus if b["cote"] > 3.0],
    }

    return {
        "par_cote": {
            label: roi_groupe(items)
            for label, items in tranches_cote.items()
            if items
        },
    }


def detecter_alertes(roi: dict, calibration: dict, profils: dict) -> list[str]:
    """Génère la liste des alertes selon les seuils configurés."""
    alertes = []

    # Dérive ROI
    if roi.get("roi_rolling_10", 0) < ROI_DRIFT_SEUIL:
        alertes.append(
            f"ROI rolling 10 paris = {roi['roi_rolling_10']*100:+.1f}% "
            f"< seuil {ROI_DRIFT_SEUIL*100:.0f}% -> recalibration recommandée"
        )
    if roi.get("roi_rolling_20", 0) < ROI_DRIFT_SEUIL:
        alertes.append(
            f"ROI rolling 20 paris = {roi['roi_rolling_20']*100:+.1f}% "
            f"< seuil {ROI_DRIFT_SEUIL*100:.0f}% -> recalibration recommandée"
        )

    # Brier score
    if calibration.get("brier_alerte"):
        alertes.append(
            f"Brier score = {calibration.get('brier_score', 0):.3f} "
            f"> {BRIER_SCORE_MAX} -> probabilités mal calibrées"
        )

    # Buckets de calibration
    for b in calibration.get("buckets", []):
        if b.get("alerte"):
            direction = "sur-estime" if b["ecart"] < 0 else "sous-estime"
            alertes.append(
                f"Calibration bucket {b['bucket']} : modèle {direction} "
                f"({b['prob_pred']*100:.0f}% prédit vs {b['win_rate']*100:.0f}% réel, "
                f"écart {b['ecart']*100:+.0f}%)"
            )

    # Outsiders
    outsiders = profils.get("par_cote", {}).get("outsider (>3)", {})
    if outsiders and outsiders.get("n", 0) >= 3 and outsiders.get("roi", 0) < -0.15:
        alertes.append(
            f"Outsiders (cote>3) : ROI {outsiders['roi']*100:+.1f}% "
            f"sur {outsiders['n']} paris -> filtre COTE_MAX à vérifier"
        )

    return alertes


# -------------------------------------------------------------
# RAPPORT
# -------------------------------------------------------------

def generer_rapport() -> dict:
    """Génère le rapport complet et le sauvegarde."""
    decisions = charger_decisions()
    paris     = charger_paris_resolus()

    roi         = analyser_roi_rolling(paris)
    calibration = analyser_calibration(decisions, paris)
    profils     = analyser_profils(paris)
    alertes     = detecter_alertes(roi, calibration, profils)

    recal_necessaire  = len(alertes) >= 2
    nb_resolus        = roi.get("nb_paris_resolus", 0)
    # Ré-entraînement complet si assez de données live pour enrichir le dataset
    SEUIL_REENTRAINEMENT = 30
    reentrainement_complet = recal_necessaire and nb_resolus >= SEUIL_REENTRAINEMENT

    action_recommandee = (
        "reentrainement_complet" if reentrainement_complet else
        "recalibration"          if recal_necessaire else
        "aucune"
    )

    rapport = {
        "genere_le":               datetime.now().isoformat(timespec="seconds"),
        "roi":                     roi,
        "calibration":             calibration,
        "profils":                 profils,
        "alertes":                 alertes,
        "recalibration_necessaire":  recal_necessaire,
        "reentrainement_complet":    reentrainement_complet,
        "action_recommandee":        action_recommandee,
        "seuil_reentrainement":      SEUIL_REENTRAINEMENT,
    }

    os.makedirs(DOSSIER_DATA, exist_ok=True)
    with open(RAPPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(rapport, f, indent=2, ensure_ascii=False)

    # Fichier texte d'alerte si nécessaire
    if alertes:
        with open(ALERTE_FILE, "w", encoding="utf-8") as f:
            f.write(f"ALERTES FEEDBACK LOOP — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
            f.write("=" * 60 + "\n")
            for a in alertes:
                f.write(f"  [!]  {a}\n")
    elif os.path.exists(ALERTE_FILE):
        os.remove(ALERTE_FILE)

    return rapport


def afficher_rapport(rapport: dict):
    """Affiche le rapport en console."""
    print("\n" + "=" * 60)
    print("  FEEDBACK LOOP — ANALYSE DU MODÈLE")
    print("=" * 60)

    roi = rapport.get("roi", {})
    if roi:
        print(f"\n  ROI global        : {roi.get('roi_global', 0)*100:+.1f}%")
        print(f"  ROI rolling 10    : {roi.get('roi_rolling_10', 0)*100:+.1f}%")
        print(f"  ROI rolling 20    : {roi.get('roi_rolling_20', 0)*100:+.1f}%")
        print(f"  Tendance          : {roi.get('tendance', '?')}")
        print(f"  Série en cours    : {roi.get('serie_en_cours', '?')}")
        print(f"  Paris résolus     : {roi.get('nb_paris_resolus', 0)}")

    cal = rapport.get("calibration", {})
    if cal.get("statut") == "ok":
        flag_b = "  [!] ELEVE" if cal.get('brier_alerte') else "  OK"
        print(f"\n  Brier score       : {cal.get('brier_score', 0):.3f}{flag_b}")
        print(f"\n  {'Bucket':<14} {'N':>4} {'Predit':>8} {'Reel':>8} {'Ecart':>8}")
        print(f"  {'-'*46}")
        for b in cal.get("buckets", []):
            flag = "  [!]" if b.get("alerte") else ""
            print(f"  {b['bucket']:<14} {b['n']:>4} {b['prob_pred']*100:>7.1f}% "
                  f"{b['win_rate']*100:>7.1f}% {b['ecart']*100:>+7.1f}%{flag}")
    else:
        print(f"\n  Calibration       : donnees insuffisantes "
              f"({cal.get('nb', 0)} paris resolus, min {MIN_PARIS_ANALYSE})")

    prof = rapport.get("profils", {}).get("par_cote", {})
    if prof:
        print(f"\n  {'Profil':<22} {'N':>4} {'W/L':>6} {'ROI':>8}")
        print(f"  {'-'*44}")
        for label, stats in prof.items():
            print(f"  {label:<22} {stats['n']:>4} "
                  f"{stats['wins']}/{stats['n']:<4} "
                  f"{stats['roi']*100:>+7.1f}%")

    alertes = rapport.get("alertes", [])
    action  = rapport.get("action_recommandee", "aucune")
    seuil   = rapport.get("seuil_reentrainement", 30)
    nb      = rapport.get("roi", {}).get("nb_paris_resolus", 0)

    if alertes:
        print(f"\n  [!][!][!]  {len(alertes)} ALERTE(S) DETECTEE(S)  [!][!][!]")
        for a in alertes:
            print(f"  -> {a}")
        if action == "reentrainement_complet":
            print(f"\n  ACTION : re-entrainement complet ({nb} paris resolus >= {seuil})")
            print("           python tennis_modele.py && python calibrer_modele.py")
            print("           (automatique au prochain pipeline du lundi)")
        elif action == "recalibration":
            print(f"\n  ACTION : recalibration temperature scaling")
            print(f"           ({nb} paris resolus < {seuil}, pas assez pour re-entrainer)")
            print("           python calibrer_modele.py")
            print("           (automatique au prochain pipeline du lundi)")
    else:
        print("\n  [OK] Aucune alerte — modele stable")

    print(f"\n  Rapport sauvegardé : {RAPPORT_FILE}")
    print("=" * 60)


# -------------------------------------------------------------
# HELPER : enregistrer une décision (appelé par tennis_auto.py)
# -------------------------------------------------------------

def log_decision(
    match: str,
    joueur: str,
    surface: str,
    cote: float,
    prob_modele: float,
    ev: float,
    accepted: bool,
    reject_reason: str | None = None,
    ev_ok: bool = True,
    cote_ok: bool = True,
    surface_ok: bool = True,
    circuit: str = "",
    tournoi: str = "",
):
    """
    Enregistre une décision dans decisions.jsonl.
    Chaque ligne = un pari considéré (accepté ou rejeté).
    """
    os.makedirs(DOSSIER_DATA, exist_ok=True)
    entree = {
        "ts":           datetime.now().isoformat(timespec="seconds"),
        "match":        match,
        "joueur":       joueur,
        "circuit":      circuit,
        "tournoi":      tournoi,
        "surface":      surface,
        "cote":         round(cote, 2),
        "prob_modele":  round(prob_modele, 3),
        "ev":           round(ev, 4),
        "accepted":     accepted,
        "reject_reason": reject_reason,
        "filters": {
            "ev_ok":      ev_ok,
            "cote_ok":    cote_ok,
            "surface_ok": surface_ok,
        },
    }
    with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False) + "\n")


# -------------------------------------------------------------
# MAIN
# -------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Feedback loop — analyse du modèle")
    parser.add_argument("--check", action="store_true",
                        help="Code retour 1 si recalibration nécessaire")
    args = parser.parse_args()

    rapport = generer_rapport()
    afficher_rapport(rapport)

    if args.check and rapport.get("recalibration_necessaire"):
        exit(1)
