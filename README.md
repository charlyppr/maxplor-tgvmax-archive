# Archive d'ouverture TGVmax

L'open data SNCF publie chaque matin un état : à 04 h 24 UTC, telle place TGVmax
était ouverte ou fermée. Il ne publie pas de trajectoire — or c'est la
trajectoire qui renseigne le voyageur. « Cette place tient d'habitude » et
« ce départ n'est pas encore ouvert » sont deux phrases que le relevé du jour ne
permet pas de prononcer.

Ce dépôt les rend prononçables. Il relève le snapshot une fois par jour, le
compare à celui de la veille, et accumule les passages dans quatre tables. De
ces compteurs se dérive une probabilité.

## Ce que la source permet

Mesuré, pas supposé :

| | |
|---|---|
| Rafraîchissement | une fois par jour, 04 h 24 UTC (cron régulier à la seconde) |
| Export complet | une seule requête, 39 Mo de CSV, 7 Mo gzippés, ~21 s |
| Fenêtre | J+0 à J+30, glissante |
| Volume | ~413 000 lignes, ~412 600 sujets, dont **12,7 % de places ouvertes** |
| Axes | 14 (dont `AUTOCAR SNCF` : le jeu contient des cars) |
| Couples (train, OD) | ~20 600 |
| Doublons de clé | ~780 par snapshot, dont ~760 en conflit OUI/NON |

La fenêtre à trente et un jours est la bonne nouvelle du dispositif : chaque
snapshot contient **tous les horizons à la fois**. Deux relevés consécutifs se
recouvrent sur trente dates de voyage et livrent d'un coup ~400 000 passages
d'un jour, répartis sur les trente horizons. On n'attend donc pas un mois pour
observer une trajectoire complète : on estime le taux à chaque horizon
séparément, puis on les compose. Le signal devient exploitable en une à deux
semaines, pas en six mois.

## Ce que dit le premier relevé

Part de places ouvertes selon l'horizon, relevé du 17/08/2026 :

```
 h  jour     part
30  mer      0.1%
29  mar      9.9%  ██████████
23  mer     11.8%  ████████████
16  mer     16.7%  █████████████████
 9  mer     15.6%  ████████████████
 2  mer     40.8%  █████████████████████████████████████████
```

Trois enseignements, qui ont chacun changé le code.

**L'horizon 30 est un état d'avant-ouverture, pas un état de marché.** À jour de
semaine constant — tous mercredis ci-dessus, donc à composition comparable — le
bord de fenêtre tombe à 0,1 % contre 11,8 % à J+23. À 06 h 24, le quota Max de
J-30 n'est pas encore chargé. Le bord montre donc du vide, ce qui explique
sans doute une part de l'impression que « beaucoup de trains ne sont pas
disponibles ». Conséquence : la bande `ouverture` isole cet horizon dans la table
par train, sans quoi une surcharge d'ouverture entrerait dans le tempérament de
chaque train.

**La disponibilité croît à l'approche du départ.** À jour de semaine constant, la
progression est monotone : 0,1 % → 11,8 % → 16,7 % → 15,6 % → 40,8 %. Les
annulations et le quota relâché tardivement l'emportent largement sur le
remplissage. La branche **réouverture** du modèle est donc la principale, pas
l'accessoire — d'où le comptage symétrique des deux sens.

**L'effet jour de semaine est massif** : de 3,4 % un dimanche à 16,7 % un
mercredi. C'est ce qui justifie de garder la date exacte plutôt qu'une étiquette.

Une réserve honnête : ces trois observations viennent d'un seul relevé. Le bord
d'avant-ouverture est structurel et certain ; la pente et le cycle hebdomadaire
sont forts mais restent à confirmer par l'archive, la mi-août n'étant pas une
semaine ordinaire.

### Ce que les sept premiers relevés en ont dit

La réserve ci-dessus a été levée le 24 août, sur 2 755 821 passages. Le détail
est dans [`ANALYSE-2026-08-24.md`](ANALYSE-2026-08-24.md) ; en trois lignes :

**Le bord d'avant-ouverture est confirmé, et largement.** 0,088 % de places
ouvertes à l'horizon 30, et 10,83 % du parc fermé qui bascule en arrivant à
J-29 — vingt et une fois la médiane des horizons voisins. Isoler cette bande
était le bon choix.

**La pente est fausse : c'est un escalier.** Deux autres vagues existent, à J-11
(8,27 % de réouverture) et à J-3 (16,47 %, la plus forte des trois), contre
1,30 % partout ailleurs. À elles trois elles portent 75 % du flux net. Entre
J-29 et J-13, la fenêtre est plate — dix-sept jours où attendre ne rapporte
rien. Ces deux vagues sont invisibles dans un état instantané : elles ne se
lisent que dans les passages, et sont le premier résultat que l'archive apporte.
C'est ce qui a fait passer les bandes de cinq à six.

**Le cycle hebdomadaire tient, mais moitié moins fort qu'annoncé.** À horizon
standardisé, 7,92 % un dimanche contre 19,16 % un mercredi : ×2,4 et non ×4,9.
Les deux chiffres ci-dessus cumulaient l'effet de jour et l'effet d'horizon. La
conclusion de conception ne change pas — garder la date exacte reste juste.

## Les quatre tables

Croiser toutes les dimensions dans une clé unique donnerait des millions de
cases vides, chacune trop peu peuplée pour dire quoi que ce soit. Quatre tables,
quatre effets orthogonaux, que le modèle recompose en les multipliant. Chacune
reste dense, ce qui est la condition pour qu'elle apprenne vite.

- **`horizon`** — `(axe, horizon, pointe)`. La structure. Bornée à 840 lignes par
  mois, elle ne grandit pas : elle s'affine.
- **`train`** — `(train, origine, destination, bande)`. Le tempérament d'un
  train : part-il tôt ou tard. Six bandes d'horizon, la forme fine venant de la
  table précédente. Les bandes isolent les trois horizons où le quota se
  recharge — 30, 12 et 4 — des stretches calmes qui les séparent : une bande
  qui mêlerait les deux fabriquerait une moyenne à laquelle aucun train ne
  ressemble. Voir `bande()` dans `collecte.py`.
- **`calendrier`** — `(axe, date de voyage, horizon)`. L'horizon y est **exact**
  et non regroupé, ce qui rend la date d'observation exactement récupérable
  (`observation = date de voyage − horizon`). Le rythme de réservation n'est pas
  celui du voyage : personne ne réserve au même rythme un mardi et un dimanche.
- **`stabilite`** — `(horizon, ancienneté, bascules)`. L'histoire du sujet
  lui-même.

Chaque case porte quatre nombres, toujours les mêmes : `n_ouvert`, `ferme`,
`n_ferme`, `rouvre`.

### Pourquoi la date, et non une étiquette

On aurait pu stocker un `jour_de_semaine`, ou un drapeau « vacances ». Ç'aurait
été une erreur sans retour : l'effet Toussaint, les ponts de mai, la montée de
Noël auraient été perdus pour toujours. **On garde la date de voyage elle-même**
— la variable calendaire la plus riche qui existe, dont se déduisent au moment du
modèle le jour de semaine, le mois, la zone de vacances, la proximité d'un férié,
et toute théorie qu'on n'a pas encore eue. Coût : ~434 cases touchées par jour,
~158 000 lignes par an.

Un motif saisonnier demande malgré tout de la répétition pour s'apprendre : on ne
connaîtra l'effet Noël qu'après avoir vu un Noël. La fenêtre à J+30 avance
l'échéance — le 25 novembre, on observe déjà les voyages du 25 décembre.

### Pourquoi l'histoire du sujet

Des compteurs par paires ne permettent de reconstituer **aucune** trajectoire :
ni la durée pendant laquelle une place est restée ouverte, ni le nombre de fois
qu'elle a basculé. Or c'est précisément là que vit « cette place tient » — une
phrase qui n'est une propriété ni de l'axe ni de l'horizon, mais de ce sujet-là.
Une place ouverte sans bouger depuis vingt jours ne se comporte pas comme une
place rouverte hier à J-3.

L'état porte donc, par sujet, l'ancienneté dans l'état courant et le nombre de
bascules — quelques octets par ligne, et impossibles à reconstruire après coup.
Les paliers comptés sont ceux de **la veille** : les seuls qu'un prédicteur
connaîtrait à l'instant de prédire.

### La censure, et comment elle se soigne

Un sujet aperçu pour la première fois en milieu de fenêtre existait avant qu'on
regarde : son ancienneté est inconnue, et la confondre avec « un jour »
empoisonnerait les paliers élevés. Deux drapeaux la marquent, l'un pour
l'ancienneté, l'autre pour le compte de bascules.

Elle ne dure pas : **une bascule observée rend l'ancienneté exacte**, puisqu'on
en tient le point de départ. Mesuré sur la chaîne de test, 20 022 sujets sur
386 603 recouvrent une ancienneté connue en un seul jour. La cohorte d'amorce se
vide donc en quelques semaines, et non en attendant qu'elle quitte la fenêtre.

### L'horizon d'une transition est celui d'où elle part

La transition va de l'état de la veille vers celui du jour : elle **part** de
l'horizon h+1 et **arrive** à h. On l'indexe sur le départ, parce que la question
posée au modèle est « je constate aujourd'hui l'état à l'horizon h, que devient-il
demain ? ». Indexer sur l'arrivée décalerait chaque taux d'un cran, et l'erreur
se propagerait en s'aggravant dans la composition, la pente étant la plus raide
près du départ.

Vérifié de bout en bout : sur une chaîne où l'on injecte une pente connue, les
taux ressortent au bon cran à moins de 0,007 près sur les horizons 1 à 25.

### Partitionnées par mois

Les compteurs sont découpés par mois d'observation
(`compteurs/horizon/2026-08.csv`). Un total unique depuis l'origine interdirait à
jamais de pondérer le récent plus fort que l'ancien — or la demande ferroviaire
dérive, et un modèle bâti dans deux ans ne devrait pas accorder le même poids à
ses premiers mois. Une fois les mois additionnés, on ne les resépare plus : il
fallait trancher tout de suite. Le modèle somme les tranches, en les pondérant
s'il le souhaite.

## Le référentiel

`referentiel.csv` garde les attributs stables de chaque service — axe, entité,
horaires, gares — avec ses dates de première et dernière apparition. Sans lui,
les états datés ne permettraient aucun recalcul : ils ne portent ni l'axe ni
l'heure.

Il rend en prime un service que le jeu brut ne donne pas commodément : **la liste
de tous les (train, OD) jamais vus**, places Max ou non. ~20 600 services.

`dernier_vu` n'est réécrit qu'au-delà d'une semaine d'ancienneté — rafraîchi
chaque jour, il ferait changer les 20 600 lignes tous les matins, et 2 Mo de
texte intégralement réécrit ne se delta pas dans git.

## Le modèle

Une chaîne de Markov à deux états. Pour chaque case, deux taux :

- `P(fermé demain | ouvert à J-k)` — le train se remplit ;
- `P(ouvert demain | fermé à J-k)` — le quota s'ouvre, ou une place se libère.

La survie jusqu'au départ est le produit des `(1 − taux)` sur les horizons
restants, de k jusqu'à 1 — l'horizon 0 n'est pas observable, le train est parti.
Pour les cases peu peuplées, un lissage bêta-binomial replie sur le niveau
supérieur : `(train, OD)` hérite de son axe, un axe rare hérite du global. Vu que
`OUIGO_est` ne pèse que 310 lignes contre 68 544 pour `SUD EST`, ce repli n'est
pas un raffinement mais une nécessité.

Les compteurs sont des statistiques suffisantes pour toute une famille de
modèles ; la forme exacte (produit de taux, régression logistique, gradient
boosting) reste ouverte et se décidera sur la calibration. C'est le substrat qui
compte, et c'est lui qu'on ne peut pas reconstituer.

**La validation prévue :** comparer une survie prédite par composition à une
survie réellement observée sur une fenêtre complète, ce qui demande de garder en
réserve plus d'un cycle de trente et un jours — d'où les soixante derniers états.

## Deux limites à ne pas masquer

**L'angle mort intra-journalier.** Le relevé fige l'état à 04 h 24. Une place qui
s'ouvre à 14 h et part à 15 h est invisible pour toujours. Le modèle décrit la
dynamique de relevé à relevé, pas le processus réel : il sous-estimera
structurellement le brassage.

**Le biais du jeune âge.** Les premières semaines n'auront vu qu'une saison. Les
taux par horizon seront corrects assez vite, les effets de calendrier non. D'où
les compteurs partitionnés et les soixante derniers états gardés en réserve : le
modèle peut être re-dérivé sans réobserver le passé.

Ces deux limites doivent se voir dans l'interface. Des bandes plutôt qu'un
pourcentage brut, et trois directions et non deux : « pas encore ouvert »,
« tient », « risque de partir ».

## Robustesse

- **Garde-fou de plausibilité** : un export amputé servi avec un code 200
  abîmerait l'archive sans bruit. En dessous de 150 000 sujets, ou de 70 % du
  relevé précédent, rien n'est écrit et le script échoue bruyamment.
- **Deux tentatives** de téléchargement à 60 s d'intervalle : un échec coûte une
  journée qu'on ne rattrapera pas.
- **Snapshot rejoué** : le script lit la date de traitement au portail et refuse
  de compter deux fois la même journée.
- **Trou de collecte** : au-delà d'un jour d'écart, le comptage est sauté (deux
  passages mélangés en un vaudraient moins qu'une case vide), mais l'histoire des
  sujets est **reprise** et marquée inconnue. Un échec de téléversement ne
  détruit pas des semaines d'ancienneté.
- **Clé de sujet** : `(date, train, origine, destination)`, sans l'heure de
  départ. Le prix est mesuré — 20 sujets sur 412 573 fusionnent deux services de
  même numéro sur la même liaison le même jour, soit 0,005 %. Y ajouter l'heure
  ferait disparaître un sujet au moindre décalage horaire, rompant la chaîne des
  transitions : on préfère la fusion à la rupture.
- **Doublons en conflit** arbitrés en faveur de OUI : si une ligne affirme qu'une
  place existe, elle existe. Sans cet arbitrage, ils fabriqueraient une fermeture
  et une réouverture chaque matin. Le compte est journalisé pour rester visible.
- **`journal.csv`** enregistre chaque passage — sujets, ouverts, écart, passages,
  entrées et sorties de fenêtre, censures, doublons, durée. Un trou, un
  effondrement du comptage, une montée des doublons s'y voient avant de se voir
  dans le modèle.

## Utilisation

```bash
python3 collecte.py                                        # relève et compte
python3 collecte.py --fichier snap.csv --jour 2026-08-17    # rejoue un CSV local
```

Aucune dépendance : bibliothèque standard seule. Le stockage est en O(1) et non
en O(jours) — l'état de la veille est écrasé, seuls les compteurs s'accumulent, et
ils sont bornés. L'état vit en release asset plutôt que dans git : 2 Mo réécrits
chaque jour, et du gzip ne se delta pas.

## Données et licence

Source : **SNCF Voyageurs**, jeu de données
[`tgvmax`](https://data.sncf.com/explore/dataset/tgvmax/), publié sous
**Open Database License (ODbL)**.

Les compteurs de ce dépôt sont une base de données dérivée. Ils sont donc
distribués sous la même licence ODbL, avec attribution à SNCF Voyageurs, comme la
clause de partage à l'identique l'exige.
