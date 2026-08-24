# « Ouvertures à venir » — plan d'implémentation

Plan pour une surface de l'app Maxplor qui montre **les jours pas encore
ouverts** — J+31 et au-delà, absents du jeu de données — avec, pour chacun, la
date à laquelle il s'ouvrira et un pronostic par liaison.

Ce document est un plan. Il ne modifie pas `charlyppr/Maxplor`, qui n'a été lu
qu'en lecture seule pour ancrer chaque décision dans le code existant.

---

## 1. Le besoin, et pourquoi il n'est pas celui qu'on croit

Un abonné Max sait déjà que rien n'ouvre au-delà de trente jours. Le lui
apprendre serait insultant. Ce qu'il ignore, c'est **lesquels des départs qui
vont s'ouvrir il devra attraper tout de suite, et lesquels peuvent attendre.**

L'analyse du 24 août établit trois faits qui décident de toute la surface :

1. **L'ouverture est un événement d'une journée.** Le bord de fenêtre est vide
   (0,088 % de places ouvertes à J-30, sur 85 311 observations) et 10,83 % du
   parc fermé bascule d'un coup en arrivant à J-29 — vingt et une fois la
   médiane des horizons voisins. Ce n'est pas une montée, c'est un interrupteur.
2. **Attendre ne rapporte rien pendant dix-sept jours.** Entre J-29 et J-13, le
   flux net vaut moins d'un demi-point de parc par jour, avec trois journées de
   recul. Les gains viennent de trois vagues — J-29, J-11, J-3 — qui portent
   75 % du flux net total.
3. **Ce qui part vite part dans les premiers jours.** Le taux de fermeture vaut
   3,02 % à l'horizon 29 et 3,51 % à l'horizon 28, contre 1,92 % à l'horizon 26.
   Le tri se fait tout de suite après l'ouverture.

D'où la thèse de la surface : **la partie se joue à l'ouverture, et l'app doit
dire où se placer avant qu'elle commence.**

Les quantités déterminantes sont donc la transition partant de l'horizon 30 et
les taux aux horizons 29 et 28. Les taux de milieu de fenêtre ne servent à rien
ici.

---

## 2. Ce sur quoi le plan s'appuie dans l'app

Relevé dans `charlyppr/Maxplor` — la surface ne réinvente rien de ce qui suit.

| élément | fichier | ce qu'il apporte |
|---|---|---|
| `AppModel.bookingWindow = 30`, `firstBookableDay`, `lastBookableDay`, `isBookable(_:)` | `Models/AppModel.swift` | la fenêtre réservable, déjà nommée. Les jours de la nouvelle surface sont **exactement ceux que `isBookable` refuse par le haut** |
| `TGVMaxAPI.fetch(where:on:openSeatsOnly:)` | `Models/TGVMaxAPI.swift:1515` | le paramètre `openSeatsOnly` existe déjà et vaut `true` par défaut. Le passer à `false` rend **tous** les trains d'une journée, ouverts ou non : c'est la liste de candidats dont le pronostic a besoin |
| `TGVMaxAPI.Segment` (`trainNo`, `axe`, `originID`, `destinationID`, `departure`) | idem | porte l'axe et le numéro de train — les deux clés du modèle |
| `TGVMaxAPI.directTrips(segments:)` et `merged(_:)` | idem | reconstitue les `Trip` et fusionne les rames jumelées. À réutiliser tel quel : un pronostic ne doit pas dédoubler ce que la liste de résultats a appris à fusionner |
| `StationCatalog` (240 gares, identifiants Resarail) | `Models/Station.swift` | **les mêmes codes que `origine_iata` dans l'archive** (`FRPLY`, `FRPAR`…). Aucune table de correspondance à écrire |
| `TripStore` (clé gare × journée, `freshness = 5 min`, `capacity = 40`) | `Models/TripStore.swift` | le patron de mémoire à imiter — avec une fraîcheur différente, voir § 5 |
| `FavoriteRoute` (un train, sans jour ; `id = "origine>destination|minutes"`) | `Models/FavoriteRoute.swift` | **l'objet naturel de l'affût.** Un favori est déjà une route et une heure sans date : exactement ce qu'on veut guetter à l'ouverture |
| `CalendarView(bookableRange:directDays:)` | `Features/CalendarView.swift` | et surtout sa doctrine : « la marque promet, son absence ne refuse pas ». À reprendre mot pour mot pour le pronostic |
| `Palette`, `Metrics`, `Motion` | `DesignSystem/Theme.swift`, `Motion.swift` | `Palette.caution` pour « part vite », `Palette.steel` pour l'incertain, `Metrics.cardRadius = 30`, `Motion.reveal(_:_:)` et `Motion.stagger(_:)` pour l'apparition en cascade |
| `MaxCard`, `SectionHeader`, `FilterChip`, `SelectionCapsule`, `Skeleton` | `DesignSystem/Components.swift`, `Skeleton.swift` | la surface n'introduit **aucun** composant nouveau |
| `Emissions` (valeurs embarquées + rafraîchissement réseau vers `.cachesDirectory`) | `Models/Emissions.swift:270-292` | le patron exact pour l'artefact de modèle |
| `fileSystemSynchronizedGroups` sur le groupe `Maxplor` | `Maxplor.xcodeproj/project.pbxproj:59` | un fichier déposé dans `Maxplor/Resources/` entre dans la cible **sans toucher au projet Xcode** |

Deux absences à noter, parce qu'elles sont des travaux :

- **Aucun framework de notification n'est lié** (ni `UserNotifications`, ni
  `BackgroundTasks`, ni `UIBackgroundModes`). L'affût en demandera un.
- **Le catalogue de gares ne connaît que 240 gares**, celles vues au moins une
  fois avec une place Max ouverte. L'archive en connaît 343. Les 103 autres
  portent 3 310 couples, dont **152 ont bel et bien montré une place Max
  ouverte** pendant les sept relevés. Ce n'est donc pas un écart sans
  conséquence : `fetch` écarte déjà ces gares
  (`StationCatalog.outOfPlanIDs`), et la surface héritera de ce choix. Il faudra
  décider si ces 152 couples méritent d'entrer au catalogue — question à
  trancher sur les données du dépôt, pas dans ce plan.

---

## 3. Le contrat de l'artefact

`modele/modele-v1.json`, 408 Ko, à déposer en `Maxplor/Resources/modele-v1.json`.

### 3.1 Ce que le client lit

```
{
  "version": "modele-v1",
  "fenetre": { "premier_releve_compte", "dernier_releve_compte", "releves_comptes" },
  "horizons": [1, 30],
  "bandes": { "ouverture": [30,30], "plateau": [13,29], "relance": [12,12],
              "intervalle": [5,11], "bascule": [4,4], "derniers_jours": [1,3] },
  "bandes_heritees": { "lointain": [15,29], "moyen": [8,14], … },
  "jours": ["lun","mar","mer","jeu","ven","sam","dim"],

  "global": { "ferme": [30 entiers], "rouvre": [30], "n_ouvert": [30], "n_ferme": [30] },
  "axes":   { "SUD EST": { "clos": false, "ferme": [30], "rouvre": [30],
                           "n_ouvert": [30], "n_ferme": [30],
                           "pointe": { "pointe": [mf, mr], "creuse": [mf, mr] } }, … },
  "jour_voyage": { "mer": { "m_ferme", "m_rouvre", "n_ouvert", "n_ferme" }, … },

  "trains": { "gares": [342 codes],
              "cles": "trainNo.iOrigine.iDestination;…",
              "sans_place_max": [indices],
              "m_ferme":  [index, millièmes, index, millièmes, …],
              "m_rouvre": [index, millièmes, …] },

  "vagues": { "horizons": [30,12,4], "taux_rouvre": {…}, "n_ferme": {…} },
  "validation": { … },
  "reserves": [ quatre phrases affichables ]
}
```

Conventions, à respecter à la lettre :

- **Les taux sont en dix-millièmes** (`1083` = 10,83 %), les multiplicateurs en
  **millièmes** (`1000` = pas d'écart à l'axe).
- Les tableaux `ferme` / `rouvre` sont **indexés par horizon 1 à 30**, indice 0
  = horizon 1.
- **L'horizon d'une transition est celui d'où elle part.** `rouvre[29]`
  (horizon 30) répond à « fermé à J-30, ouvert à J-29 ». Se tromper d'un cran
  décale tout, et l'erreur s'aggrave en se composant.
- Un couple absent de `cles` se lit « prendre le taux de l'axe ». Un couple
  présent dans `sans_place_max` rend **zéro**, sans repli.
- Un axe à `"clos": true` rend **zéro**, sans repli. Sept axes le sont
  (`IC SRO`, `AUTOCAR SNCF`, les cinq `OUIGO_*`), soit 22,5 % de l'archive.
- `n_ouvert` et `n_ferme` accompagnent chaque taux : **c'est l'effectif qui
  décide de la bande affichée**, pas seulement le taux. Voir § 6.
- `bandes` et `bandes_heritees` sont **informatifs** : le client n'en a pas
  besoin, la table `trains` portant un multiplicateur unique par couple. Ils
  disent seulement à quels horizons correspondent les libellés des compteurs,
  l'archive en ayant connu deux découpages successifs (le second isole les trois
  horizons de vague, à partir du relevé du 25 août).

### 3.2 Ce que v1 ne porte pas, et ce que v2 devra porter

- **Pas de taux par couple à l'horizon 30.** La bande `ouverture` ne compte
  qu'un passage par jour et par couple : sept observations au maximum. Le
  pronostic d'ouverture d'un train donné se fait donc au niveau de l'axe, corrigé
  du jour de voyage, plus le drapeau `sans_place_max`. C'est la limite la plus
  contraignante du dispositif actuel, et elle se lèvera d'elle-même vers
  **mi-octobre 2026** (≈ 56 observations par couple).
- **Pas de roster embarqué.** L'artefact ne dit pas quels trains roulent un jour
  donné, seulement comment ils se comportent. La v1 va donc chercher le roster
  au portail (§ 4.3). Une v2 pourra embarquer, pour les 12 133 couples déjà vus
  ouverts, leur heure de départ et leur axe (≈ 240 Ko) et supprimer cet appel.
- **Pas d'ancienneté.** Les paliers 8-15 j et 16 j + sont vides. « Cette place
  tient d'habitude » n'est pas prononçable avant début octobre.

### 3.3 Chargement

Patron `Emissions`, à la lettre :

1. `Bundle.main.url(forResource: "modele-v1", withExtension: "json")` — la
   version embarquée, qui suffit à tout faire hors ligne.
2. Au lancement, en tâche détachée et sans faire attendre personne, un `GET`
   sur `raw.githubusercontent.com/charlyppr/maxplor-tgvmax-archive/main/modele/modele-v1.json`,
   écrit dans `.cachesDirectory`. Session éphémère, `timeoutIntervalForRequest = 10`,
   `waitsForConnectivity = false`.
3. À la lecture, le fichier de cache s'il existe et se décode, sinon l'embarqué.
   Le système peut vider les caches quand il veut : rien ne s'y perd.

Le champ `fenetre.dernier_releve_compte` sert à dater le modèle dans
l'interface (« modèle du 24 août, 7 relevés »). Il ne sert pas à périmer quoi
que ce soit : un modèle de trois semaines vaut mieux que pas de modèle.

---

## 4. Les fichiers à écrire

Sept fichiers, dont quatre modèles et trois vues. Commentaires en français,
narratifs, expliquant le pourquoi.

### 4.1 `Maxplor/Models/OpeningModel.swift`

Le décodeur et le moteur. `nonisolated`, sans état observable — c'est une table,
pas un service.

```swift
nonisolated struct OpeningModel: Decodable {
    struct AxisRates { let closed: Bool
                       let ferme: [Int]; let rouvre: [Int]      // dix-millièmes, index 0 = h1
                       let nOuvert: [Int]; let nFerme: [Int]
                       let pointe: [String: [Int]] }
    struct DayFactor { let mFerme: Int; let mRouvre: Int; let nOuvert: Int; let nFerme: Int }

    let window: Window
    let global: AxisRates
    let axes: [String: AxisRates]
    let travelDay: [String: DayFactor]
    let trains: TrainTable

    /// Les deux taux d'une case, en repliant ce qui manque. `horizon` est celui
    /// **d'où part** la transition.
    func rates(axe: String, horizon: Int, pointe: Pointe?,
               travelWeekday: Int?, couple: CoupleKey?) -> (ferme: Double, rouvre: Double)

    /// Propage la chaîne à deux états de `horizon` jusqu'au départ, et rend la
    /// probabilité d'être ouvert à chaque cran. L'indice 0 est l'état de départ,
    /// le dernier est l'état au moment où le train part.
    func propagate(open: Double, from horizon: Int, …) -> [Double]
}
```

Trois méthodes de haut niveau, et ce sont elles que les vues appellent :

```swift
/// « Ce départ ouvrira-t-il une place Max ? » — la transition partant de
/// l'horizon 30, seule quantité que l'archive mesure pour cette question.
func opensAtRelease(axe:pointe:travelWeekday:couple:) -> Double

/// « Partira-t-il vite ? » — la probabilité qu'une place ouverte à J-29 le soit
/// encore trois jours plus tard. Ce sont les horizons 29, 28 et 27 qui décident,
/// et aucun autre.
func holdsAfterRelease(days: Int = 3, …) -> Double

/// « Et si je laisse courir ? » — la survie jusqu'au départ, chaîne complète.
/// À n'afficher qu'en bande : la composition n'est validée que sur cinq à sept
/// pas (1,45 point d'écart moyen sur 332 cohortes réelles). Sur vingt-neuf pas,
/// elle ne le sera pas avant le 16 septembre 2026.
func survivesToDeparture(from horizon: Int, …) -> Double
```

**Le piège à documenter dans le fichier :** la survie qui ne compte que les
fermetures vaut 26,0 % là où la chaîne complète en donne 47,2 %. Oublier la
branche réouverture divise la réponse par près de deux.

### 4.2 `Maxplor/Models/Opening.swift`

Le vocabulaire du domaine. Données pures.

```swift
/// Un jour de voyage pas encore ouvert, et le jour où il s'ouvrira.
nonisolated struct OpeningDay: Identifiable, Hashable {
    let travelDay: Date          // J+31 et au-delà
    var releaseDay: Date         // travelDay − 30 : déterministe, aucune donnée requise
    var daysUntilRelease: Int
    var id: Date { travelDay }
}

/// Le pronostic d'une liaison sur un jour pas encore ouvert.
nonisolated struct OpeningForecast: Identifiable, Hashable {
    let sample: Trip             // le train du jour miroir, jamais affiché daté
    let opensProbability: Double
    let holdsProbability: Double
    let confidence: Confidence   // adossée à l'effectif, voir § 6
    var band: OpeningBand        // .unlikely / .plausible / .likely
    var urgency: Urgency         // .grabAtRelease / .canWait / .unknown
}
```

`sample: Trip` reprend exactement le parti de `FavoriteRoute` : *« un favori n'a
pas de jour, mais il faut bien un train pour le dessiner »*. Un pronostic non
plus. On garde le train du jour miroir comme échantillon et **on n'affiche
jamais sa date** — ce qu'on montre de lui vaut pour le jour visé.

### 4.3 `Maxplor/Models/OpeningRoster.swift`

Le problème que personne ne voit venir : **le jeu de données ne contient aucune
ligne pour J+31.** On ne peut donc pas demander au portail les trains d'un jour
pas encore ouvert. Il faut un roster de candidats.

**Le miroir de la semaine.** Pour un jour visé `D`, on prend le jour de même
rang hebdomadaire le plus récent qui soit encore dans la fenêtre : `D − 7`,
sinon `D − 14`, etc. Pour J+31 à J+37, `D − 7` tombe entre J+24 et J+30 : il est
toujours dans la fenêtre. Au-delà de J+37, on remonte de sept en sept.

Deux raisons de choisir le même jour de semaine et pas la veille :

- La desserte est hebdomadaire. Le 07:52 du mardi n'existe pas forcément le
  mercredi.
- L'effet de jour de voyage est le plus fort des effets mesurés — la part
  ouverte varie de 7,92 % (dimanche) à 19,16 % (mercredi), un rapport de 2,4.
  Un miroir pris la veille se tromperait de jour deux fois : sur la desserte et
  sur le pronostic.

L'appel est celui qui existe déjà, avec un seul paramètre changé :

```swift
/// Tous les trains d'une journée entre deux ensembles de gares, **places Max
/// ouvertes ou non**. C'est la liste des candidats : un train qui n'a pas de
/// place aujourd'hui est précisément celui dont on veut savoir s'il en ouvrira.
static func roster(fromAny: [String], toAny: [String], on day: Date) async throws -> [Trip] {
    let segments = try await fetch(where: …, on: day, openSeatsOnly: false)
    return directTrips(segments: segments)
}
```

**Ce que le miroir coûte en honnêteté, et qui doit se dire :** un train qui ne
roule pas le jour visé apparaîtra tout de même dans la liste (travaux, fête,
changement de service au 13 décembre). Le pronostic parle d'un train *du même
rang hebdomadaire*, pas du train de ce jour-là. Une ligne de mention en pied de
liste, une seule, et pas un astérisque par carte.

### 4.4 `Maxplor/Models/OpeningStore.swift`

`@MainActor @Observable`, sur le patron de `TripStore`, avec deux différences
assumées :

- **Fraîcheur de 24 h et non de 5 minutes.** `TripStore` garde 5 minutes parce
  qu'*« un train se remplit en quelques minutes »*. Ici, rien ne se remplit :
  le jour n'est pas ouvert. Ce qui change d'un jour à l'autre, c'est le miroir,
  et il change une fois par jour.
- **La clé est le jour visé**, pas le jour miroir : deux jours visés différents
  peuvent partager un miroir, et l'un ne doit pas servir de réponse à l'autre —
  leur jour de semaine est le même, mais leur date d'ouverture ne l'est pas.

Même discipline mémoire : `didReceiveMemoryWarningNotification` rend la place.

### 4.5 `Maxplor/Features/OpeningsView.swift`

La surface. **Une bascule dans le calendrier, pas un quatrième onglet.**

`RootView` porte trois onglets (`explore`, `ideas`, `journal`) et le
commentaire dit ce qu'ils sont : *« on cherche, on compose, on garde »*. Les
ouvertures à venir ne sont aucun des trois : c'est un prolongement de la
recherche dans le temps. Un quatrième onglet obligerait à renommer les trois
autres pour qu'ils continuent de se lire ensemble.

Le geste juste est ailleurs : **le calendrier de recherche s'arrête aujourd'hui
au dernier jour réservable.** C'est là que l'utilisateur bute contre la limite,
et c'est donc là que la surface doit s'ouvrir — une bande sous la grille, « et
après le 23 septembre ? », qui pousse `OpeningsView` dans la pile de navigation
avec la route déjà choisie.

Structure :

```
PageTitle « Ouvertures à venir »
  ligne de contexte : « Paris → Lyon · modèle du 24 août, 7 relevés »

SectionHeader « S'ouvre demain »        ← si un jour bascule dans les 24 h
  OpeningDayCard(travelDay: …)

SectionHeader « Les jours qui viennent »
  ForEach(days) { OpeningDayCard }       ← J+31 … J+45, quinze jours

  chaque carte, dépliée, montre ses liaisons triées par urgence :
    OpeningForecastRow × n
```

`OpeningDayCard` est un `MaxCard`. Elle porte, du plus grand au plus petit :

1. **La date du voyage** et son jour de semaine, en gros.
2. **La date d'ouverture** et le compte à rebours : « s'ouvre dans 4 jours, le
   samedi 29 août ». C'est le seul chiffre de l'écran qui soit certain.
3. **Une jauge de saison**, tirée du seul `jour_voyage` : « un mercredi — le
   meilleur jour de la semaine pour Max ». Pas de pourcentage.
4. **Le compte des liaisons par bande** : « 3 départs probables, 6 possibles ».

`OpeningForecastRow` reprend la ligne d'un `TripCard` réduite — heure, marque,
destination — plus deux marques :

- **la bande d'ouverture**, un anneau `Palette.accent` plein / creux / absent ;
- **l'urgence**, une puce `Palette.caution` « à prendre tout de suite » quand
  `holdsProbability` est basse, rien sinon.

Rien de gris, rien de barré, rien de désactivé — la doctrine de `CalendarView`
s'applique intégralement : **la marque promet, son absence ne refuse pas.**

### 4.6 `Maxplor/Features/OpeningWatch.swift`

L'affût. Un bouton par jour, ou par liaison quand la route est nommée.

Le mécanisme est simple parce que **la date d'ouverture est déterministe** :
`travelDay − 30`. Aucune donnée n'est nécessaire pour la connaître, aucun
serveur pour la surveiller. Une `UNCalendarNotificationTrigger` posée au matin
du jour d'ouverture suffit.

Travaux nécessaires, à budgéter :

- lier `UserNotifications` ;
- demander l'autorisation **au premier affût et pas au lancement** — une app qui
  demande à notifier avant d'avoir rien à dire se fait refuser ;
- `NSUserNotificationsUsageDescription` n'existe pas ; c'est
  `UNUserNotificationCenter.requestAuthorization` qui porte la demande, sans clé
  Info.plist. Rien à ajouter au projet Xcode, dont le groupe est synchronisé.

**L'heure ne peut pas être promise.** L'archive relève un état par jour à
04 h 24 UTC : elle voit que le quota s'est ouvert, jamais à quelle heure. Le
texte de la notification doit donc dire « Les trains du 29 septembre s'ouvrent
aujourd'hui » et jamais « à 6 h ». C'est une limite de la source, pas du modèle,
et elle ne se lèvera pas.

Le bon rattachement pour l'affût d'une liaison est **`FavoriteRoute`** : un
favori est déjà une route et une heure sans date, et son identité
(`origine>destination|minutes`) est stable d'un jour à l'autre. Un affût est
donc un `FavoriteRoute` plus une date visée, rangé par `TripArchive` comme le
reste — un quatrième fichier JSON à côté de `favorite-routes.json`, sur le même
patron.

### 4.7 `Maxplor/Resources/modele-v1.json`

Déposé tel quel. Le groupe `Maxplor` étant synchronisé au système de fichiers,
il entre dans la cible sans modification du projet Xcode.

---

## 5. Les états d'interface

Sept, et chacun a une raison d'exister.

| état | quand | ce qu'on montre |
|---|---|---|
| **Chargement** | le roster miroir est en vol | `Skeleton` sur trois cartes de jour. Les dates d'ouverture, elles, s'affichent **tout de suite** : elles ne dépendent d'aucune donnée |
| **Pronostic posé** | roster reçu, modèle appliqué | la liste complète, apparition en cascade `Motion.reveal(_:_:)` avec `Motion.stagger(index:)` |
| **Sans miroir** | aucun train sur le jour de même rang hebdomadaire | « Aucun direct ce jour-là dans la fenêtre actuelle. » Et surtout **pas** « il n'y en aura pas » : le miroir ne sait rien du jour visé |
| **Axe clos** | tous les candidats sont sur un axe `clos` | « Cette liaison n'est pas ouverte à Max. » C'est le seul cas où l'app a le droit d'être catégorique : 620 640 passages, zéro ouverture |
| **Hors ligne** | le roster échoue | on garde les cartes de jour et les dates d'ouverture, on retire les liaisons. Le calendrier des ouvertures fonctionne sans réseau, et c'est déjà l'essentiel |
| **Le jour s'ouvre** | `travelDay − 30 == aujourd'hui` | la carte bascule en `Palette.accent`, `Motion.morph`, et un bouton mène droit à `ResultsView` sur ce jour. **C'est l'aboutissement de toute la surface** |
| **Le jour est ouvert** | `travelDay` est entré dans la fenêtre | la carte quitte la liste par `Motion.collapse`. Elle n'a plus rien à dire : la recherche existante fait mieux |

---

## 6. Le vocabulaire : des bandes, jamais un pourcentage

Le README de l'archive le demandait déjà — *« des bandes plutôt qu'un
pourcentage brut »* — et la validation du modèle en donne la raison chiffrée.

**Trois bandes d'ouverture**, et le seuil dépend de l'effectif :

| bande | condition | texte |
|---|---|---|
| `.likely` | `opensProbability ≥ 0,15` et `n_ferme` de la case ≥ 1 000 | « ouvre souvent » |
| `.plausible` | `≥ 0,04` | « ouvre parfois » |
| `.unlikely` | le reste, ou `sans_place_max`, ou axe `clos` | « rarement ouvert » |

**Deux bandes d'urgence :**

| bande | condition | texte |
|---|---|---|
| `.grabAtRelease` | `holdsProbability` sous la médiane des axes | « à prendre le jour même » |
| `.canWait` | au-dessus | « tient quelques jours » |

Quatre raisons de ne jamais montrer le nombre :

1. **La fermeture est mal calibrée dans le haut.** Le modèle annonce 15,05 % là
   où l'on observe 8,40 % sur la tranche haute de la retenue. Un seuil affiché
   serait optimiste d'un facteur proche de deux.
2. **La volatilité journalière est d'environ ±12 %**, et une journée sur sept en
   sort complètement (le 24 août : 1 648 fermetures pour 2 989 attendues).
3. **La composition n'est validée que sur cinq à sept pas.** Elle y tient bien
   — 1,45 point d'écart absolu moyen sur 332 cohortes — mais la surface a besoin
   de vingt-neuf pas, et aucune date de voyage n'aura parcouru toute la chaîne
   avant le 16 septembre 2026.
4. **Sept relevés couvrent une fin de vacances.** Le rapport mercredi/dimanche
   mesuré ici ne vaut pas pour novembre.

Une ligne de pied de page, discrète, en `Palette.inkMuted` : « Estimé sur
7 relevés, du 18 au 24 août 2026. » Les quatre phrases du bloc `reserves` de
l'artefact s'affichent derrière un appui sur cette ligne.

---

## 7. Ce que le modèle ne permet pas encore d'afficher honnêtement

À relire avant d'écrire le moindre texte d'interface.

| ce qu'on aimerait dire | pourquoi c'est interdit aujourd'hui | quand |
|---|---|---|
| « **ce train-là** ouvre presque toujours » | 7 observations par couple au maximum à l'horizon 30. Un succès sur sept donne [2,6 % – 41 %] | **mi-octobre 2026** |
| « cette place tient d'habitude » | paliers d'ancienneté 8-15 j et 16 j + entièrement vides | palier 16 j + le 2 septembre, exploitable début octobre |
| « il reste 2 places » | la source est binaire | jamais |
| « ça ouvre à 6 h » | un relevé par jour, à 04 h 24 UTC | jamais |
| « ça a rouvert cet après-midi » | l'angle mort intra-journalier : ouverture et reprise entre deux relevés sont invisibles. Le modèle sous-estime structurellement le brassage | jamais |
| « 62 % de chances » | § 6 | quand la calibration de la fermeture le permettra |
| « en novembre, le mercredi… » | l'archive couvre le 18 août au 22 septembre | après avoir vu la saison |
| « ce train partira dans l'heure » | le modèle décrit une dynamique de relevé à relevé, pas un processus continu | jamais avec cette source |

Et une nuance qui vaut pour toute la surface : **le pronostic porte sur un train
du même rang hebdomadaire, pas sur celui de ce jour-là.** Tant que l'artefact
n'embarque pas de roster (§ 3.2), c'est le miroir qui parle.

---

## 8. Ordre de travail

Cinq jalons, chacun livrable seul.

1. **Le calendrier des ouvertures, sans pronostic.** `Opening.swift`,
   `OpeningsView` réduite aux `OpeningDayCard`, la bascule depuis
   `CalendarView`. Aucun réseau, aucun modèle : quinze jours à venir, chacun
   avec sa date d'ouverture et son compte à rebours. **Utile en soi**, et c'est
   le seul morceau qui ne peut pas se tromper.
2. **L'affût.** `OpeningWatch`, `UserNotifications`, rangement par
   `TripArchive`. Toujours sans modèle : la date d'ouverture suffit. Deux jalons
   sans une seule probabilité affichée — c'est délibéré.
3. **Le roster miroir.** `TGVMaxAPI.roster(…)`, `OpeningRoster`,
   `OpeningStore`. Les liaisons apparaissent, sans pronostic, avec la mention du
   miroir.
4. **Le modèle.** `OpeningModel`, l'artefact embarqué, les bandes du § 6.
   Le rafraîchissement réseau vient après, sur le patron `Emissions`.
5. **La reprise du modèle**, à mi-octobre, quand la bande `ouverture` portera
   une cinquantaine d'observations par couple : le pronostic devient un
   pronostic *de train* et non plus d'axe. C'est le jalon qui change la valeur
   de la fonctionnalité, et il ne dépend que du temps qui passe.

---

## 9. Ce que la fonctionnalité doit à l'archive

Trois choses qu'aucun relevé instantané ne pouvait donner, et sans lesquelles
cette surface n'aurait rien à dire :

- **Que le bord de fenêtre est vide et bascule d'un coup.** Un état instantané
  montre 0,088 % à J-30 sans pouvoir dire si c'est un marché atone ou un quota
  non chargé. Seule la transition tranche : 10,83 % du parc fermé s'ouvre en
  arrivant à J-29.
- **Que deux autres vagues existent, à J-11 et J-3.** Elles sont invisibles dans
  un stock. C'est ce qui permet de dire à un abonné « ce départ peut attendre,
  il rouvrira » au lieu de « prends-le maintenant ».
- **Que la réouverture pèse vingt et un points sur quarante-sept** dans la
  survie d'une place jusqu'au départ. Une app qui ne compterait que les
  fermetures diviserait chacune de ses réponses par près de deux.

---

*Source du modèle : SNCF Voyageurs, jeu de données `tgvmax`, sous Open Database
License (ODbL), archivé et compté par `maxplor-tgvmax-archive`. Toute
redistribution de l'artefact reste soumise à la même licence, avec attribution.*
