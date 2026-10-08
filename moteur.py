"""
MOTEUR DE PAPER TRADING — Stratégie Momentum S&P 500 (V5)
=========================================================

Exécuté automatiquement chaque semaine par GitHub Actions (voir
.github/workflows/moteur.yml). À chaque exécution :
  1. Télécharge les cours récents des membres ACTUELS du S&P 500 + le SPY.
  2. Si un nouveau mois est terminé depuis le dernier rééquilibrage :
     calcule les signaux à la clôture de fin de mois et rééquilibre le
     portefeuille virtuel au dernier cours connu (frais de 0,10 % inclus).
  3. Valorise le portefeuille et le SPY de référence.
  4. Écrit l'état dans le dossier etat/ et un résumé dans etat/rapport.json,
     que l'agent Claude lit pour rédiger le rapport hebdomadaire.

Règles de la stratégie (identiques au backtest V5, figées) :
  - Univers : membres actuels du S&P 500
  - Signal : rendement de t-6 mois à t-1 mois, divisé par la volatilité
    quotidienne annualisée sur 6 mois (« momentum ajusté du risque »)
  - Exclusion des titres aux cours figés (> 30 % de jours sans variation)
  - 50 titres à poids égal, au maximum 15 par secteur GICS (30 %)
  - Zone de tolérance : un titre détenu est gardé tant qu'il reste dans le top 100
  - Rééquilibrage mensuel, sans filtre de tendance

Simplifications assumées : les cours sont en dollars et le capital est noté
en euros sans conversion de change ; les dividendes sont ignorés pour la
stratégie comme pour le SPY (comparaison équitable).

Utilisation locale :
    python moteur.py              # exécution réelle
    python moteur.py --demo       # test complet sur données simulées
"""

import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# PARAMÈTRES (figés : ce sont ceux du backtest V5)
# ----------------------------------------------------------------------------
CAPITAL_INITIAL = 10_000.0
N_TITRES = 50
LOOKBACK_MOIS = 6
SKIP_MOIS = 1
PART_MAX_SECTEUR = 0.30
ZONE_TOLERANCE = 2.0
SEUIL_FIGE = 0.30
FRAIS = 0.001
BENCHMARK = "SPY"

DOSSIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etat")
URL_MEMBRES = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500.csv"
SECTEURS_FR = {
    "Information Technology": "IT", "Health Care": "Santé", "Financials": "Finance",
    "Consumer Discretionary": "Conso. cyclique", "Consumer Staples": "Conso. de base",
    "Communication Services": "Communication", "Industrials": "Industrie",
    "Energy": "Énergie", "Utilities": "Services publics", "Real Estate": "Immobilier",
    "Materials": "Matériaux",
}


# ----------------------------------------------------------------------------
# DONNÉES
# ----------------------------------------------------------------------------
def membres_actuels():
    """Membres actuels du S&P 500 et leur secteur (copie de secours dans etat/)."""
    secours = os.path.join(DOSSIER, "membres_sp500.csv")
    try:
        req = urllib.request.Request(URL_MEMBRES, headers={"User-Agent": "Mozilla/5.0"})
        brut = urllib.request.urlopen(req, timeout=30).read().decode()
        df = pd.read_csv(io.StringIO(brut))
        df.to_csv(secours, index=False)
    except Exception as e:
        print(f"Liste en ligne indisponible ({e}) -> copie locale")
        df = pd.read_csv(secours)
    df["ticker"] = df["Symbol"].str.replace(".", "-", regex=False)
    return dict(zip(df["ticker"], df["GICS Sector"].map(SECTEURS_FR).fillna("Autre")))


def telecharger_cours(tickers):
    import yfinance as yf
    data = yf.download(sorted(set(tickers) | {BENCHMARK}), period="400d", auto_adjust=False,
                       progress=False, group_by="column", threads=True)
    close = data["Close"].dropna(how="all")
    adj = data["Adj Close"].reindex(close.index)
    return close, adj


# ----------------------------------------------------------------------------
# SIGNAUX ET SÉLECTION
# ----------------------------------------------------------------------------
def scores_fin_de_mois(adj, date_fin_mois, tickers):
    """Momentum ajusté du risque, calculé uniquement avec les données <= fin de mois."""
    a = adj.loc[:date_fin_mois, [t for t in tickers if t in adj.columns]]
    m = a.resample("ME").last()
    if len(m) < LOOKBACK_MOIS + 1:
        raise ValueError("Historique insuffisant pour calculer le momentum")
    mom = m.iloc[-1 - SKIP_MOIS] / m.iloc[-1 - LOOKBACK_MOIS] - 1
    r = a.pct_change(fill_method=None)
    jours = 21 * LOOKBACK_MOIS
    vol = r.tail(jours).std() * np.sqrt(252)
    nb_ok = r.tail(jours).notna().sum()
    fige = (r.tail(63) == 0).mean() > SEUIL_FIGE
    sc = mom / vol.replace(0, np.nan)
    sc = sc[(nb_ok >= jours * 0.8) & ~fige]
    return sc.dropna().sort_values(ascending=False)


def selectionner(classement, secteurs, detenus):
    max_sec = int(np.ceil(N_TITRES * PART_MAX_SECTEUR))
    choix, compte = [], {}

    def ajouter(t):
        s = secteurs.get(t, "Autre")
        if t in choix or compte.get(s, 0) >= max_sec:
            return
        choix.append(t)
        compte[s] = compte.get(s, 0) + 1

    for t in classement.index[: int(N_TITRES * ZONE_TOLERANCE)]:
        if t in detenus and len(choix) < N_TITRES:
            ajouter(t)
    for t in classement.index:
        if len(choix) >= N_TITRES:
            break
        ajouter(t)
    return choix


# ----------------------------------------------------------------------------
# ÉTAT DU PORTEFEUILLE
# ----------------------------------------------------------------------------
def charger_etat():
    f = os.path.join(DOSSIER, "etat.json")
    if os.path.exists(f):
        with open(f) as fh:
            return json.load(fh)
    return None


def sauver(etat, historique, transactions):
    os.makedirs(DOSSIER, exist_ok=True)
    with open(os.path.join(DOSSIER, "etat.json"), "w") as fh:
        json.dump(etat, fh, indent=1, ensure_ascii=False)
    historique.to_csv(os.path.join(DOSSIER, "historique.csv"), index=False)
    transactions.to_csv(os.path.join(DOSSIER, "transactions.csv"), index=False)


def lire_csv(nom, colonnes):
    f = os.path.join(DOSSIER, nom)
    return pd.read_csv(f) if os.path.exists(f) else pd.DataFrame(columns=colonnes)


def dernier_cours(close, t, avant=None):
    s = close[t].dropna() if t in close.columns else pd.Series(dtype=float)
    if avant is not None:
        s = s.loc[:avant]
    return (float(s.iloc[-1]), s.index[-1]) if len(s) else (None, None)


def valoriser(etat, close):
    total, lignes, alertes = etat["liquidites"], [], []
    date_ref = close.index[-1]
    for t, pos in etat["positions"].items():
        px, d = dernier_cours(close, t)
        if px is None:
            px, d = pos["dernier_prix"], None
            alertes.append(f"{t} : aucun cours disponible, dernier prix connu utilisé")
        elif (date_ref - d).days > 5:
            alertes.append(f"{t} : pas de cotation depuis le {d:%d/%m/%Y} (rachat ou suspension ?)")
        pos["dernier_prix"] = px
        val = pos["actions"] * px
        total += val
        lignes.append({"ticker": t, "secteur": pos["secteur"], "valeur": val,
                       "prix": px, "prix_achat": pos["prix_achat"],
                       "perf_depuis_achat": px / pos["prix_achat"] - 1,
                       "date_achat": pos["date_achat"]})
    return total, lignes, alertes


def reequilibrer(etat, close, adj, secteurs, univers, date_signal, date_exec, transactions):
    """Vend ce qui sort, achète ce qui entre, remet chaque ligne à poids égal."""
    classement = scores_fin_de_mois(adj, date_signal, univers)
    choix = selectionner(classement, secteurs, set(etat["positions"]))
    valeur, _, _ = valoriser(etat, close.loc[:date_exec])
    cible = valeur * (1 - FRAIS) / len(choix)          # petite marge pour les frais
    ops = []
    tous = sorted(set(etat["positions"]) | set(choix))
    for t in tous:
        px, _ = dernier_cours(close, t, date_exec)
        if px is None:
            continue
        actuel = etat["positions"].get(t, {}).get("actions", 0.0)
        voulu = cible / px if t in choix else 0.0
        delta = voulu - actuel
        if abs(delta * px) < 1:                         # ignore les ajustements < 1 €
            continue
        montant = delta * px
        frais = abs(montant) * FRAIS
        etat["liquidites"] -= montant + frais
        ops.append({"date": str(date_exec.date()), "ticker": t,
                    "operation": "ACHAT" if delta > 0 else "VENTE",
                    "type": ("nouvelle" if actuel == 0 else "sortie" if voulu == 0 else "ajustement"),
                    "actions": round(delta, 6), "prix": round(px, 4),
                    "montant": round(montant, 2), "frais": round(frais, 2)})
        if voulu == 0:
            etat["positions"].pop(t, None)
        elif t in etat["positions"]:
            etat["positions"][t]["actions"] = voulu
        else:
            etat["positions"][t] = {"actions": voulu, "prix_achat": px, "secteur": secteurs.get(t, "Autre"),
                                    "date_achat": str(date_exec.date()), "dernier_prix": px}
    etat["dernier_reequilibrage"] = date_signal.strftime("%Y-%m")
    etat["signaux"] = [{"rang": i + 1, "ticker": t, "score": round(float(classement[t]), 3),
                        "secteur": secteurs.get(t, "Autre")} for i, t in enumerate(choix)]
    ops_df = pd.DataFrame(ops)
    return pd.concat([transactions, ops_df], ignore_index=True) if len(ops_df) else transactions, ops


# ----------------------------------------------------------------------------
# EXÉCUTION
# ----------------------------------------------------------------------------
def executer(close, adj, secteurs, univers=None, maintenant=None):
    univers = univers or list(secteurs)
    maintenant = maintenant or datetime.now(timezone.utc)
    date_exec = close.index[-1]
    historique = lire_csv("historique.csv", ["date", "strategie", "spy"])
    transactions = lire_csv("transactions.csv", ["date", "ticker", "operation", "type", "actions",
                                                 "prix", "montant", "frais"])
    etat = charger_etat()
    spy_px, _ = dernier_cours(close, BENCHMARK)
    if etat is None:
        print("Première exécution : création du portefeuille virtuel")
        etat = {"debut": str(date_exec.date()), "capital_initial": CAPITAL_INITIAL,
                "liquidites": CAPITAL_INITIAL, "positions": {}, "dernier_reequilibrage": None,
                "spy_parts": CAPITAL_INITIAL / spy_px, "spy_prix_depart": spy_px}

    # Dernière fin de mois COMPLÈTE présente dans les données
    debut_mois_courant = date_exec.to_period("M").start_time
    fins = close.index[close.index < debut_mois_courant]
    ops = []
    if len(fins):
        date_signal = fins[-1]
        if etat["dernier_reequilibrage"] != date_signal.strftime("%Y-%m"):
            print(f"Rééquilibrage : signaux du {date_signal:%d/%m/%Y}, exécution au {date_exec:%d/%m/%Y}")
            transactions, ops = reequilibrer(etat, close, adj, secteurs, univers, date_signal, date_exec,
                                             transactions)

    valeur, lignes, alertes = valoriser(etat, close)
    valeur_spy = etat["spy_parts"] * spy_px
    ligne_hist = {"date": str(date_exec.date()), "strategie": round(valeur, 2),
                  "spy": round(valeur_spy, 2)}
    historique = historique[historique["date"] != ligne_hist["date"]]
    historique = pd.concat([historique, pd.DataFrame([ligne_hist])], ignore_index=True)
    etat["derniere_execution"] = maintenant.strftime("%Y-%m-%d %H:%M UTC")
    sauver(etat, historique, transactions)
    rapport = construire_rapport(etat, historique, lignes, ops, transactions, alertes, date_exec)
    with open(os.path.join(DOSSIER, "rapport.json"), "w") as fh:
        json.dump(rapport, fh, indent=1, ensure_ascii=False)
    print(f"Valeur stratégie : {valeur:,.2f} € | SPY : {valeur_spy:,.2f} €".replace(",", " "))
    return rapport


def perf(h, col, depuis):
    sub = h[h["date"] <= depuis]
    base = sub[col].iloc[-1] if len(sub) else h[col].iloc[0]
    return h[col].iloc[-1] / base - 1


def construire_rapport(etat, historique, lignes, ops, transactions, alertes, date_exec):
    h = historique.copy()
    h["date"] = pd.to_datetime(h["date"])
    d = h["date"].iloc[-1]
    periodes = {
        "semaine": d - pd.Timedelta(days=7),
        "mois_en_cours": d.to_period("M").start_time - pd.Timedelta(days=1),
        "annee_en_cours": d.to_period("Y").start_time - pd.Timedelta(days=1),
        "depuis_debut": h["date"].iloc[0],
    }
    perfs = {k: {"strategie": round(perf(h, "strategie", v), 5), "spy": round(perf(h, "spy", v), 5)}
             for k, v in periodes.items()}
    for v in perfs.values():
        v["ecart"] = round(v["strategie"] - v["spy"], 5)
    eq = h.set_index("date")
    mdd = {c: round(float((eq[c] / eq[c].cummax() - 1).min()), 5) for c in ("strategie", "spy")}
    valeur = float(h["strategie"].iloc[-1])
    lignes = sorted(lignes, key=lambda x: -x["valeur"])
    for l in lignes:
        l["poids"] = round(l["valeur"] / valeur, 4)
        l["valeur"] = round(l["valeur"], 2)
        l["perf_depuis_achat"] = round(l["perf_depuis_achat"], 4)
    rep_sect = {}
    for l in lignes:
        rep_sect[l["secteur"]] = round(rep_sect.get(l["secteur"], 0) + l["poids"], 4)
    t = transactions.copy()
    return {
        "strategie": "Momentum S&P 500 (V5) — 50 titres, rééquilibrage mensuel",
        "date_cours": str(date_exec.date()),
        "derniere_execution": etat["derniere_execution"],
        "debut_simulation": etat["debut"],
        "capital_initial": etat["capital_initial"],
        "valeur_strategie": round(valeur, 2),
        "valeur_spy": round(float(h["spy"].iloc[-1]), 2),
        "liquidites": round(etat["liquidites"], 2),
        "performances": perfs,
        "pire_baisse_depuis_debut": mdd,
        "nb_semaines_suivies": int(len(h)),
        "semaines_battant_spy": int(((h["strategie"].pct_change() - h["spy"].pct_change()) > 0).sum()),
        "dernier_reequilibrage": etat["dernier_reequilibrage"],
        "reequilibrage_cette_semaine": bool(ops),
        "operations_cette_semaine": ops,
        "frais_totaux": round(float(t["frais"].sum()) if len(t) else 0.0, 2),
        "nb_transactions_totales": int(len(t)),
        "positions": lignes,
        "repartition_secteurs": dict(sorted(rep_sect.items(), key=lambda x: -x[1])),
        "meilleures_lignes": [{"ticker": l["ticker"], "perf": l["perf_depuis_achat"]}
                              for l in sorted(lignes, key=lambda x: -x["perf_depuis_achat"])[:5]],
        "pires_lignes": [{"ticker": l["ticker"], "perf": l["perf_depuis_achat"]}
                         for l in sorted(lignes, key=lambda x: x["perf_depuis_achat"])[:5]],
        "signaux_du_mois": etat.get("signaux", []),
        "alertes": alertes,
        "historique": [{"date": str(r.date.date()), "strategie": r.strategie, "spy": r.spy}
                       for r in h.itertuples()],
    }


# ----------------------------------------------------------------------------
# MODE DÉMO : plusieurs semaines simulées, pour vérifier le moteur
# ----------------------------------------------------------------------------
def demo():
    global DOSSIER
    import shutil
    import tempfile
    DOSSIER = os.path.join(tempfile.mkdtemp(), "etat")
    rng = np.random.default_rng(1)
    jours = pd.bdate_range("2025-06-02", "2026-10-09")
    n = 503
    tickers = [f"T{i:03d}" for i in range(n)]
    secteurs = {t: list(SECTEURS_FR.values())[i % 11] for i, t in enumerate(tickers)}
    drift = pd.DataFrame(rng.normal(0.0004, 0.0008, (len(jours), n))).ewm(span=120).mean().values
    rets = drift + rng.normal(0.0003, 0.012, (len(jours), 1)) + rng.normal(0, 0.015, (len(jours), n))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, 0)), index=jours, columns=tickers)
    close[BENCHMARK] = 500 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(jours))))
    close.loc["2026-08-15":, "T007"] = np.nan                     # titre « racheté »
    adj = close.copy()
    vendredis = [d for d in jours if d.weekday() == 4 and d >= pd.Timestamp("2026-07-01")]
    for v in vendredis:
        r = executer(close.loc[:v], adj.loc[:v], secteurs,
                     maintenant=datetime(v.year, v.month, v.day, 7, tzinfo=timezone.utc))
    print("\n--- Dernier rapport (extrait) ---")
    extrait = {k: r[k] for k in ("date_cours", "valeur_strategie", "valeur_spy", "liquidites",
                                 "performances", "pire_baisse_depuis_debut", "dernier_reequilibrage",
                                 "nb_transactions_totales", "frais_totaux", "alertes",
                                 "repartition_secteurs", "semaines_battant_spy",
                                 "nb_semaines_suivies")}
    print(json.dumps(extrait, indent=1, ensure_ascii=False))
    print(f"Positions : {len(r['positions'])} | somme des poids : "
          f"{sum(p['poids'] for p in r['positions']):.4f}")
    tr = pd.read_csv(os.path.join(DOSSIER, "transactions.csv"))
    print(tr.groupby(["date", "type"]).size().to_string())
    shutil.rmtree(os.path.dirname(DOSSIER))


def main():
    if "--demo" in sys.argv:
        demo()
        return
    secteurs = membres_actuels()
    univers = list(secteurs)                      # seuls les membres actuels sont achetables
    close, adj = telecharger_cours(univers)
    print(f"Cours téléchargés : {close.shape[1]} titres, dernier jour {close.index[-1]:%d/%m/%Y}")
    etat = charger_etat()
    if etat:   # garder les titres détenus même s'ils sont sortis de l'indice
        manquants = [t for t in etat["positions"] if t not in close.columns]
        if manquants:
            c2, a2 = telecharger_cours(manquants)
            close, adj = close.join(c2[manquants], how="left"), adj.join(a2[manquants], how="left")
        for t, p in etat["positions"].items():
            secteurs.setdefault(t, p["secteur"])
    executer(close, adj, secteurs, univers)


if __name__ == "__main__":
    main()
