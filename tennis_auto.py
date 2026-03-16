"""
=============================================================
  TENNIS AUTO v2 - Système complet ATP + WTA
=============================================================

NOUVEAUTÉS v2 :
  - WTA support (tennis_wta_* tournois)
  - Comparaison Betclic / Unibet / Winamax simultanément
  - Meilleure cote automatiquement sélectionnée
  - Alerte email quand EV > SEUIL_ALERTE_EMAIL
  - Rang ATP/WTA affiché pour chaque joueur
  - Avertissement joueurs absents / potentiellement blessés
  - Rapport enrichi avec stats de service

SETUP (une seule fois) :
    1. pip install -r requirements.txt
    2. Configure ta clé The Odds API ci-dessous
    3. Configure ton email ci-dessous (optionnel)
    4. python tennis_auto.py --setup  (planificateur Windows ou instructions cron Linux)

UTILISATION :
    python tennis_auto.py            -> pipeline complet + analyse
    python tennis_auto.py --analyse  -> analyse seule (rapide)
    python tennis_auto.py --setup    -> planificateur (Windows) ou cron (Linux/PythonAnywhere)

PYTHONANYWHERE :
    Déploiement : uploader tous les fichiers dans ~/tennis-betting/
    Cron (onglet "Tasks") : python3.11 ~/tennis-betting/tennis_auto.py --analyse
    Horaires suggérés : 08:00 / 13:00 / 19:00 (analyse) + lundi 07:00 (pipeline)
"""

import subprocess, sys, os, pickle, argparse, smtplib, json, math
import requests, pandas as pd, numpy as np
from datetime import datetime, timezone, timedelta
from difflib import get_close_matches
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────────────────────────
# CONFIGURATION — à modifier avant utilisation
# ─────────────────────────────────────────────────────────────

# Secrets : lus depuis l'environnement si présents (GitHub Actions),
# sinon valeurs locales en fallback.
ODDS_API_KEY   = os.environ.get("ODDS_API_KEY",    "71fdf5d253189e041ca3f314458bffe6")
BANKROLL       = 100      # bankroll en euros
SEUIL_VALUE    = 0.05     # EV minimum pour recommander un pari (+5%)
SEUIL_ALERTE   = 0.10     # EV minimum pour alerte email (+10%)
FRACTION_KELLY = 0.5      # demi-Kelly
MISE_MIN       = 5.0      # mise minimum en €
MISE_MAX       = 50.0     # mise maximum en €

# Bookmakers à comparer en priorité (dans l'ordre de préférence)
BOOKMAKERS_CIBLES = [
    "betclic_fr", "unibet_fr", "winamax_fr",
    "pmu_fr", "parionssport", "bwin_fr",
    "zebet", "netbet_fr", "vbet_fr", "france_pari",
]

# ── Email (laisser vide pour désactiver) ──────────────────────
EMAIL_ACTIF    = True             # True pour activer
EMAIL_FROM     = "fabien29830@gmail.com"
EMAIL_TO       = "fabien29830@gmail.com"
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = "fabien29830@gmail.com"
SMTP_PASS      = os.environ.get("EMAIL_PASSWORD",  "cngg gfew wthp baiw")
# Pour Gmail : active "Mots de passe d'application" dans Sécurité Google
# https://myaccount.google.com/apppasswords

# ── Notifications push (ntfy.sh) ─────────────────────────────
# Créer un topic unique sur https://ntfy.sh  (ex: tennis-fabien-abc123)
# Puis installer l'app ntfy sur iPhone et s'abonner au topic.
# Mettre le nom du topic dans le secret GitHub NTFY_TOPIC.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")   # laisser vide pour désactiver

# ── Google Sheets ─────────────────────────────────────────────
GSHEET_ACTIF      = True
GSHEET_CREDENTIALS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tennis_data", "gsheet_credentials.json"
)
GSHEET_CONFIG     = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tennis_data", "gsheet_config.json"
)

# ─────────────────────────────────────────────────────────────
# CHEMINS
# ─────────────────────────────────────────────────────────────

DOSSIER      = os.path.dirname(os.path.abspath(__file__))
DOSSIER_DATA = os.path.join(DOSSIER, "tennis_data")
RAPPORT_DIR  = os.path.join(DOSSIER_DATA, "rapports")
os.makedirs(RAPPORT_DIR, exist_ok=True)

CACHE_COTES_FILE = os.path.join(DOSSIER_DATA, "cache_cotes.json")
CACHE_COTES_TTL  = 90   # minutes avant expiration du cache

# ─────────────────────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────────────────────

def run_script(label, script):
    print(f"\n  >> {label}...")
    # -X utf8 : uniquement sur Windows (Linux utilise UTF-8 par défaut)
    cmd = [sys.executable]
    if sys.platform == "win32":
        cmd += ["-X", "utf8"]
    cmd.append(os.path.join(DOSSIER, script))
    r = subprocess.run(cmd, cwd=DOSSIER)
    ok = r.returncode == 0
    print(f"     {'OK' if ok else 'ERREUR (code ' + str(r.returncode) + ')'}")
    return ok


def normaliser(nom):
    import unicodedata
    n = unicodedata.normalize("NFD", nom)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return n.lower().strip()


# ─────────────────────────────────────────────────────────────
# CACHE COTES (The Odds API)
# ─────────────────────────────────────────────────────────────

def _charger_cache_cotes():
    """
    Retourne (events, restantes, age_min) si le cache est frais
    (< CACHE_COTES_TTL min ET même jour UTC), sinon (None, None, None).
    """
    if not os.path.exists(CACHE_COTES_FILE):
        return None, None, None
    try:
        with open(CACHE_COTES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ts      = datetime.fromisoformat(data["timestamp"])
        age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        # Cache valide uniquement si < TTL et même jour calendaire
        if (age_min < CACHE_COTES_TTL
                and ts.date() == datetime.now(timezone.utc).date()):
            return data["events"], data.get("restantes", "?"), int(age_min)
    except Exception:
        pass
    return None, None, None


def _sauvegarder_cache_cotes(events, restantes):
    """Persiste les cotes et le compteur restant dans le cache JSON."""
    try:
        with open(CACHE_COTES_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "restantes": str(restantes),
                "events":    events,
            }, f)
    except Exception:
        pass


def trouver_joueur(nom_api, noms_connus_norm):
    n = normaliser(nom_api)
    if n in noms_connus_norm:
        return noms_connus_norm[n]
    # Fuzzy sur nom complet
    matches = get_close_matches(n, noms_connus_norm.keys(), n=1, cutoff=0.75)
    if matches:
        return noms_connus_norm[matches[0]]
    # Fallback WTA : API donne "Aryna Sabalenka", notre DB a "A. Sabalenka"
    # Tenter la clé "initiale. nom_famille" (format WTA tennis-data.co.uk)
    parties = n.split()
    if len(parties) >= 2:
        nom_famille = parties[-1]            # "sabalenka"
        initiale    = parties[0][0]          # "a"
        cle_wta     = f"{initiale}. {nom_famille}"   # "a. sabalenka"
        if cle_wta in noms_connus_norm:
            return noms_connus_norm[cle_wta]
        # Fuzzy sur la clé WTA
        matches2 = get_close_matches(cle_wta, noms_connus_norm.keys(), n=1, cutoff=0.7)
        if matches2:
            return noms_connus_norm[matches2[0]]
        # Dernier recours : nom de famille seul (non ambigu)
        candidats = [k for k in noms_connus_norm if k.endswith(nom_famille)]
        if len(candidats) == 1:
            return noms_connus_norm[candidats[0]]
    return None


def charger_modele():
    chemins = {
        "modele": os.path.join(DOSSIER_DATA, "modele_tennis.pkl"),
        "scaler": os.path.join(DOSSIER_DATA, "scaler.pkl"),
        "cols":   os.path.join(DOSSIER_DATA, "colonnes.pkl"),
    }
    for k, p in chemins.items():
        if not os.path.exists(p):
            print(f"  Fichier manquant : {p}")
            return None, None, None
    with open(chemins["modele"], "rb") as f: modele = pickle.load(f)
    with open(chemins["scaler"], "rb") as f: scaler = pickle.load(f)
    with open(chemins["cols"],   "rb") as f: cols   = pickle.load(f)
    return modele, scaler, cols


def charger_elo(circuit="atp"):
    """Charge elo_joueurs.csv (ATP) ou wta_elo_joueurs.csv (WTA)."""
    nom_fichier = "elo_joueurs.csv" if circuit == "atp" else "wta_elo_joueurs.csv"
    p = os.path.join(DOSSIER_DATA, nom_fichier)
    if os.path.exists(p):
        df = pd.read_csv(p).set_index("joueur")
        return df.to_dict(orient="index")

    # Fallback depuis atp_elo.csv
    p2 = os.path.join(DOSSIER_DATA, "atp_elo.csv")
    if not os.path.exists(p2):
        return {}
    print("  [fallback] Dérivation Elo depuis atp_elo.csv...")
    df = pd.read_csv(p2, parse_dates=["date"]).sort_values("date")
    elo = {}
    for _, row in df.iterrows():
        surf = row["surface"]
        for who, elo_g, elo_s in [
            (row["winner"], row["elo_w_global"], row["elo_w_surface"]),
            (row["loser"],  row["elo_l_global"], row["elo_l_surface"]),
        ]:
            if who not in elo:
                elo[who] = {"elo_global": elo_g, "elo_dur": elo_g,
                             "elo_terre": elo_g, "elo_gazon": elo_g}
            elo[who]["elo_global"]  = elo_g
            elo[who][f"elo_{surf}"] = elo_s
    return elo


def charger_stats():
    p = os.path.join(DOSSIER_DATA, "stats_joueurs.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p).set_index("joueur")
    return df.to_dict(orient="index")


def charger_rankings():
    """Charge les rankings ATP et WTA.
    Retourne un dict {joueur: rang} avec double index :
      - nom complet exact   → rang
      - __ln__nom_famille   → rang  (fallback pour noms abrégés WTA)
    """
    ranks = {}
    for fichier in ["rankings_atp.csv", "rankings_wta.csv"]:
        p = os.path.join(DOSSIER_DATA, fichier)
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p)
        if "joueur" not in df.columns or "rang" not in df.columns:
            continue
        # Garder le rang le plus récent par joueur
        if "date" in df.columns:
            df = df.sort_values("date").groupby("joueur").last().reset_index()
        for _, row in df.iterrows():
            nom  = str(row["joueur"])
            rang = int(row["rang"])
            ranks[nom] = rang
            # Index secondaire par nom de famille (gère "A. Sabalenka" → "sabalenka")
            parts = nom.split()
            if parts:
                ranks[f"__ln__{parts[-1].lower()}"] = rang
    return ranks


def _get_rang(nom, ranks, rangs_elo=None):
    """Retourne le rang d'un joueur.
    Priorité : exact → nom de famille → pseudo-rang Elo → '?'
    """
    if not nom:
        return "?"
    # 1. Correspondance exacte
    r = ranks.get(nom)
    if r is not None:
        return r
    # 2. Par nom de famille ("Sabalenka" → clé __ln__sabalenka)
    parts = nom.split()
    if parts:
        r = ranks.get(f"__ln__{parts[-1].lower()}")
        if r is not None:
            return r
    # 3. Fallback Elo : pseudo-rang basé sur classement Elo
    if rangs_elo:
        r = rangs_elo.get(nom)
        if r is not None:
            return f"Elo~{r}"
    return "?"


def charger_rangs_elo(elo_dict):
    """Crée un pseudo-classement {joueur: rang} par ordre Elo décroissant."""
    sorted_players = sorted(
        elo_dict.items(), key=lambda x: -x[1].get("elo_global", 0)
    )
    return {nom: i + 1 for i, (nom, _) in enumerate(sorted_players)}


def charger_forme_recente(n=5):
    """Win rate sur les n derniers matchs pour chaque joueur.
    Retourne {joueur: win_rate}  (0.0–1.0).
    """
    dfs = []
    for f in ["atp_matchs_bruts.csv", "wta_matchs_bruts.csv"]:
        p = os.path.join(DOSSIER_DATA, f)
        if os.path.exists(p):
            try:
                dfs.append(pd.read_csv(
                    p, usecols=["winner_name", "loser_name", "date"],
                    parse_dates=["date"]
                ))
            except Exception:
                pass
    if not dfs:
        return {}
    df = pd.concat(dfs, ignore_index=True).sort_values("date")
    # Transformer en format long : (joueur, date, win)
    wins_df   = df[["winner_name", "date"]].rename(
        columns={"winner_name": "joueur"}).assign(win=1)
    losses_df = df[["loser_name",  "date"]].rename(
        columns={"loser_name":  "joueur"}).assign(win=0)
    long_df = pd.concat([wins_df, losses_df], ignore_index=True) \
                .sort_values(["joueur", "date"])
    forme = {}
    for joueur, grp in long_df.groupby("joueur"):
        last_n = grp.tail(n)
        if len(last_n) >= 2:
            forme[joueur] = round(float(last_n["win"].mean()), 3)
    return forme


def charger_h2h():
    """Head-to-head depuis l'historique des matchs.
    Retourne {(j1, j2): (victoires_j1, total_matchs)}.
    """
    dfs = []
    for f in ["atp_matchs_bruts.csv", "wta_matchs_bruts.csv"]:
        p = os.path.join(DOSSIER_DATA, f)
        if os.path.exists(p):
            try:
                dfs.append(pd.read_csv(p, usecols=["winner_name", "loser_name"]))
            except Exception:
                pass
    if not dfs:
        return {}
    df = pd.concat(dfs, ignore_index=True).dropna(
        subset=["winner_name", "loser_name"])
    # Compter (gagnant → perdant)
    counts = df.groupby(["winner_name", "loser_name"]).size()
    # Construire toutes les paires sans doublon
    all_pairs = set()
    for w, l in counts.index:
        all_pairs.add(tuple(sorted([w, l])))
    result = {}
    for a, b in all_pairs:
        wa = int(counts.get((a, b), 0))
        wb = int(counts.get((b, a), 0))
        total = wa + wb
        if total > 0:
            result[(a, b)] = (wa, total)
            result[(b, a)] = (wb, total)
    return result


def charger_absents():
    """Retourne un set de noms de joueurs absents."""
    p = os.path.join(DOSSIER_DATA, "absents.csv")
    if not os.path.exists(p):
        return set()
    df = pd.read_csv(p)
    if "joueur" in df.columns:
        return set(df["joueur"].tolist())
    return set()


# ─────────────────────────────────────────────────────────────
# PRÉDICTION
# ─────────────────────────────────────────────────────────────

ELO_INCONNU = 1600

def predire(modele, scaler, features, elo_j1, elo_j2, surface,
            stats_j1=None, stats_j2=None, fatigue_j1=3, fatigue_j2=3, h2h=0.5):
    def pv(a, b): return 1 / (1 + 10 ** ((b - a) / 400))
    col_surf = f"elo_{surface}"
    eg1 = elo_j1.get("elo_global", ELO_INCONNU)
    eg2 = elo_j2.get("elo_global", ELO_INCONNU)
    es1 = elo_j1.get(col_surf, eg1)
    es2 = elo_j2.get(col_surf, eg2)
    prob_g = pv(eg1, eg2)
    prob_s = pv(es1, es2)
    prob_c = 0.6 * prob_s + 0.4 * prob_g
    data = {
        "diff_elo_global":   eg1 - eg2,
        "diff_elo_surface":  es1 - es2,
        "prob_global":       prob_g,
        "prob_surface":      prob_s,
        "prob_combinee":     prob_c,
        "diff_fatigue":      fatigue_j2 - fatigue_j1,
        "h2h_winner":        h2h,
        "diff_1st_in_pct":   0.0,
        "diff_1st_won_pct":  0.0,
        "diff_2nd_won_pct":  0.0,
        "diff_bp_saved_pct": 0.0,
        "diff_ace_rate":     0.0,
    }
    if stats_j1 and stats_j2:
        for k in ["1st_in_pct","1st_won_pct","2nd_won_pct","bp_saved_pct","ace_rate"]:
            v1 = stats_j1.get(k, np.nan)
            v2 = stats_j2.get(k, np.nan)
            if not (pd.isna(v1) or pd.isna(v2)):
                data[f"diff_{k}"] = v1 - v2
    row = pd.DataFrame([{f: data.get(f, 0.0) for f in features}])
    prob = modele.predict_proba(scaler.transform(row))[0][1]
    return prob, eg1, eg2, es1, es2


# ─────────────────────────────────────────────────────────────
# MODÈLES PROBABILISTES — marchés secondaires
# ─────────────────────────────────────────────────────────────

def _normal_cdf(x, mu, sigma):
    """CDF normale approchée via math.erf (pas de scipy requis)."""
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def p_set_from_p_match(p_match):
    """
    Résout p_set tel que 3·p_set² − 2·p_set³ = p_match (best-of-3).
    Retourne la probabilité de gagner un set.
    """
    p_match = max(0.001, min(0.999, p_match))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        val = 3 * mid**2 - 2 * mid**3
        if val < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prob_nombre_sets(p_match):
    """Retourne (P_2sets, P_3sets) pour le vainqueur du match."""
    ps = p_set_from_p_match(p_match)
    p20 = ps ** 2
    p02 = (1 - ps) ** 2
    p21 = 2 * ps**2 * (1 - ps)
    p12 = 2 * ps * (1 - ps) ** 2
    return p20 + p02, p21 + p12   # (P_2sets, P_3sets)


def prob_total_jeux(p_match, line, over=True):
    """
    P(total jeux > line) ou P(total jeux < line) — approximation gaussienne.
    Calibration :
      match en 2 sets  → ~20.5 jeux (std 2.5)
      match en 3 sets  → ~30.5 jeux (std 3.0)
    """
    p2, p3 = prob_nombre_sets(p_match)
    p_over_2 = 1.0 - _normal_cdf(line, 20.5, 2.5)
    p_over_3 = 1.0 - _normal_cdf(line, 30.5, 3.0)
    p_over   = p2 * p_over_2 + p3 * p_over_3
    return p_over if over else 1.0 - p_over


def prob_handicap_jeux(p_match, handicap):
    """
    P(joueur couvre son handicap de jeux).
    handicap < 0 → joueur doit gagner de plus de |handicap| jeux.
    handicap > 0 → joueur peut perdre jusqu'à handicap jeux.
    Calibration : marge attendue ≈ 20·(p_match − 0.5), std ≈ 5 jeux.
    """
    expected_margin = 20.0 * (p_match - 0.5)
    std_margin = 5.0
    threshold = -handicap   # ex: handicap=-3.5 → besoin margin > 3.5
    return 1.0 - _normal_cdf(threshold, expected_margin, std_margin)


def extraire_cotes_marche(event, market_key, bookmakers_cibles):
    """
    Pour un marché donné (ex: 'spreads', 'totals'), retourne la liste de
    (outcome_name, meilleure_cote, meilleur_bookmaker, point)
    en privilégiant les bookmakers cibles.
    """
    # Agrège par outcome_name → {bm_key: (price, point)}
    outcomes_agg = {}
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for oc in mkt.get("outcomes", []):
                name  = oc.get("name", "")
                price = oc.get("price", 0.0)
                point = oc.get("point", None)
                if price <= 1.0:
                    continue
                if name not in outcomes_agg:
                    outcomes_agg[name] = {}
                outcomes_agg[name][bm["key"]] = (price, point)

    results = []
    for name, bm_dict in outcomes_agg.items():
        if bookmakers_cibles:
            # Filtrer sur les bookmakers cibles ; ignorer si aucun disponible
            src = {bm: v for bm, v in bm_dict.items()
                   if any(bm.startswith(c) or c in bm for c in bookmakers_cibles)}
            if not src:
                continue
        else:
            # Pas de filtre : meilleure cote tous bookmakers
            src = bm_dict
        best_bm = max(src, key=lambda b: src[b][0])
        best_price, point = src[best_bm]
        results.append((name, best_price, best_bm, point))
    return results


# ─────────────────────────────────────────────────────────────
# ODDS API — comparaison multi-bookmakers
# ─────────────────────────────────────────────────────────────

def recuperer_matchs_et_cotes():
    """
    Récupère ATP + WTA pour aujourd'hui + demain.
    Stratégie en 2 passes par tournoi :
      1. /events  → liste complète de TOUS les matchs (sans cotes)
      2. /odds    → enrichit avec les cotes bookmakers
    Les matchs sans cotes sont conservés (affichés sans EV).

    Cache : si le dernier appel date de moins de CACHE_COTES_TTL minutes
    (et du même jour), retourne les données en cache sans appeler l'API.
    """
    if ODDS_API_KEY == "REMPLACE_PAR_TA_CLE":
        print("\n  ATTENTION : ODDS_API_KEY non configuré.")
        return []

    # ── Vérification du cache ─────────────────────────────────
    events_cache, restantes_cache, age_min = _charger_cache_cotes()
    if events_cache is not None:
        nb_avec_cotes = sum(1 for e in events_cache if e.get("bookmakers"))
        print(f"\n  Cotes en cache ({age_min} min) — appel API économisé")
        print(f"  {len(events_cache)} matchs en cache "
              f"({nb_avec_cotes} avec cotes) "
              f"| Requêtes restantes : {restantes_cache}")
        return events_cache

    # Fenêtre : aujourd'hui 00h00 UTC → après-demain 00h00 UTC
    maintenant = datetime.now(timezone.utc)
    debut_utc  = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_utc    = debut_utc + timedelta(days=2)
    from_iso   = debut_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso     = fin_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    print("\n  Récupération des matchs via The Odds API...")
    print(f"  Fenêtre : {from_iso} → {to_iso}")
    base        = "https://api.the-odds-api.com/v4"
    params_base = {"apiKey": ODDS_API_KEY}

    try:
        # ── 1. Sports tennis actifs ───────────────────────────────
        r = requests.get(f"{base}/sports/", params=params_base, timeout=15)
        r.raise_for_status()
        sports_tennis = [
            s["key"] for s in r.json()
            if s.get("active") and (
                "tennis_atp" in s["key"] or "tennis_wta" in s["key"]
            )
        ]
        print(f"  Tournois actifs : {sports_tennis}")
        if not sports_tennis:
            print("  Aucun tournoi tennis actif détecté.")
            return []

        tous_events = []
        restantes   = "?"

        for sport_key in sports_tennis:
            circuit = "wta" if "wta" in sport_key else "atp"

            # ── Passe 1 : tous les matchs (sans cotes) ────────────
            events_par_id = {}
            try:
                r_ev = requests.get(
                    f"{base}/sports/{sport_key}/events/",
                    params={**params_base, "dateFormat": "iso",
                            "commenceTimeFrom": from_iso,
                            "commenceTimeTo":   to_iso},
                    timeout=15,
                )
                if r_ev.status_code == 200:
                    for e in r_ev.json():
                        e["_tournament"] = sport_key
                        e["_circuit"]    = circuit
                        e["bookmakers"]  = []   # sera rempli en passe 2
                        events_par_id[e["id"]] = e
            except Exception:
                pass  # /events optionnel, on continue quand même

            # ── Passe 2 : cotes (enrichit les events déjà connus) ─
            try:
                r_odds = requests.get(
                    f"{base}/sports/{sport_key}/odds/",
                    params={**params_base,
                            "regions": "eu",
                            "markets": "h2h,spreads,totals",
                            "oddsFormat": "decimal", "dateFormat": "iso",
                            "commenceTimeFrom": from_iso,
                            "commenceTimeTo":   to_iso},
                    timeout=15,
                )
                if r_odds.status_code == 200:
                    restantes = r_odds.headers.get("x-requests-remaining", "?")
                    for e in r_odds.json():
                        eid = e["id"]
                        if eid in events_par_id:
                            # Enrichir l'event déjà connu avec ses cotes
                            events_par_id[eid]["bookmakers"] = e.get("bookmakers", [])
                        else:
                            # Match présent dans /odds mais absent de /events
                            e["_tournament"] = sport_key
                            e["_circuit"]    = circuit
                            events_par_id[eid] = e
            except Exception as err:
                print(f"  Erreur /odds {sport_key} : {err}")

            nb = len(events_par_id)
            nb_avec_cotes = sum(1 for e in events_par_id.values() if e.get("bookmakers"))
            print(f"  {sport_key:<30} {nb} matchs "
                  f"({nb_avec_cotes} avec cotes, "
                  f"{nb - nb_avec_cotes} sans cotes)")

            tous_events.extend(events_par_id.values())

        print(f"  Total : {len(tous_events)} matchs | Requêtes restantes : {restantes}")
        _sauvegarder_cache_cotes(tous_events, restantes)
        return tous_events

    except Exception as e:
        print(f"  Erreur API : {e}")
        return []


def cotes_par_bookmaker(event, idx):
    """
    Retourne un dict {bookmaker_key: cote} pour le joueur idx.
    Met en évidence les bookmakers cibles (Betclic/Unibet/Winamax).
    """
    cotes = {}
    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] == "h2h":
                outcomes = mkt.get("outcomes", [])
                if idx < len(outcomes):
                    cotes[bm["key"]] = outcomes[idx]["price"]
    return cotes


def meilleure_cote(event, idx, bookmakers_prioritaires=None):
    """
    Retourne (meilleure_cote, nom_bookmaker).
    Si bookmakers_prioritaires est défini, cherche d'abord parmi eux.
    Sinon, prend la meilleure cote globale.
    """
    cotes = cotes_par_bookmaker(event, idx)
    if not cotes:
        return 0.0, "?"

    # Chercher uniquement parmi les bookmakers cibles
    if bookmakers_prioritaires:
        cotes_cibles = {bm: c for bm, c in cotes.items()
                        if any(bm.startswith(bp) or bp in bm
                               for bp in bookmakers_prioritaires)}
        if cotes_cibles:
            best_bm = max(cotes_cibles, key=cotes_cibles.get)
            return cotes_cibles[best_bm], best_bm
        return 0.0, "?"  # aucun bookmaker cible disponible

    # Sans filtre : meilleure cote globale
    best_bm = max(cotes, key=cotes.get)
    return cotes[best_bm], best_bm


def tableau_cotes(event, nom1, nom2):
    """
    Génère les lignes du tableau de comparaison des bookmakers ANJ.
    """
    lignes = []
    cotes0 = cotes_par_bookmaker(event, 0)
    cotes1 = cotes_par_bookmaker(event, 1)

    labels = {
        "betclic_fr":   "Betclic    ",
        "unibet_fr":    "Unibet     ",
        "winamax_fr":   "Winamax    ",
        "pmu_fr":       "PMU        ",
        "parionssport": "ParionsSport",
        "bwin_fr":      "Bwin       ",
        "zebet":        "ZEbet      ",
        "netbet_fr":    "NetBet     ",
        "vbet_fr":      "VBet       ",
        "france_pari":  "France Pari",
    }

    for bm_key, bm_label in labels.items():
        c0 = cotes0.get(bm_key, "-")
        c1 = cotes1.get(bm_key, "-")
        c0_s = f"{c0:.2f}" if isinstance(c0, float) else c0
        c1_s = f"{c1:.2f}" if isinstance(c1, float) else c1
        lignes.append(f"  {bm_label}       {c0_s:<8} {c1_s}")

    # Meilleure cote parmi les bookmakers cibles uniquement
    cotes0_fr = {bm: v for bm, v in cotes0.items() if bm in BOOKMAKERS_CIBLES}
    cotes1_fr = {bm: v for bm, v in cotes1.items() if bm in BOOKMAKERS_CIBLES}
    if cotes0_fr and cotes1_fr:
        best0 = max(cotes0_fr.values())
        best1 = max(cotes1_fr.values())
        bm0   = max(cotes0_fr, key=cotes0_fr.get)
        bm1   = max(cotes1_fr, key=cotes1_fr.get)
        lignes.append(f"  Meilleure cote   {best0:.2f} ({bm0:<10}) {best1:.2f} ({bm1})")
    return lignes


# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────

def lire_bankroll_gsheet(sh):
    """
    Lit la bankroll actuelle depuis 'Tableau de bord'!B7
    (=D3+SUM(Paris!N2:N) — bankroll initiale + P&L réalisé).
    Retourne la valeur float, ou None si non disponible.
    """
    if not sh:
        return None
    try:
        ws = sh.worksheet("Tableau de bord")
        val = ws.acell("B7").value
        if val is None:
            return None
        return float(str(val).replace(" ", "").replace(",", ".").replace("€", "").strip())
    except Exception:
        return None


def mettre_a_jour_tableau_bord(sh):
    """
    Réécrit les formules des onglets 'Tableau de bord' et 'Analyse Modèle'
    en référençant correctement l'onglet Paris (layout actuel A–O) :

      A:Date  B:Tournoi  C:Circuit  D:Surface  E:Match
      F:Joueur parié  G:Adversaire  H:Bookmaker
      I:Cote  J:Mise (€)  K:EV Modèle (%)  L:Prob. Modèle (%)
      M:Résultat  N:Gain/Perte (€)  O:Bankroll (€)

    D3 = bankroll initiale (NE PAS TOUCHER — référencée par les formules Paris!O).
    D6 = bankroll actuelle (INDEX/MATCH sur Paris!O).

    Appelé à chaque --analyse et --resultats pour maintenir les deux onglets
    à jour sans intervention manuelle.
    """
    if not sh:
        return

    ts = datetime.now().strftime("%d/%m/%Y %H:%M")

    # ── Onglet "Tableau de bord" (layout propre + formatage) ────────────
    try:
        # 1. Supprime et recrée l'onglet (élimine toutes les fusions et formatages)
        all_sheets = sh.worksheets()
        bord_idx = next((i for i, w in enumerate(all_sheets) if w.title == "Tableau de bord"), None)
        if bord_idx is not None:
            sh.del_worksheet(all_sheets[bord_idx])
        ws_bord = sh.add_worksheet(title="Tableau de bord", rows=50, cols=6)
        # Restaure la position de l'onglet
        if bord_idx is not None:
            all_sheets_new = sh.worksheets()
            ordered = [w for w in all_sheets_new if w.title != "Tableau de bord"]
            ordered.insert(bord_idx, ws_bord)
            sh.reorder_worksheets(ordered)

        # 2. Écriture des valeurs et formules
        # D3 = bankroll initiale — NE JAMAIS DÉPLACER (référencée par Paris!O formulas)
        ws_bord.batch_update([
            {"range": "D3",  "values": [[100]]},  # bankroll initiale (config cachée)
            # ── Titre ─────────────────────────────────────────────────
            {"range": "A1",  "values": [["TABLEAU DE BORD — Tennis Value Betting"]]},
            {"range": "A2",  "values": [["Mis à jour :"]]},
            {"range": "B2",  "values": [[ts]]},
            # ── PERFORMANCE GLOBALE ────────────────────────────────────
            {"range": "A5",  "values": [["PERFORMANCE GLOBALE"]]},
            {"range": "A6",  "values": [["Bankroll initiale"]]},
            {"range": "B6",  "values": [["=D3"]]},
            {"range": "A7",  "values": [["Bankroll actuelle"]]},
            {"range": "B7",  "values": [['=D3+IFERROR(SUM(Paris!N2:N);0)']]},
            {"range": "A8",  "values": [["P&L total"]]},
            {"range": "B8",  "values": [['=IFERROR(SUM(Paris!N2:N);"-")']]},
            {"range": "A9",  "values": [["ROI"]]},
            {"range": "B9",  "values": [['=IFERROR(SUM(Paris!N2:N)/SUM(Paris!J2:J);"-")']]},
            {"range": "A10", "values": [["Taux de réussite"]]},
            {"range": "B10", "values": [['=IFERROR(COUNTIF(Paris!M2:M;"Gagné")/(COUNTIF(Paris!M2:M;"Gagné")+COUNTIF(Paris!M2:M;"Perdu"));"-")']]},
            # ── STATISTIQUES ────────────────────────────────────────────
            {"range": "A12", "values": [["STATISTIQUES"]]},
            {"range": "A13", "values": [["Total paris"]]},
            {"range": "B13", "values": [['=COUNTIF(Paris!M2:M;"Gagné")+COUNTIF(Paris!M2:M;"Perdu")+COUNTIF(Paris!M2:M;"En cours")']]},
            {"range": "A14", "values": [["Gagnés"]]},
            {"range": "B14", "values": [['=COUNTIF(Paris!M2:M;"Gagné")']]},
            {"range": "A15", "values": [["Perdus"]]},
            {"range": "B15", "values": [['=COUNTIF(Paris!M2:M;"Perdu")']]},
            {"range": "A16", "values": [["En cours"]]},
            {"range": "B16", "values": [['=COUNTIF(Paris!M2:M;"En cours")']]},
            {"range": "A17", "values": [["Total misé"]]},
            {"range": "B17", "values": [['=IFERROR(SUM(Paris!J2:J);"-")']]},
            {"range": "A18", "values": [["Mise moyenne"]]},
            {"range": "B18", "values": [['=IFERROR(AVERAGE(Paris!J2:J);"-")']]},
            # ── PAR SURFACE ─────────────────────────────────────────────
            {"range": "A20",    "values": [["PAR SURFACE"]]},
            {"range": "A21:D21","values": [["Surface", "Paris", "Taux", "ROI"]]},
            {"range": "A22", "values": [["Dur"]]},
            {"range": "B22", "values": [['=COUNTIFS(Paris!D2:D;"dur";Paris!M2:M;"<>En cours")']]},
            {"range": "C22", "values": [['=IFERROR(COUNTIFS(Paris!D2:D;"dur";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!D2:D;"dur";Paris!M2:M;"Gagné")+COUNTIFS(Paris!D2:D;"dur";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D22", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!D2:D;"dur")/SUMIFS(Paris!J2:J;Paris!D2:D;"dur");"-")']]},
            {"range": "A23", "values": [["Terre"]]},
            {"range": "B23", "values": [['=COUNTIFS(Paris!D2:D;"terre";Paris!M2:M;"<>En cours")']]},
            {"range": "C23", "values": [['=IFERROR(COUNTIFS(Paris!D2:D;"terre";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!D2:D;"terre";Paris!M2:M;"Gagné")+COUNTIFS(Paris!D2:D;"terre";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D23", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!D2:D;"terre")/SUMIFS(Paris!J2:J;Paris!D2:D;"terre");"-")']]},
            {"range": "A24", "values": [["Gazon"]]},
            {"range": "B24", "values": [['=COUNTIFS(Paris!D2:D;"gazon";Paris!M2:M;"<>En cours")']]},
            {"range": "C24", "values": [['=IFERROR(COUNTIFS(Paris!D2:D;"gazon";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!D2:D;"gazon";Paris!M2:M;"Gagné")+COUNTIFS(Paris!D2:D;"gazon";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D24", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!D2:D;"gazon")/SUMIFS(Paris!J2:J;Paris!D2:D;"gazon");"-")']]},
            # ── PAR BOOKMAKER ────────────────────────────────────────────
            {"range": "A26",    "values": [["PAR BOOKMAKER"]]},
            {"range": "A27:D27","values": [["Bookmaker", "Paris", "Taux", "ROI"]]},
            {"range": "A28", "values": [["Betclic"]]},
            {"range": "B28", "values": [['=COUNTIF(Paris!H2:H;"betclic_fr")']]},
            {"range": "C28", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"betclic_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"betclic_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"betclic_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D28", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"betclic_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"betclic_fr");"-")']]},
            {"range": "A29", "values": [["Unibet"]]},
            {"range": "B29", "values": [['=COUNTIF(Paris!H2:H;"unibet_fr")']]},
            {"range": "C29", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"unibet_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"unibet_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"unibet_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D29", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"unibet_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"unibet_fr");"-")']]},
            {"range": "A30", "values": [["Winamax"]]},
            {"range": "B30", "values": [['=COUNTIF(Paris!H2:H;"winamax_fr")']]},
            {"range": "C30", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"winamax_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"winamax_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"winamax_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D30", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"winamax_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"winamax_fr");"-")']]},
            {"range": "A31", "values": [["PMU"]]},
            {"range": "B31", "values": [['=COUNTIF(Paris!H2:H;"pmu_fr")']]},
            {"range": "C31", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"pmu_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"pmu_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"pmu_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D31", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"pmu_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"pmu_fr");"-")']]},
            {"range": "A32", "values": [["ParionsSport"]]},
            {"range": "B32", "values": [['=COUNTIF(Paris!H2:H;"parionssport")']]},
            {"range": "C32", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"parionssport";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"parionssport";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"parionssport";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D32", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"parionssport")/SUMIFS(Paris!J2:J;Paris!H2:H;"parionssport");"-")']]},
            {"range": "A33", "values": [["Bwin"]]},
            {"range": "B33", "values": [['=COUNTIF(Paris!H2:H;"bwin_fr")']]},
            {"range": "C33", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"bwin_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"bwin_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"bwin_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D33", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"bwin_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"bwin_fr");"-")']]},
            {"range": "A34", "values": [["ZEbet"]]},
            {"range": "B34", "values": [['=COUNTIF(Paris!H2:H;"zebet")']]},
            {"range": "C34", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"zebet";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"zebet";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"zebet";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D34", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"zebet")/SUMIFS(Paris!J2:J;Paris!H2:H;"zebet");"-")']]},
            {"range": "A35", "values": [["NetBet"]]},
            {"range": "B35", "values": [['=COUNTIF(Paris!H2:H;"netbet_fr")']]},
            {"range": "C35", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"netbet_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"netbet_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"netbet_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D35", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"netbet_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"netbet_fr");"-")']]},
            {"range": "A36", "values": [["VBet"]]},
            {"range": "B36", "values": [['=COUNTIF(Paris!H2:H;"vbet_fr")']]},
            {"range": "C36", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"vbet_fr";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"vbet_fr";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"vbet_fr";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D36", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"vbet_fr")/SUMIFS(Paris!J2:J;Paris!H2:H;"vbet_fr");"-")']]},
            {"range": "A37", "values": [["France Pari"]]},
            {"range": "B37", "values": [['=COUNTIF(Paris!H2:H;"france_pari")']]},
            {"range": "C37", "values": [['=IFERROR(COUNTIFS(Paris!H2:H;"france_pari";Paris!M2:M;"Gagné")/(COUNTIFS(Paris!H2:H;"france_pari";Paris!M2:M;"Gagné")+COUNTIFS(Paris!H2:H;"france_pari";Paris!M2:M;"Perdu"));"-")']]},
            {"range": "D37", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!H2:H;"france_pari")/SUMIFS(Paris!J2:J;Paris!H2:H;"france_pari");"-")']]},
            # ── PAR TRANCHE EV ───────────────────────────────────────────
            {"range": "A39",    "values": [["PAR TRANCHE EV"]]},
            {"range": "A40:D40","values": [["Tranche", "Paris", "Taux", "ROI"]]},
            {"range": "A41", "values": [["EV 5–10 %"]]},
            {"range": "B41", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!K2:K;"<10";Paris!M2:M;"<>En cours")']]},
            {"range": "C41", "values": [['=IFERROR(COUNTIFS(Paris!K2:K;">=5";Paris!K2:K;"<10";Paris!M2:M;"Gagné")/B41;"-")']]},
            {"range": "D41", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=5";Paris!K2:K;"<10")/SUMIFS(Paris!J2:J;Paris!K2:K;">=5";Paris!K2:K;"<10");"-")']]},
            {"range": "A42", "values": [["EV 10–15 %"]]},
            {"range": "B42", "values": [['=COUNTIFS(Paris!K2:K;">=10";Paris!K2:K;"<15";Paris!M2:M;"<>En cours")']]},
            {"range": "C42", "values": [['=IFERROR(COUNTIFS(Paris!K2:K;">=10";Paris!K2:K;"<15";Paris!M2:M;"Gagné")/B42;"-")']]},
            {"range": "D42", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=10";Paris!K2:K;"<15")/SUMIFS(Paris!J2:J;Paris!K2:K;">=10";Paris!K2:K;"<15");"-")']]},
            {"range": "A43", "values": [["EV > 15 %"]]},
            {"range": "B43", "values": [['=COUNTIFS(Paris!K2:K;">=15";Paris!M2:M;"<>En cours")']]},
            {"range": "C43", "values": [['=IFERROR(COUNTIFS(Paris!K2:K;">=15";Paris!M2:M;"Gagné")/B43;"-")']]},
            {"range": "D43", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=15")/SUMIFS(Paris!J2:J;Paris!K2:K;">=15");"-")']]},
            {"range": "A44", "values": [["EV > 5 % (tous)"]]},
            {"range": "B44", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!M2:M;"<>En cours")']]},
            {"range": "C44", "values": [['=IFERROR(COUNTIFS(Paris!K2:K;">=5";Paris!M2:M;"Gagné")/B44;"-")']]},
            {"range": "D44", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=5")/SUMIFS(Paris!J2:J;Paris!K2:K;">=5");"-")']]},
            # ── Timestamp ────────────────────────────────────────────────
            {"range": "A46", "values": [["Dernière mise à jour :"]]},
            {"range": "B46", "values": [[ts]]},
        ], value_input_option="USER_ENTERED")
        # 3. Formatage visuel (couleurs, polices, alignement, formats nombres)
        sid = ws_bord.id
        C = {
            "bleu_fonce": {"red": 0.118, "green": 0.227, "blue": 0.373},  # #1e3a5f
            "bleu_med":   {"red": 0.18,  "green": 0.42,  "blue": 0.64},
            "bleu_clair": {"red": 0.808, "green": 0.886, "blue": 0.953},
            "gris_clair": {"red": 0.953, "green": 0.953, "blue": 0.953},
            "indigo_pale":{"red": 0.918, "green": 0.929, "blue": 0.965},
            "blanc":      {"red": 1.0,   "green": 1.0,   "blue": 1.0},
            "vert":       {"red": 0.075, "green": 0.447, "blue": 0.196},
            "rouge":      {"red": 0.784, "green": 0.059, "blue": 0.180},
            "texte":      {"red": 0.2,   "green": 0.2,   "blue": 0.2},
            "gris_texte": {"red": 0.5,   "green": 0.5,   "blue": 0.5},
        }
        def rng(r1, c1, r2=None, c2=None):
            if r2 is None: r2 = r1
            if c2 is None: c2 = c1
            return {"sheetId": sid, "startRowIndex": r1-1, "endRowIndex": r2,
                    "startColumnIndex": c1-1, "endColumnIndex": c2}
        def rep(r1, c1, r2, c2, fmt, fields="userEnteredFormat"):
            return {"repeatCell": {"range": rng(r1,c1,r2,c2),
                                   "cell": {"userEnteredFormat": fmt}, "fields": fields}}

        reqs = []

        # Merges
        for (r1,c1,r2,c2) in [(1,1,1,4),(2,2,2,4),(5,1,5,4),(12,1,12,4),
                               (20,1,20,4),(26,1,26,4),(39,1,39,4)]:
            reqs.append({"mergeCells": {"range": rng(r1,c1,r2,c2),
                                        "mergeType": "MERGE_ALL"}})
        # Titre row 1
        reqs.append(rep(1,1,1,4, {
            "backgroundColor": C["bleu_fonce"],
            "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": C["blanc"]},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        }))
        # Timestamp row 2
        reqs.append(rep(2,1,2,4, {
            "backgroundColor": C["gris_clair"],
            "textFormat": {"italic": True, "fontSize": 9, "foregroundColor": C["gris_texte"]},
        }))
        # Section headers (rows 5,12,20,26,39)
        for row in [5, 12, 20, 26, 39]:
            reqs.append(rep(row,1,row,4, {
                "backgroundColor": C["bleu_med"],
                "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": C["blanc"]},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }))
        # Column headers in tables (rows 21,27,40)
        for row in [21, 27, 40]:
            reqs.append(rep(row,1,row,4, {
                "backgroundColor": C["bleu_clair"],
                "textFormat": {"bold": True, "fontSize": 9},
                "horizontalAlignment": "CENTER",
            }))
        # Alternating row colors + alignment
        for (r1, r2) in [(6,10), (13,18), (22,24), (28,37), (41,44)]:
            for r in range(r1, r2+1):
                bg = C["gris_clair"] if r % 2 == 0 else C["blanc"]
                reqs.append(rep(r,1,r,1, {"backgroundColor": bg,
                                           "horizontalAlignment": "LEFT",
                                           "textFormat": {"fontSize": 10}}))
                reqs.append(rep(r,2,r,4, {"backgroundColor": bg,
                                           "horizontalAlignment": "RIGHT",
                                           "textFormat": {"fontSize": 10}}))
        # EV "tous" row (44) légèrement différente
        reqs.append(rep(44,1,44,4, {"backgroundColor": C["indigo_pale"],
                                     "textFormat": {"bold": True, "fontSize": 10}}))
        # Number formats — € (B6:B8, B17, B18)
        money = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00 \"€\""}}
        pct   = {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}
        ints  = {"numberFormat": {"type": "NUMBER",  "pattern": "0"}}
        for r in [6, 7, 8, 17, 18]:
            reqs.append(rep(r,2,r,2, money, "userEnteredFormat.numberFormat"))
        for r in [9, 10]:
            reqs.append(rep(r,2,r,2, pct, "userEnteredFormat.numberFormat"))
        for r in [13, 14, 15, 16]:
            reqs.append(rep(r,2,r,2, ints, "userEnteredFormat.numberFormat"))
        reqs.append(rep(22,2,24,2, ints,  "userEnteredFormat.numberFormat"))
        reqs.append(rep(22,3,24,4, pct,   "userEnteredFormat.numberFormat"))
        reqs.append(rep(28,2,37,2, ints,  "userEnteredFormat.numberFormat"))
        reqs.append(rep(28,3,37,4, pct,   "userEnteredFormat.numberFormat"))
        reqs.append(rep(41,2,44,2, ints,  "userEnteredFormat.numberFormat"))
        reqs.append(rep(41,3,44,4, pct,   "userEnteredFormat.numberFormat"))
        # Conditional formatting: rouge si <0, vert si >0
        for (r1,c1,r2,c2) in [(8,2,8,2),(9,2,9,2),(22,4,24,4),(28,4,37,4),(41,4,44,4)]:
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [rng(r1,c1,r2,c2)],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": C["rouge"], "bold": True}},
                }}, "index": 0}})
            reqs.append({"addConditionalFormatRule": {"rule": {
                "ranges": [rng(r1,c1,r2,c2)],
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": C["vert"], "bold": True}},
                }}, "index": 0}})
        # Hauteur titre + largeurs colonnes
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 36}, "fields": "pixelSize"}})
        for i, w in enumerate([200, 120, 100, 100]):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i+1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        # Freeze titre
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})

        sh.batch_update({"requests": reqs})
        print("  Tableau de bord : mis à jour et formaté")
    except Exception as e:
        print(f"  Tableau de bord erreur : {e}")

    # ── Onglet "Analyse Modèle" ───────────────────────────────────────
    # Layout :
    #   Row 1        : Titre
    #   Row 2        : Timestamp
    #   Row 4        : Section INFORMATIONS MODÈLE
    #   Rows 5-9     : Infos modèle (A=label, B=valeur)
    #   Row 11       : Section PERFORMANCE PAR TRANCHE EV
    #   Row 12 hdrs  : Tranche / Paris / Gagnés / Taux / ROI
    #   Rows 13-16   : EV buckets  ← check_bord: rows 13-16 (B=paris, D=taux, E=ROI)
    #   Row 18       : Section PERFORMANCE PAR MOIS
    #   Row 19 hdrs  : Mois / Paris / Gagnés / Taux / P&L / Bankroll / ROI
    #   Rows 20-31   : 12 mois
    #   Row 33       : Section CALIBRATION PROBABILITÉ
    #   Row 34 hdrs  : Bucket / Paris / Win% prédit / Win% réel / Écart / Qualité
    #   Rows 35-39   : 5 buckets
    #   Row 40       : Note
    # Paris!K = EV Modèle (%) stocké en nombre entier/décimal : ex. 7.3 = 7.3 %
    # NOTE: check_bord.py lit EV buckets en rows 12-15 cols B/D/E — ces rows sont décalées
    #       à 13-16 dans le nouveau layout; mettre à jour check_bord.py en conséquence.
    try:
        # Supprime et recrée pour éliminer fusions/formatages anciens
        all_sheets_am = sh.worksheets()
        am_idx = next((i for i, w in enumerate(all_sheets_am) if w.title == "Analyse Modèle"), None)
        if am_idx is not None:
            sh.del_worksheet(all_sheets_am[am_idx])
        ws_am = sh.add_worksheet(title="Analyse Modèle", rows=45, cols=7)
        if am_idx is not None:
            all_sheets_am2 = sh.worksheets()
            ordered_am = [w for w in all_sheets_am2 if w.title != "Analyse Modèle"]
            ordered_am.insert(am_idx, ws_am)
            sh.reorder_worksheets(ordered_am)

        ws_am.batch_update([
            # ── Titre ──────────────────────────────────────────────
            {"range": "A1",  "values": [["ANALYSE MODÈLE — Performance & Calibration"]]},
            {"range": "A2",  "values": [["Mis à jour :"]]},
            {"range": "B2",  "values": [[ts]]},
            # ── INFORMATIONS MODÈLE ────────────────────────────────
            {"range": "A4",  "values": [["INFORMATIONS MODÈLE"]]},
            {"range": "A5",  "values": [["Type"]]},
            {"range": "B5",  "values": [["XGBoost"]]},
            {"range": "A6",  "values": [["Données"]]},
            {"range": "B6",  "values": [["ATP + WTA 2020-2026"]]},
            {"range": "A7",  "values": [["Précision estimée"]]},
            {"range": "B7",  "values": [["~65 %"]]},
            {"range": "A8",  "values": [["Nb features"]]},
            {"range": "B8",  "values": [[12]]},
            {"range": "A9",  "values": [["Features principales"]]},
            {"range": "D8",  "values": [["Elo global/surface · rang ATP/WTA · "
                                          "forme 5 matchs · H2H · 1stIn% · pts servi/reçu"]]},
            # ── Performance par tranche d'EV ──────────────────────────
            # ── PERFORMANCE PAR TRANCHE EV ─────────────────────────
            # Paris!K = EV% (7.3 = 7.3%) ; Paris!M = Résultat ; Paris!N = Gain ; Paris!J = Mise
            # check_bord.py lit les rows 13-16 (B=paris, D=taux, E=ROI)
            {"range": "A11", "values": [["PERFORMANCE PAR TRANCHE EV"]]},
            {"range": "A12:E12", "values": [["Tranche EV", "Paris résolus", "Gagnés", "Taux", "ROI"]]},
            {"range": "A13", "values": [["EV 5–10 %"]]},
            {"range": "A14", "values": [["EV 10–15 %"]]},
            {"range": "A15", "values": [["EV > 15 %"]]},
            {"range": "A16", "values": [["EV > 5 % (tous)"]]},
            # EV 5–10 %
            {"range": "B13", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!K2:K;"<10";Paris!M2:M;"<>En cours")']]},
            {"range": "C13", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!K2:K;"<10";Paris!M2:M;"Gagné")']]},
            {"range": "D13", "values": [['=IFERROR(C13/B13;"-")']]},
            {"range": "E13", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=5";Paris!K2:K;"<10")/SUMIFS(Paris!J2:J;Paris!K2:K;">=5";Paris!K2:K;"<10");"-")']]},
            # EV 10–15 %
            {"range": "B14", "values": [['=COUNTIFS(Paris!K2:K;">=10";Paris!K2:K;"<15";Paris!M2:M;"<>En cours")']]},
            {"range": "C14", "values": [['=COUNTIFS(Paris!K2:K;">=10";Paris!K2:K;"<15";Paris!M2:M;"Gagné")']]},
            {"range": "D14", "values": [['=IFERROR(C14/B14;"-")']]},
            {"range": "E14", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=10";Paris!K2:K;"<15")/SUMIFS(Paris!J2:J;Paris!K2:K;">=10";Paris!K2:K;"<15");"-")']]},
            # EV > 15 %
            {"range": "B15", "values": [['=COUNTIFS(Paris!K2:K;">=15";Paris!M2:M;"<>En cours")']]},
            {"range": "C15", "values": [['=COUNTIFS(Paris!K2:K;">=15";Paris!M2:M;"Gagné")']]},
            {"range": "D15", "values": [['=IFERROR(C15/B15;"-")']]},
            {"range": "E15", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=15")/SUMIFS(Paris!J2:J;Paris!K2:K;">=15");"-")']]},
            # Tous EV > 5 %
            {"range": "B16", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!M2:M;"<>En cours")']]},
            {"range": "C16", "values": [['=COUNTIFS(Paris!K2:K;">=5";Paris!M2:M;"Gagné")']]},
            {"range": "D16", "values": [['=IFERROR(C16/B16;"-")']]},
            {"range": "E16", "values": [['=IFERROR(SUMIFS(Paris!N2:N;Paris!K2:K;">=5")/SUMIFS(Paris!J2:J;Paris!K2:K;">=5");"-")']]},
            # ── PERFORMANCE PAR MOIS ───────────────────────────────
            {"range": "A18", "values": [["PERFORMANCE PAR MOIS 2026"]]},
            {"range": "A19:G19", "values": [["Mois", "Paris résolus", "Gagnés", "Taux (%)", "P&L (€)", "Bankroll (€)", "ROI (%)"]]},
        ], value_input_option="USER_ENTERED")

        # ── Section Par mois (rows 20-31) ─────────────────────────────
        mois_noms = ["Janvier","Février","Mars","Avril","Mai","Juin",
                     "Juillet","Août","Septembre","Octobre","Novembre","Décembre"]
        mois_updates = []
        for i, (nom, mois) in enumerate(zip(mois_noms, range(1, 13))):
            r = str(20 + i)  # rows 20-31
            mois_updates.extend([
                {"range": f"A{r}", "values": [[nom]]},
                {"range": f"B{r}", "values": [[
                    f'=SUMPRODUCT((IFERROR(YEAR(Paris!A2:A201);0)=2026)'
                    f'*(IFERROR(MONTH(Paris!A2:A201);0)={mois})'
                    f'*(Paris!M2:M201="Gagné"))'
                    f'+SUMPRODUCT((IFERROR(YEAR(Paris!A2:A201);0)=2026)'
                    f'*(IFERROR(MONTH(Paris!A2:A201);0)={mois})'
                    f'*(Paris!M2:M201="Perdu"))'
                ]]},
                {"range": f"C{r}", "values": [[
                    f'=SUMPRODUCT((IFERROR(YEAR(Paris!A2:A201);0)=2026)'
                    f'*(IFERROR(MONTH(Paris!A2:A201);0)={mois})'
                    f'*(Paris!M2:M201="Gagné"))'
                ]]},
                {"range": f"D{r}", "values": [[f'=IFERROR(C{r}/B{r};"-")']]},
                {"range": f"E{r}", "values": [[
                    f'=SUMPRODUCT((IFERROR(YEAR(Paris!A2:A201);0)=2026)'
                    f'*(IFERROR(MONTH(Paris!A2:A201);0)={mois})'
                    f'*(Paris!M2:M201="Gagné")*Paris!N2:N201)'
                    f'+SUMPRODUCT((IFERROR(YEAR(Paris!A2:A201);0)=2026)'
                    f'*(IFERROR(MONTH(Paris!A2:A201);0)={mois})'
                    f'*(Paris!M2:M201="Perdu")*Paris!N2:N201)'
                ]]},
                # F : Bankroll fin mois (EOMONTH évite DATE(2026;m;31) qui déborde)
                {"range": f"F{r}", "values": [[
                    f"='Tableau de bord'!D3"
                    f"+SUMPRODUCT((ISNUMBER(Paris!A2:A201))"
                    f"*(Paris!A2:A201<=EOMONTH(DATE(2026;{mois};1);0))"
                    f"*(Paris!M2:M201=\"Gagné\")*Paris!N2:N201)"
                    f"+SUMPRODUCT((ISNUMBER(Paris!A2:A201))"
                    f"*(Paris!A2:A201<=EOMONTH(DATE(2026;{mois};1);0))"
                    f"*(Paris!M2:M201=\"Perdu\")*Paris!N2:N201)"
                ]]},
                # G : ROI cumulé (ratio 0–1 stocké, formaté en %)
                {"range": f"G{r}", "values": [[
                    f"=IFERROR((F{r}-'Tableau de bord'!D3)/'Tableau de bord'!D3;\"-\")"
                ]]},
            ])
        ws_am.batch_update(mois_updates, value_input_option="USER_ENTERED")

        # ── Section Calibration (rows 35-39) ──────────────────────────
        # Buckets de probabilité prédit vs réel : Paris!L = Prob. Modèle (0-1)
        # IMPORTANT locale FR : décimaux avec virgule (0,5 pas 0.5) dans les formules
        cal_ranges = [
            (35, "0,5",  "0,6",  "0,55", "50% – 60%"),
            (36, "0,6",  "0,7",  "0,65", "60% – 70%"),
            (37, "0,7",  "0,8",  "0,75", "70% – 80%"),
            (38, "0,8",  "0,9",  "0,85", "80% – 90%"),
            (39, "0,9",  "1,01", "0,95", "> 90%"),
        ]
        cal_updates = [
            {"range": "A33", "values": [["CALIBRATION PROBABILITÉ MODÈLE"]]},
            {"range": "A34:F34", "values": [["Tranche prob.", "Paris", "Win % prédit", "Win % réel", "Écart", "Qualité"]]},
        ]
        for row, lo, hi, mid, label in cal_ranges:
            r = str(row)
            cal_updates.extend([
                {"range": f"A{r}", "values": [[label]]},
                {"range": f"B{r}", "values": [[
                    f"=SUMPRODUCT((Paris!L2:L201>={lo})*(Paris!L2:L201<{hi})*(Paris!M2:M201=\"Gagné\"))"
                    f"+SUMPRODUCT((Paris!L2:L201>={lo})*(Paris!L2:L201<{hi})*(Paris!M2:M201=\"Perdu\"))"
                ]]},
                {"range": f"C{r}", "values": [[mid]]},  # Win% prédit (virgule = décimal FR)
                {"range": f"D{r}", "values": [[
                    f"=IFERROR("
                    f"SUMPRODUCT((Paris!L2:L201>={lo})*(Paris!L2:L201<{hi})*(Paris!M2:M201=\"Gagné\"))/"
                    f"(SUMPRODUCT((Paris!L2:L201>={lo})*(Paris!L2:L201<{hi})*(Paris!M2:M201=\"Gagné\"))"
                    f"+SUMPRODUCT((Paris!L2:L201>={lo})*(Paris!L2:L201<{hi})*(Paris!M2:M201=\"Perdu\")));"
                    f"\"-\")"
                ]]},
                {"range": f"E{r}", "values": [[f'=IFERROR(D{r}-C{r};"-")']]},
                {"range": f"F{r}", "values": [[
                    f'=IF(B{r}<5;"Insuff. données";IF(ABS(E{r})<0,05;"Bon";"À surveiller"))'
                ]]},
            ])
        cal_updates.append({
            "range": "A41",
            "values": [["Note : Écart persistant > 10% = biais à corriger dans tennis_modele.py"]]
        })
        ws_am.batch_update(cal_updates, value_input_option="USER_ENTERED")

        # ── Formatage visuel ───────────────────────────────────────────
        # Même palette de couleurs que Tableau de bord
        CA = {
            "bleu_fonce": {"red": 0.118, "green": 0.227, "blue": 0.373},
            "bleu_med":   {"red": 0.18,  "green": 0.42,  "blue": 0.64},
            "bleu_clair": {"red": 0.808, "green": 0.886, "blue": 0.953},
            "gris_clair": {"red": 0.953, "green": 0.953, "blue": 0.953},
            "indigo_pale":{"red": 0.918, "green": 0.929, "blue": 0.965},
            "blanc":      {"red": 1.0,   "green": 1.0,   "blue": 1.0},
            "vert":       {"red": 0.075, "green": 0.447, "blue": 0.196},
            "rouge":      {"red": 0.784, "green": 0.059, "blue": 0.180},
            "texte":      {"red": 0.2,   "green": 0.2,   "blue": 0.2},
        }
        sam = ws_am.id
        NC = 7  # colonnes A-G

        def rng_am(r1, c1, r2, c2):
            return {"sheetId": sam, "startRowIndex": r1-1, "endRowIndex": r2,
                    "startColumnIndex": c1-1, "endColumnIndex": c2}

        def rep_am(r1, c1, r2, c2, fmt):
            return {"repeatCell": {"range": rng_am(r1,c1,r2,c2),
                                   "cell": {"userEnteredFormat": fmt},
                                   "fields": "userEnteredFormat"}}

        def merge_am(r1, c2):
            """Merge A{r1}:{col}{r1}"""
            return {"mergeCells": {"range": rng_am(r1,1,r1,c2), "mergeType": "MERGE_ALL"}}

        reqs_am = []

        # ── Titre row 1 ────────────────────────────────────────────────
        reqs_am.append(merge_am(1, NC))
        reqs_am.append(rep_am(1,1,1,NC, {
            "backgroundColor": CA["bleu_fonce"],
            "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": CA["blanc"]},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        }))
        reqs_am.append({"updateDimensionProperties": {
            "range": {"sheetId": sam, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 36}, "fields": "pixelSize"}})

        # ── Row 2 : timestamp ──────────────────────────────────────────
        reqs_am.append(rep_am(2,1,2,NC, {
            "backgroundColor": CA["gris_clair"],
            "textFormat": {"italic": True, "fontSize": 9, "foregroundColor": CA["texte"]},
        }))

        # ── Section headers (rows 4, 11, 18, 33) ──────────────────────
        for sr in [4, 11, 18, 33]:
            reqs_am.append(merge_am(sr, NC))
            reqs_am.append(rep_am(sr,1,sr,NC, {
                "backgroundColor": CA["bleu_med"],
                "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": CA["blanc"]},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
            }))

        # ── Column headers (rows 12, 19, 34) ──────────────────────────
        for hr in [12, 19, 34]:
            reqs_am.append(rep_am(hr,1,hr,NC, {
                "backgroundColor": CA["bleu_clair"],
                "textFormat": {"bold": True, "fontSize": 9},
                "horizontalAlignment": "CENTER",
            }))

        # ── Info modèle rows 5-9 : alternating ────────────────────────
        for i, row in enumerate(range(5, 10)):
            bg = CA["blanc"] if i % 2 == 0 else CA["gris_clair"]
            reqs_am.append(rep_am(row,1,row,NC, {"backgroundColor": bg}))
            # Label col A : bold
            reqs_am.append(rep_am(row,1,row,1, {"textFormat": {"bold": True}}))
            # Value col B : right-align
            reqs_am.append(rep_am(row,2,row,NC, {"horizontalAlignment": "RIGHT"}))

        # ── EV rows 13-15 alternating, row 16 = "tous" bold indigo ────
        for i, row in enumerate(range(13, 16)):
            bg = CA["blanc"] if i % 2 == 0 else CA["gris_clair"]
            reqs_am.append(rep_am(row,1,row,NC, {"backgroundColor": bg}))
        reqs_am.append(rep_am(16,1,16,NC, {
            "backgroundColor": CA["indigo_pale"],
            "textFormat": {"bold": True},
        }))

        # ── Mois rows 20-31 alternating ────────────────────────────────
        for i, row in enumerate(range(20, 32)):
            bg = CA["blanc"] if i % 2 == 0 else CA["gris_clair"]
            reqs_am.append(rep_am(row,1,row,NC, {"backgroundColor": bg}))

        # ── Calibration rows 35-39 alternating ────────────────────────
        for i, row in enumerate(range(35, 40)):
            bg = CA["blanc"] if i % 2 == 0 else CA["gris_clair"]
            reqs_am.append(rep_am(row,1,row,NC, {"backgroundColor": bg}))

        # ── Note row 41 ────────────────────────────────────────────────
        reqs_am.append(rep_am(41,1,41,NC, {
            "textFormat": {"italic": True, "fontSize": 8},
            "backgroundColor": CA["gris_clair"],
        }))

        # ── Number formats ─────────────────────────────────────────────
        pct_fmt   = {"numberFormat": {"type": "NUMBER", "pattern": "0.0%"}}
        int_fmt   = {"numberFormat": {"type": "NUMBER", "pattern": "0"}}
        eur_fmt   = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00 \"€\""}}
        pct1_fmt  = {"numberFormat": {"type": "NUMBER", "pattern": "0.0%"}}
        right_al  = {"horizontalAlignment": "RIGHT"}
        center_al = {"horizontalAlignment": "CENTER"}

        # EV section : cols B/C = int, D/E = %
        for row in range(13, 17):
            reqs_am.append(rep_am(row,2,row,3, {**int_fmt, **right_al}))
            reqs_am.append(rep_am(row,4,row,5, {**pct_fmt, **right_al}))

        # Mois section : col B/C = int, D/G = %, E = €, F = €
        for row in range(20, 32):
            reqs_am.append(rep_am(row,2,row,3, {**int_fmt, **right_al}))
            reqs_am.append(rep_am(row,4,row,4, {**pct_fmt, **right_al}))  # Taux
            reqs_am.append(rep_am(row,5,row,5, {**eur_fmt, **right_al}))  # P&L
            reqs_am.append(rep_am(row,6,row,6, {**eur_fmt, **right_al}))  # Bankroll
            reqs_am.append(rep_am(row,7,row,7, {**pct_fmt, **right_al}))  # ROI

        # Calibration section : col B = int, C/D/E = %
        for row in range(35, 40):
            reqs_am.append(rep_am(row,2,row,2, {**int_fmt, **right_al}))
            reqs_am.append(rep_am(row,3,row,5, {**pct1_fmt, **right_al}))  # Win% prédit/réel/écart
            reqs_am.append(rep_am(row,6,row,6, {**center_al}))  # Qualité centré

        # ── Alignements généraux ───────────────────────────────────────
        # Col A (labels) : texte par défaut (left)
        # Cols B-G : droite pour données dans section EV / Mois
        reqs_am.append(rep_am(13,1,17,1, {"horizontalAlignment": "LEFT"}))  # EV labels
        reqs_am.append(rep_am(20,1,32,1, {"horizontalAlignment": "LEFT"}))  # Mois labels
        reqs_am.append(rep_am(35,1,40,1, {"horizontalAlignment": "LEFT"}))  # Cal labels

        # ── Conditional formatting ─────────────────────────────────────
        def cond_rule(r1,c1,r2,c2, color, formula_template):
            return {"addConditionalFormatRule": {"rule": {
                "ranges": [rng_am(r1,c1,r2,c2)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula_template}]},
                    "format": {"textFormat": {"bold": True, "foregroundColor": color}}
                }
            }, "index": 0}}

        # EV ROI (col E rows 13-16) : vert si >0, rouge si <0
        reqs_am.append(cond_rule(13,5,17,5, CA["vert"],  "=E13>0"))
        reqs_am.append(cond_rule(13,5,17,5, CA["rouge"], "=E13<0"))
        # Mois P&L col E + ROI col G (rows 20-31)
        reqs_am.append(cond_rule(20,5,32,5, CA["vert"],  "=E20>0"))
        reqs_am.append(cond_rule(20,5,32,5, CA["rouge"], "=E20<0"))
        reqs_am.append(cond_rule(20,7,32,7, CA["vert"],  "=G20>0"))
        reqs_am.append(cond_rule(20,7,32,7, CA["rouge"], "=G20<0"))
        # Calibration écart col E (rows 35-39) : rouge si abs > 5%, vert si <=5%
        reqs_am.append(cond_rule(35,5,40,5, CA["rouge"], "=ABS(E35)>0,05"))
        reqs_am.append(cond_rule(35,5,40,5, CA["vert"],  "=ABS(E35)<=0,05"))

        # ── Column widths ──────────────────────────────────────────────
        for i, w in enumerate([160, 110, 90, 90, 90, 110, 90]):
            reqs_am.append({"updateDimensionProperties": {
                "range": {"sheetId": sam, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i+1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"}})

        # ── Freeze titre ───────────────────────────────────────────────
        reqs_am.append({"updateSheetProperties": {
            "properties": {"sheetId": sam,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}})

        sh.batch_update({"requests": reqs_am})
        print("  Analyse Modèle : mis à jour et formaté")
    except Exception as e:
        print(f"  Analyse Modèle erreur : {e}")


def init_gsheet():
    """Ouvre le Google Sheet configuré. Retourne le spreadsheet ou None."""
    if not GSHEET_ACTIF:
        return None
    if not os.path.exists(GSHEET_CREDENTIALS) or not os.path.exists(GSHEET_CONFIG):
        return None
    try:
        import gspread
        with open(GSHEET_CONFIG) as f:
            cfg = json.load(f)
        gc = gspread.service_account(filename=GSHEET_CREDENTIALS)
        return gc.open_by_key(cfg["sheet_id"])
    except Exception as e:
        print(f"  Google Sheets indisponible : {e}")
        return None


def _lire_index_sheet(ws):
    """
    Relit les données du sheet et retourne :
      index_match : "date|match" → [(row_idx_1based, ev_pct_float, statut)]

    Layout colonnes actuel :
      A:Date  B:Tournoi  C:Circuit  D:Surface  E:Match
      F:Joueur  G:Adversaire  H:Bookmaker  I:Cote  J:Mise
      K:EV%(idx10)  L:Prob%(idx11)  M:Résultat(idx12)  N:Gain/Perte  O:Bankroll

    Robuste aux anciens formats (EV en idx 10 ou 11, statut en idx 12/13).
    """
    STATUTS = {"En cours", "Gagné", "Perdu"}
    data = ws.get_all_values()
    index = {}
    for ri, row in enumerate(data[1:], start=2):
        if len(row) < 5:
            continue
        key = f"{row[0]}|{row[4]}"

        # EV : col K (idx 10) en priorité, puis L (idx 11) pour anciens formats
        ev_val = 0.0
        for ci in (10, 11):
            if len(row) > ci:
                try:
                    v = float(str(row[ci]).replace(",", "."))
                    if v > 0:
                        ev_val = v
                        break
                except ValueError:
                    pass

        # Statut : col M (idx 12) en priorité, puis N (13) pour anciens formats
        statut = ""
        for ci in (12, 13):
            if len(row) > ci and row[ci] in STATUTS:
                statut = row[ci]
                break

        index.setdefault(key, []).append((ri, ev_val, statut))
    return index


def _parse_float_fr(v, default=0.0):
    """
    Parse un nombre provenant de get_all_values() avec la locale FR.

    Gère tous les formats renvoyés par Google Sheets :
      '5,00 €'  →  5.0       (€, espace, virgule décimale)
      '100,00 €' → 100.0
      '5.00'    →  5.0       (point décimal, pas de devise)
      '-5,00'   → -5.0
      ''        →  default
    """
    try:
        clean = (
            str(v)
            .replace('\u202f', '')   # espace fine insécable (format FR)
            .replace('\xa0',   '')   # espace insécable
            .replace('€',      '')   # symbole euro
            .replace(' ',      '')   # espaces restants
            .replace(',',      '.')  # virgule → point décimal
            .strip()
        )
        return float(clean) if clean else default
    except (ValueError, TypeError):
        return default


def _ecrire_extras(ws, r):
    """
    Réservé à la création d'une nouvelle ligne (résultat 'En cours') :
    laisse N/O/P vides — _reparer_colonnes_no les remplira avec des valeurs
    numériques à la fin de la session d'enregistrement.

    Layout colonnes :
      A:Date  B:Tournoi  C:Circuit  D:Surface  E:Match
      F:Joueur  G:Adversaire  H:Bookmaker  I:Cote  J:Mise
      K:EV%  L:Prob%  M:Résultat  N:Gain/Perte  O:Bankroll
    """
    # N, O, P laissés vides : seront remplis par _reparer_colonnes_no()
    pass


HEADER_PARIS = [
    "Date", "Tournoi", "Circuit", "Surface", "Match",
    "Joueur parié", "Adversaire", "Bookmaker",
    "Cote", "Mise (€)", "EV Modèle (%)", "Prob. Modèle (%)",
    "Résultat", "Gain/Perte (€)", "Bankroll (€)",
]


def _verifier_header(ws):
    """Met à jour la ligne 1 du sheet 'Paris' si elle ne correspond pas au layout actuel."""
    try:
        current = ws.row_values(1)
        if current != HEADER_PARIS:
            ws.update("A1:O1", [HEADER_PARIS], value_input_option="USER_ENTERED")
            print("  Google Sheets : en-tête mis à jour")
    except Exception as e:
        print(f"  Avertissement : impossible de vérifier l'en-tête ({e})")


def _formater_colonnes_numeriques(ws):
    """
    Applique un format numérique uniforme à toutes les colonnes numériques
    de l'onglet Paris (lignes 2 à 1000) — layout 15 colonnes A-O :
      I:Cote       → 0.00
      J:Mise       → 0.00 €
      K:EV%        → 0.0  (stocké en %, ex. 7.3)
      L:Prob%      → 0.0  (stocké en %, ex. 85.6)
      N:Gain/Perte → #,##0.00 €
      O:Bankroll   → #,##0.00 €
    Toutes ces colonnes sont alignées à droite.
    """
    try:
        fmt_cote    = {"numberFormat": {"type": "NUMBER",   "pattern": "0.00"},
                       "horizontalAlignment": "RIGHT"}
        fmt_euro    = {"numberFormat": {"type": "NUMBER",   "pattern": '#,##0.00 "€"'},
                       "horizontalAlignment": "RIGHT"}
        fmt_pct     = {"numberFormat": {"type": "NUMBER",   "pattern": "0.0"},
                       "horizontalAlignment": "RIGHT"}

        ws.format("I2:I1000", fmt_cote)   # Cote
        ws.format("J2:J1000", fmt_euro)   # Mise
        ws.format("K2:K1000", fmt_pct)    # EV%
        ws.format("L2:L1000", fmt_pct)    # Prob%
        ws.format("N2:N1000", fmt_euro)   # Gain/Perte
        ws.format("O2:O1000", fmt_euro)   # Bankroll
    except Exception as e:
        print(f"  Avertissement formatage : {e}")


def _reparer_colonnes_no(ws):
    """
    Écrit les valeurs NUMÉRIQUES (sans formule) dans les colonnes N et O,
    et efface la colonne P (ancien layout à 16 colonnes).

    Colonnes écrites :
      N = Gain/Perte  → mise*(cote-1) si Gagné, -mise si Perdu, "" si En cours
      O = Bankroll    → cascade cumulée depuis 100 €
                        "En cours" reprend la dernière bankroll connue
      P = ""          → effacé (reliquat de l'ancien layout)

    Colonnes lues :
      I (idx 8)  = Cote
      J (idx 9)  = Mise
      M (idx 12) = Résultat

    Avantage vs formules : aucune dépendance inter-cellules → jamais de #REF!
    """
    try:
        data = ws.get_all_values()
        if len(data) <= 1:
            return

        bankroll = 100.0
        updates = []
        for ri, row in enumerate(data[1:], start=2):
            if not row or not str(row[0]).strip():   # ligne vide
                continue
            result = row[12] if len(row) > 12 else ""
            cote = _parse_float_fr(row[8]) if len(row) > 8 else 0.0
            mise = _parse_float_fr(row[9]) if len(row) > 9 else 0.0

            if result == "Gagné":
                gain = round(mise * (cote - 1), 2)
                bankroll = round(bankroll + gain, 2)
                updates.append({"range": f"N{ri}", "values": [[gain]]})
                updates.append({"range": f"O{ri}", "values": [[bankroll]]})
            elif result == "Perdu":
                gain = round(-mise, 2)
                bankroll = round(bankroll + gain, 2)
                updates.append({"range": f"N{ri}", "values": [[gain]]})
                updates.append({"range": f"O{ri}", "values": [[bankroll]]})
            else:
                # "En cours" : Gain/Perte vide, Bankroll = dernière connue
                updates.append({"range": f"N{ri}", "values": [[""]]})
                updates.append({"range": f"O{ri}", "values": [[bankroll]]})

            # Efface l'ancienne colonne P (présente sur les lignes migrées)
            updates.append({"range": f"P{ri}", "values": [[""]]})

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            nb_lignes = len(updates) // 3
            print(f"  Paris N/O/P : {nb_lignes} ligne(s) mises à jour "
                  f"(valeurs numériques, sans formule)")
    except Exception as e:
        print(f"  Avertissement réparation N/O : {e}")


# Alias conservé pour compatibilité avec les appels existants
_reparer_bankroll_paris = _reparer_colonnes_no


# Mots-clés détectant l'ancien format "type_pari en col H"
_TP_PATTERNS = ("vainqueur", "handicap", "total", "over", "under", "sets", "set")


def _migrer_lignes_ancien_format(ws):
    """
    Détecte les lignes dans l'ancien format (type_pari en col H = idx 7)
    et les réécrit dans le nouveau format (bookmaker en H, type_pari supprimé).

    Ancien format (14 cols A–N) :
      A:Date … G:Adversaire H:Type de pari I:Bookmaker J:Cote K:Mise L:EV% M:Prob% N:Résultat
      O:Gain/Perte(formule)  P:Bankroll(formule)

    Nouveau format (13 cols A–M) :
      A:Date … G:Adversaire H:Bookmaker I:Cote J:Mise K:EV% L:Prob% M:Résultat
      N:Gain/Perte(formule)  O:Bankroll(formule)
    """
    try:
        data = ws.get_all_values()
        if len(data) <= 1:
            return

        updates = []
        extra_rows = []

        for ri, row in enumerate(data[1:], start=2):
            if len(row) < 9:
                continue
            col_h = row[7].lower().strip()
            # Ancien format détecté si col H contient un mot-clé de type_pari
            if not any(tp in col_h for tp in _TP_PATTERNS):
                continue

            new_row = [
                row[0],   # A: Date
                row[1],   # B: Tournoi
                row[2],   # C: Circuit
                row[3],   # D: Surface
                row[4],   # E: Match
                row[5],   # F: Joueur
                row[6],   # G: Adversaire
                row[8],   # H: Bookmaker  (était en I)
                row[9],   # I: Cote       (était en J)
                row[10],  # J: Mise       (était en K)
                row[11],  # K: EV%        (était en L)
                row[12],  # L: Prob%      (était en M)
                row[13],  # M: Résultat   (était en N)
            ]
            updates.append({"range": f"A{ri}:M{ri}", "values": [new_row]})
            extra_rows.append(ri)

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            for ri in extra_rows:
                _ecrire_extras(ws, ri)
            print(f"  Google Sheets : {len(updates)} ligne(s) migrée(s) "
                  f"(type_pari supprimé)")
    except Exception as e:
        print(f"  Avertissement migration : {e}")


def enregistrer_paris_gsheet(sh, value_bets):
    """
    Maintient UNE SEULE ligne par match dans l'onglet 'Paris'.
    Clé de déduplication : date + match (sans joueur ni type_pari).

    Logique :
      - Aucune ligne existante        → append
      - Ligne(s) "En cours" existante(s)
          · nouvel EV > EV enregistré → mise à jour de la ligne (cols F–N)
          · doublons supplémentaires  → supprimés (en ordre inverse)
      - Match déjà terminé (Gagné/Perdu) → ignoré
    """
    if not sh or not value_bets:
        return
    try:
        ws = sh.worksheet("Paris")
        _verifier_header(ws)
        _migrer_lignes_ancien_format(ws)
        _formater_colonnes_numeriques(ws)

        # ── Étape 1 : supprimer les doublons existants (même date+match) ──
        idx = _lire_index_sheet(ws)
        rows_a_supprimer = set()
        for key, lignes in idx.items():
            en_cours = [(ri, ev, st) for ri, ev, st in lignes if st == "En cours"]
            if len(en_cours) > 1:
                # Garder la ligne avec le meilleur EV, supprimer les autres
                best_ri = max(en_cours, key=lambda x: x[1])[0]
                for ri, _, _ in en_cours:
                    if ri != best_ri:
                        rows_a_supprimer.add(ri)

        if rows_a_supprimer:
            for ri in sorted(rows_a_supprimer, reverse=True):
                ws.delete_rows(ri)
            print(f"  Google Sheets : {len(rows_a_supprimer)} doublon(s) supprimé(s)")
            idx = _lire_index_sheet(ws)   # recalculer après suppressions

        # ── Étape 2 : ajouter ou mettre à jour chaque value bet ───────────
        nb_ajouts = 0
        nb_mises_a_jour = 0

        def _data_row(vb):
            # 13 colonnes A–M
            # A:Date  B:Tournoi  C:Circuit  D:Surface  E:Match
            # F:Joueur  G:Adversaire  H:Bookmaker  I:Cote  J:Mise
            # K:EV%  L:Prob%  M:Résultat
            return [
                vb["date"],
                vb.get("tournoi", ""),
                vb["circuit"],
                vb.get("surface", ""),
                vb.get("match", f"{vb['joueur']} vs {vb['adversaire']}"),
                vb["joueur"],
                vb["adversaire"],
                vb["bookmaker"],
                vb["cote"],
                vb["mise"],
                round(vb["ev"] * 100, 1),
                round(vb["prob_modele"] * 100, 1),
                "En cours",
            ]

        for vb in value_bets:
            key     = f"{vb['date']}|{vb.get('match', '')}"
            new_ev  = round(vb["ev"] * 100, 1)
            lignes  = idx.get(key, [])
            en_cours  = [(ri, ev, st) for ri, ev, st in lignes if st == "En cours"]
            terminees = [(ri, ev, st) for ri, ev, st in lignes
                         if st in ("Gagné", "Perdu")]

            if terminees:
                # Match déjà terminé — ne pas toucher
                continue

            if not en_cours:
                # Nouvelle ligne : A–M (13 cols) puis N/O formules
                next_row = len(ws.get_all_values()) + 1
                ws.append_row(_data_row(vb), value_input_option="USER_ENTERED")
                _ecrire_extras(ws, next_row)
                idx.setdefault(key, []).append((next_row, new_ev, "En cours"))
                nb_ajouts += 1
            else:
                existing_ri, existing_ev, _ = en_cours[0]
                if new_ev > existing_ev:
                    # Meilleur pari → écraser F:M (8 cols sans type_pari)
                    ws.update(
                        f"F{existing_ri}:M{existing_ri}",
                        [[
                            vb["joueur"],
                            vb["adversaire"],
                            vb["bookmaker"],
                            vb["cote"],
                            vb["mise"],
                            round(vb["ev"] * 100, 1),
                            round(vb["prob_modele"] * 100, 1),
                            "En cours",
                        ]],
                        value_input_option="USER_ENTERED",
                    )
                    nb_mises_a_jour += 1
                # else : pari existant déjà meilleur, on ne touche pas

        msgs = []
        if nb_ajouts:       msgs.append(f"{nb_ajouts} ajouté(s)")
        if nb_mises_a_jour: msgs.append(f"{nb_mises_a_jour} mis à jour")
        if msgs:
            print(f"  Google Sheets : {', '.join(msgs)}")
        else:
            print("  Google Sheets : aucun changement (paris déjà optimaux)")

        # Recalcule N (Gain/Perte) et O (Bankroll) en valeurs numériques
        # — après toutes les insertions/mises à jour, nouvelles lignes incluses.
        _reparer_colonnes_no(ws)

    except Exception as e:
        print(f"  Erreur Google Sheets (enregistrement) : {e}")


def mettre_a_jour_resultats_gsheet(sh):
    """
    Appelée automatiquement depuis analyser_semaine().
    Guard : skip silencieux si aucun pari passé n'est encore 'En cours'
    (évite tout appel réseau inutile).
    Source : ESPN scoreboard (même logique que --resultats).
    """
    if not sh:
        return
    try:
        ws   = sh.worksheet("Paris")
        data = ws.get_all_values()
        if len(data) <= 1:
            return

        aujourd_hui = datetime.now(timezone.utc).date()

        # ── Guard : y a-t-il des paris passés encore "En cours" ? ─
        paris_pendants = []
        for ri, row in enumerate(data[1:], start=2):
            if len(row) < 13 or row[12] != "En cours":
                continue
            try:
                date_match = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if date_match < aujourd_hui:
                paris_pendants.append({
                    "ri":         ri,
                    "date":       date_match,
                    "circuit":    row[2].lower(),
                    "joueur":     row[5],
                    "adversaire": row[6],
                })

        if not paris_pendants:
            return          # rien à résoudre — 0 appel réseau

        # ── Résultats via ESPN (pas d'appel Odds API) ─────────────
        circuits  = set(p["circuit"] for p in paris_pendants)
        espn_data = {}
        csv_local = {}
        for circuit in circuits:
            dates          = {p["date"] for p in paris_pendants
                              if p["circuit"] == circuit}
            espn_data[circuit] = _resultats_espn(circuit, dates)
            csv_local[circuit] = _resultats_csv(circuit)

        updates = []
        for pari in paris_pendants:
            circuit  = pari["circuit"]
            resultat = _chercher_resultat(pari, espn_data.get(circuit, {}))
            if not resultat:
                resultat = _chercher_resultat(pari, csv_local.get(circuit, {}))
            if resultat:
                updates.append({
                    "range":  f"M{pari['ri']}",
                    "values": [[resultat]],
                })

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
            print(f"  Résultats auto (ESPN) : {len(updates)} mis à jour")

    except Exception as e:
        print(f"  Erreur résultats auto : {e}")


def envoyer_notification(titre, message, priorite=3, tags=None):
    """
    Envoie une notification push via ntfy.sh.
    priorite : 1=min  2=low  3=default  4=high  5=urgent
    tags      : liste d'emojis/mots-clés ntfy (ex: ["tennis", "money_with_wings"])
    """
    if not NTFY_TOPIC:
        return
    try:
        hdrs = {"Priority": str(priorite)}
        # ntfy attend les headers en ASCII — encoder les caractères spéciaux
        hdrs["Title"] = titre.encode("utf-8").decode("latin-1", errors="replace")
        if tags:
            hdrs["Tags"] = ",".join(tags)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=hdrs,
            timeout=10,
        )
        print(f"  Notification push : {titre}")
    except Exception as e:
        print(f"  Notification ntfy : erreur ({e})")


def envoyer_email_resultats(updates, bankroll_apres):
    """Email récapitulatif quand des paris sont réglés (Gagné/Perdu)."""
    if not EMAIL_ACTIF or not updates:
        return
    if not EMAIL_FROM or not EMAIL_TO or not SMTP_PASS:
        return
    try:
        date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        gagnes   = [u for u in updates if u["resultat"] == "Gagné"]
        perdus   = [u for u in updates if u["resultat"] == "Perdu"]

        rows_html = ""
        for u in updates:
            couleur = "#1e8449" if u["resultat"] == "Gagné" else "#922b21"
            icone   = "+" if u["resultat"] == "Gagné" else "x"
            rows_html += f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #eee;">
                <b>{u['joueur']}</b> vs {u['adversaire']}</td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         color:{couleur};font-weight:bold;text-align:center;">
                [{icone}] {u['resultat']}</td>
            </tr>"""

        bk_str = f"{bankroll_apres:.2f}" if bankroll_apres else "—"
        corps = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
        <h2 style="color:#1a5276;border-bottom:2px solid #1a5276;padding-bottom:8px;">
          Paris réglés — {date_str}</h2>
        <p>{len(gagnes)} gagné(s) &nbsp;·&nbsp; {len(perdus)} perdu(s)
           &nbsp;·&nbsp; <b>Bankroll : {bk_str}&nbsp;€</b></p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead><tr style="background:#f0f3f4;">
            <th style="padding:8px 12px;text-align:left;">Match</th>
            <th style="padding:8px 12px;">Résultat</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Tennis] {len(gagnes)}W / {len(perdus)}L — Bankroll {bk_str}€"
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(corps, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        print(f"  Email résultats envoyé : {len(updates)} pari(s) réglé(s)")
    except Exception as e:
        print(f"  Email résultats : erreur ({e})")


def envoyer_email_alerte(value_bets, rapport_chemin=None, bankroll=None):
    """
    Envoie un email HTML récapitulatif des value bets.
    Tableau couleur (vert EV>15%, orange EV 5-15%), résumé en haut,
    meilleur pari mis en avant. Compatible mobile.
    """
    if not EMAIL_ACTIF:
        return
    if not value_bets:
        return
    if not EMAIL_FROM or not EMAIL_TO or not SMTP_PASS:
        print("  Email : paramètres incomplets (EMAIL_FROM/TO/SMTP_PASS)")
        return

    try:
        nb         = len(value_bets)
        total_mise = sum(vb["mise"] for vb in value_bets)
        best       = max(value_bets, key=lambda x: x["ev"])
        date_str   = datetime.now().strftime("%d/%m/%Y %H:%M")
        bk_str     = f"{bankroll:.0f}" if bankroll else "—"
        best_ev_pc = best["ev"] * 100
        best_gain  = best["mise"] * (best["cote"] - 1)

        def _ev_color(ev):
            return "#1e8449" if ev >= 0.15 else "#b7950b"

        def _ev_bg(ev):
            return "#d5f5e3" if ev >= 0.15 else "#fef9e7"

        # ── Meilleur pari (bloc mis en avant) ─────────────────
        best_html = f"""
        <div style="background:#eaf4fb;border-left:5px solid #1a5276;
                    padding:16px 18px;margin:16px 0;border-radius:4px;">
          <div style="font-size:10px;color:#555;text-transform:uppercase;
                      letter-spacing:1px;margin-bottom:8px;">
            ⭐ Meilleur pari du jour
          </div>
          <div style="font-size:15px;font-weight:bold;color:#1a5276;">
            {best['match']}
          </div>
          <div style="margin-top:5px;font-size:14px;color:#222;">
            <b>{best['joueur']}</b> @ <b>{best['cote']:.2f}</b>
            &nbsp;·&nbsp; {best.get('bookmaker','?')}
            &nbsp;·&nbsp; {best.get('type_pari','Vainqueur')}
          </div>
          <div style="margin-top:6px;font-weight:bold;font-size:15px;
                      color:{_ev_color(best['ev'])};">
            EV +{best_ev_pc:.1f}%
            &nbsp;·&nbsp; Mise {best['mise']:.2f}&nbsp;€
            &nbsp;·&nbsp; Gain potentiel +{best_gain:.2f}&nbsp;€
          </div>
        </div>"""

        # ── Lignes du tableau (triées par EV décroissant) ─────
        rows_html = ""
        for i, vb in enumerate(sorted(value_bets, key=lambda x: -x["ev"])):
            ev_pc  = vb["ev"] * 100
            gain   = vb["mise"] * (vb["cote"] - 1)
            bg_row = "#f9f9f9" if i % 2 == 0 else "#ffffff"
            ec     = _ev_color(vb["ev"])
            eb     = _ev_bg(vb["ev"])
            rows_html += f"""
            <tr style="background:{bg_row};">
              <td style="padding:10px 12px;border-bottom:1px solid #eee;">
                <b style="color:#1a5276">{vb['match']}</b><br>
                <span style="font-size:11px;color:#888;">
                  {vb['date']} &middot; {vb['circuit']}
                  &middot; {vb.get('type_pari','Vainqueur')}
                </span>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;">
                <b>{vb['joueur']}</b>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         white-space:nowrap;font-size:12px;">
                {vb.get('bookmaker','?')}
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         text-align:center;font-weight:bold;">
                {vb['cote']:.2f}
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         text-align:center;color:#555;">
                {vb['prob_modele']*100:.1f}%
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         text-align:center;font-weight:bold;
                         background:{eb};color:{ec};">
                +{ev_pc:.1f}%
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         text-align:center;">
                {vb['mise']:.2f}&nbsp;€
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #eee;
                         text-align:center;color:#1e8449;font-weight:bold;">
                +{gain:.2f}&nbsp;€
              </td>
            </tr>"""

        # ── Template HTML complet ──────────────────────────────
        html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tennis Value Bets</title>
</head>
<body style="margin:0;padding:0;background:#ecf0f1;font-family:Arial,sans-serif;">
<div style="max-width:660px;margin:20px auto;">

  <!-- En-tête -->
  <div style="background:#1a5276;color:white;
              padding:24px 22px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">🎾 Tennis Value Bets</div>
    <div style="margin-top:5px;opacity:.8;font-size:13px;">{date_str}</div>
  </div>

  <!-- Résumé 4 métriques -->
  <div style="background:white;padding:18px 0;display:flex;
              border-bottom:2px solid #ecf0f1;">
    <div style="flex:1;text-align:center;border-right:1px solid #eee;padding:0 10px;">
      <div style="font-size:22px;font-weight:bold;color:#1a5276;">{bk_str}&nbsp;€</div>
      <div style="font-size:11px;color:#888;margin-top:2px;">Bankroll</div>
    </div>
    <div style="flex:1;text-align:center;border-right:1px solid #eee;padding:0 10px;">
      <div style="font-size:22px;font-weight:bold;color:#1a5276;">{nb}</div>
      <div style="font-size:11px;color:#888;margin-top:2px;">Paris</div>
    </div>
    <div style="flex:1;text-align:center;border-right:1px solid #eee;padding:0 10px;">
      <div style="font-size:22px;font-weight:bold;color:#1a5276;">{total_mise:.1f}&nbsp;€</div>
      <div style="font-size:11px;color:#888;margin-top:2px;">Mise totale</div>
    </div>
    <div style="flex:1;text-align:center;padding:0 10px;">
      <div style="font-size:22px;font-weight:bold;
                  color:{_ev_color(best['ev'])};">+{best_ev_pc:.1f}%</div>
      <div style="font-size:11px;color:#888;margin-top:2px;">Meilleur EV</div>
    </div>
  </div>

  <!-- Meilleur pari -->
  <div style="background:white;padding:0 20px 4px 20px;">
    {best_html}
  </div>

  <!-- Tableau value bets -->
  <div style="background:white;overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#1a5276;color:white;">
          <th style="padding:10px 12px;text-align:left;font-weight:600;">Match</th>
          <th style="padding:10px 8px;text-align:left;font-weight:600;">Pari</th>
          <th style="padding:10px 8px;text-align:left;font-weight:600;">Book.</th>
          <th style="padding:10px 8px;text-align:center;font-weight:600;">Cote</th>
          <th style="padding:10px 8px;text-align:center;font-weight:600;">Prob.</th>
          <th style="padding:10px 8px;text-align:center;font-weight:600;">EV</th>
          <th style="padding:10px 8px;text-align:center;font-weight:600;">Mise</th>
          <th style="padding:10px 8px;text-align:center;font-weight:600;">Gain</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>

  <!-- Légende couleurs -->
  <div style="background:white;padding:10px 20px 14px;font-size:11px;color:#888;">
    <span style="background:#d5f5e3;color:#1e8449;padding:2px 8px;
                 border-radius:10px;margin-right:8px;">■ EV &gt; 15%</span>
    <span style="background:#fef9e7;color:#b7950b;padding:2px 8px;
                 border-radius:10px;">■ EV 5–15%</span>
  </div>

  <!-- Pied de page -->
  <div style="background:#bdc3c7;padding:12px 20px;text-align:center;
              font-size:11px;color:#666;border-radius:0 0 8px 8px;">
    Tennis Betting System &middot; Généré le {date_str}
  </div>

</div>
</body>
</html>"""

        # ── Fallback texte plat ────────────────────────────────
        corps_txt = f"Tennis Value Bets — {date_str}\n{'='*50}\n"
        for vb in value_bets:
            corps_txt += (
                f"\n{vb['match']}\n"
                f"  {vb['joueur']} @ {vb['cote']:.2f} ({vb.get('bookmaker','?')})\n"
                f"  EV +{vb['ev']*100:.1f}% | Prob {vb['prob_modele']*100:.1f}%"
                f" | Mise {vb['mise']:.2f}EUR\n"
            )

        # ── Envoi ──────────────────────────────────────────────
        msg = MIMEMultipart("alternative")
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg["Subject"] = (
            f"[Tennis] {nb} value bet{'s' if nb>1 else ''}"
            f" — Meilleur EV +{best_ev_pc:.1f}%"
        )
        msg.attach(MIMEText(corps_txt, "plain", "utf-8"))
        msg.attach(MIMEText(html,      "html",  "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER or EMAIL_FROM, SMTP_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        print(f"  Email HTML envoyé à {EMAIL_TO} ({nb} value bet(s))")

    except Exception as e:
        print(f"  Email échec : {e}")
        print("  → Vérifier SMTP_HOST, SMTP_PORT, EMAIL_FROM, SMTP_PASS")


# ─────────────────────────────────────────────────────────────
# RÉCUPÉRATION DES RÉSULTATS  (--resultats)
# ─────────────────────────────────────────────────────────────

def _noms_correspondent(a, b):
    """Retourne True si les deux noms désignent le même joueur (fuzzy)."""
    from difflib import SequenceMatcher
    na, nb = normaliser(a), normaliser(b)
    if na == nb:
        return True
    # Contenu partiel (ex: "sinner" in "jannik sinner")
    if na in nb or nb in na:
        return True
    # Initiale + nom de famille  "j. sinner" vs "jannik sinner"
    pa, pb = na.split(), nb.split()
    if pa and pb and pa[-1] == pb[-1]:          # même nom de famille
        return True
    return SequenceMatcher(None, na, nb).ratio() > 0.82


def _resultats_espn(circuit, dates_a_verifier):
    """
    Source primaire : ESPN scoreboard ATP/WTA (gratuit, sans clé).
    Retourne {frozenset({nom1_norm, nom2_norm}): winner_display_name}
    pour tous les matchs terminés couvrant les dates demandées.
    ESPN publie les résultats dans les minutes qui suivent la fin du match.
    """
    base = (f"http://site.api.espn.com/apis/site/v2"
            f"/sports/tennis/{circuit}/scoreboard")
    resultats = {}

    # Interroger chaque date + J+1 et J+2 pour les matchs terminés tard
    dates_espn = set()
    for d in dates_a_verifier:
        for delta in range(3):
            dates_espn.add(d + timedelta(days=delta))

    for d in sorted(dates_espn):
        try:
            r = requests.get(base, params={"dates": d.strftime("%Y%m%d")},
                             timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue

        for event in data.get("events", []):
            for grp in event.get("groupings", []):
                for comp in grp.get("competitions", []):
                    if not comp.get("status", {}).get("type", {}).get("completed"):
                        continue
                    competitors = comp.get("competitors", [])
                    if len(competitors) < 2:
                        continue
                    names  = [c.get("athlete", {}).get("displayName", "")
                              for c in competitors]
                    winner = next(
                        (c.get("athlete", {}).get("displayName", "")
                         for c in competitors if c.get("winner")),
                        None,
                    )
                    if winner and all(names):
                        key = frozenset(normaliser(n) for n in names)
                        resultats[key] = winner
    return resultats


def _resultats_csv(circuit):
    """
    Source secondaire : CSV locaux mis à jour par tennis_scraper.py.
    Retourne {frozenset({winner_norm, loser_norm}): winner_name}.
    """
    fname = ("atp_matchs_bruts.csv" if circuit == "atp"
             else "wta_matchs_bruts.csv")
    p = os.path.join(DOSSIER_DATA, fname)
    resultats = {}
    if not os.path.exists(p):
        return resultats
    try:
        import pandas as pd
        df = pd.read_csv(p, usecols=["winner_name", "loser_name", "date"])
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        df = df[df["date"] >= cutoff]
        for _, row in df.iterrows():
            w, l = str(row["winner_name"]), str(row["loser_name"])
            if w and l and w != "nan" and l != "nan":
                key = frozenset([normaliser(w), normaliser(l)])
                resultats[key] = w
    except Exception:
        pass
    return resultats


def _chercher_resultat(pari, resultats_dict):
    """
    Cherche le résultat d'un pari dans {frozenset(noms_normalisés): winner}.
    Retourne 'Gagné', 'Perdu' ou None.
    """
    for key, winner in resultats_dict.items():
        noms = list(key)
        if len(noms) < 2:
            continue
        # Les deux joueurs du pari doivent correspondre aux deux noms de la clé
        match_j = any(_noms_correspondent(pari["joueur"],     n) for n in noms)
        match_a = any(_noms_correspondent(pari["adversaire"], n) for n in noms)
        if match_j and match_a:
            return "Gagné" if _noms_correspondent(pari["joueur"], winner) else "Perdu"
    return None


def recuperer_resultats():
    """
    --resultats : met à jour Gagné/Perdu pour tous les paris passés
    encore marqués 'En cours' dans l'onglet Paris du Google Sheet.

    Sources (priorité décroissante) :
      1. ESPN scoreboard ATP/WTA — temps réel, sans clé, mis à jour
         dans les minutes suivant la fin de chaque match.
      2. CSV locaux atp/wta_matchs_bruts.csv — données tennis-data.co.uk
         mises à jour lors du pipeline hebdomadaire.

    Seule la colonne M (Résultat) est écrite.
    Les colonnes N (Gain/Perte) et O (Bankroll) se recalculent
    automatiquement via leurs formules Google Sheets en cascade.
    """
    print("\n" + "=" * 60)
    print("  MISE À JOUR DES RÉSULTATS")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    sh = init_gsheet()
    if not sh:
        print("  Erreur connexion Google Sheets")
        return

    ws      = sh.worksheet("Paris")
    ws_bord = sh.worksheet("Tableau de bord")
    data    = ws.get_all_values()

    if len(data) <= 1:
        print("  Onglet Paris vide — rien à mettre à jour.")
        return

    aujourd_hui = datetime.now(timezone.utc).date()

    # ── 1. Sélectionner les paris passés encore "En cours" ────
    paris = []
    for ri, row in enumerate(data[1:], start=2):
        if len(row) < 13 or row[12] != "En cours":
            continue
        try:
            date_match = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if date_match >= aujourd_hui:
            continue                    # match pas encore joué
        paris.append({
            "ri":         ri,
            "date":       date_match,
            "circuit":    row[2].lower(),   # C : "atp" ou "wta"
            "joueur":     row[5],           # F : joueur parié
            "adversaire": row[6],           # G : adversaire
        })

    if not paris:
        print("  Aucun pari passé marqué 'En cours' — tout est à jour.")
        # Recalcule quand même la cascade N/O pour corriger d'éventuelles incohérences
        _reparer_colonnes_no(ws)
        mettre_a_jour_tableau_bord(sh)
        if GSHEET_ACTIF:
            generer_dashboard_json(sh)
            git_push_dashboard()
        return

    print(f"  {len(paris)} pari(s) à résoudre :\n")
    for p in paris:
        print(f"    [{p['circuit'].upper()}] {p['joueur']} vs "
              f"{p['adversaire']}  ({p['date']})")

    # ── 2. Charger les résultats (ESPN + CSV) ─────────────────
    circuits   = set(p["circuit"] for p in paris)
    espn_data  = {}
    csv_data   = {}

    for circuit in circuits:
        dates = {p["date"] for p in paris if p["circuit"] == circuit}

        print(f"\n  ESPN {circuit.upper()} :", end="  ")
        espn = _resultats_espn(circuit, dates)
        espn_data[circuit] = espn
        print(f"{len(espn)} match(s) terminé(s) trouvé(s)")

        csv = _resultats_csv(circuit)
        csv_data[circuit] = csv
        print(f"  CSV  {circuit.upper()} :  {len(csv)} match(s) en base locale")

    # ── 3. Matcher chaque pari ────────────────────────────────
    updates     = []
    non_trouves = []

    for pari in paris:
        circuit  = pari["circuit"]
        resultat = None
        source   = None

        # Source 1 : ESPN (temps réel)
        r = _chercher_resultat(pari, espn_data.get(circuit, {}))
        if r:
            resultat, source = r, "ESPN"

        # Source 2 : CSV local (pipeline hebdomadaire)
        if not resultat:
            r = _chercher_resultat(pari, csv_data.get(circuit, {}))
            if r:
                resultat, source = r, "CSV local"

        if resultat:
            updates.append({
                "ri":         pari["ri"],
                "resultat":   resultat,
                "joueur":     pari["joueur"],
                "adversaire": pari["adversaire"],
                "source":     source,
            })
        else:
            non_trouves.append(pari)

    # ── 4. Écriture col M → N et O recalculés par formules ────
    print()
    if updates:
        ws.batch_update(
            [{"range": f"M{u['ri']}", "values": [[u["resultat"]]]}
             for u in updates],
            value_input_option="USER_ENTERED",
        )
        gagnes = sum(1 for u in updates if u["resultat"] == "Gagné")
        perdus = len(updates) - gagnes
        print(f"  {len(updates)} résultat(s) enregistré(s) :")
        for u in updates:
            icone = "OK" if u["resultat"] == "Gagné" else "X"
            print(f"    {icone}  {u['joueur']} vs {u['adversaire']}"
                  f"  ->  {u['resultat']}  [{u['source']}]")
        print(f"\n  Bilan : {gagnes} gagne(s) · {perdus} perdu(s)")

        # ── Email résultats ────────────────────────────────────────
        try:
            bk_notif = sh.worksheet("Tableau de bord").acell("B7").value
        except Exception:
            bk_notif = None
        envoyer_email_resultats(updates, bk_notif)

    else:
        print("  Aucun résultat disponible pour le moment "
              "(matchs peut-être encore en cours).")

    if non_trouves:
        print(f"\n  ⚠  {len(non_trouves)} résultat(s) non trouvé(s) :")
        for p in non_trouves:
            print(f"    ?  {p['joueur']} vs {p['adversaire']}  ({p['date']})")

    # ── 5. Recalculer Gain/Perte (N) + Bankroll (O) en cascade ──
    _reparer_colonnes_no(ws)

    # ── 6. Mettre à jour tableau de bord + afficher bankroll ─────
    mettre_a_jour_tableau_bord(sh)
    if updates:
        import time; time.sleep(2)
        try:
            bk = sh.worksheet("Tableau de bord").acell("B7").value
            print(f"\n  Bankroll actuelle : {bk}")
        except Exception:
            pass

    # ── 6. Dashboard GitHub Pages — bankroll mise à jour en temps réel
    if GSHEET_ACTIF and updates:
        print("\n  Mise à jour dashboard GitHub Pages (bankroll)...")
        generer_dashboard_json(sh)
        git_push_dashboard()

    print()


# ─────────────────────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────────────────────

def mise_a_jour_pipeline():
    print("\n" + "=" * 60)
    print("  MISE A JOUR DU PIPELINE")
    print("=" * 60)
    ok = True
    ok = ok and run_script("Collecte données Sackmann 2020-2024", "tennis_data_pipeline.py")
    ok = ok and run_script("Scraping ATP + WTA 2025-2026",        "tennis_scraper.py")
    ok = ok and run_script("Rankings + Absents + Stats service",  "tennis_enrichissement.py")
    ok = ok and run_script("Calcul Elo + stats service",          "tennis_elo.py")
    ok = ok and run_script("Entraînement modèle",                 "tennis_modele.py")
    if not ok:
        print("\n  Une étape a échoué. Voir les erreurs ci-dessus.")
    return ok


# ─────────────────────────────────────────────────────────────
# GITHUB PAGES DASHBOARD
# ─────────────────────────────────────────────────────────────

BANKROLL_INIT_DASHBOARD = 100.0   # doit correspondre à D3 dans Tableau de bord
DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")


def generer_dashboard_json(sh, matchs_analyses=None):
    """
    Lit l'onglet Paris du Google Sheet et génère docs/data.json
    pour le dashboard GitHub Pages.

    Structure JSON :
      meta                 → timestamp, bankroll_initiale
      stats                → bankroll, ROI, compteurs
      bankroll_historique  → [{date, bankroll, match, joueur, resultat, gain_perte}]
      roi_cumulatif        → [{date, roi}]
      ev_par_semaine       → [{semaine, ev_moyen, nb_paris}]
      paris_recents        → 20 derniers paris (ordre anti-chrono)
      matchs_venir         → matchs ATP/WTA à venir avec cotes et proba
    """
    if not sh:
        return
    try:
        ws   = sh.worksheet("Paris")
        data = ws.get_all_values()
        if len(data) <= 1:
            print("  Dashboard : onglet Paris vide, data.json non généré")
            return

        init = BANKROLL_INIT_DASHBOARD

        # ── Index colonnes depuis le header (robuste si layout change) ──
        header = [h.strip() for h in data[0]]
        def _col(name, fallback):
            """Cherche le nom de colonne (insensible à la casse/accents)."""
            nl = name.lower()
            for i, h in enumerate(header):
                if nl in h.lower():
                    return i
            return fallback
        IDX_DATE    = _col("date",        0)
        IDX_MATCH   = _col("match",       4)
        IDX_JOUEUR  = _col("joueur",      5)
        IDX_ADV     = _col("adversaire",  6)
        IDX_BM      = _col("bookmaker",   7)
        IDX_COTE    = _col("cote",        8)
        IDX_MISE    = _col("mise",        9)
        IDX_EV      = _col("ev",         10)
        IDX_PROB    = _col("prob",       11)
        IDX_RES     = _col("sultat",     12)   # "Résultat" → cherche "sultat"
        IDX_GP      = _col("gain",       13)
        IDX_BK      = _col("bankroll",   14)

        # ── Parse toutes les lignes de données ───────────────────
        paris = []
        for row in data[1:]:
            if not row or not str(row[0]).strip():
                continue

            def _s(idx, default=""):
                return str(row[idx]).strip() if len(row) > idx else default

            def _f(idx, default=0.0):
                return _parse_float_fr(row[idx], default) if len(row) > idx else default

            def _fopt(idx):
                """Retourne float ou None si vide/tiret."""
                if len(row) <= idx:
                    return None
                raw = str(row[idx]).strip()
                if not raw or raw in ("-", "—"):
                    return None
                return _parse_float_fr(raw, None)

            paris.append({
                "date":       _s(IDX_DATE),
                "match":      _s(IDX_MATCH),
                "joueur":     _s(IDX_JOUEUR),
                "adversaire": _s(IDX_ADV),
                "bookmaker":  _s(IDX_BM),
                "cote":       _f(IDX_COTE),
                "mise":       _f(IDX_MISE),
                "ev":         _f(IDX_EV),
                "prob":       _f(IDX_PROB),
                "resultat":   _s(IDX_RES),
                "gain_perte": _fopt(IDX_GP),
                "bankroll":   _fopt(IDX_BK),
            })

        paris.sort(key=lambda x: x["date"])

        # ── Compteurs ────────────────────────────────────────────
        gagnes    = [p for p in paris if p["resultat"] == "Gagné"]
        perdus    = [p for p in paris if p["resultat"] == "Perdu"]
        en_cours  = [p for p in paris if p["resultat"] == "En cours"]
        termines  = gagnes + perdus

        nb_gagnes   = len(gagnes)
        nb_perdus   = len(perdus)
        nb_en_cours = len(en_cours)
        total       = len(paris)
        taux        = (nb_gagnes / len(termines) * 100) if termines else 0.0

        # Bankroll actuelle = dernière valeur numérique en col O
        bankroll_actuelle = init
        for p in reversed(paris):
            if p["bankroll"] is not None:
                bankroll_actuelle = p["bankroll"]
                break

        roi_global      = (bankroll_actuelle - init) / init * 100
        total_mise      = sum(p["mise"] for p in paris)
        gain_perte_net  = bankroll_actuelle - init
        ev_moyen_global = (sum(p["ev"] for p in paris) / total) if total else 0.0

        # ── Historique bankroll (paris terminés) ─────────────────
        bankroll_historique = []
        for p in paris:
            if p["resultat"] in ("Gagné", "Perdu") and p["bankroll"] is not None:
                bankroll_historique.append({
                    "date":       p["date"],
                    "bankroll":   round(p["bankroll"], 2),
                    "match":      p["match"],
                    "joueur":     p["joueur"],
                    "resultat":   p["resultat"],
                    "gain_perte": round(p["gain_perte"], 2) if p["gain_perte"] is not None else None,
                })
        if bankroll_historique:
            bankroll_historique.insert(0, {
                "date":       paris[0]["date"],
                "bankroll":   init,
                "match":      "Départ",
                "joueur":     "",
                "resultat":   "",
                "gain_perte": 0,
            })

        # ── ROI cumulatif ─────────────────────────────────────────
        roi_cumulatif = [
            {"date": h["date"], "roi": round((h["bankroll"] - init) / init * 100, 2)}
            for h in bankroll_historique
        ]

        # ── EV moyen par semaine ──────────────────────────────────
        semaines = defaultdict(lambda: {"ev_total": 0.0, "nb": 0})
        for p in paris:
            try:
                d   = datetime.strptime(p["date"], "%Y-%m-%d")
                sem = d.strftime("%Y-S%V")
                semaines[sem]["ev_total"] += p["ev"]
                semaines[sem]["nb"]       += 1
            except ValueError:
                continue
        ev_par_semaine = [
            {
                "semaine":  sem,
                "ev_moyen": round(v["ev_total"] / v["nb"], 1) if v["nb"] else 0.0,
                "nb_paris": v["nb"],
            }
            for sem, v in sorted(semaines.items())
        ]

        # ── Paris récents (20 derniers, anti-chrono) ─────────────
        paris_recents = [
            {
                "date":       p["date"],
                "match":      p["match"],
                "joueur":     p["joueur"],
                "bookmaker":  p["bookmaker"],
                "cote":       round(p["cote"], 2),
                "mise":       round(p["mise"], 2),
                "ev":         round(p["ev"], 1),
                "resultat":   p["resultat"],
                "gain_perte": round(p["gain_perte"], 2) if p["gain_perte"] is not None else None,
                "bankroll":   round(p["bankroll"], 2) if p["bankroll"] is not None else None,
            }
            for p in reversed(paris[-20:])
        ]

        # ── Assemblage JSON ───────────────────────────────────────
        output = {
            "meta": {
                "updated_at":        datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "bankroll_initiale": init,
            },
            "stats": {
                "bankroll_actuelle": round(bankroll_actuelle, 2),
                "roi_global":        round(roi_global, 2),
                "total_paris":       total,
                "paris_gagnes":      nb_gagnes,
                "paris_perdus":      nb_perdus,
                "paris_en_cours":    nb_en_cours,
                "taux_reussite":     round(taux, 1),
                "total_mise":        round(total_mise, 2),
                "gain_perte_total":  round(gain_perte_net, 2),
                "ev_moyen_global":   round(ev_moyen_global, 1),
            },
            "bankroll_historique": bankroll_historique,
            "roi_cumulatif":       roi_cumulatif,
            "ev_par_semaine":      ev_par_semaine,
            "paris_recents":       paris_recents,
        }

        # ── Matchs à venir ────────────────────────────────────────
        output["matchs_venir"] = matchs_analyses or []

        os.makedirs(DOCS_DIR, exist_ok=True)
        json_path = os.path.join(DOCS_DIR, "data.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"  Dashboard : data.json généré "
              f"({total} paris · bankroll {bankroll_actuelle:.2f}€ · ROI {roi_global:+.1f}%)")

    except Exception as e:
        print(f"  Dashboard : erreur génération JSON ({e})")


def git_push_dashboard():
    """
    Commit et push docs/data.json sur GitHub Pages.
    Silencieux si git indisponible ou aucun changement.
    """
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        ts       = datetime.now().strftime("%Y-%m-%d %H:%M")

        subprocess.run(["git", "add", "docs/"], cwd=repo_dir,
                       capture_output=True, text=True, timeout=30)

        # Vérifier s'il y a réellement des changements à committer
        status = subprocess.run(
            ["git", "status", "--short", "docs/"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30
        )
        if not status.stdout.strip():
            print("  Dashboard : aucun changement à pousser")
            return

        subprocess.run(
            ["git", "commit", "-m", f"Dashboard update {ts}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30
        )
        push = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo_dir, capture_output=True, text=True, timeout=60
        )
        if push.returncode == 0:
            print(f"  Dashboard : poussé sur GitHub Pages ✓")
        else:
            err = push.stderr.strip()[:120]
            print(f"  Dashboard : erreur git push ({err})")
    except Exception as e:
        print(f"  Dashboard : erreur git ({e})")


# ─────────────────────────────────────────────────────────────
# EXPORTS GOOGLE SHEETS — NOUVEAUX ONGLETS
# ─────────────────────────────────────────────────────────────

def _get_or_create_ws(sh, title, rows=500, cols=20):
    """Retourne l'onglet (existant ou créé)."""
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)




def exporter_matchs_venir_gsheet(sh, matchs_analyses):
    """Écrit tous les matchs analysés dans l'onglet 'Matchs_Venir'."""
    if not sh or not matchs_analyses:
        return
    try:
        ws = _get_or_create_ws(sh, "Matchs_Venir")
        header = [
            "Date", "Tournoi", "Circuit", "Surface",
            "Joueur 1", "Joueur 2", "Elo J1", "Elo J2",
            "Cote 1", "Cote 2", "BM 1", "BM 2",
            "Prob. J1", "EV J1%", "EV J2%",
        ]
        rows = [header]
        for m in matchs_analyses:
            ev1 = m.get("ev1", 0) * 100
            ev2 = m.get("ev2", 0) * 100
            rows.append([
                m.get("date", ""),
                m.get("tournoi", ""),
                m.get("circuit", ""),
                m.get("surface", ""),
                m.get("joueur1", ""),
                m.get("joueur2", ""),
                m.get("elo1", ""),
                m.get("elo2", ""),
                m.get("cote1", ""),
                m.get("cote2", ""),
                m.get("bm1", ""),
                m.get("bm2", ""),
                f"{m.get('prob1', 0)*100:.1f}%",
                f"{ev1:+.1f}%",
                f"{ev2:+.1f}%",
            ])
        ws.clear()
        ws.update("A1", rows)
        print(f"  GSheets Matchs_Venir : {len(matchs_analyses)} matchs exportés")
    except Exception as e:
        print(f"  GSheets Matchs_Venir : erreur ({e})")




# ─────────────────────────────────────────────────────────────
# ANALYSE HEBDOMADAIRE
# ─────────────────────────────────────────────────────────────

SURFACE_DEFAUT = "dur"

def analyser_semaine():
    print("\n" + "=" * 60)
    print("  ANALYSE VALUE BETS DE LA SEMAINE")
    print("=" * 60)

    modele, scaler, features = charger_modele()
    if modele is None:
        print("  Modèle non disponible. Lance le pipeline d'abord.")
        return

    elo_atp = charger_elo("atp")
    elo_wta = charger_elo("wta")
    stats   = charger_stats()
    ranks   = charger_rankings()
    absents = charger_absents()

    # Données enrichies pour le modèle
    print("  Chargement forme récente + H2H...", end=" ", flush=True)
    forme      = charger_forme_recente(n=5)
    h2h_data   = charger_h2h()
    rangs_elo_atp = charger_rangs_elo(elo_atp)
    rangs_elo_wta = charger_rangs_elo(elo_wta)
    print(f"{len(forme)} joueurs · {len(h2h_data)//2} paires H2H")

    # ── Bankroll dynamique depuis Google Sheets ───────────────
    sh_early = init_gsheet() if GSHEET_ACTIF else None
    bankroll_gsheet = lire_bankroll_gsheet(sh_early)
    if bankroll_gsheet and bankroll_gsheet > 0:
        bankroll = bankroll_gsheet
        print(f"  Bankroll actuelle (Google Sheets) : {bankroll:.2f}EUR")
    else:
        bankroll = BANKROLL
        print(f"  Bankroll (config) : {bankroll:.2f}EUR  "
              f"(Google Sheets non disponible)")

    # Index normalisé pour la correspondance de noms
    tous_joueurs = {**elo_atp, **elo_wta}
    noms_norm = {normaliser(j): j for j in tous_joueurs.keys()}

    events = recuperer_matchs_et_cotes()
    if not events:
        print("  Aucun match récupéré.")
        return

    lignes          = []
    value_bets      = []
    value_bets_email = []  # Uniquement EV > SEUIL_ALERTE
    matchs_analyses  = []  # Tous les matchs analysés (pour export GSheets + dashboard)

    ts_affichage = datetime.now().strftime("%d/%m/%Y %H:%M")
    lignes.append(f"RAPPORT VALUE BETS — {ts_affichage}")
    lignes.append("=" * 60)
    lignes.append(f"Matchs analysés : {len(events)}")
    lignes.append("")

    for event in events:
        nom1_api = event.get("home_team", "")
        nom2_api = event.get("away_team", "")
        date_str = event.get("commence_time", "")[:10]
        circuit  = event.get("_circuit", "atp")

        # Cotes h2h : meilleure parmi Betclic/Unibet/Winamax, sinon meilleure globale
        cote1, bm1 = meilleure_cote(event, 0, BOOKMAKERS_CIBLES)
        cote2, bm2 = meilleure_cote(event, 1, BOOKMAKERS_CIBLES)
        if cote1 <= 1.0 or cote2 <= 1.0:
            circuit_label_tmp = "WTA" if circuit == "wta" else "ATP"
            lignes.append(f"[{circuit_label_tmp}] {nom1_api} vs {nom2_api} [{date_str}]")
            lignes.append("  (cotes non disponibles sur les bookmakers configurés)")
            lignes.append("")
            continue

        # Correspondance noms / Elo
        j1 = trouver_joueur(nom1_api, noms_norm)
        j2 = trouver_joueur(nom2_api, noms_norm)
        elo_defaut = {"elo_global": ELO_INCONNU, "elo_dur": ELO_INCONNU,
                      "elo_terre": ELO_INCONNU, "elo_gazon": ELO_INCONNU}
        elo_j1   = tous_joueurs.get(j1, elo_defaut) if j1 else elo_defaut
        elo_j2   = tous_joueurs.get(j2, elo_defaut) if j2 else elo_defaut
        stats_j1 = stats.get(j1) if j1 else None
        stats_j2 = stats.get(j2) if j2 else None

        circuit_label = "WTA" if circuit == "wta" else "ATP"

        eg1_test = elo_j1.get("elo_global", ELO_INCONNU)
        eg2_test = elo_j2.get("elo_global", ELO_INCONNU)
        elo_valide = (eg1_test > ELO_INCONNU or eg2_test > ELO_INCONNU) and \
                     not (circuit == "wta" and eg1_test <= ELO_INCONNU
                          and eg2_test <= ELO_INCONNU + 50)
        if j1 is None and j2 is None:
            elo_valide = False
        if circuit == "wta" and (j1 is None or eg1_test <= ELO_INCONNU) \
                             and (j2 is None or eg2_test <= ELO_INCONNU):
            elo_valide = False

        if not elo_valide:
            lignes.append(f"[{circuit_label}] {nom1_api} vs {nom2_api} [{date_str}]")
            lignes.append("  (données Elo insuffisantes — relancer tennis_elo.py --wta)")
            lignes.append("")
            continue

        # ── H2H (calculé ici car utilisé par predire) ─────────
        h2h_entry = h2h_data.get((j1, j2)) if j1 and j2 else None
        h2h_val   = (h2h_entry[0] / h2h_entry[1]) if h2h_entry else 0.5
        if h2h_entry and h2h_entry[1] >= 2:
            h2h_str = f"{h2h_entry[0]}/{h2h_entry[1]} victoires {j1 or nom1_api}"
        else:
            h2h_str = "Pas d'historique"

        prob1, eg1, eg2, es1, es2 = predire(
            modele, scaler, features, elo_j1, elo_j2, SURFACE_DEFAUT,
            stats_j1, stats_j2, h2h=h2h_val
        )
        prob2 = 1 - prob1

        ev1 = prob1 * cote1 - 1
        ev2 = prob2 * cote2 - 1

        matchs_analyses.append({
            "date":    date_str,
            "tournoi": event.get("_tournament", ""),
            "circuit": circuit_label,
            "surface": SURFACE_DEFAUT,
            "joueur1": j1 or nom1_api,
            "joueur2": j2 or nom2_api,
            "elo1":    round(eg1),
            "elo2":    round(eg2),
            "cote1":   round(cote1, 2),
            "cote2":   round(cote2, 2),
            "bm1":     bm1,
            "bm2":     bm2,
            "prob1":   round(prob1, 3),
            "prob2":   round(prob2, 3),
            "ev1":     round(ev1, 3),
            "ev2":     round(ev2, 3),
        })

        rangs_elo_cur = rangs_elo_wta if circuit == "wta" else rangs_elo_atp
        rang1 = _get_rang(j1, ranks, rangs_elo_cur)
        rang2 = _get_rang(j2, ranks, rangs_elo_cur)
        abs1  = " ABSENT?" if j1 and j1 in absents else ""
        abs2  = " ABSENT?" if j2 and j2 in absents else ""
        tag1  = " [inconnu]" if not j1 else ""
        tag2  = " [inconnu]" if not j2 else ""

        # ── Forme récente (5 derniers matchs) ─────────────────
        forme1 = forme.get(j1) if j1 else None
        forme2 = forme.get(j2) if j2 else None
        f1_str = f"{forme1*100:.0f}%/5m" if forme1 is not None else "n/a"
        f2_str = f"{forme2*100:.0f}%/5m" if forme2 is not None else "n/a"

        lignes.append(f"[{circuit_label}] {nom1_api}{tag1} vs {nom2_api}{tag2} [{date_str}]")
        lignes.append(f"  {'':28} {nom1_api:<18} {nom2_api}")
        lignes.append(f"  {'Rang ' + circuit_label:<28} #{rang1:<18} #{rang2}")
        lignes.append(f"  {'Elo global':<28} {round(eg1):<18} {round(eg2)}")
        lignes.append(f"  {'Elo surface (' + SURFACE_DEFAUT + ')':<28} {round(es1):<18} {round(es2)}")
        lignes.append(f"  {'Forme récente':<28} {f1_str:<18} {f2_str}")
        lignes.append(f"  {'H2H (depuis 2020)':<28} {h2h_str}")
        if abs1 or abs2:
            lignes.append(f"  ATTENTION : {nom1_api}{abs1} | {nom2_api}{abs2}")

        # ── Tableau bookmakers h2h ────────────────────────────
        lignes.append(f"  {'─'*56}")
        lignes.append(f"  {'Bookmaker':<28} {nom1_api:<18} {nom2_api}")
        for ligne_bm in tableau_cotes(event, nom1_api, nom2_api):
            lignes.append(ligne_bm)
        lignes.append(f"  {'─'*56}")

        # Accumulateur de tous les value bets pour CE match (tous marchés)
        vbs_ce_match = []

        def _kelly_mise(prob, cote):
            ev = prob * cote - 1
            if ev <= 0:
                return 0.0
            mise = bankroll * (ev / (cote - 1)) * FRACTION_KELLY
            return max(MISE_MIN, min(mise, MISE_MAX))  # min 5€ / max 50€

        def _vb_dict(type_pari, label_joueur, adversaire, prob, cote, bm_used, ev):
            mise = _kelly_mise(prob, cote)
            return {
                "date":        date_str,
                "tournoi":     event.get("_tournament", ""),
                "circuit":     circuit_label,
                "surface":     SURFACE_DEFAUT,
                "match":       f"{nom1_api} vs {nom2_api}",
                "joueur":      label_joueur,
                "adversaire":  adversaire,
                "type_pari":   type_pari,
                "cote":        round(cote, 2),
                "bookmaker":   bm_used,
                "prob_modele": round(prob, 3),
                "ev":          round(ev, 3),
                "mise":        round(mise, 2),
                "_event_id":   event.get("id", ""),
            }

        # ── MARCHÉ 1 : Vainqueur (h2h) ───────────────────────
        lignes.append(f"\n  [Vainqueur]")
        lignes.append(f"  {'Prob. modèle':<28} {prob1*100:.1f}%{'':<14} {prob2*100:.1f}%")
        lignes.append(f"  {'Cote retenue':<28} {cote1:.2f} ({bm1:<10}) {cote2:.2f} ({bm2})")
        lignes.append(f"  {'Expected Value':<28} {ev1*100:+.1f}%{'':<13} {ev2*100:+.1f}%")

        for nom_api, adv, prob, cote, ev, bm_used in [
            (nom1_api, nom2_api, prob1, cote1, ev1, bm1),
            (nom2_api, nom1_api, prob2, cote2, ev2, bm2),
        ]:
            if ev >= SEUIL_VALUE and _kelly_mise(prob, cote) > 0:
                emoji = ">>>" if ev >= SEUIL_ALERTE else "  >"
                mise  = _kelly_mise(prob, cote)
                lignes.append(
                    f"\n  {emoji} VALUE BET : {nom_api} @ {cote:.2f} sur {bm_used} | "
                    f"EV {ev*100:+.1f}% | Mise {mise:.2f}EUR | +{mise*(cote-1):.2f}EUR si win"
                )
                vbs_ce_match.append(_vb_dict("Vainqueur", nom_api, adv, prob, cote, bm_used, ev))

        # ── MARCHÉ 2 : Handicap de jeux (spreads) ────────────
        cotes_hc = extraire_cotes_marche(event, "spreads", BOOKMAKERS_CIBLES)
        if cotes_hc:
            lignes.append(f"\n  [Handicap de jeux]")
            for oc_name, cote_hc, bm_hc, point_hc in cotes_hc:
                if point_hc is None:
                    continue
                # Déterminer à quel joueur correspond cet outcome
                is_j1 = any(tok.lower() in normaliser(oc_name)
                            for tok in normaliser(nom1_api).split()
                            if len(tok) > 3)
                p_match_ref = prob1 if is_j1 else prob2
                prob_hc = prob_handicap_jeux(p_match_ref, point_hc)
                ev_hc   = prob_hc * cote_hc - 1
                emoji   = ">>>" if ev_hc >= SEUIL_ALERTE else ("  >" if ev_hc >= SEUIL_VALUE else "   ")
                lignes.append(
                    f"  {emoji} {oc_name} ({point_hc:+.1f}) @ {cote_hc:.2f} ({bm_hc}) | "
                    f"Prob {prob_hc*100:.1f}% | EV {ev_hc*100:+.1f}%"
                )
                if ev_hc >= SEUIL_VALUE and _kelly_mise(prob_hc, cote_hc) > 0:
                    adv = nom2_api if is_j1 else nom1_api
                    label = f"{oc_name} ({point_hc:+.1f})"
                    vbs_ce_match.append(
                        _vb_dict(f"Handicap {point_hc:+.1f}", label, adv,
                                 prob_hc, cote_hc, bm_hc, ev_hc)
                    )

        # ── MARCHÉ 3 : Total de jeux (totals) ────────────────
        cotes_tot = extraire_cotes_marche(event, "totals", BOOKMAKERS_CIBLES)
        # Séparer sets (point <= 4) et jeux (point >= 10)
        cotes_jeux = [(n, c, bm, pt) for n, c, bm, pt in cotes_tot
                      if pt is not None and pt >= 10]
        cotes_sets  = [(n, c, bm, pt) for n, c, bm, pt in cotes_tot
                       if pt is not None and pt <= 4]

        if cotes_jeux:
            lignes.append(f"\n  [Total de jeux]")
            for oc_name, cote_tot_, bm_tot, point_tot in cotes_jeux:
                over = "over" in oc_name.lower()
                prob_tot = prob_total_jeux(prob1, point_tot, over)
                ev_tot   = prob_tot * cote_tot_ - 1
                emoji    = ">>>" if ev_tot >= SEUIL_ALERTE else ("  >" if ev_tot >= SEUIL_VALUE else "   ")
                dir_label = "Over" if over else "Under"
                lignes.append(
                    f"  {emoji} {dir_label} {point_tot} @ {cote_tot_:.2f} ({bm_tot}) | "
                    f"Prob {prob_tot*100:.1f}% | EV {ev_tot*100:+.1f}%"
                )
                if ev_tot >= SEUIL_VALUE and _kelly_mise(prob_tot, cote_tot_) > 0:
                    label = f"{dir_label} {point_tot} jeux"
                    vbs_ce_match.append(
                        _vb_dict(f"Total {dir_label} {point_tot}", label,
                                 f"{nom1_api} vs {nom2_api}", prob_tot,
                                 cote_tot_, bm_tot, ev_tot)
                    )

        # ── MARCHÉ 4 : Nombre de sets (2-0 / 2-1) ────────────
        p2sets, p3sets = prob_nombre_sets(prob1)
        if cotes_sets:
            lignes.append(f"\n  [Nombre de sets]")
            for oc_name, cote_s, bm_s, point_s in cotes_sets:
                over = "over" in oc_name.lower()
                prob_s = p3sets if over else p2sets
                ev_s   = prob_s * cote_s - 1
                emoji  = ">>>" if ev_s >= SEUIL_ALERTE else ("  >" if ev_s >= SEUIL_VALUE else "   ")
                dir_s  = "Over" if over else "Under"
                lignes.append(
                    f"  {emoji} {dir_s} {point_s} sets @ {cote_s:.2f} ({bm_s}) | "
                    f"Prob {prob_s*100:.1f}% | EV {ev_s*100:+.1f}%"
                )
                if ev_s >= SEUIL_VALUE and _kelly_mise(prob_s, cote_s) > 0:
                    label = f"{dir_s} {point_s} sets"
                    vbs_ce_match.append(
                        _vb_dict(f"Sets {dir_s} {point_s}", label,
                                 f"{nom1_api} vs {nom2_api}", prob_s,
                                 cote_s, bm_s, ev_s)
                    )
        else:
            # Pas de cotes API pour les sets → afficher prob modèle seul
            lignes.append(
                f"\n  [Nombre de sets — prob. modèle uniquement, pas de cotes API]"
            )
            lignes.append(
                f"  Match en 2 sets : {p2sets*100:.1f}%   |   "
                f"Match en 3 sets : {p3sets*100:.1f}%"
            )

        # ── Meilleur pari ce match → GSheets + email ─────────
        if vbs_ce_match:
            best = max(vbs_ce_match, key=lambda x: x["ev"])
            alerte_str = " <<< MEILLEUR PARI CE MATCH" if best["ev"] >= SEUIL_ALERTE \
                         else " << MEILLEUR PARI CE MATCH"
            lignes.append(
                f"\n  {alerte_str} : [{best['type_pari']}] "
                f"{best['joueur']} @ {best['cote']:.2f} ({best['bookmaker']}) | "
                f"EV {best['ev']*100:+.1f}% | Mise {best['mise']:.2f}EUR"
            )
            value_bets.append(best)
            if best["ev"] >= SEUIL_ALERTE:
                value_bets_email.append(best)

        lignes.append("")

    # ── Résumé ────────────────────────────────────────────────
    lignes.append("=" * 60)
    lignes.append(f"VALUE BETS IDENTIFIES : {len(value_bets)}")
    lignes.append("=" * 60)
    if value_bets:
        total_mise = sum(vb["mise"] for vb in value_bets)
        for vb in value_bets:
            alerte = " <<< ALERTE EV>10%" if vb["ev"] >= SEUIL_ALERTE else ""
            lignes.append(
                f"  [{vb['circuit']}] {vb['match']} [{vb['date']}]"
            )
            lignes.append(
                f"  Type : {vb.get('type_pari','Vainqueur'):<22} "
                f"Pari : {vb['joueur']}"
            )
            lignes.append(
                f"  Cote {vb['cote']:.2f} sur {vb['bookmaker']} | "
                f"Prob {vb['prob_modele']*100:.1f}% | "
                f"EV +{vb['ev']*100:.1f}% | Mise {vb['mise']:.2f}EUR{alerte}"
            )
            lignes.append("")
        lignes.append(
            f"  Total mises : {total_mise:.1f}EUR / {bankroll:.0f}EUR "
            f"({total_mise/bankroll*100:.1f}%)"
        )
    else:
        lignes.append("  Aucun value bet cette semaine.")

    rapport = "\n".join(lignes)
    print("\n" + rapport)

    # Sauvegarde rapport texte
    ts     = datetime.now().strftime("%Y%m%d_%H%M")
    chemin = os.path.join(RAPPORT_DIR, f"rapport_{ts}.txt")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(rapport)
    print(f"\n  Rapport sauvegardé : {chemin}")

    # ── Google Sheets ──────────────────────────────────────────
    if GSHEET_ACTIF:
        print("\n  Mise à jour Google Sheets...")
        sh = sh_early or init_gsheet()   # réutilise la connexion déjà ouverte
        if sh:
            mettre_a_jour_resultats_gsheet(sh)
            enregistrer_paris_gsheet(sh, value_bets)
            mettre_a_jour_tableau_bord(sh)
            exporter_matchs_venir_gsheet(sh, matchs_analyses)

    # Email déclenché si au moins 1 bet EV > SEUIL_ALERTE,
    # mais le tableau contient TOUS les value bets (EV > SEUIL_VALUE)
    if value_bets_email:
        nb_alerte = len(value_bets_email)
        nb_total  = len(value_bets)
        print(f"\n  {nb_alerte} value bet(s) EV>{SEUIL_ALERTE*100:.0f}% → envoi email "
              f"({nb_total} paris au total)...")
        envoyer_email_alerte(value_bets, chemin, bankroll=bankroll)
    elif EMAIL_ACTIF:
        print(f"  Aucun value bet >{SEUIL_ALERTE*100:.0f}% → pas d'email")

    # ── Dashboard GitHub Pages ──────────────────────────────────
    if GSHEET_ACTIF:
        print("\n  Génération dashboard GitHub Pages...")
        sh_dash = sh_early or init_gsheet()
        if sh_dash:
            generer_dashboard_json(sh_dash, matchs_analyses=matchs_analyses)
            git_push_dashboard()


# ─────────────────────────────────────────────────────────────
# PLANIFICATEUR (Windows) / CRON (Linux — PythonAnywhere)
# ─────────────────────────────────────────────────────────────

def configurer_tache_linux():
    """Affiche les instructions cron pour PythonAnywhere."""
    script_path = os.path.abspath(__file__)
    py = "python3.11"  # adapter selon la version sur PythonAnywhere

    print("\n  ── Déploiement PythonAnywhere ──")
    print("  Ajoute ces 4 lignes dans l'onglet 'Tasks' de PythonAnywhere :\n")

    taches = [
        ("Lundi 07:00", f"{py} {script_path}",           "pipeline complet (data + elo + modele + analyse)"),
        ("Quotidien 08:00", f"{py} {script_path} --analyse", "analyse matin + email"),
        ("Quotidien 13:00", f"{py} {script_path} --analyse", "analyse midi + email"),
        ("Quotidien 19:00", f"{py} {script_path} --analyse", "analyse soir + email"),
    ]
    for horaire, cmd, desc in taches:
        print(f"  {horaire:<18} {cmd}")
        print(f"  {'':18} # {desc}\n")

    print("  Logs → tennis_data/rapports/  (un fichier .txt par exécution)")
    print("  Pour vérifier : tail -f ~/tennis-betting/tennis_data/rapports/rapport_*.txt")


def configurer_tache_windows():
    script_path = os.path.abspath(__file__)
    python_path = sys.executable

    # ── Supprimer l'ancienne tâche ────────────────────────────────
    print("\nSuppression de l'ancienne tâche TennisBetting_Auto (si existante)...")
    subprocess.run(
        'schtasks /delete /tn "TennisBetting_Auto" /f',
        shell=True, capture_output=True, text=True
    )

    # ── Créer les 3 nouvelles tâches quotidiennes ─────────────────
    taches = [
        ("TennisBetting_Matin", "08:00", "matin"),
        ("TennisBetting_Midi",  "13:00", "midi"),
        ("TennisBetting_Soir",  "19:00", "soir"),
    ]

    ok = True
    for nom_tache, heure, label in taches:
        cmd = (
            f'schtasks /create /tn "{nom_tache}" '
            f'/tr "\\"{python_path}\\" -X utf8 \\"{script_path}\\" --analyse" '
            f'/sc daily /st {heure} '
            f'/f /rl HIGHEST'
        )
        print(f"\n  Création : {nom_tache} ({label} — {heure})...")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            print(f"    OK")
        else:
            print(f"    Erreur : {r.stderr.strip()}")
            ok = False

    # ── Tâche pipeline hebdomadaire (lundi uniquement) ────────────
    nom_pipeline = "TennisBetting_Pipeline"
    cmd_pipeline = (
        f'schtasks /create /tn "{nom_pipeline}" '
        f'/tr "\\"{python_path}\\" -X utf8 \\"{script_path}\\"" '
        f'/sc weekly /d MON /st 07:00 '
        f'/f /rl HIGHEST'
    )
    print(f"\n  Création : {nom_pipeline} (pipeline complet — lundi 07:00)...")
    r2 = subprocess.run(cmd_pipeline, shell=True, capture_output=True, text=True)
    if r2.returncode == 0:
        print(f"    OK")
    else:
        print(f"    Erreur : {r2.stderr.strip()}")
        ok = False

    print()
    if ok:
        print("  Toutes les tâches créées avec succès !")
        print("  Planning :")
        print("    Lun 07:00 — TennisBetting_Pipeline (mise à jour complète)")
        print("    Quotidien 08:00 — TennisBetting_Matin (analyse + email)")
        print("    Quotidien 13:00 — TennisBetting_Midi  (analyse + email)")
        print("    Quotidien 19:00 — TennisBetting_Soir  (analyse + email)")
        print("\n  Pour supprimer :")
        for nom_tache, _, _ in taches:
            print(f'    schtasks /delete /tn "{nom_tache}" /f')
        print(f'    schtasks /delete /tn "{nom_pipeline}" /f')
    else:
        print("  Certaines tâches ont échoué => relancer en tant qu'Administrateur")


# ─────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tennis Auto v2 — ATP + WTA")
    parser.add_argument("--setup",      action="store_true",
                        help="Crée les tâches planifiées (Windows schtasks ou instructions cron Linux)")
    parser.add_argument("--pipeline",   action="store_true",
                        help="Pipeline complet : data + elo + modèle + analyse (alias sans flag)")
    parser.add_argument("--analyse",    action="store_true",
                        help="Analyse seule, sans relancer le pipeline")
    parser.add_argument("--resultats",  action="store_true",
                        help="Récupère les résultats des matchs passés et met à jour Gagné/Perdu")
    args = parser.parse_args()

    print("=" * 60)
    print("  TENNIS AUTO v2 — Système de paris automatisé")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    if args.setup:
        if sys.platform == "win32":
            configurer_tache_windows()
        else:
            configurer_tache_linux()
    elif args.resultats:
        recuperer_resultats()
    elif args.analyse:
        analyser_semaine()
    else:
        # --pipeline ou aucun flag : pipeline complet
        mise_a_jour_pipeline()
        analyser_semaine()
