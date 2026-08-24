#!/usr/bin/env python3
"""Collecte quotidienne de l'ouverture des places TGVmax.

L'open data SNCF ne publie qu'un état : ce matin à 04 h 24 UTC, telle place
était ouverte ou fermée. Il ne dit rien de la trajectoire — or c'est la
trajectoire qui intéresse le voyageur. « Cette place tient d'habitude » et
« ce départ ferme dans les trois jours » sont deux phrases que le snapshot du
jour ne permet pas de prononcer.

Ce script construit la trajectoire, un jour à la fois. Il télécharge le
snapshot, le compare à celui de la veille, et compte les passages dans quatre
tables qui séparent quatre effets. Les compteurs s'accumulent ; les snapshots ne
sont gardés que le temps de pouvoir tout recalculer si le modèle change d'avis.

Une remarque sur la fenêtre, parce qu'elle est la bonne nouvelle du dispositif :
le dataset couvre J+0 à J+30, donc chaque snapshot contient tous les horizons à
la fois. Deux snapshots consécutifs se recouvrent sur trente dates de voyage et
livrent d'un coup ~400 000 passages d'un jour, répartis sur les trente
horizons. On n'attend pas un mois pour observer une trajectoire complète : on
estime le risque à chaque horizon, puis on les compose.

Données : SNCF Voyageurs, jeu « tgvmax », sous Open Database License (ODbL).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# La source

CATALOGUE = "https://data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax"
EXPORT = f"{CATALOGUE}/exports/csv?delimiter=%3B"

# Le portail répond en une vingtaine de secondes pour 39 Mo. On laisse large :
# un timeout serré transformerait une matinée lente en trou dans l'archive, et
# un trou ne se rattrape pas.
DELAI = 180

# Un export tronqué servi avec un code 200 est le scénario qui abîmerait
# l'archive sans bruit : l'état de demain serait écrit sur une base amputée.
# On refuse plutôt que d'écrire. Le plancher absolu écarte les réponses vides ;
# le plancher relatif écarte les amputations partielles.
PLANCHER_SUJETS = 150_000
PLANCHER_RELATIF = 0.70

RACINE = Path(__file__).resolve().parent
ETAT = RACINE / "etat" / "precedent.csv.gz"
CPT = RACINE / "compteurs"
REFERENTIEL = RACINE / "referentiel.csv"
JOURNAL = RACINE / "journal.csv"

# La fenêtre du jeu de données. L'horizon d'une transition est celui d'où elle
# part — voir `compter` — donc il vaut au moins 1 : une place ne bascule plus le
# jour du départ, le train est parti.
HORIZON_MIN, HORIZON_MAX = 1, 30


# ─────────────────────────────────────────────────────────────────────────────
# Les quatre tables
#
# Croiser toutes les dimensions dans une clé unique donnerait des millions de
# cases vides, et chacune trop peu peuplée pour dire quoi que ce soit. Quatre
# tables, quatre effets orthogonaux, que le modèle recompose en les multipliant.
# Chacune reste dense, ce qui est la condition pour qu'elle apprenne vite.

TABLES = {
    # La structure : où, quand dans la journée, à quelle distance du départ.
    "horizon": ("axe", "horizon", "pointe"),
    # Le tempérament d'un train : part-il tôt ou tard. La forme fine de la
    # décroissance venant de la table ci-dessus, six bandes suffisent ici — mais
    # il en faut six et non quatre, parce qu'une bande doit être homogène et que
    # trois horizons de la fenêtre ouvrent dix fois plus que les autres. Voir
    # `bande`.
    "train": ("train_no", "origine_iata", "destination_iata", "bande"),
    # Le calendrier. On y garde l'horizon *exact* et non une bande : la date
    # d'observation s'en déduit (date_voyage − horizon), et avec elle le rythme
    # de réservation, qui n'est pas celui du voyage. Personne ne réserve au même
    # rythme un mardi et un dimanche.
    "calendrier": ("axe", "date_voyage", "horizon"),
    # L'histoire du sujet lui-même : depuis combien de temps il est dans cet
    # état, et combien de fois il a déjà basculé. C'est ici que vit « cette
    # place tient d'habitude », qui n'est une propriété ni de l'axe ni de
    # l'horizon mais de ce sujet-là.
    "stabilite": ("horizon", "anciennete", "bascules"),
}

# Quatre nombres par case, et toujours les mêmes. La réouverture compte autant
# que la fermeture : « fermé, mais rouvre souvent » est une information que
# personne ne donne au voyageur aujourd'hui.
CHAMPS = ("n_ouvert", "ferme", "n_ferme", "rouvre")

CHAMPS_REF = (
    "axe",
    "entity",
    "heure_depart",
    "heure_arrivee",
    "origine",
    "destination",
    "premier_vu",
    "dernier_vu",
)


def bande(horizon: int) -> str:
    """Regroupe les horizons pour la table par train.

    Une bande doit être homogène, sinon elle ne dit rien : mélanger un horizon
    où 16 % du parc s'ouvre avec quatre horizons où il ne s'en ouvre que 3 %
    fabrique une moyenne à laquelle aucun train ne ressemble.

    Le découpage d'origine partait du seul relevé du 17/08, et n'isolait donc
    que l'horizon 30 — le bord de fenêtre, où le quota de J-30 n'est pas encore
    chargé et où l'on ne voit que 0,09 % de places ouvertes. Cette bande-là était
    juste, et elle ne bouge pas.

    Les sept premiers relevés en ont révélé deux autres, invisibles dans un état
    instantané parce qu'elles ne se lisent que dans les passages (voir
    ANALYSE-2026-08-24.md) : le quota se recharge aussi en arrivant à J-11 et à
    J-3. Mesuré sur 2 755 821 passages, le taux de réouverture vaut 10,83 % au
    départ de l'horizon 30, 8,27 % au départ du 12, 16,47 % au départ du 4, et
    1,30 % partout ailleurs. Ces trois horizons portent 75 % du flux net de toute
    la fenêtre.

    Or 12 tombait au milieu de « moyen » (8 à 14) et 4 au milieu de « proche »
    (3 à 7). Chacune de ces deux bandes mélangeait donc une vague et six
    horizons calmes. Elles sont ici coupées de part et d'autre des vagues.

    **Les libellés changent tous, sauf `ouverture`.** Une bande dont le sens
    change mais pas le nom serait un piège : « moyen » voudrait dire 8 à 14 dans
    les lignes d'avant et 5 à 11 dans celles d'après, dans le même fichier, sans
    rien pour les distinguer. Les partitions mensuelles ne se resépareront jamais
    — autant que les clés, elles, se séparent. `ouverture` garde son nom parce
    qu'elle garde exactement son horizon.
    """
    if horizon >= HORIZON_MAX:
        return "ouverture"       # 30 — le quota n'est pas encore chargé
    if horizon >= 13:
        return "plateau"         # 13 à 29 — dix-sept jours où rien ne bouge
    if horizon == 12:
        return "relance"         # la vague qui ouvre en arrivant à J-11
    if horizon >= 5:
        return "intervalle"      # 5 à 11 — entre les deux dernières vagues
    if horizon == 4:
        return "bascule"         # la vague qui ouvre en arrivant à J-3
    return "derniers_jours"      # 1 à 3 — après la dernière vague


def pointe(heure_depart: str) -> str:
    """Sépare les départs disputés du reste, sur l'heure seule.

    Grossier, assumé : le vrai effet de calendrier est porté par la date. Cette
    colonne ne sert qu'à ne pas mélanger le 7 h 12 d'un lundi avec le 14 h 30 du
    même jour.
    """
    try:
        h = int(heure_depart[:2])
    except (ValueError, IndexError):
        return "?"
    return "pointe" if (6 <= h < 10 or 16 <= h < 21) else "creuse"


def palier_anciennete(jours: int, censure: bool) -> str:
    """Regroupe l'ancienneté dans l'état courant.

    `censure` dit que le sujet était déjà en cours de fenêtre quand l'archive a
    commencé : son ancienneté réelle est inconnue et ne doit pas être confondue
    avec une ancienneté de un jour, sans quoi les premières semaines
    empoisonneraient les paliers élevés. Cette cohorte se vide d'elle-même en
    trente et un jours.
    """
    if censure:
        return "inconnue"
    if jours <= 1:
        return "1j"
    if jours <= 3:
        return "2-3j"
    if jours <= 7:
        return "4-7j"
    if jours <= 15:
        return "8-15j"
    return "16j+"


def palier_bascules(n: int, censure: bool) -> str:
    if censure:
        return "inconnu"
    if n == 0:
        return "jamais"
    if n == 1:
        return "une"
    if n <= 3:
        return "quelques"
    return "agitee"


# ─────────────────────────────────────────────────────────────────────────────
# Le téléchargement

def horodatage_source() -> str | None:
    """Rend la date du dernier traitement du jeu, côté portail.

    Sert de garde-fou : si le cron passe avant le rafraîchissement de 04 h 24,
    on ne veut pas compter deux fois le même snapshot comme deux journées.
    """
    try:
        with urllib.request.urlopen(CATALOGUE, timeout=30) as r:
            metas = json.load(r).get("metas", {}).get("default", {})
        stamp = metas.get("data_processed") or metas.get("modified")
        return stamp[:10] if stamp else None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
        print(f"  ! métadonnées illisibles ({e}) — on continue à l'aveugle")
        return None


def telecharger() -> bytes:
    requete = urllib.request.Request(
        EXPORT, headers={"User-Agent": "maxplor-tgvmax-archive/1 (+open data ODbL)"}
    )
    # Deux essais : le portail rend parfois une erreur passagère, et un échec
    # coûte une journée d'archive qu'on ne rattrapera pas.
    for tentative in (1, 2):
        try:
            with urllib.request.urlopen(requete, timeout=DELAI) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if tentative == 2:
                raise
            print(f"  ! échec ({e}) — nouvelle tentative dans 60 s")
            time.sleep(60)
    raise AssertionError("inatteignable")


def lire_snapshot(brut: bytes) -> tuple[dict, dict, dict[str, int]]:
    """Réduit le CSV à un état par sujet, et relève le référentiel au passage.

    Un sujet est un (date de voyage, train, origine, destination). L'heure de
    départ n'entre pas dans la clé, et c'est un choix : elle ferait disparaître
    un sujet au moindre décalage horaire, rompant la chaîne des transitions.
    Le prix est mesuré — 20 sujets sur 412 573 fusionnent deux services de même
    numéro sur la même liaison le même jour, soit 0,005 %. On préfère cette
    fusion-là à une rupture de suivi.

    Le portail sert parfois deux lignes pour un même sujet, l'une à OUI l'autre
    à NON — 779 doublons dont 759 en conflit sur un relevé mesuré. On tranche
    pour OUI : si une ligne affirme qu'une place existe, elle existe. Sans cet
    arbitrage, les doublons fabriqueraient une fermeture et une réouverture
    chaque matin.
    """
    etat: dict[tuple[str, str, str, str], tuple[str, str, str]] = {}
    ref: dict[tuple[str, str, str], dict[str, str]] = {}
    bilan = {"lignes": 0, "doublons": 0, "conflits": 0, "heures_divergentes": 0}

    flux = io.TextIOWrapper(io.BytesIO(brut), encoding="utf-8-sig", newline="")
    for ligne in csv.DictReader(flux, delimiter=";"):
        bilan["lignes"] += 1
        cle = (
            ligne["date"],
            ligne["train_no"],
            ligne["origine_iata"],
            ligne["destination_iata"],
        )
        dispo = "O" if ligne["od_happy_card"] == "OUI" else "N"

        ancien = etat.get(cle)
        if ancien is not None:
            bilan["doublons"] += 1
            if ancien[0] != dispo:
                bilan["conflits"] += 1
            if ancien[2] != ligne["heure_depart"]:
                bilan["heures_divergentes"] += 1
            if ancien[0] == "O":
                continue  # OUI déjà posé : rien ne le déloge
        etat[cle] = (dispo, ligne["axe"], ligne["heure_depart"])

        # Le référentiel garde les attributs stables d'un service. Sans lui,
        # les états datés ne permettraient aucun recalcul — ils ne portent ni
        # l'axe ni l'heure. Il rend aussi, gratuitement, la liste de tous les
        # (train, OD) jamais vus, places Max ou non.
        ref[(ligne["train_no"], ligne["origine_iata"], ligne["destination_iata"])] = {
            "axe": ligne["axe"],
            "entity": ligne["entity"],
            "heure_depart": ligne["heure_depart"],
            "heure_arrivee": ligne["heure_arrivee"],
            "origine": ligne["origine"],
            "destination": ligne["destination"],
        }

    return etat, ref, bilan


# ─────────────────────────────────────────────────────────────────────────────
# L'état de la veille
#
# L'état porte, par sujet : la disponibilité, l'ancienneté dans cet état, le
# nombre de bascules, et un drapeau de censure. Les trois derniers sont la
# raison d'être de ce fichier au-delà de la comparaison : des compteurs par
# paires ne permettent de reconstituer aucune trajectoire, donc ni durée ni
# agitation. Ces informations ne se rattrapent pas après coup.

def charger_etat() -> tuple[str | None, dict]:
    if not ETAT.exists():
        return None, {}
    etat: dict[tuple[str, str, str, str], tuple[str, int, int, bool, bool]] = {}
    with gzip.open(ETAT, "rt", encoding="utf-8", newline="") as f:
        entete = f.readline().strip()
        jour = entete.split("=", 1)[1] if entete.startswith("#snapshot=") else None
        for l in csv.reader(f, delimiter=";"):
            if len(l) == 9:
                etat[(l[0], l[1], l[2], l[3])] = (
                    l[4], int(l[5]), int(l[6]), l[7] == "c", l[8] == "c",
                )
    return jour, etat


def ecrire_etat(jour: str, etat: dict) -> None:
    ETAT.parent.mkdir(parents=True, exist_ok=True)
    tmp = ETAT.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        f.write(f"#snapshot={jour}\n")
        w = csv.writer(f, delimiter=";")
        for (d, t, o, dst), (dispo, anc, basc, ca, cb) in etat.items():
            w.writerow(
                (d, t, o, dst, dispo, anc, basc, "c" if ca else "n", "c" if cb else "n")
            )
    tmp.replace(ETAT)


def vieillir(
    dispo: str, avant: tuple[str, int, int, bool, bool]
) -> tuple[str, int, int, bool, bool]:
    """Fait avancer un sujet d'une journée.

    Une bascule observée éteint la censure sur l'ancienneté : on tient désormais
    le point de départ exact du compte, même si l'on ignore toujours combien de
    fois le sujet avait basculé auparavant. La censure se soigne donc d'elle-même
    au premier mouvement, au lieu d'attendre que le sujet quitte la fenêtre —
    et comme 1 à 3 % des sujets bougent chaque jour, la cohorte d'amorce se vide
    en quelques semaines plutôt qu'en un mois plein.
    """
    etat_avant, anc, basc, cens_anc, cens_basc = avant
    if dispo == etat_avant:
        return (dispo, anc + 1, basc, cens_anc, cens_basc)
    return (dispo, 1, basc + 1, False, cens_basc)


# ─────────────────────────────────────────────────────────────────────────────
# Les compteurs

def partition(nom: str, mois: str) -> Path:
    """Chemin d'une tranche mensuelle de compteurs.

    Les compteurs sont partitionnés par mois d'observation, et c'est un choix
    irréversible qu'il fallait faire tout de suite : un total unique depuis
    l'origine interdirait à jamais de pondérer le récent plus fort que l'ancien.
    Or la demande ferroviaire dérive — travaux, ouvertures de lignes, tarifs — et
    un modèle bâti dans deux ans ne devrait pas accorder le même poids à ses
    premiers mois. Une fois les mois additionnés, on ne les resépare plus.

    Le modèle somme les tranches, en les pondérant s'il le souhaite.
    """
    return CPT / nom / f"{mois}.csv"


def charger_compteurs(nom: str, mois: str) -> dict[tuple, list[int]]:
    chemin = partition(nom, mois)
    table: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    if not chemin.exists():
        return table
    cles = TABLES[nom]
    with chemin.open(encoding="utf-8", newline="") as f:
        for ligne in csv.DictReader(f):
            table[tuple(ligne[k] for k in cles)] = [int(ligne[c]) for c in CHAMPS]
    return table


def ecrire_compteurs(nom: str, mois: str, table: dict) -> None:
    cles = TABLES[nom]
    chemin = partition(nom, mois)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cles + CHAMPS)
        # Tri sur les entiers là où la clé en contient, pour que le fichier se
        # lise et que ses diffs restent lisibles.
        idx = [i for i, k in enumerate(cles) if k == "horizon"]
        w.writerows(
            list(c) + table[c]
            for c in sorted(
                table, key=lambda c: tuple(int(c[i]) if i in idx else c[i] for i in range(len(c)))
            )
        )
    tmp.replace(chemin)


def charger_referentiel() -> dict[tuple[str, str, str], dict[str, str]]:
    if not REFERENTIEL.exists():
        return {}
    ref = {}
    with REFERENTIEL.open(encoding="utf-8", newline="") as f:
        for l in csv.DictReader(f):
            ref[(l["train_no"], l["origine_iata"], l["destination_iata"])] = l
    return ref


def fusionner_referentiel(ancien: dict, vu: dict, jour: str) -> dict:
    """Accumule les services connus, sans jamais en retirer.

    Un service qui disparaît du jeu garde son `dernier_vu` : c'est ainsi qu'on
    repère une suppression d'horaire. Les attributs sont rafraîchis à chaque
    passage, un train pouvant être décalé.

    `dernier_vu` n'est réécrit qu'au-delà d'une semaine d'ancienneté, et c'est
    délibéré : rafraîchi chaque jour, il ferait changer les 20 600 lignes du
    fichier tous les matins, et 2 Mo de texte intégralement réécrit ne se delta
    pas dans git. À la semaine près, l'information reste largement suffisante
    pour repérer un service retiré.
    """
    aujourdhui = date.fromisoformat(jour)
    for cle, attrs in vu.items():
        entree = ancien.get(cle)
        garde = ""
        if entree:
            precedent = entree.get("dernier_vu", "")
            try:
                if precedent and (aujourdhui - date.fromisoformat(precedent)).days < 7:
                    garde = precedent
            except ValueError:
                garde = ""
        ancien[cle] = {
            **attrs,
            "premier_vu": entree["premier_vu"] if entree else jour,
            "dernier_vu": garde or jour,
        }
    return ancien


def ecrire_referentiel(ref: dict) -> None:
    tmp = REFERENTIEL.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(("train_no", "origine_iata", "destination_iata") + CHAMPS_REF)
        for cle in sorted(ref):
            w.writerow(list(cle) + [ref[cle].get(c, "") for c in CHAMPS_REF])
    tmp.replace(REFERENTIEL)


# ─────────────────────────────────────────────────────────────────────────────
# Le passage d'un jour

def compter(
    snapshot: dict, veille: dict, veille_jour: date, mois: str
) -> tuple[dict, dict, dict]:
    """Compare deux snapshots, compte les passages, et fait vieillir l'état.

    L'horizon d'une transition est celui **d'où elle part**, c'est-à-dire celui
    de la veille. La question à laquelle le modèle répond est « je constate
    aujourd'hui l'état à l'horizon h, que devient-il demain ? » : le taux utile
    est donc indexé par l'horizon où l'on se tient au moment de prédire, pas par
    celui où l'on atterrit. Indexer sur l'arrivée décalerait chaque taux d'un
    cran, et l'erreur se propagerait en s'aggravant dans la composition — la
    pente étant la plus raide près du départ.

    Seuls les sujets présents dans les deux relevés sont comptés. Un voyage qui
    vient d'entrer dans la fenêtre n'a pas de passé ; un voyage qui en est sorti
    n'a plus d'avenir. Ni l'un ni l'autre n'enseigne quoi que ce soit.

    Les paliers d'ancienneté et de bascules sont ceux de **la veille** : ce sont
    les seuls qu'un prédicteur connaîtrait à l'instant de prédire.
    """
    tables = {nom: charger_compteurs(nom, mois) for nom in TABLES}
    neuf: dict = {}
    bilan = {
        "communs": 0, "ferme": 0, "rouvre": 0, "stable_o": 0, "stable_n": 0,
        "nouveaux": 0, "disparus": 0, "censures": 0, "hors_fenetre": 0,
    }

    for cle, (dispo, axe, heure) in snapshot.items():
        date_voyage, train, orig, dest = cle
        avant = veille.get(cle)

        if avant is None:
            # Nouveau dans la fenêtre. Après l'amorce, une absence de la veille
            # signifie une vraie apparition : ancienneté 1, aucune bascule, pas
            # de censure.
            bilan["nouveaux"] += 1
            neuf[cle] = (dispo, 1, 0, False, False)
            continue

        etat_veille, anc, basc, cens_anc, cens_basc = avant
        try:
            horizon = (date.fromisoformat(date_voyage) - veille_jour).days
        except ValueError:
            continue

        if not HORIZON_MIN <= horizon <= HORIZON_MAX:
            # Se produit si la fenêtre du portail bouge : on ne compte pas, mais
            # on fait tout de même vieillir le sujet pour ne pas perdre sa
            # trajectoire.
            bilan["hors_fenetre"] += 1
            neuf[cle] = vieillir(dispo, avant)
            continue

        bilan["communs"] += 1
        if cens_anc:
            bilan["censures"] += 1

        h = str(horizon)
        cases = (
            tables["horizon"][(axe, h, pointe(heure))],
            tables["train"][(train, orig, dest, bande(horizon))],
            tables["calendrier"][(axe, date_voyage, h)],
            tables["stabilite"][
                (h, palier_anciennete(anc, cens_anc), palier_bascules(basc, cens_basc))
            ],
        )

        if etat_veille == "O":
            for c in cases:
                c[0] += 1
            if dispo == "N":
                for c in cases:
                    c[1] += 1
                bilan["ferme"] += 1
            else:
                bilan["stable_o"] += 1
        else:
            for c in cases:
                c[2] += 1
            if dispo == "O":
                for c in cases:
                    c[3] += 1
                bilan["rouvre"] += 1
            else:
                bilan["stable_n"] += 1

        neuf[cle] = vieillir(dispo, avant)

    bilan["disparus"] = len(veille) - (len(snapshot) - bilan["nouveaux"])
    return tables, neuf, bilan


def amorcer(snapshot: dict, jour: date) -> dict:
    """Pose l'état initial en marquant la censure là où elle s'impose.

    Un sujet aperçu pour la première fois au bord de la fenêtre (J+30) est vu
    depuis sa naissance : son ancienneté est exacte. Un sujet aperçu en milieu
    de fenêtre existait avant qu'on regarde, et son ancienneté est inconnue —
    la confondre avec « un jour » empoisonnerait les paliers élevés pendant un
    mois. Le bord du jeu étant en pente (12 221 lignes à J+30 contre 13 768 à
    J+0), cette cohorte est importante ; elle se vide au fil des bascules
    observées, et non seulement à la sortie de la fenêtre.
    """
    etat = {}
    for cle, (dispo, _axe, _h) in snapshot.items():
        try:
            horizon = (date.fromisoformat(cle[0]) - jour).days
        except ValueError:
            horizon = 0
        inconnu = horizon < HORIZON_MAX
        etat[cle] = (dispo, 1, 0, inconnu, inconnu)
    return etat


def reamorcer(snapshot: dict, veille: dict, jour: date) -> dict:
    """Repart après un trou, en sauvant ce qui peut l'être.

    Un échec de téléversement ou une matinée sans portail suffit à créer un
    écart de deux jours. Remettre alors toutes les anciennetés à zéro
    détruirait des semaines d'histoire pour un incident d'infrastructure — or
    la trajectoire est désormais l'actif principal de l'archive.

    On conserve donc ancienneté et bascules des sujets encore présents, en les
    marquant inconnues : ce qui s'est passé pendant le trou n'est pas
    observable, et la seule réponse honnête est de le dire. Le compte reprend
    sa valeur exacte dès la première bascule observée.
    """
    neuf = amorcer(snapshot, jour)
    repris = 0
    for cle, (dispo, anc, basc, _ca, _cb) in list(neuf.items()):
        avant = veille.get(cle)
        if avant is None:
            continue
        _etat, anc_av, basc_av, _, _ = avant
        neuf[cle] = (dispo, anc_av + 1, basc_av, True, True)
        repris += 1
    print(f"  {repris} sujets dont l'histoire est reprise, marquée inconnue")
    return neuf


def journaliser(ligne: dict) -> None:
    """Ajoute une ligne au journal : la santé de l'archive, en un fichier.

    Un trou, un snapshot rejoué, un effondrement du nombre de passages, une
    montée des doublons — tout se voit ici avant de se voir dans le modèle.
    """
    colonnes = [
        "snapshot", "sujets", "ouverts", "ecart_jours", "communs", "ferme",
        "rouvre", "nouveaux", "disparus", "censures", "doublons", "conflits",
        "heures_divergentes", "services", "secondes", "note",
    ]
    neuf = not JOURNAL.exists()
    with JOURNAL.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        if neuf:
            w.writeheader()
        w.writerow({c: ligne.get(c, "") for c in colonnes})


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fichier", type=Path, help="lire un CSV local au lieu du portail")
    ap.add_argument("--jour", help="date du snapshot en ISO (défaut : celle du portail)")
    ap.add_argument(
        "--force", action="store_true",
        help="compter même si le snapshot a déjà été vu (mise au point seulement)",
    )
    args = ap.parse_args()
    debut = time.monotonic()

    if args.fichier:
        print(f"→ lecture locale de {args.fichier}")
        brut = args.fichier.read_bytes()
        jour_source = args.jour
    else:
        jour_source = args.jour or horodatage_source()
        print(f"→ téléchargement du snapshot (portail : {jour_source or 'inconnu'})")
        try:
            brut = telecharger()
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"✗ portail injoignable : {e}", file=sys.stderr)
            return 1
        print(f"  {len(brut) / 1e6:.1f} Mo reçus")

    if jour_source is None:
        jour_source = datetime.now(timezone.utc).date().isoformat()
    jour = date.fromisoformat(jour_source)

    snapshot, vu, lecture = lire_snapshot(brut)
    ouverts = sum(1 for v in snapshot.values() if v[0] == "O")
    print(
        f"  {len(snapshot)} sujets pour {lecture['lignes']} lignes, "
        f"dont {ouverts} ouverts ({ouverts / max(len(snapshot), 1):.1%})"
    )
    if lecture["doublons"]:
        print(
            f"  doublons : {lecture['doublons']} "
            f"({lecture['conflits']} en conflit OUI/NON, "
            f"{lecture['heures_divergentes']} d'heures divergentes)"
        )

    veille_jour, veille = charger_etat()

    # Le garde-fou de plausibilité vient avant toute écriture : un export
    # amputé ne doit pas remplacer un état sain. On échoue bruyamment plutôt
    # que d'abîmer l'archive en silence.
    seuil = max(PLANCHER_SUJETS, int(len(veille) * PLANCHER_RELATIF)) if veille else PLANCHER_SUJETS
    if len(snapshot) < seuil:
        print(
            f"✗ snapshot invraisemblable : {len(snapshot)} sujets pour un seuil de "
            f"{seuil}. Rien n'est écrit ; l'état précédent reste en place.",
            file=sys.stderr,
        )
        return 1

    base = {
        "snapshot": jour_source, "sujets": len(snapshot), "ouverts": ouverts,
        "doublons": lecture["doublons"], "conflits": lecture["conflits"],
        "heures_divergentes": lecture["heures_divergentes"],
    }

    def clore(extra: dict) -> None:
        ref = fusionner_referentiel(charger_referentiel(), vu, jour_source)
        ecrire_referentiel(ref)
        journaliser({
            **base, **extra,
            "services": len(ref),
            "secondes": round(time.monotonic() - debut, 1),
        })

    if not veille:
        print("→ premier relevé : rien à comparer, on pose la pierre d'attente")
        etat = amorcer(snapshot, jour)
        censures = sum(1 for v in etat.values() if v[3])
        ecrire_etat(jour_source, etat)
        clore({"note": "amorce", "censures": censures})
        print(f"  {censures} sujets à ancienneté inconnue (cohorte d'amorce)")
        return 0

    ecart = (jour - date.fromisoformat(veille_jour)).days if veille_jour else None

    if ecart == 0 and not args.force:
        print("→ snapshot déjà traité : on ne compte pas deux fois la même journée")
        clore({"ecart_jours": 0, "note": "rejoué, ignoré"})
        return 0

    if ecart != 1 and not args.force:
        # Deux jours d'écart mélangeraient deux passages en un. Mieux vaut une
        # case vide qu'une case fausse : on renonce au comptage et on réamorce.
        print(f"! écart de {ecart} jour(s) : comptage sauté, l'état est réamorcé")
        etat = reamorcer(snapshot, veille, jour)
        ecrire_etat(jour_source, etat)
        clore({
            "ecart_jours": ecart,
            "censures": sum(1 for v in etat.values() if v[3]),
            "note": "trou, réamorcé",
        })
        return 0

    print(f"→ comparaison avec {veille_jour}")
    # La transition est imputée au mois du relevé d'arrivée : c'est le mois où
    # l'observation a été faite, ce qui est la bonne unité pour pondérer le
    # récent.
    mois = jour_source[:7]
    tables, neuf, bilan = compter(snapshot, veille, date.fromisoformat(veille_jour), mois)

    for nom, table in tables.items():
        ecrire_compteurs(nom, mois, table)
    ecrire_etat(jour_source, neuf)
    clore({"ecart_jours": ecart, **bilan})

    print(
        f"  {bilan['communs']} sujets communs — {bilan['ferme']} fermetures, "
        f"{bilan['rouvre']} réouvertures, {bilan['stable_o']} restés ouverts, "
        f"{bilan['stable_n']} restés fermés"
    )
    print(
        f"  {bilan['nouveaux']} entrés dans la fenêtre, {bilan['disparus']} sortis, "
        f"{bilan['censures']} d'ancienneté encore inconnue"
    )
    print("  tables : " + ", ".join(f"{n} {len(t)}" for n, t in tables.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
