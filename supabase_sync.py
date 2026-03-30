"""
=============================================================
  SUPABASE SYNC — Push value bets vers l'API SaaS
=============================================================

Appelé depuis tennis_auto.py après chaque analyse.
Écrit les value bets dans la table Supabase `value_bets`
pour qu'ils s'affichent dans le dashboard des abonnés.
"""

import os
import requests
import json
import math
from datetime import datetime, timezone, timedelta


# ── Configuration ─────────────────────────────────────────
SUPABASE_URL             = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY     = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
VALUE_BET_EXPIRY_HOURS   = 24   # les value bets expirent après 24h


def _headers():
    return {
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def push_value_bets(bets: list[dict]) -> dict:
    """
    Pousse une liste de value bets dans Supabase.

    Chaque bet doit avoir les clés :
        player1, player2, surface, tournament, round (optionnel),
        ev (float, ex: 14.2 pour 14.2%),
        bookmaker, cote (float), prob_model (float, 0-100), kelly (float, %)

    Retourne {"inserted": N, "errors": [...]}
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[Supabase] ⚠  Variables SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY manquantes — sync ignorée.")
        return {"inserted": 0, "errors": ["credentials manquants"]}

    if not bets:
        print("[Supabase] Aucun value bet à pousser.")
        return {"inserted": 0, "errors": []}

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=VALUE_BET_EXPIRY_HOURS)).isoformat()

    rows = []
    for b in bets:
        ev_pct   = float(b.get("ev",   0))
        kelly    = float(b.get("kelly", 0))
        prob     = float(b.get("prob_model", 50))
        cote     = float(b.get("cote", 0))

        row = {
            "player1":    str(b.get("player1", "")),
            "player2":    str(b.get("player2", "")),
            "surface":    str(b.get("surface", "Dur")),
            "tournament": str(b.get("tournament", "")),
            "round":      str(b.get("round", "")) or None,
            "ev":         round(ev_pct, 2),
            "bookmaker":  str(b.get("bookmaker", "")),
            "cote":       round(cote, 2),
            "prob_model": round(prob, 2),
            "kelly":      round(kelly, 2),
            "expires_at": expires_at,
        }
        rows.append(row)

    url = f"{SUPABASE_URL}/rest/v1/value_bets"

    # Supprimer les doublons du même match avant d'insérer
    for row in rows:
        requests.delete(
            url,
            headers=_headers(),
            params={
                "player1": f"eq.{row['player1']}",
                "player2": f"eq.{row['player2']}",
                "tournament": f"eq.{row['tournament']}",
            },
            timeout=10,
        )

    resp = requests.post(url, headers=_headers(), data=json.dumps(rows), timeout=15)

    if resp.status_code in (200, 201):
        inserted = len(resp.json()) if resp.text.strip() else len(rows)
        print(f"[Supabase] ✅ {inserted} value bet(s) insérés (doublons supprimés).")
        return {"inserted": inserted, "errors": []}
    else:
        print(f"[Supabase] ❌ Erreur {resp.status_code}: {resp.text[:200]}")
        return {"inserted": 0, "errors": [resp.text]}


def clear_expired_bets() -> None:
    """Supprime les value bets dont expires_at est dépassé."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/value_bets"
    params = {"expires_at": f"lt.{datetime.now(timezone.utc).isoformat()}"}
    resp = requests.delete(url, headers=_headers(), params=params, timeout=10)
    if resp.status_code in (200, 204):
        print("[Supabase] 🧹 Anciens value bets supprimés.")
    else:
        print(f"[Supabase] ⚠  Erreur suppression: {resp.status_code}")


def get_active_subscriber_emails() -> list[str]:
    """
    Retourne la liste des emails de tous les abonnés actifs
    (plans starter/pro/elite + trial en cours).
    Utilise l'Admin API Supabase pour accéder aux emails auth.users.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return []

    admin_headers = {
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }

    # 1. Récupérer les profils actifs
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/profiles",
            headers=_headers(),
            params={"select": "user_id,plan,trial_ends_at"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Supabase] get_active_subscriber_emails: erreur profiles {resp.status_code}")
            return []
        profiles = resp.json()
    except Exception as e:
        print(f"[Supabase] get_active_subscriber_emails: {e}")
        return []

    now = datetime.now(timezone.utc)
    active_ids = set()
    for p in profiles:
        plan = p.get("plan", "")
        if plan in ("starter", "pro", "elite"):
            active_ids.add(p["user_id"])
        elif plan == "trial":
            trial_ends = p.get("trial_ends_at")
            if trial_ends:
                try:
                    if datetime.fromisoformat(trial_ends.replace("Z", "+00:00")) > now:
                        active_ids.add(p["user_id"])
                except Exception:
                    pass

    if not active_ids:
        return []

    # 2. Récupérer tous les users en une seule requête Admin
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers=admin_headers,
            params={"per_page": 1000, "page": 1},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[Supabase] Admin users: erreur {resp.status_code}")
            return []
        users = resp.json().get("users", [])
    except Exception as e:
        print(f"[Supabase] Admin users: {e}")
        return []

    emails = [
        u["email"] for u in users
        if u.get("id") in active_ids and u.get("email")
    ]
    return emails


def get_pending_bets() -> list[dict]:
    """
    Récupère tous les paris en attente (result='pending') de tous les utilisateurs.
    Nécessite SUPABASE_SERVICE_ROLE_KEY (bypass RLS).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/bet_history"
    try:
        resp = requests.get(
            url,
            headers=_headers(),
            params={
                "result": "eq.pending",
                "select": "id,user_id,player,tournament,surface,cote,amount,created_at",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        print(f"[Supabase] Erreur get_pending_bets: {resp.status_code}")
        return []
    except Exception as e:
        print(f"[Supabase] get_pending_bets exception: {e}")
        return []


def update_bet_result(bet_id: str, result: str, profit: float) -> bool:
    """Met à jour result et profit d'un pari dans bet_history."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False
    url = f"{SUPABASE_URL}/rest/v1/bet_history"
    headers = {**_headers(), "Prefer": "return=minimal"}
    try:
        resp = requests.patch(
            url,
            headers=headers,
            params={"id": f"eq.{bet_id}"},
            data=json.dumps({"result": result, "profit": round(profit, 2)}),
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] update_bet_result exception: {e}")
        return False


def format_bet_for_supabase(
    player1: str,
    player2: str,
    surface: str,
    tournament: str,
    ev_pct: float,        # ex: 14.2 (déjà en %)
    bookmaker: str,
    cote: float,
    prob_model: float,    # ex: 54.8 (déjà en %)
    kelly_pct: float,     # ex: 2.3 (déjà en %)
    round_: str = "",
) -> dict:
    """Helper pour formater un pari avant push_value_bets()."""
    return {
        "player1":    player1,
        "player2":    player2,
        "surface":    surface,
        "tournament": tournament,
        "round":      round_,
        "ev":         round(ev_pct, 2),
        "bookmaker":  bookmaker,
        "cote":       round(cote, 2),
        "prob_model": round(prob_model, 2),
        "kelly":      round(kelly_pct, 2),
    }
