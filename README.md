# cmon-ticketing-sync

Synchronisation **billetterie Infomaniak → Brevo** pour les clubs C'mon Sports, indépendante
du FanLab. Premier club : Pully Lausanne Foxes.

À chaque passage, la synchro lit toutes les commandes du club et met à jour Brevo :

- **contacts** créés ou mis à jour (email, sinon téléphone) avec les attributs d'achat,
  de présence et de montant (`TICKETING_*`), `SMS`, `NPA`, `BIRTHDAY` ;
- **guideline « Source d'acquisition - Billetterie »** : `SOURCE_ACQUISITION` + `OPT_IN`,
  `DERNIER_MATCH`, `DERNIER_IMPORT` ;
- **référentiel DMOS** : `TICKET_STATUT`, `CATEGORIE_PLACE`, `CATEGORIE_TARIF`, et une liste
  par match / camp / événement, rangée dans le bon dossier (Billetterie, Abonnement,
  Business Engagement, Members & Staff).

Toutes les règles sont décrites dans [`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## Quand la synchro tourne

| Moment | Pourquoi |
|---|---|
| **H-24** avant chaque match | les acheteurs du match sont dans Brevo la veille (campagnes avant-match, SMS matchday) |
| **H+24** après chaque match | les scans sont définitifs : présence, `DERNIER_MATCH` et `DERNIER_IMPORT` à jour pour les automations après-match |

Le workflow [`sync.yml`](.github/workflows/sync.yml) se réveille **toutes les heures** et ne
synchronise que si un match du club atteint H-24 ou H+24 (fenêtre de 90 minutes pour absorber
les retards de GitHub ; une synchro répétée ne change rien). Les heures des événements sont
lues dans Infomaniak, en heure de Zurich. Réglages : section `schedule` du fichier du club.

## Mise en route

1. **Secrets** (Settings → Secrets and variables → Actions → *Secrets*) :
   - `INFOMANIAK_API_KEY_FOXES` : clé API de la boutique, avec droits de vente (Manager
     Infomaniak → Billetterie → Boutique / Mise en ligne → Accès API) ;
   - `INFOMANIAK_TOKEN_FOXES` : jeton API du compte Manager (Mon profil → Développeur →
     Jetons API, jeton pour « etickets »), nécessaire pour lire commandes, billets et
     abonnements ;
   - `BREVO_API_KEY_FOXES` : clé API Brevo du club.
   Si le compte Brevo limite les appels à certaines IP (« Authorized IPs »), désactiver ce
   filtre pour cette clé : les machines GitHub changent d'adresse.
2. **Config du club** : [`clubs/foxes.json`](clubs/foxes.json) (adresses de l'API :
   voir `docs/INTEGRATION.md`).
3. **Vérifier** (onglet *Actions* → *Sync billetterie -> Brevo* → *Run workflow*) :
   - mode `test` : événements (clé) et commandes (jeton) lisibles ;
   - mode `probe` (en cas d'échec) : essaie chaque en-tête d'authentification ;
   - mode `dry-run` : lecture seule. Le journal montre les types d'événements, les listes
     prévues, les compteurs (nouveaux contacts, sources, `DERNIER_IMPORT`…) et les
     avertissements. Aucune donnée de contact n'y figure.
4. **Première vraie synchro** : mettre en pause les automations Brevo basées sur
   `DERNIER_IMPORT`, lancer le mode `sync`, contrôler dans Brevo, puis réactiver.
5. **Activer l'automatique** : créer la variable du dépôt `SYNC_ENABLED` = `true`
   (Settings → Secrets and variables → Actions → *Variables*). Tant qu'elle n'existe pas,
   les passages planifiés ne font qu'un dry run.

Un échec (clé invalide, API indisponible) fait échouer le workflow : GitHub envoie un email.

## Ajouter un club

1. Copier `clubs/foxes.json` en `clubs/<club>.json` et l'adapter (code club, adversaires,
   saison, correspondances de tarifs et de zones, mots-clés du club).
2. Ajouter les secrets `INFOMANIAK_API_KEY_<CLUB>`, `INFOMANIAK_TOKEN_<CLUB>` et
   `BREVO_API_KEY_<CLUB>`.
3. Ajouter `<club>` dans `sync.yml` : liste `options` du lancement manuel et liste `club` de
   la matrice.
4. Faire un `test` puis un `dry-run` avant toute synchro réelle.

## Développement

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && pytest -q

# en local, avec les clés dans l'environnement
INFOMANIAK_API_KEY=... INFOMANIAK_TOKEN=... python -m ticketing_sync --club clubs/foxes.json --test
INFOMANIAK_API_KEY=... INFOMANIAK_TOKEN=... BREVO_API_KEY=... python -m ticketing_sync --club clubs/foxes.json --dry-run
```

| Fichier | Rôle |
|---|---|
| `ticketing_sync/infomaniak.py` | client API Infomaniak, lecture des commandes, lignes, scans |
| `ticketing_sync/brevo.py` | client API Brevo (contacts, attributs, dossiers, listes, import) |
| `ticketing_sync/sync.py` | règles de synchro (attributs, source, présence, montants) |
| `ticketing_sync/event_lists.py` | classement des événements, nommage DMOS des listes |
| `ticketing_sync/schedule.py` | fenêtres H-24 / H+24 |
| `ticketing_sync/__main__.py` | ligne de commande |
| `clubs/*.json` | configuration par club (jamais de clé API ici) |

Ne jamais ajouter d'export de billetterie (`.xlsx`, `.csv`) au dépôt : données personnelles.
