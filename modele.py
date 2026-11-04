#!/usr/bin/env python3
"""Dérive un modèle de disponibilité TGVmax à partir des compteurs d'archive.

Le dépôt accumule des passages ; ce script en fait deux taux par case et les
compose. La chaîne est à deux états — une place est ouverte ou fermée — et se
propage d'un horizon au suivant :

    P(fermée demain | ouverte à J-h)   noté `ferme`
    P(ouverte demain | fermée à J-h)   noté `rouvre`

La survie jusqu'au départ s'obtient en propageant cette chaîne de l'horizon k
jusqu'à 1. L'horizon 0 n'est pas observable : le train est parti.

Trois choses distinguent ce script d'une simple division.

**Le repli hiérarchique.** Un couple (train, OD) n'a pas assez d'observations
pour un taux propre — la bande `ouverture` ne lui compte qu'un passage par jour.
Chaque niveau hérite donc du niveau au-dessus par un lissage dont la force est
estimée sur les données elles-mêmes, et non posée à la main : (train, OD) dans
sa bande hérite du (train, OD), qui hérite de son axe, qui hérite du global.

**La validation par retenue.** Les compteurs sont cumulés, mais le dépôt garde
un commit par relevé : `git show` rend l'état des compteurs à chaque date, et
la différence de deux états consécutifs rend l'apport propre d'une journée. On
estime donc sur les cinq premiers relevés, on prédit les deux derniers, et on
regarde l'écart. Sans cette reconstruction, aucune retenue temporelle ne serait
possible sur trois des quatre tables.

**L'aveu.** Le modèle sort avec ses effectifs. Une case à douze observations
n'est pas une case, et le JSON produit le dit plutôt que de le cacher derrière
un nombre à trois décimales.

Données : SNCF Voyageurs, jeu « tgvmax », sous Open Database License (ODbL).
Les compteurs dérivés, et donc ce modèle, restent sous la même licence.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent
CPT = RACINE / "compteurs"
REFERENTIEL = RACINE / "referentiel.csv"
SORTIE = RACINE / "modele" / "modele-v2.json"

CHAMPS = ("n_ouvert", "ferme", "n_ferme", "rouvre")
CLES = {
    "horizon": ("axe", "horizon", "pointe"),
    "train": ("train_no", "origine_iata", "destination_iata", "bande"),
    "calendrier": ("axe", "date_voyage", "horizon"),
    "stabilite": ("horizon", "anciennete", "bascules"),
}
HORIZON_MIN, HORIZON_MAX = 1, 30
JOURS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")

# Les bandes de `collecte.py`, redites ici pour que le modèle sache à quel
# horizon chaque bande s'applique — c'est ce qui permet de replier une bande sur
# la forme fine de la table par horizon.
BANDES = {
    "ouverture": (30, 30),
    "plateau": (13, 29),
    "relance": (12, 12),
    "intervalle": (5, 11),
    "bascule": (4, 4),
    "derniers_jours": (1, 3),
}

# Le découpage d'avant le 24/08, qui n'isolait que le bord de fenêtre. Les
# partitions mensuelles ne se resépareront jamais : les lignes déjà écrites
# gardent leurs libellés pour toujours, et le modèle doit continuer de savoir à
# quels horizons elles correspondaient. C'est précisément pour rendre cette
# cohabitation lisible que les nouveaux libellés sont tous différents des
# anciens — `ouverture` mise à part, qui n'a pas changé d'horizon.
BANDES_HERITEES = {
    "lointain": (15, 29),
    "moyen": (8, 14),
    "proche": (3, 7),
    "imminent": (1, 2),
}

PORTEE_BANDE = {**BANDES, **BANDES_HERITEES}


def bande(h: int) -> str:
    """Le libellé courant d'un horizon. Ne rend jamais un libellé hérité."""
    for nom, (lo, hi) in BANDES.items():
        if lo <= h <= hi:
            return nom
    raise ValueError(h)


# ─────────────────────────────────────────────────────────────────────────────
# Lecture

def lire_csv(texte: str, nom: str) -> dict[tuple, list[int]]:
    table: dict[tuple, list[int]] = {}
    for l in csv.DictReader(io.StringIO(texte)):
        table[tuple(l[k] for k in CLES[nom])] = [int(l[c]) for c in CHAMPS]
    return table


def lire_table(nom: str) -> dict[tuple, list[int]]:
    """Somme toutes les tranches mensuelles d'une table.

    Les tranches existent pour qu'un modèle futur puisse pondérer le récent plus
    fort que l'ancien. Tant que l'archive n'a pas vu une saison deux fois, une
    pondération n'aurait rien de sûr à préférer : on somme, et on laisse le
    point d'accroche en place.
    """
    total: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for chemin in sorted((CPT / nom).glob("*.csv")):
        for k, v in lire_csv(chemin.read_text(encoding="utf-8"), nom).items():
            for i in range(4):
                total[k][i] += v[i]
    return dict(total)


def lire_axes() -> dict[tuple[str, str, str], str]:
    """L'axe d'un couple (train, OD). La table `train` ne le porte pas."""
    axes = {}
    with REFERENTIEL.open(encoding="utf-8", newline="") as f:
        for l in csv.DictReader(f):
            axes[(l["train_no"], l["origine_iata"], l["destination_iata"])] = l["axe"]
    return axes


def releves_git() -> list[str]:
    """Les dates de relevé, dans l'ordre, telles que git les a enregistrées."""
    sortie = subprocess.run(
        ["git", "-C", str(RACINE), "log", "--reverse", "--format=%H %s", "--", "compteurs"],
        capture_output=True, text=True, check=True,
    ).stdout
    jours = []
    for ligne in sortie.splitlines():
        sha, _, sujet = ligne.partition(" ")
        # « Compte les passages du AAAA-MM-JJ »
        jour = sujet.strip().split()[-1]
        try:
            date.fromisoformat(jour)
        except ValueError:
            continue
        jours.append((sha, jour))
    # Un relevé rejoué produit un commit sans changement de compteur : on garde
    # le dernier commit de chaque date.
    dernier = {}
    for sha, jour in jours:
        dernier[jour] = sha
    return [(dernier[j], j) for j in sorted(dernier)]


def apports_quotidiens(nom: str) -> dict[str, dict[tuple, list[int]]]:
    """L'apport propre de chaque relevé, par différence de deux cumuls.

    Les compteurs sont cumulés depuis l'origine du mois ; l'histoire git rend
    l'état à chaque date, donc la dérivée. C'est le seul chemin vers une
    retenue temporelle sur les tables qui ne portent pas la date d'observation.
    """
    apports: dict[str, dict[tuple, list[int]]] = {}
    precedent: dict[tuple, list[int]] = {}
    for sha, jour in releves_git():
        cumul: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        chemins = subprocess.run(
            ["git", "-C", str(RACINE), "ls-tree", "-r", "--name-only", sha, f"compteurs/{nom}/"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        for chemin in chemins:
            texte = subprocess.run(
                ["git", "-C", str(RACINE), "show", f"{sha}:{chemin}"],
                capture_output=True, text=True, check=True,
            ).stdout
            for k, v in lire_csv(texte, nom).items():
                for i in range(4):
                    cumul[k][i] += v[i]
        delta = {}
        for k, v in cumul.items():
            p = precedent.get(k, (0, 0, 0, 0))
            d = [v[i] - p[i] for i in range(4)]
            if any(d):
                delta[k] = d
        if delta:
            apports[jour] = delta
        precedent = cumul
    return apports


# ─────────────────────────────────────────────────────────────────────────────
# Statistique

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de Wilson. Sur douze observations il est large, et c'est le
    but : il rend visible ce qu'une simple division cache."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    demi = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - demi), min(1.0, centre + demi))


def force_beta(obs: list[tuple[int, int]], mu: float) -> float:
    """Estime la force du lissage bêta-binomial par vraisemblance marginale.

    La force κ dit combien d'observations fictives, tirées du niveau parent,
    on ajoute à chaque case avant de diviser. Elle n'est pas posée à la main :
    on cherche celle qui explique le mieux la dispersion réellement observée
    entre les cases. Si les cases se ressemblent, κ sort grand et tout le monde
    se replie sur le parent ; si elles diffèrent vraiment, κ sort petit et
    chaque case garde sa voix.
    """
    obs = [(k, n) for k, n in obs if n > 0]
    if len(obs) < 3 or not 0 < mu < 1:
        return 200.0

    def logvrais(kappa: float) -> float:
        a, b = kappa * mu, kappa * (1 - mu)
        s = 0.0
        for k, n in obs:
            s += (math.lgamma(k + a) + math.lgamma(n - k + b) - math.lgamma(n + kappa)
                  + math.lgamma(kappa) - math.lgamma(a) - math.lgamma(b))
        return s

    meilleur, score = 200.0, -math.inf
    kappa = 0.5
    while kappa <= 40000:
        v = logvrais(kappa)
        if v > score:
            score, meilleur = v, kappa
        kappa *= 1.35
    return meilleur


def lisser(k: int, n: int, mu: float, kappa: float) -> float:
    return (k + kappa * mu) / (n + kappa)


def force_gamma(obs: list[tuple[int, float]]) -> float:
    """Force du lissage d'un multiplicateur, par vraisemblance marginale.

    Le multiplicateur d'un couple (train, OD) est le rapport de ce qu'on a
    observé à ce que son axe laissait attendre. On le suppose tiré d'une loi
    gamma de moyenne 1 et de force κ, ce qui rend le comptage négatif-binomial.
    On cherche le κ qui explique le mieux la dispersion réelle des couples :
    grand si les couples se ressemblent, petit s'ils diffèrent vraiment.

    Le même principe que `force_beta`, transposé au multiplicatif — et pour la
    même raison : une force posée à la main serait une opinion déguisée en
    paramètre.
    """
    obs = [(k, e) for k, e in obs if e > 0.05]
    if len(obs) < 20:
        return 50.0
    tot_k = sum(k for k, _ in obs)
    tot_e = sum(e for _, e in obs)
    if tot_k == 0:
        return 1000.0

    def logvrais(kappa: float) -> float:
        s = 0.0
        for k, e in obs:
            s += (math.lgamma(k + kappa) - math.lgamma(kappa) - math.lgamma(k + 1)
                  + kappa * math.log(kappa / (kappa + e))
                  + k * math.log(e / (kappa + e)))
        return s

    meilleur, score = 50.0, -math.inf
    kappa = 0.02
    while kappa <= 5000:
        v = logvrais(kappa)
        if v > score:
            score, meilleur = v, kappa
        kappa *= 1.3
    return meilleur


# ─────────────────────────────────────────────────────────────────────────────
# Le modèle

class Modele:
    """Taux de fermeture et de réouverture, par couches emboîtées.

    Couche 0 : global × horizon — dense, toujours estimable.
    Couche 1 : axe × horizon — replié sur la couche 0.
    Couche 2 : pointe/creuse — multiplicateur replié sur 1.
    Couche 3 : jour de semaine du voyage — multiplicateur replié sur 1.
    Couche 4 : couple (train, OD) — multiplicateur replié sur 1.

    Les couches 2 à 4 sont multiplicatives sur le taux, et non additives en
    logit. Ce n'est pas une élégance : c'est le seul choix qui se valide sur
    les tables dont on dispose, où l'on n'observe jamais le croisement complet.
    Le prix — une composition qui peut dériver — est mesuré plus bas.
    """

    def __init__(self, tables: dict, axes_couple: dict):
        self.axes_couple = axes_couple
        self._ajuster(tables)

    # ── couche 0 et 1 ────────────────────────────────────────────────────────
    def _ajuster(self, tables: dict) -> None:
        hor = tables["horizon"]

        glob = {h: [0, 0, 0, 0] for h in range(HORIZON_MIN, HORIZON_MAX + 1)}
        par_axe_h: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        par_axe_h_p: dict[tuple[str, int, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        for (axe, h, pte), v in hor.items():
            h = int(h)
            for i in range(4):
                glob[h][i] += v[i]
                par_axe_h[(axe, h)][i] += v[i]
                par_axe_h_p[(axe, h, pte)][i] += v[i]

        self.axes = sorted({a for a, _ in par_axe_h})
        self.global_n = glob

        # Couche 0 : le taux global par horizon. Aucune case ne descend sous
        # 60 000 observations, il n'y a rien à lisser.
        self.g_ferme = {h: (glob[h][1] / glob[h][0] if glob[h][0] else 0.0) for h in glob}
        self.g_rouvre = {h: (glob[h][3] / glob[h][2] if glob[h][2] else 0.0) for h in glob}

        # Couche 1 : chaque axe est lissé vers le global, horizon par horizon.
        # La force est réestimée à chaque horizon : à l'horizon 30 les axes
        # divergent franchement, au milieu de fenêtre beaucoup moins.
        self.k_ferme, self.k_rouvre = {}, {}
        self.a_ferme, self.a_rouvre, self.a_n = {}, {}, {}
        for h in glob:
            of = [(par_axe_h[(a, h)][1], par_axe_h[(a, h)][0]) for a in self.axes]
            orv = [(par_axe_h[(a, h)][3], par_axe_h[(a, h)][2]) for a in self.axes]
            self.k_ferme[h] = force_beta(of, self.g_ferme[h] or 1e-4)
            self.k_rouvre[h] = force_beta(orv, self.g_rouvre[h] or 1e-4)
            for a in self.axes:
                no, fe, nf, ro = par_axe_h[(a, h)]
                self.a_ferme[(a, h)] = lisser(fe, no, self.g_ferme[h], self.k_ferme[h])
                self.a_rouvre[(a, h)] = lisser(ro, nf, self.g_rouvre[h], self.k_rouvre[h])
                self.a_n[(a, h)] = (no, fe, nf, ro)

        # Un axe sans la moindre ouverture sur toute la fenêtre n'est pas un axe
        # mal estimé : c'est un axe sans places Max. OUIGO, les autocars et
        # IC SRO sont dans ce cas. Les replier sur le global leur prêterait une
        # disponibilité qu'ils n'ont jamais eue — le repli doit s'arrêter là.
        self.axes_clos = []
        for a in self.axes:
            no = sum(par_axe_h[(a, h)][0] for h in glob)
            ro = sum(par_axe_h[(a, h)][3] for h in glob)
            nf = sum(par_axe_h[(a, h)][2] for h in glob)
            if no == 0 and ro == 0 and nf >= 1_000:
                self.axes_clos.append(a)

        # ── couche 2 : pointe / creuse ───────────────────────────────────────
        # Multiplicateur sur le taux, estimé par axe, tous horizons confondus :
        # découpé par horizon il serait trop maigre pour dire quelque chose.
        self.m_pointe = {}
        obs_f, obs_r = [], []
        for (a, h, pte), v in par_axe_h_p.items():
            h = int(h)
            e_f = v[0] * self.a_ferme[(a, h)]
            e_r = v[2] * self.a_rouvre[(a, h)]
            obs_f.append((v[1], e_f))
            obs_r.append((v[3], e_r))
        kf, kr = force_gamma(obs_f), force_gamma(obs_r)
        att: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
        for (a, h, pte), v in par_axe_h_p.items():
            h = int(h)
            c = att[(a, pte)]
            c[0] += v[1]
            c[1] += v[0] * self.a_ferme[(a, h)]
            c[2] += v[3]
            c[3] += v[2] * self.a_rouvre[(a, h)]
        for cle, (kobs_f, e_f, kobs_r, e_r) in att.items():
            self.m_pointe[cle] = (
                (kobs_f + kf) / (e_f + kf) if e_f > 0 else 1.0,
                (kobs_r + kr) / (e_r + kr) if e_r > 0 else 1.0,
            )

        # ── couche 3 : jour de semaine du voyage ─────────────────────────────
        self.m_jour, self.n_jour = {}, {}
        cal = tables["calendrier"]
        att_j: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0])
        for (axe, dv, h), v in cal.items():
            h = int(h)
            j = date.fromisoformat(dv).weekday()
            c = att_j[j]
            c[0] += v[1]
            c[1] += v[0] * self.a_ferme[(axe, h)]
            c[2] += v[3]
            c[3] += v[2] * self.a_rouvre[(axe, h)]
            c[4] += v[0]
            c[5] += v[2]
        for j in range(7):
            kf_, ef_, kr_, er_, no, nf = att_j[j]
            self.m_jour[j] = (kf_ / ef_ if ef_ > 0 else 1.0, kr_ / er_ if er_ > 0 else 1.0)
            self.n_jour[j] = (int(no), int(kf_), int(nf), int(kr_))

        # ── couche 4 : le couple (train, OD) ─────────────────────────────────
        # Un multiplicateur par couple et par sens, commun à toutes les bandes :
        # le tempérament général du train, estimé sur tous ses passages. C'est
        # la couche que les premiers relevés pouvaient porter ; la couche 5 la
        # précise bande par bande quand l'archive le permet.
        self.m_train = {}
        self.n_train = {}
        att_t: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0, 0])
        for (t, o, d, bd), v in tables["train"].items():
            axe = self.axes_couple.get((t, o, d))
            if axe is None:
                continue
            f_axe, r_axe = self.taux_bande(axe, bd)
            e_f, e_r = v[0] * f_axe, v[2] * r_axe
            c = att_t[(t, o, d)]
            c[0] += v[1]
            c[1] += e_f
            c[2] += v[3]
            c[3] += e_r
            c[4] += v[0]
            c[5] += v[2]
        self.kt_ferme = force_gamma([(c[0], c[1]) for c in att_t.values()])
        self.kt_rouvre = force_gamma([(c[2], c[3]) for c in att_t.values()])
        for cle, c in att_t.items():
            self.m_train[cle] = (
                (c[0] + self.kt_ferme) / (c[1] + self.kt_ferme) if c[1] > 0 else 1.0,
                (c[2] + self.kt_rouvre) / (c[3] + self.kt_rouvre) if c[3] > 0 else 1.0,
            )
            self.n_train[cle] = (int(c[4]), int(c[0]), int(c[5]), int(c[2]))

        # Un lissage vers 1 ne conserve pas les totaux : la moyenne des rapports
        # n'est pas le rapport des moyennes. Sans recentrage, la couche par
        # train déplacerait le niveau général du modèle alors qu'elle n'est
        # censée redistribuer qu'entre trains. On la renormalise donc pour que
        # la somme des événements prédits sur la période d'apprentissage égale
        # la somme observée.
        self.recentrage = (1.0, 1.0)
        sf = [0.0, 0.0]
        sr = [0.0, 0.0]
        for cle, c in att_t.items():
            mf, mr = self.m_train[cle]
            sf[0] += c[1] * mf
            sf[1] += c[0]
            sr[0] += c[3] * mr
            sr[1] += c[2]
        cf = sf[1] / sf[0] if sf[0] > 0 else 1.0
        cr = sr[1] / sr[0] if sr[0] > 0 else 1.0
        self.recentrage = (cf, cr)
        for cle in self.m_train:
            mf, mr = self.m_train[cle]
            self.m_train[cle] = (mf * cf, mr * cr)

        # ── couche 5 : le couple dans sa bande ───────────────────────────────
        # La question de l'abonné n'est pas « ce train part-il tôt », mais « ce
        # train ouvre-t-il à J-30 ». Un train qui libère tout son quota à
        # l'ouverture et un autre qui le distille à J-11 peuvent avoir le même
        # tempérament moyen : seule une voix par bande les sépare. Sept relevés
        # ne la portaient pas (un passage par jour et par couple dans la bande
        # `ouverture`) ; à la mi-octobre, la moitié des couples en comptent
        # plus de cinquante.
        #
        # Le repli se fait sur la couche 4 et non sur 1 : un couple muet dans
        # une bande garde le tempérament que ses autres bandes lui connaissent.
        # Seules les bandes courantes ont une voix ; les libellés hérités ne
        # recouvrent pas les mêmes horizons et continuent de nourrir la seule
        # couche 4.
        self.m_bande = {}
        self.n_bande = {}
        self.kb_ferme, self.kb_rouvre = {}, {}
        par_bande: dict[str, list[tuple]] = defaultdict(list)
        for (t, o, d, bd), v in tables["train"].items():
            if bd not in BANDES:
                continue
            axe = self.axes_couple.get((t, o, d))
            if axe is None:
                continue
            f_axe, r_axe = self.taux_bande(axe, bd)
            mf, mr = self.m_train.get((t, o, d), (1.0, 1.0))
            par_bande[bd].append(((t, o, d), v[1], v[0] * f_axe * mf,
                                  v[3], v[2] * r_axe * mr, v[0], v[2]))
        for bd, lignes in par_bande.items():
            kf = force_gamma([(l[1], l[2]) for l in lignes])
            kr = force_gamma([(l[3], l[4]) for l in lignes])
            self.kb_ferme[bd], self.kb_rouvre[bd] = kf, kr
            brut = {}
            for cle, kof, ef, kor, er, no, nf in lignes:
                brut[cle] = ((kof + kf) / (ef + kf) if ef > 0 else 1.0,
                             (kor + kr) / (er + kr) if er > 0 else 1.0)
                self.n_bande[(cle, bd)] = (no, kof, nf, kor)
            # Même recentrage qu'à la couche 4, bande par bande : la couche
            # redistribue entre trains, elle ne déplace pas le niveau de la bande.
            sf = sum(l[2] * brut[l[0]][0] for l in lignes)
            sr = sum(l[4] * brut[l[0]][1] for l in lignes)
            cf = sum(l[1] for l in lignes) / sf if sf > 0 else 1.0
            cr = sum(l[3] for l in lignes) / sr if sr > 0 else 1.0
            for cle, (mf, mr) in brut.items():
                self.m_bande[(cle, bd)] = (mf * cf, mr * cr)

    def taux_bande(self, axe: str, bd: str) -> tuple[float, float]:
        """Les taux de l'axe, moyennés sur les horizons que couvre une bande.

        Une bande couvre plusieurs horizons ; on répartit son effectif sur eux
        au prorata du volume global, faute de connaître le détail. C'est cette
        répartition qui permet aux deux découpages de coexister sans se
        contredire : une ligne héritée et une ligne courante ne portent pas la
        même étendue, mais chacune est ramenée aux horizons qui sont réellement
        les siens avant d'être comparée à son axe.
        """
        lo, hi = PORTEE_BANDE[bd]
        po = sum(self.global_n[h][0] for h in range(lo, hi + 1)) or 1
        pf = sum(self.global_n[h][2] for h in range(lo, hi + 1)) or 1
        return (
            sum(self.global_n[h][0] / po * self.a_ferme[(axe, h)] for h in range(lo, hi + 1)),
            sum(self.global_n[h][2] / pf * self.a_rouvre[(axe, h)] for h in range(lo, hi + 1)),
        )

    # ── prédiction ───────────────────────────────────────────────────────────
    def taux(self, axe, h, *, pointe=None, jour_voyage=None, couple=None):
        """Les deux taux d'une case, en repliant ce qui manque."""
        if axe in self.axes_clos:
            return (0.0, 0.0)
        f = self.a_ferme.get((axe, h), self.g_ferme.get(h, 0.0))
        r = self.a_rouvre.get((axe, h), self.g_rouvre.get(h, 0.0))
        if pointe is not None:
            mf, mr = self.m_pointe.get((axe, pointe), (1.0, 1.0))
            f, r = f * mf, r * mr
        if jour_voyage is not None:
            mf, mr = self.m_jour.get(jour_voyage, (1.0, 1.0))
            f, r = f * mf, r * mr
        if couple is not None:
            mf, mr = self.m_train.get(couple, (1.0, 1.0))
            f, r = f * mf, r * mr
            mf, mr = self.m_bande.get((couple, bande(h)), (1.0, 1.0))
            f, r = f * mf, r * mr
        return (min(max(f, 0.0), 0.999), min(max(r, 0.0), 0.999))

    def propager(self, p_ouvert: float, depart_horizon: int, **kw) -> list[float]:
        """Propage la chaîne de l'horizon donné jusqu'au départ.

        Rend la probabilité d'être ouvert à chaque horizon, de `depart_horizon`
        jusqu'à 0. L'horizon 0 est l'état au départ : il n'est pas observé, il
        est calculé. C'est la valeur que l'interface appelle « survie ».
        """
        p = p_ouvert
        suite = [p]
        for h in range(depart_horizon, 0, -1):
            f, r = self.taux(kw.get("axe"), h, pointe=kw.get("pointe"),
                             jour_voyage=kw.get("jour_voyage"), couple=kw.get("couple"))
            p = p * (1 - f) + (1 - p) * r
            suite.append(p)
        return suite


# ─────────────────────────────────────────────────────────────────────────────
# Validation par retenue

def calibration(paires: list[tuple[float, int, int]], tranches: int = 10) -> list[dict]:
    """Regroupe (probabilité prédite, succès, essais) par tranche de probabilité.

    On compare une moyenne de probabilités prédites à une fréquence observée.
    Les tranches sont des quantiles d'effectif et non des largeurs égales :
    sinon neuf tranches sur dix seraient vides, la plupart des taux vivant sous
    5 %.
    """
    paires = [(p, k, n) for p, k, n in paires if n > 0]
    paires.sort(key=lambda x: x[0])
    total = sum(n for _, _, n in paires)
    if total == 0:
        return []
    cible = total / tranches
    sortie, cur, acc = [], [], 0
    for p, k, n in paires:
        cur.append((p, k, n))
        acc += n
        if acc >= cible and len(sortie) < tranches - 1:
            sortie.append(cur)
            cur, acc = [], 0
    if cur:
        sortie.append(cur)
    lignes = []
    for grp in sortie:
        n = sum(x[2] for x in grp)
        k = sum(x[1] for x in grp)
        pred = sum(x[0] * x[2] for x in grp) / n
        lo, hi = wilson(k, n)
        lignes.append({"n": n, "k": k, "predit": pred, "observe": k / n,
                       "ic_bas": lo, "ic_haut": hi,
                       "p_min": grp[0][0], "p_max": grp[-1][0]})
    return lignes


def scores(paires: list[tuple[float, int, int]]) -> dict:
    """Brier, log-perte et écart de calibration, pondérés par l'effectif."""
    n_tot = sum(n for _, _, n in paires)
    if n_tot == 0:
        return {}
    brier = sum(n * (p * p) + k * (1 - 2 * p) for p, k, n in paires) / n_tot
    logp = 0.0
    for p, k, n in paires:
        pc = min(max(p, 1e-6), 1 - 1e-6)
        logp += k * math.log(pc) + (n - k) * math.log(1 - pc)
    attendu = sum(p * n for p, _, n in paires)
    observe = sum(k for _, k, _ in paires)
    ece = sum(abs(l["predit"] - l["observe"]) * l["n"] for l in calibration(paires)) / n_tot
    return {
        "n": n_tot, "brier": brier, "logperte": -logp / n_tot,
        "attendu": attendu, "observe": observe,
        "biais_relatif": (attendu - observe) / observe if observe else float("nan"),
        "ece": ece,
    }


def composition(m: "Modele", tables: dict, pas_min: int = 5,
                effectif_min: int = 200) -> dict:
    """La composition de taux marginaux tient-elle, à date de voyage fixée ?

    L'épreuve évidente — partir du stock du bord de fenêtre et propager jusqu'au
    départ — est piégée : le stock observé à l'horizon h et celui observé à
    l'horizon h−1 ne portent pas sur les mêmes dates de voyage. À sept relevés,
    l'horizon 29 couvre la mi-septembre et l'horizon 6 la fin août. On y
    comparerait deux populations, pas deux instants d'une même population, et
    l'on prendrait le calendrier pour une dérive du modèle.

    La bonne question se pose donc à date de voyage fixée : on part du stock
    réellement observé au plus grand horizon de la cohorte, on propage, et on
    compare au stock observé au plus petit. Le prix de cette rigueur est la
    portée : une date de voyage n'est suivie que sur les horizons que l'archive
    a vus passer. Depuis le 16 septembre, les dates entrées par le bord de
    fenêtre sont suivies sur toute la chaîne ; `pas_valides` dit jusqu'où.
    """
    par_cle: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for (axe, dv, h), v in tables["calendrier"].items():
        for i in range(4):
            par_cle[(axe, dv, int(h))][i] += v[i]

    suites: dict[tuple, list[int]] = defaultdict(list)
    for axe, dv, h in par_cle:
        suites[(axe, dv)].append(h)

    par_axe: dict[str, list[float]] = defaultdict(list)
    tous: list[float] = []
    longueurs: list[int] = []
    for (axe, dv), hs in suites.items():
        hs.sort(reverse=True)
        if len(hs) < pas_min or hs != list(range(hs[0], hs[0] - len(hs), -1)):
            continue
        debut = par_cle[(axe, dv, hs[0])]
        if debut[0] + debut[2] < effectif_min:
            continue
        p = debut[0] / (debut[0] + debut[2])
        longueurs.append(len(hs))
        j = date.fromisoformat(dv).weekday()
        for h in hs[:-1]:
            f, r = m.taux(axe, h, jour_voyage=j)
            p = p * (1 - f) + (1 - p) * r
        fin = par_cle[(axe, dv, hs[-1])]
        ecart = p - fin[0] / (fin[0] + fin[2])
        par_axe[axe].append(ecart)
        tous.append(ecart)

    def resume(e):
        return {
            "cohortes": len(e),
            "biais_moyen": sum(e) / len(e),
            "ecart_absolu_moyen": sum(map(abs, e)) / len(e),
            "ecart_max": max(map(abs, e)),
        }

    return {
        "pas_valides": f"{pas_min} à {max(longueurs, default=0)} par date de voyage",
        "ensemble": resume(tous) if tous else {},
        "par_axe": {a: resume(e) for a, e in sorted(par_axe.items()) if e},
    }


def valider(jours_retenus: int = 2) -> dict:
    """Estime sur les premiers relevés, prédit les derniers, mesure l'écart."""
    apports = {nom: apports_quotidiens(nom) for nom in ("horizon", "calendrier", "train")}
    jours = sorted(apports["horizon"])
    if len(jours) <= jours_retenus + 1:
        return {"impossible": f"{len(jours)} relevés comptés, retenue impossible"}
    appris, retenus = jours[:-jours_retenus], jours[-jours_retenus:]

    def cumuler(nom, liste):
        t = defaultdict(lambda: [0, 0, 0, 0])
        for j in liste:
            for k, v in apports[nom].get(j, {}).items():
                for i in range(4):
                    t[k][i] += v[i]
        return dict(t)

    axes_couple = lire_axes()
    tables_app = {nom: cumuler(nom, appris) for nom in apports}
    tables_app["stabilite"] = {}
    m = Modele(tables_app, axes_couple)

    resultats = {"appris_sur": appris, "retenus": retenus, "epreuves": {}}

    # Épreuve 1 — la table par horizon, prédite par les seules couches 0 à 2.
    ep = {"ferme": [], "rouvre": []}
    base = {"ferme": [], "rouvre": []}
    for (axe, h, pte), v in cumuler("horizon", retenus).items():
        h = int(h)
        f, r = m.taux(axe, h, pointe=pte)
        ep["ferme"].append((f, v[1], v[0]))
        ep["rouvre"].append((r, v[3], v[2]))
        base["ferme"].append((m.g_ferme[h], v[1], v[0]))
        base["rouvre"].append((m.g_rouvre[h], v[3], v[2]))
    resultats["epreuves"]["horizon"] = {
        "modele": {s: scores(ep[s]) for s in ep},
        "temoin_global": {s: scores(base[s]) for s in base},
        "calibration": {s: calibration(ep[s]) for s in ep},
    }

    # Épreuve 2 — la table calendrier : c'est le vrai test de la composition,
    # puisque la case y croise déjà l'axe, l'horizon et la date de voyage.
    ep = {"ferme": [], "rouvre": []}
    sans_jour = {"ferme": [], "rouvre": []}
    for (axe, dv, h), v in cumuler("calendrier", retenus).items():
        h = int(h)
        j = date.fromisoformat(dv).weekday()
        f, r = m.taux(axe, h, jour_voyage=j)
        f0, r0 = m.taux(axe, h)
        ep["ferme"].append((f, v[1], v[0]))
        ep["rouvre"].append((r, v[3], v[2]))
        sans_jour["ferme"].append((f0, v[1], v[0]))
        sans_jour["rouvre"].append((r0, v[3], v[2]))
    resultats["epreuves"]["calendrier"] = {
        "modele": {s: scores(ep[s]) for s in ep},
        "temoin_sans_jour": {s: scores(sans_jour[s]) for s in sans_jour},
        "calibration": {s: calibration(ep[s]) for s in ep},
    }

    # Épreuve 3 — la table par train : le lissage y gagne-t-il sa place, et la
    # voix par bande (couche 5) bat-elle le seul tempérament du train ?
    ep = {"ferme": [], "rouvre": []}
    sans_bande = {"ferme": [], "rouvre": []}
    sans_train = {"ferme": [], "rouvre": []}
    brut = {"ferme": [], "rouvre": []}
    # Les mêmes paires, rangées par bande : c'est bande par bande que la
    # couche 5 entre ou non dans l'artefact.
    par_bande: dict[str, dict[str, dict[str, list]]] = defaultdict(
        lambda: {c: {"ferme": [], "rouvre": []} for c in ("modele", "sans_bande", "axe_seul")})
    app_train: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for (t, o, d, bd), v in cumuler("train", appris).items():
        for i in range(4):
            app_train[(t, o, d, bd)][i] += v[i]
    for (t, o, d, bd), v in cumuler("train", retenus).items():
        axe = axes_couple.get((t, o, d))
        if axe is None:
            continue
        f_axe, r_axe = m.taux_bande(axe, bd)
        mf, mr = m.m_train.get((t, o, d), (1.0, 1.0))
        bf, br = m.m_bande.get(((t, o, d), bd), (1.0, 1.0))
        p = {
            "modele": (min(f_axe * mf * bf, 0.999), min(r_axe * mr * br, 0.999)),
            "sans_bande": (min(f_axe * mf, 0.999), min(r_axe * mr, 0.999)),
            "axe_seul": (f_axe, r_axe),
        }
        ep["ferme"].append((p["modele"][0], v[1], v[0]))
        ep["rouvre"].append((p["modele"][1], v[3], v[2]))
        sans_bande["ferme"].append((p["sans_bande"][0], v[1], v[0]))
        sans_bande["rouvre"].append((p["sans_bande"][1], v[3], v[2]))
        sans_train["ferme"].append((f_axe, v[1], v[0]))
        sans_train["rouvre"].append((r_axe, v[3], v[2]))
        a = app_train.get((t, o, d, bd), [0, 0, 0, 0])
        brut["ferme"].append((a[1] / a[0] if a[0] else f_axe, v[1], v[0]))
        brut["rouvre"].append((a[3] / a[2] if a[2] else r_axe, v[3], v[2]))
        for c, (pf, pr) in p.items():
            par_bande[bd][c]["ferme"].append((pf, v[1], v[0]))
            par_bande[bd][c]["rouvre"].append((pr, v[3], v[2]))
    resultats["epreuves"]["train"] = {
        "modele": {s: scores(ep[s]) for s in ep},
        "temoin_sans_bande": {s: scores(sans_bande[s]) for s in sans_bande},
        "temoin_axe_seul": {s: scores(sans_train[s]) for s in sans_train},
        "temoin_brut_non_lisse": {s: scores(brut[s]) for s in brut},
        "calibration": {s: calibration(ep[s]) for s in ep},
    }
    resultats["bandes"] = {
        bd: {c: {s: scores(paires) for s, paires in sens.items()} for c, sens in blocs.items()}
        for bd, blocs in sorted(par_bande.items()) if bd in BANDES
    }

    # ── dérive : le même modèle appliqué à chaque relevé, un par un ──────────
    # Si le biais du jour retenu se retrouve, en plus petit, sur les jours
    # d'apprentissage eux-mêmes, c'est le modèle qui est mal spécifié. S'il
    # n'apparaît que le dernier jour, c'est la journée qui est particulière —
    # et sept relevés ne permettent pas de faire la différence autrement.
    resultats["derive"] = []
    for j in jours:
        f_, r_ = [], []
        for (axe, dv, h), v in apports["calendrier"].get(j, {}).items():
            h = int(h)
            jj = date.fromisoformat(dv).weekday()
            f, r = m.taux(axe, h, jour_voyage=jj)
            f_.append((f, v[1], v[0]))
            r_.append((r, v[3], v[2]))
        resultats["derive"].append({
            "jour": j, "retenu": j in retenus,
            "ferme": scores(f_), "rouvre": scores(r_),
        })
    return resultats


# ─────────────────────────────────────────────────────────────────────────────
# L'artefact

def q(x: float, ech: int = 10000) -> int:
    return int(round(x * ech))


def voix_validees(validation: dict, gain_min: float = 0.005) -> list[tuple[str, str]]:
    """Les (bande, sens) où la couche 5 bat la couche 4 sur la retenue.

    Une voix qui ne prédit pas mieux que le tempérament du train n'est que du
    poids dans le binaire, ou pire, du bruit qu'on présenterait comme un fait.
    Et une voix qui gagne un millième de log-perte coûte autant d'octets qu'une
    qui en gagne huit centièmes : sous le demi-point, elle reste dehors.
    Sans validation, aucune n'entre : on ne publie pas ce qu'on n'a pas éprouvé.
    """
    voix = []
    for bd, blocs in (validation or {}).get("bandes", {}).items():
        for sens in ("ferme", "rouvre"):
            avec, sans = blocs["modele"][sens], blocs["sans_bande"][sens]
            if avec and sans and avec["logperte"] < sans["logperte"] * (1 - gain_min):
                voix.append((bd, sens))
    return voix


def reserves(m: Modele, jours: list[str], validation: dict) -> list[str]:
    """Ce que le modèle ne sait pas, écrit d'après ce qu'il a vu.

    La v1 figeait ces phrases sur sept relevés ; elles mentaient dès le
    huitième. Elles se calculent donc, comme le reste.
    """
    debut, fin = date.fromisoformat(jours[0]), date.fromisoformat(jours[-1])
    out = [
        f"{len(jours)} relevés comptés, du {debut:%d/%m/%Y} au {fin:%d/%m/%Y} : "
        "les saisons que l'archive n'a pas encore traversées (Noël, hiver, ponts "
        "de mai, été) ne sont pas apprises ; le jour de semaine du voyage l'est.",
    ]
    ouv = [n[2] for (_, bd), n in m.n_bande.items() if bd == "ouverture"]
    if ouv:
        assez = sum(n >= 50 for n in ouv)
        out.append(
            f"À l'ouverture, {assez} couples (train, OD) sur {len(ouv)} comptent "
            "au moins cinquante passages : leur pronostic est celui du train. "
            "Les autres empruntent davantage à leur axe."
        )
    ep = (validation or {}).get("epreuves", {}).get("train", {}).get("modele", {})
    if ep.get("ferme") and ep.get("rouvre"):
        n_ret = len(validation["retenus"])
        out.append(
            f"Sur les {n_ret} derniers relevés, tenus à l'écart de l'apprentissage, "
            f"le modèle prévoit {abs(ep['ferme']['biais_relatif']):.0%} de fermetures "
            f"{'de moins' if ep['ferme']['biais_relatif'] < 0 else 'de plus'} et "
            f"{abs(ep['rouvre']['biais_relatif']):.0%} de réouvertures "
            f"{'de moins' if ep['rouvre']['biais_relatif'] < 0 else 'de plus'} "
            "qu'observé : afficher des bandes, jamais un pourcentage."
        )
    out.append(
        "Le relevé fige l'état à 04 h 24 UTC ; une place ouverte et reprise dans "
        "la journée est invisible. Le modèle sous-estime le brassage réel."
    )
    return out


def construire_artefact(m: Modele, tables: dict, validation: dict,
                        compo: dict | None = None) -> dict:
    """Rend le modèle sous une forme qu'un client iOS lit sans rien recalculer.

    Contrainte de taille : le fichier voyage dans le binaire de l'app. Les taux
    sont donc des entiers en dix-millièmes, les gares un dictionnaire, et les
    couples (train, OD) ne sont retenus que s'ils apportent quelque chose que
    l'axe ne dit pas déjà. Un multiplicateur à 1,02 estimé sur onze passages ne
    mérite pas ses vingt octets.
    """
    jours = [j for _, j in releves_git()]
    art = {
        "version": "modele-v2",
        "genere_le": datetime.now(timezone.utc).date().isoformat(),
        "source": "SNCF Voyageurs — jeu « tgvmax » (ODbL), archivé par maxplor-tgvmax-archive",
        "licence": "ODbL",
        "fenetre": {
            "premier_releve_compte": jours[0],
            "dernier_releve_compte": jours[-1],
            "releves_comptes": len(jours),
        },
        "horizons": [HORIZON_MIN, HORIZON_MAX],
        "bandes": {b: list(v) for b, v in BANDES.items()},
        "bandes_heritees": {b: list(v) for b, v in BANDES_HERITEES.items()},
        "jours": list(JOURS),
    }

    art["global"] = {
        "ferme": [q(m.g_ferme[h]) for h in range(1, 31)],
        "rouvre": [q(m.g_rouvre[h]) for h in range(1, 31)],
        "n_ouvert": [m.global_n[h][0] for h in range(1, 31)],
        "n_ferme": [m.global_n[h][2] for h in range(1, 31)],
    }

    art["axes"] = {}
    for a in m.axes:
        art["axes"][a] = {
            "clos": a in m.axes_clos,
            "ferme": [q(m.a_ferme[(a, h)]) for h in range(1, 31)],
            "rouvre": [q(m.a_rouvre[(a, h)]) for h in range(1, 31)],
            "n_ouvert": [m.a_n[(a, h)][0] for h in range(1, 31)],
            "n_ferme": [m.a_n[(a, h)][2] for h in range(1, 31)],
            "pointe": {
                p: [q(v[0], 1000), q(v[1], 1000)]
                for (aa, p), v in sorted(m.m_pointe.items()) if aa == a
            },
        }

    art["jour_voyage"] = {
        JOURS[j]: {
            "m_ferme": q(m.m_jour[j][0], 1000),
            "m_rouvre": q(m.m_jour[j][1], 1000),
            "n_ouvert": m.n_jour[j][0],
            "n_ferme": m.n_jour[j][2],
        }
        for j in range(7)
    }

    # ── les couples ──────────────────────────────────────────────────────────
    # On ne garde que ceux qui s'écartent visiblement de leur axe. Le seuil est
    # posé sur l'écart du multiplicateur lissé, donc après que le lissage a déjà
    # ramené vers 1 tout ce qui n'était que du bruit.
    #
    # Le fichier voyage dans l'app : à quatre-vingts relevés, les couples qui
    # méritent leur place sont deux fois plus nombreux qu'à sept, et le format
    # de la v1 dépassait la cible. Trois économies, aucune perte utile :
    # les couples sont groupés par train (un numéro dessert plusieurs OD), les
    # indices sont des écarts au précédent, et les multiplicateurs sont en
    # centièmes — l'interface affiche des bandes, pas le troisième chiffre.
    SEUIL, MIN_OUVERT, MIN_FERME = 0.12, 20, 40
    gares, index_gare = [], {}

    def ig(code: str) -> int:
        if code not in index_gare:
            index_gare[code] = len(gares)
            gares.append(code)
        return index_gare[code]

    def creux(paires: list[tuple[int, float]]) -> list[int]:
        """[écart d'index, multiplicateur en centièmes], à plat."""
        out, prec = [], 0
        for i, x in paires:
            out += [i - prec, q(x, 100)]
            prec = i
        return out

    voix = voix_validees(validation)
    gardes: dict[tuple, int] = {}
    sans_max, mf_p, mr_p = [], [], []
    mb_p: dict[tuple[str, str], list] = {v: [] for v in voix}
    for cle in sorted(m.m_train):
        mf, mr = m.m_train[cle]
        no, _, nf, _ = m.n_train[cle]
        muet = no == 0
        garde_f = abs(mf - 1) >= SEUIL and no >= MIN_OUVERT
        garde_r = abs(mr - 1) >= SEUIL and nf >= MIN_FERME
        garde_b = []
        for bd, sens in voix:
            if (cle, bd) not in m.m_bande:
                continue
            b = m.m_bande[(cle, bd)][0 if sens == "ferme" else 1]
            nb = m.n_bande[(cle, bd)][0 if sens == "ferme" else 2]
            if abs(b - 1) >= SEUIL and nb >= (MIN_OUVERT if sens == "ferme" else MIN_FERME):
                garde_b.append((bd, sens, b))
        if not (garde_f or garde_r or muet or garde_b):
            continue
        i = gardes[cle] = len(gardes)
        if muet:
            sans_max.append(i)
        if garde_f:
            mf_p.append((i, mf))
        if garde_r:
            mr_p.append((i, mr))
        for bd, sens, b in garde_b:
            mb_p[(bd, sens)].append((i, b))

    groupes: dict[str, list[str]] = {}
    for t, o, d in gardes:
        groupes.setdefault(t, []).append(f"{ig(o)}.{ig(d)}")
    art["trains"] = {
        "format": (
            "`cles` : groupes « train:iO.iD,iO.iD » séparés par ';', l'index "
            "renvoyant à `gares` ; l'ordre de lecture des couples donne leur "
            "index, à partir de 0. `sans_place_max` : indices des couples dont "
            "aucune place Max n'a jamais été observée. `m_ferme` / `m_rouvre` "
            "(couche 4, toutes bandes) et `bandes.<bande>.<sens>` (couche 5, en "
            "plus de la couche 4) : listes à plat [écart d'index, multiplicateur "
            "en centièmes, …], l'écart partant de 0 ; tout couple absent vaut 100, "
            "c'est-à-dire « ne rien changer ». Seules figurent les bandes dont la "
            "voix bat le seul tempérament du train sur la retenue."
        ),
        "seuils": {"ecart_min": SEUIL, "n_ouvert_min": MIN_OUVERT, "n_ferme_min": MIN_FERME},
        "gares": gares,
        "cles": ";".join(f"{t}:{','.join(ods)}" for t, ods in groupes.items()),
        "sans_place_max": [i - p for p, i in zip([0] + sans_max, sans_max)],
        "m_ferme": creux(mf_p),
        "m_rouvre": creux(mr_p),
        "bandes": {
            bd: {sens: creux(mb_p[(b, sens)]) for b, sens in voix if b == bd and mb_p[(b, sens)]}
            for bd in sorted({b for b, s in voix if mb_p[(b, s)]})
        },
        "couples_suivis": len(m.m_train),
    }

    # ── ce que le modèle sait de lui-même ────────────────────────────────────
    ouv = {h: m.global_n[h] for h in (30, 12, 4)}
    art["vagues"] = {
        "note": "horizons de départ où le flux d'ouverture est massif ; "
                "la place s'ouvre en arrivant à l'horizon h-1",
        "horizons": [30, 12, 4],
        "taux_rouvre": {str(h): q(m.g_rouvre[h]) for h in (30, 12, 4)},
        "n_ferme": {str(h): ouv[h][2] for h in (30, 12, 4)},
    }
    art["forces_lissage"] = {
        "beta_axe_ferme": {str(h): round(m.k_ferme[h], 1) for h in range(1, 31)},
        "beta_axe_rouvre": {str(h): round(m.k_rouvre[h], 1) for h in range(1, 31)},
        "gamma_train_ferme": round(m.kt_ferme, 1),
        "gamma_train_rouvre": round(m.kt_rouvre, 1),
        "gamma_bande_ferme": {bd: round(k, 1) for bd, k in sorted(m.kb_ferme.items())},
        "gamma_bande_rouvre": {bd: round(k, 1) for bd, k in sorted(m.kb_rouvre.items())},
    }
    art["validation"] = _resume_validation(validation)
    if compo:
        art["composition"] = compo
    art["reserves"] = reserves(m, jours, validation)
    return art


def _resume_validation(v: dict) -> dict:
    if not v:
        return {"non_executee": True}
    if "impossible" in v:
        return v
    out = {"appris_sur": v["appris_sur"], "retenus": v["retenus"], "epreuves": {}}
    for nom, ep in v["epreuves"].items():
        out["epreuves"][nom] = {
            cle: {s: {k: (round(x, 6) if isinstance(x, float) else x)
                      for k, x in sc.items()}
                  for s, sc in bloc.items()}
            for cle, bloc in ep.items() if cle != "calibration"
        }
        out["epreuves"][nom]["calibration"] = {
            s: [{k: (round(x, 6) if isinstance(x, float) else x) for k, x in l.items()}
                for l in lignes]
            for s, lignes in ep["calibration"].items()
        }
    out["bandes"] = {
        bd: {c: {s: {"n": sc["n"], "logperte": round(sc["logperte"], 6),
                      "biais_relatif": round(sc["biais_relatif"], 5)}
                  for s, sc in sens.items() if sc}
             for c, sens in blocs.items()}
        for bd, blocs in v.get("bandes", {}).items()
    }
    out["derive"] = [
        {"jour": d["jour"], "retenu": d["retenu"],
         "ferme_biais": round(d["ferme"]["biais_relatif"], 5),
         "rouvre_biais": round(d["rouvre"]["biais_relatif"], 5)}
        for d in v.get("derive", [])
    ]
    return out


# ─────────────────────────────────────────────────────────────────────────────

def afficher_validation(v: dict) -> None:
    if "impossible" in v:
        print(f"validation impossible : {v['impossible']}")
        return
    print(f"appris sur {', '.join(v['appris_sur'])} — retenus {', '.join(v['retenus'])}")
    for nom, ep in v["epreuves"].items():
        print(f"\n── épreuve « {nom} »")
        for cle, bloc in ep.items():
            if cle == "calibration":
                continue
            for sens, sc in bloc.items():
                if not sc:
                    continue
                print(f"   {cle:<22} {sens:<7} n={sc['n']:>8}  "
                      f"attendu={sc['attendu']:>9.0f}  observé={sc['observe']:>7}  "
                      f"biais={sc['biais_relatif']:+7.1%}  "
                      f"logperte={sc['logperte']:.5f}  ECE={sc['ece']:.5f}")
        for sens, lignes in ep["calibration"].items():
            detail = "  ".join(
                "{:.2f}→{:.2f}%".format(l["predit"] * 100, l["observe"] * 100)
                for l in lignes
            )
            print(f"   calibration ({sens}) : {detail}")
    if v.get("bandes"):
        print("\n── la couche 5, bande par bande (log-perte sur la retenue)")
        for bd, blocs in v["bandes"].items():
            for sens in ("ferme", "rouvre"):
                sc = {c: blocs[c][sens] for c in blocs}
                if not sc["modele"]:
                    continue
                gain = 1 - sc["modele"]["logperte"] / sc["sans_bande"]["logperte"]
                print(f"   {bd:<15} {sens:<7} n={sc['modele']['n']:>8}  "
                      f"axe={sc['axe_seul']['logperte']:.5f}  "
                      f"train={sc['sans_bande']['logperte']:.5f}  "
                      f"train×bande={sc['modele']['logperte']:.5f}  ({gain:+.1%})  "
                      f"biais={sc['modele']['biais_relatif']:+.1%}")
    if v.get("derive"):
        print("\n── dérive jour par jour (modèle appris sur les seuls premiers relevés)")
        for d in v["derive"]:
            marque = "retenu" if d["retenu"] else "appris"
            print(f"   {d['jour']} {marque}  "
                  f"ferme attendu={d['ferme']['attendu']:>7.0f} observé={d['ferme']['observe']:>6}"
                  f" ({d['ferme']['biais_relatif']:+6.1%})   "
                  f"rouvre attendu={d['rouvre']['attendu']:>7.0f} "
                  f"observé={d['rouvre']['observe']:>6} ({d['rouvre']['biais_relatif']:+6.1%})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sortie", type=Path, default=SORTIE)
    # Deux semaines : assez pour que chaque jour de semaine soit retenu deux
    # fois, et pour qu'une journée hors norme ne décide pas seule d'une voix.
    ap.add_argument("--retenue", type=int, default=14,
                    help="nombre de relevés gardés hors de l'estimation")
    ap.add_argument("--sans-validation", action="store_true")
    args = ap.parse_args()

    print("→ lecture des compteurs")
    tables = {nom: lire_table(nom) for nom in CLES}
    for nom, t in tables.items():
        print(f"   {nom:<11} {len(t):>6} cases")
    axes_couple = lire_axes()
    print(f"   référentiel {len(axes_couple)} couples (train, OD)")

    print("→ ajustement du modèle sur la totalité de l'archive")
    m = Modele(tables, axes_couple)
    print(f"   axes : {len(m.axes)} dont {len(m.axes_clos)} sans aucune place Max "
          f"({', '.join(m.axes_clos)})")
    print(f"   forces de lissage : gamma train ferme={m.kt_ferme:.1f} "
          f"rouvre={m.kt_rouvre:.1f}")

    print("→ épreuve de composition, à date de voyage fixée")
    compo = composition(m, tables)
    ens = compo["ensemble"]
    print(f"   {ens['cohortes']} cohortes — biais moyen {ens['biais_moyen']*100:+.2f} pp, "
          f"écart absolu moyen {ens['ecart_absolu_moyen']*100:.2f} pp, "
          f"max {ens['ecart_max']*100:.2f} pp")
    for a, r in compo["par_axe"].items():
        print(f"     {a:<18} n={r['cohortes']:>3}  biais {r['biais_moyen']*100:+6.2f} pp  "
              f"|écart| {r['ecart_absolu_moyen']*100:5.2f} pp  max {r['ecart_max']*100:5.2f} pp")

    validation = {}
    if not args.sans_validation:
        print(f"→ validation par retenue des {args.retenue} derniers relevés")
        validation = valider(args.retenue)
        afficher_validation(validation)

    print("\n→ écriture de l'artefact")
    art = construire_artefact(m, tables, validation, compo)
    args.sortie.parent.mkdir(parents=True, exist_ok=True)
    args.sortie.write_text(
        json.dumps(art, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    taille = args.sortie.stat().st_size
    tr = art["trains"]
    print(f"   {args.sortie} — {taille/1024:.0f} Ko")
    n_couples = sum(len(g.split(":")[1].split(",")) for g in tr["cles"].split(";"))
    print(f"   couples : {n_couples} retenus sur {len(m.m_train)} — "
          f"{len(tr['sans_place_max'])} sans place Max, "
          f"{len(tr['m_ferme'])//2} avec un multiplicateur de fermeture, "
          f"{len(tr['m_rouvre'])//2} de réouverture")
    for bd, sens in tr["bandes"].items():
        print(f"   voix de bande « {bd} » : "
              + ", ".join(f"{len(v)//2} couples en {s}" for s, v in sens.items()))
    if taille > 500 * 1024:
        print("   ! au-dessus de la cible de 500 Ko", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
