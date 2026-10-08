# Agent Momentum — paper trading de la stratégie V5

Portefeuille **virtuel** de 10 000 € qui applique chaque mois la stratégie
momentum S&P 500 mise au point en backtest (version 5), sur les vrais cours.
Aucun argent réel n'est engagé.

## Comment ça marche

1. **Chaque samedi vers 8 h (Paris)**, GitHub Actions lance `moteur.py` :
   - télécharge les cours de clôture du vendredi (Yahoo Finance) ;
   - au premier passage après une fin de mois, rééquilibre le portefeuille
     (50 actions, poids égaux, 15 maximum par secteur, frais de 0,10 %) ;
   - valorise le portefeuille et un placement de 10 000 € dans le SPY ;
   - enregistre tout dans le dossier `etat/`.
2. **Chaque lundi matin**, l'agent Claude lit `etat/rapport.json` et rédige
   le rapport hebdomadaire.

## Fichiers produits (dossier `etat/`)

| Fichier | Contenu |
|---|---|
| `rapport.json` | Résumé lu par l'agent : performances, positions, opérations, alertes |
| `historique.csv` | Valeur de la stratégie et du SPY à chaque semaine |
| `transactions.csv` | Toutes les opérations (achats, ventes, frais) |
| `etat.json` | Positions détaillées, liquidités, date du dernier rééquilibrage |

## Règles de la stratégie (figées)

- Univers : membres actuels du S&P 500
- Signal : rendement de t-6 mois à t-1 mois divisé par la volatilité sur 6 mois
- 50 titres à poids égal, 15 maximum par secteur GICS
- Un titre détenu est conservé tant qu'il reste dans le top 100
- Rééquilibrage mensuel, sans filtre de tendance

Simplifications : capital noté en euros sans conversion de change ;
dividendes ignorés pour la stratégie comme pour le SPY.

## Lancer à la main

Onglet **Actions** → « Moteur momentum (hebdomadaire) » → **Run workflow**.
