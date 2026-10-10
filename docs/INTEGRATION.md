# Règles de la synchronisation billetterie → Brevo

Pousse les acheteurs de la billetterie Infomaniak dans Brevo (upsert par email),
avec des attributs d'achat utilisables pour segmenter les newsletters.

## Attributs Brevo créés automatiquement

| Attribut | Type | Contenu |
|---|---|---|
| `TICKETING_SOURCE` | text | `Infomaniak` |
| `TICKETING_ORDERS` | float | nombre de commandes |
| `TICKETING_TICKETS` | float | nombre de billets |
| `TICKETING_TOTAL_SPENT` | float | montant total (devise de la boutique) |
| `TICKETING_LAST_ORDER_AMOUNT` | float | montant de la dernière commande |
| `TICKETING_AVG_BASKET` | float | panier moyen (total ÷ commandes ayant un montant) |
| `TICKETING_MATCHES` | float | matchs distincts « présents » (scannés, ou billet acheté — voir règles de présence) |
| `TICKETING_MATCHES_ATTENDED` | float | matchs joués avec billet **scanné** (écrit seulement si l'API fournit le statut de scan) |
| `NPA` / `BIRTHDAY` | (référentiel) | code postal / date de naissance, depuis les abonnements, **seulement si vides** |
| `TICKETING_FREE_TICKETS` | float | billets offerts (payé 0 CHF pour un prix catalogue > 0) |
| `TICKETING_FREE_VALUE` | float | valeur offerte : Σ (prix catalogue − montant payé), invitations et rabais |
| `TICKETING_ONSITE_TICKETS` | float | billets achetés sur place (tarifs « … - sur place ») |
| `TICKETING_PAYMENT_METHOD` | text | mode de paiement de la dernière commande (Twint, Carte de crédit…) |
| `TICKETING_LAST_ORDER_ID` | text | numéro de la dernière commande |
| `TICKETING_PASS_CODE` | text | code de l'abonnement (pass) le plus récent |
| `TICKETING_SEASON_TICKET` | text | produit d'abonnement de la saison en cours (ex. « Abonnement VIP Expérience Njørden ») |
| `TICKETING_FIRST_ORDER` / `TICKETING_LAST_ORDER` | date | première / dernière commande |
| `TICKETING_LAST_EVENT` | text | dernier match acheté |
| `DERNIER_MATCH` | date | date du dernier **match** déjà joué pour lequel le contact a acheté (abonnements et autres événements exclus) |
| `DERNIER_IMPORT` | date | date de la sync, posée **uniquement quand `DERNIER_MATCH` change**, c.-à-d. après un nouveau match joué |
| `SMS` | (standard) | téléphone au format international (`+41791234567`), seulement si le contact n'en a pas |
| `SOURCE_ACQUISITION` | (existant) | **seulement si vide** : `Business Engagement` si le premier achat est un événement partenaire, sinon `Billetterie` |
| `OPT_IN` | (existant) | `YES`, posé en même temps que `SOURCE_ACQUISITION` |

## Attributs billetterie du référentiel DMOS

| Attribut | Calcul | Valeurs |
|---|---|---|
| `TICKET_STATUT` | **abonné de la saison en cours → `REGULAR`** ; sinon nombre de matchs distincts achetés (tout l'historique) | `NEW` = 1, `ONBOARDED` = 2, `REGULAR` ≥ 3 |
| `CATEGORIE_PLACE` | catégorie de place du **dernier match acheté** (la plus fréquente de la commande), à défaut celle de l'abonnement en cours | options Brevo (Tribune, Virage, VIP…) |
| `CATEGORIE_TARIF` | tarif du dernier match acheté (le plus fréquent de la commande : 2 Plein + 1 Enfant → Plein), à défaut celui de l'abonnement en cours | options Brevo (Plein, Réduit, Enfant, Gratuit) |

Ce sont des attributs *catégorie* : ils ne sont **jamais créés** par la sync. Les libellés
Infomaniak sont rapprochés des options Brevo (majuscules/accents ignorés) ; si les noms
diffèrent, ajouter une correspondance. Un libellé sans option, ou un attribut absent de Brevo,
n'est pas écrit et un avertissement le signale (visible au dry run).

```json
"ticket_attributes": {
  "statut_thresholds": {"ONBOARDED": 2, "REGULAR": 3},
  "place_mapping": {"Gradin Nord": "Tribune"},
  "tarif_mapping": {"Adulte": "Plein", "AVS / Étudiant": "Réduit"}
}
```

`{"enabled": false}` désactive ces trois attributs. Les champs lus sur chaque billet de la
commande sont, dans l'ordre : `place_category`, `seat_category`, `category`, `zone`, `sector`…
pour la place, et `tariff`, `tarif`, `rate`, `price_category`, `price_name`… pour le tarif.

Un abonnement compte pour la saison de son événement (date, sinon `YYYY-YYYY` dans son nom,
sinon date de commande). Un abonnement d'une saison passée ne rend pas `REGULAR` : le statut
retombe sur le nombre de matchs.

**Montants** (toutes commandes : matchs, abonnements, camps, partenaires), lus dans le champ
`total` / `amount` de la commande Infomaniak et recalculés sur tout l'historique :
`TICKETING_TOTAL_SPENT`, `TICKETING_LAST_ORDER_AMOUNT`, `TICKETING_AVG_BASKET`.

**Source d'acquisition par canal** : la source d'un contact suit son **premier achat**.
Premier achat = événement partenaire → `Business Engagement` (`partner_acquisition_value`,
`""` pour toujours écrire `Billetterie`) ; tout autre premier achat → `Billetterie`. Si l'option
manque dans Brevo, ces contacts ne sont pas marqués et un avertissement le signale.

## Données vérifiées sur les exports Foxes

Vérifié sur les exports *CMON_DATA_FOXES_DASH_TICKETING* (onglets Billets, Abonnements,
Camps Foxes & Foxes Night, Résumé ; saisons 2025-2026 et 2026-2027) :

- **Une ligne = un billet** avec `TYPE DE LIGNE` : `Billet` (acheté) ou `Place abonné`
  (place **pré-générée** pour chaque match, y compris « Carte membre »). La sync distingue
  billet / place abonné / place membre (`line_kind`).
- **`Statut billet`** (`Billet scanné` / `Billet valide`) = présence réelle.
  **Règles de présence** (`counts_for_event`) : une ligne scannée compte toujours ; une place
  abonné / membre ne compte que si elle est scannée ; un billet acheté compte, sauf pour un
  match joué où il n'a pas été scanné (absent). Sans donnée de scan, l'achat compte.
  Ces règles alimentent `DERNIER_MATCH`, `TICKET_STATUT`, `TICKETING_MATCHES*` et les listes
  de match (seuls les présents y sont).
- **Abonnements** : pas d'événement dédié ; ils apparaissent comme `Place abonné` (tarif =
  nom du produit) et dans l'onglet Abonnements (détenteur, type, montant net, mode de paiement,
  date de naissance, code postal, téléphone). Les deux sont pris en compte : places abonné
  de la saison → `REGULAR` + liste `CMON-Abonnes-AAAA-AAAA` ; passes via `passes_path`
  (`/passes`). Le **détenteur** est le
  contact (pas l'acheteur), le type d'abonnement sert de tarif.
- **Montants** : commande `Free` ou abonnement payé `Gratuit` → 0 CHF (le montant net
  affiché est la valeur catalogue, pas un encaissement).
- **Matchs « A DEFINIR »** (date ou adversaire) : pas de liste tant qu'ils ne sont pas programmés.
- **Téléphones** exportés en nombre (`41791234567.0`) et **codes postaux** mixtes
  (`1004 Lausanne`) sont normalisés.

Toutes les colonnes utiles aux analyses sont remontées dans Brevo (voir le tableau des
attributs). Seules données absentes des exports : genre et consentement newsletter (recueillis
ensuite par la newsletter d'accueil, voir « Consentement »).

### Configuration Foxes

Fichier de config du club :
[`clubs/foxes.json`](../clubs/foxes.json) — codes adversaires
(clés courtes pour couvrir les variantes « Starwings Bâle / Starwings Basket »,
« BBC Monthey / BBC Monthey-Chablais »…), Foxes Night en partenaire, placeholder ignoré,
correspondances de tarifs. Un test vérifie cette config sur tous les noms d'événements des
exports. `passes_path` = `/passes` (voir « Adresses de l'API Infomaniak »).

`tarif_mapping` / `place_mapping` acceptent des mots-clés : la première clé dont tous les mots
figurent dans le libellé l'emporte (« VIP - billet offert » → Gratuit car `offert` est avant).

## Conformité à la guideline « Source d'acquisition - Billetterie »

Référence : Notion, Products Library › Enrichissement quantitatif de la base CRM.
La guideline décrit l'import **manuel** ; la sync automatise les mêmes règles.

| Étape de la guideline | Dans la sync |
|---|---|
| 1. Export selon la guideline de la stack | Lecture directe de l'API Infomaniak |
| 2. Email ou téléphone obligatoire | Email, sinon téléphone : les acheteurs sans email sont créés via leur numéro (`SMS`) |
| 3. Colonnes `DERNIER_IMPORT` et `DERNIER_MATCH` | Calculées à chaque sync (voir tableau ci-dessus) |
| 4. Nettoyage (vides, doublons, emails incorrects) | Emails validés et mis en minuscules, téléphones normalisés, un contact par acheteur |
| 5. Import dans une liste du dossier Billetterie, nommée selon la guideline | Une liste par match `CMON-FOXES-[ADVERSAIRE]-[YYYYMMDD]` dans `Billetterie 2026-2027` |
| 6. Filtre « `SOURCE_ACQUISITION` est vide » → `SOURCE_ACQUISITION = Billetterie`, `OPT_IN = YES` | Idem, sur tous les acheteurs synchronisés (nouveaux **et** existants sans source) ; un contact qui a déjà une source n'est pas modifié, son `OPT_IN` non plus |

Règles de détail :
- **`DERNIER_MATCH`** : une vente pour un match futur ne compte qu'une fois le match joué,
  comme un export manuel fait après le match.
- **`DERNIER_IMPORT`** n'est posé que lorsqu'un nouveau match a été joué pour le contact
  (`DERNIER_MATCH` change), comme un import manuel fait après le match. Un achat seul (billet
  pour un match à venir, abonnement, autre événement) ne le modifie pas. Lors de la
  **première** sync, tous les acheteurs d'un match déjà joué reçoivent `DERNIER_IMPORT` :
  mettre en pause les automations qui en dépendent pour ce premier passage.
- **Téléphone** : normalisé au format international (`079 123 45 67` → `+41791234567`, Suisse
  par défaut). Une commande sans email mais dont le numéro est connu (autre commande avec
  email, ou contact Brevo existant avec ce `SMS`) est rattachée à ce contact ; sinon le contact
  est créé avec le numéro seul (`POST /contacts`, qui accepte `SMS` comme identifiant). Le
  `SMS` d'un contact existant n'est jamais écrasé, et un numéro déjà porté par un autre contact
  n'est pas réutilisé (Brevo le refuse) : compteur `sms_conflicts`.
- **Format des dates** : attribut de type *date* → `2026-10-18` (Brevo l'affiche
  « dimanche, 18 octobre 2026 ») ; attribut de type *texte* → la chaîne
  `dimanche, 18 octobre 2026` est écrite (format recommandé par la guideline).
  S'ils n'existent pas, ils sont créés en type *date*.
- **`OPT_IN`** : la sync s'adapte au type existant (texte `YES`, catégorie → option « YES »,
  booléen → vrai). S'il n'existe pas dans Brevo, il n'est **pas** créé et un avertissement
  le signale. Configurable via `optin_attribute` / `optin_value` (`""` désactive).
- **`SOURCE_ACQUISITION`** : même adaptation au type (catégorie → identifiant de l'option
  « Billetterie » ; si l'option manque, rien n'est écrit et un avertissement liste les options).

Limites connues :
- La guideline prévoit un dossier `Billetterie` ; la sync utilise `Billetterie 2026-2027`
  (un dossier par saison, à la demande du club).

Le dry run affiche les valeurs qui seront envoyées (`acquisition_value_sent`, `optin_value_sent`),
les compteurs `new_contacts`, `source_filled`, `last_import_updated`, `phone_only_buyers`,
`sms_conflicts`, `orders_ignored`, le type retenu pour chaque événement (`event_types`) et les avertissements.

Prénom / nom vont dans `FIRSTNAME`/`LASTNAME` (ou `PRENOM`/`NOM` sur un compte Brevo francophone).
Les totaux sont recalculés sur tout l'historique à chaque synchronisation.

## Une liste par événement (référentiel DMOS)

Chaque événement de la saison obtient sa liste Brevo, classée dans le dossier de la saison et
alimentée à chaque sync : une nouvelle vente ajoute l'acheteur à la liste du match.

Nommage, selon *DMOS — Référentiel de conventions de nommage* (section 3) :

| Élément | Règle | Exemple |
|---|---|---|
| Dossier | français, majuscule, saison `YYYY-YYYY` | `Billetterie 2026-2027` |
| Liste de match | `CMON-[CLUB]-[OPPOSANT]-[YYYYMMDD]` | `CMON-FOXES-LIONS-20261018` |
| Autre événement | `CMON-[CLUB]-[Description]-[YYYYMMDD]` | `CMON-FOXES-Soiree-Partenaires-20261105` |
| Abonnement | `CMON-Abonnes-[YYYY]-[YYYY]`, dossier `Abonnement` | `CMON-Abonnes-2026-2027` |

### Événements hors match standard

Chaque événement est classé avant la sync, et chaque type a son dossier Brevo :

| Type | Détection automatique (mots du nom) | Dossier | Liste | Compte pour `DERNIER_MATCH`, `TICKET_STATUT`, `CATEGORIE_*` |
|---|---|---|---|---|
| `match` | le club contre un adversaire (« Foxes vs Lions ») | `Billetterie 2026-2027` | `CMON-FOXES-[ADV]-[date]` | oui |
| `abonnement` | abonnement, abo, season ticket, pass (pass ou places « Place abonné ») | `Abonnement` | `CMON-Abonnes-2026-2027` | non |
| abonnement **VIP** | produit d'abonnement contenant « VIP » | `Business Engagement` | `CMON-Abonnes-VIP-2026-2027` (pas répété dans la liste abonnés) | non |
| membre | places « Carte membre » (matchs et camps) | `Members & Staff` | `CMON-Membres-2026-2027` | non (sauf billet scanné) |
| `camp` | camp, stage | `Billetterie 2026-2027` | `CMON-FOXES-[Description]-[date]` | non |
| `partenaire` | partenaire, sponsor, business, b2b | `Business Engagement` | `CMON-FOXES-[Description]-[date]` | non |
| `autre` | tout le reste (portes ouvertes…) | `Billetterie 2026-2027` | `CMON-FOXES-[Description]-[date]` | non |
| `ignorer` | jamais automatique | — | aucune, commandes exclues de la sync | non |

Un événement partenaire lié à un match (« Soirée partenaires Foxes vs Lions ») est classé
`partenaire`. Les dossiers se changent par type avec `folders`
(ex. `{"abonnement": "Abonnement {season}"}`) et les mots-clés avec
`abonnement_keywords`, `partenaire_keywords`, `camp_keywords`.

`event_types` force un type par mot-clé (insensible aux majuscules, accents et pluriels),
par exemple :

```json
"event_types": {"test": "ignorer", "invitation": "ignorer", "match amical": "match"}
```

Le résultat liste le type retenu pour chaque événement (`event_types`) : vérifier au dry run.
Si aucun match n'est reconnu, un avertissement le signale (`club_keywords` à compléter).

- L'adversaire est lu dans le nom de l'événement (« Foxes vs Lions de Genève »,
  « Massagno – Pully Lausanne Foxes ») ; son code vient de `opponent_codes`, comparé mot à mot
  dans les deux sens (« Lions » ou « Lions de Genève » → `LIONS`, « Fribourg » → `FOB`). Sans
  correspondance, les 3 premières lettres sont prises **et un avertissement le signale** :
  compléter `opponent_codes` avec les abréviations officielles.
- Date obligatoire (règle DMOS) : un événement sans date n'a pas de liste (avertissement).
- Seuls les événements de la saison configurée (par défaut la saison en cours, début en juillet)
  ont une liste ; les autres sont comptés dans `events_other_season`.
- Dossier et listes sont recherchés par nom avant création : relancer la sync ne crée pas de
  doublon. Une liste renommée à la main dans Brevo serait recréée.
- Le dry run affiche les listes prévues (`[à créer]` pour celles qui n'existent pas).

Configuration (section `event_lists` du fichier `clubs/<club>.json`) :

```json
{"event_lists": {
   "club_code": "FOXES",
   "season": "2026-2027",
   "opponent_codes": {"Lions de Genève": "LIONS", "Fribourg Olympic": "FOB"},
   "club_keywords": ["foxes", "pully"]
}}
```

`club_code` est obligatoire (sans lui, aucune liste n'est créée) ; `lists_folder` permet un autre
nom de dossier (`{season}` est remplacé). Sans clé `event_lists`, la fonctionnalité est désactivée.

## Adresses de l'API Infomaniak

Référence officielle : <https://etickets.infomaniak.com/docs/app/index.html> (apiDoc ;
`tools/summarize_api_docs.py` en extrait le catalogue via le workflow *API docs*).

| Donnée | Adresse | Réglage | Accès |
|---|---|---|---|
| Base de l'API boutique | `https://etickets.infomaniak.com/api/shop` | `infomaniak.base_url` | — |
| Événements | `GET {base}/events` | `infomaniak.events_path` | clé boutique |
| Commandes | `GET {base}/orders` (`limit` / `offset`, filtres `status`, `begin`, `end`) | `infomaniak.orders_path` | guichet |
| Abonnements / passes | `GET {base}/passes` (`limit` / `offset`) | `infomaniak.passes_path` | guichet |
| Billets (scan, zone, tarif) | `GET {base}/tickets`, `GET {base}/order/{id}/tickets` | — | guichet |

**Authentification** (méthode *Login* de la doc) :

- en-tête `key` : clé API de la boutique avec droits de vente (Manager → Boutique / Mise en
  ligne → Accès API), plus `currency` (`1` = CHF) et `Accept-Language` (`fr_FR`) ;
- endpoints « guichet » (commandes, billets, passes) : en plus, **`Authorization`** = jeton
  API du compte Manager (*Mon profil → Développeur → Jetons API*, jeton pour « etickets »),
  envoyé en `Bearer <jeton>` (`infomaniak.token_scheme` : `""` pour l'envoyer brut) ;
- alternative : **`Credential`** renvoyé par `GET /login` (clé + utilisateur + mot de passe
  d'application ; incompatible avec la double authentification).

Secrets GitHub : `INFOMANIAK_API_KEY_<CLUB>` (clé boutique) et `INFOMANIAK_TOKEN_<CLUB>`
(jeton). Le mode `probe` du workflow essaie chaque variante sur `/events` et `/orders`.
Le compte qui crée le jeton doit avoir accès à la billetterie du club.

Champs attendus d'un pass : produit (`pass_type` / `name`), détenteur (`holder` : email,
prénom, nom, date de naissance, code postal, téléphone), montant net, mode de paiement, date
de commande, code du pass. Ensuite :

1. workflow en mode `test` (événements) ;
2. workflow en mode `dry-run` : vérifier `orders_fetched`, les types d'événements, les listes
   prévues, la présence de `CMON-Abonnes-*` et les avertissements ;
3. si un nom de champ diffère (ligne, scan, zone, tarif, prix catalogue), l'indiquer ici et
   l'ajouter aux clés lues dans `ticketing_sync/infomaniak.py`.

Avec `passes_path` vide, les abonnés ne sont reconnus que via leurs places « Place abonné » sur les
matchs, souvent sans email : leurs dates de naissance, NPA et téléphones ne remontent pas.

## Consentement

Processus validé par le club : la source d'acquisition vaut consentement
(`SOURCE_ACQUISITION` + `OPT_IN = YES`, comme l'import manuel), puis la **newsletter
d'accueil** recueille les autres consentements. Options disponibles si besoin :
`only_optin` (ne synchroniser que les acheteurs ayant coché la newsletter à l'achat) et
`"optin_attribute": ""` (ne pas poser `OPT_IN`).
