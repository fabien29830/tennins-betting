"""
Calibration légère du modèle tennis.
Corrige la surestimation ~28% -> ROI réel -8.8%

Approche: Temperature Scaling
  - Divise les logits par T > 1 -> réduit la confiance vers 0.5
  - Simple, rapide, pas de retraining
  - T est estimé par minimisation du log-loss sur données récentes
"""
import pandas as pd
import numpy as np
import os
import pickle
from scipy.optimize import minimize_scalar
from scipy.special import expit  # sigmoid

BASE = os.path.dirname(os.path.abspath(__file__))
# Cherche tennis_data depuis le script, remonte si besoin
_candidate = os.path.join(BASE, "tennis_data")
if not os.path.exists(_candidate):
    _candidate = os.path.normpath(os.path.join(BASE, "..", "..", "..", "tennis_data"))
DATA_DIR = _candidate

FICHIER_MODELE    = os.path.join(DATA_DIR, "modele_tennis.pkl")
FICHIER_MODELE_CAL = os.path.join(DATA_DIR, "modele_tennis_calibre.pkl")
FICHIER_SCALER    = os.path.join(DATA_DIR, "scaler.pkl")
FICHIER_COLS      = os.path.join(DATA_DIR, "colonnes.pkl")
FICHIER_ELO       = os.path.join(DATA_DIR, "atp_elo.csv")

FEATURES = [
    "diff_elo_global", "diff_elo_surface",
    "prob_global", "prob_surface", "prob_combinee",
    "diff_fatigue", "h2h_winner",
    "diff_1st_in_pct", "diff_1st_won_pct", "diff_2nd_won_pct",
    "diff_bp_saved_pct", "diff_ace_rate",
]

print("=" * 55)
print("  CALIBRATION DU MODELE TENNIS (Temperature Scaling)")
print("=" * 55)

# 1. Chargement modèle + scaler
print(f"\n1. Chargement modele: {os.path.basename(FICHIER_MODELE)}")
with open(FICHIER_MODELE, "rb") as f:
    modele = pickle.load(f)
with open(FICHIER_SCALER, "rb") as f:
    scaler = pickle.load(f)
print(f"   Type: {type(modele).__name__}")

# 2. Données historiques (max 500 exemples)
print(f"\n2. Chargement données (max 500 exemples)...")
df_elo = pd.read_csv(FICHIER_ELO, parse_dates=["date"])
df_elo = df_elo.sort_values("date").reset_index(drop=True)
print(f"   {len(df_elo)} matchs totaux")

N = min(250, len(df_elo))
df_elo = df_elo.tail(N).reset_index(drop=True)
print(f"   Utilisation des {N} derniers matchs -> {N*2} exemples")

stat_keys = ["diff_1st_in_pct", "diff_1st_won_pct", "diff_2nd_won_pct",
             "diff_bp_saved_pct", "diff_ace_rate"]

lignes = []
for _, row in df_elo.iterrows():
    srv = {k: (row.get(k, 0.0) if not pd.isna(row.get(k, np.nan)) else 0.0)
           for k in stat_keys}
    lignes.append({
        "diff_elo_global":  row["diff_elo_global"],
        "diff_elo_surface": row["diff_elo_surface"],
        "prob_global":      row["prob_global"],
        "prob_surface":     row["prob_surface"],
        "prob_combinee":    row["prob_combinee"],
        "diff_fatigue":     row["diff_fatigue"],
        "h2h_winner":       row["h2h_winner"],
        **srv, "label": 1
    })
    lignes.append({
        "diff_elo_global":  -row["diff_elo_global"],
        "diff_elo_surface": -row["diff_elo_surface"],
        "prob_global":      1 - row["prob_global"],
        "prob_surface":     1 - row["prob_surface"],
        "prob_combinee":    1 - row["prob_combinee"],
        "diff_fatigue":     -row["diff_fatigue"],
        "h2h_winner":       row["h2h_loser"],
        **{k: -v for k, v in srv.items()}, "label": 0
    })

df = pd.DataFrame(lignes)
X = df[FEATURES]
y = df["label"].values
X_scaled = scaler.transform(X)

# 3. Diagnostic avant
probs_avant = modele.predict_proba(X_scaled)[:, 1]
acc_avant = (probs_avant.round() == y).mean()
prob_moy_avant = probs_avant[probs_avant > 0.5].mean()

# Calibration check par buckets
buckets = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0)]
print(f"\n3. Calibration avant (prediction vs realite):")
print(f"   {'Bucket':<12} {'Predit':>8} {'Reel':>8} {'N':>5}")
print(f"   {'-'*35}")
for lo, hi in buckets:
    mask = (probs_avant >= lo) & (probs_avant < hi)
    if mask.sum() > 0:
        pred_moy = probs_avant[mask].mean()
        reel_moy = y[mask].mean()
        print(f"   {lo:.1f}-{hi:.1f}       {pred_moy*100:>6.1f}%  {reel_moy*100:>6.1f}%  {mask.sum():>5}")
print(f"   Accuracy globale: {acc_avant*100:.1f}%")

# 4. Optimisation de la température T par minimisation log-loss
print(f"\n4. Optimisation Temperature Scaling...")

# Logits du modèle de base
logits = np.log(np.clip(probs_avant, 1e-7, 1-1e-7) /
                (1 - np.clip(probs_avant, 1e-7, 1-1e-7)))

def log_loss_T(T):
    p = expit(logits / T)
    p = np.clip(p, 1e-7, 1-1e-7)
    return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))

res = minimize_scalar(log_loss_T, bounds=(0.5, 5.0), method='bounded')
T_optimal = res.x
print(f"   Temperature optimale: T = {T_optimal:.4f}")
print(f"   (T=1.0 = pas de changement, T>1 = plus conservateur)")

from tennis_modele import ModeleTemperature
calibrateur = ModeleTemperature(modele, T_optimal)

# 5. Vérification après calibration
probs_apres = calibrateur.predict_proba(X_scaled)[:, 1]
acc_apres = (probs_apres.round() == y).mean()
prob_moy_apres = probs_apres[probs_apres > 0.5].mean()

print(f"\n5. Résultats après calibration:")
print(f"   {'Bucket':<12} {'Predit':>8} {'Reel':>8} {'N':>5}")
print(f"   {'-'*35}")
for lo, hi in buckets:
    p_cal_lo = expit(np.log(lo/(1-lo)) / T_optimal) if lo > 0 else 0
    p_cal_hi = expit(np.log(hi/(1-hi)) / T_optimal) if hi < 1 else 1
    mask = (probs_apres >= p_cal_lo) & (probs_apres < p_cal_hi)
    if mask.sum() > 0:
        pred_moy = probs_apres[mask].mean()
        reel_moy = y[mask].mean()
        print(f"   {p_cal_lo:.2f}-{p_cal_hi:.2f}   {pred_moy*100:>6.1f}%  {reel_moy*100:>6.1f}%  {mask.sum():>5}")
print(f"   Accuracy globale: {acc_apres*100:.1f}%")
print(f"   Prob moyenne (>0.5): {prob_moy_avant*100:.1f}% -> {prob_moy_apres*100:.1f}%")
print(f"   Reduction confiance: {(prob_moy_avant - prob_moy_apres)*100:.1f}pp")

# 6. Sauvegarde
print(f"\n6. Sauvegarde -> {os.path.basename(FICHIER_MODELE_CAL)}")
with open(FICHIER_MODELE_CAL, "wb") as f:
    pickle.dump(calibrateur, f)
print(f"   OK ({FICHIER_MODELE_CAL})")

print(f"\n{'='*55}")
print(f"  Calibration terminee!")
print(f"  Temperature T = {T_optimal:.4f}")
print(f"  Confiance moy: {prob_moy_avant*100:.1f}% -> {prob_moy_apres*100:.1f}%")
print(f"{'='*55}\n")
