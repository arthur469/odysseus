# Arrêter Odysseus sur Windows

L'installation locale possède deux moyens d'arrêt :

- Le raccourci **Arrêter Odysseus** sur le Bureau lance `Stop-Odysseus.ps1`,
  qui arrête aussi les services auxiliaires prévus par l'installation locale.
- **Paramètres → Compte → Arrêter Odysseus** demande la fermeture normale du
  serveur. Les connexions en cours sont drainées et les hooks de fermeture
  de l'application sont exécutés. Les services externes restent indépendants.

Le bouton nécessite une confirmation. Les tâches automatiques restent
suspendues tant que le serveur est arrêté. Relancer avec le raccourci
**Odysseus** habituel. Fermer uniquement l'onglet ne ferme pas le serveur.

Le bouton est proposé uniquement à un administrateur connecté depuis
`localhost`, `127.0.0.1` ou `::1`, sur Windows natif avec un seul serveur
Uvicorn, sans rechargement automatique ni proxy. Les requêtes d'arrêt doivent
venir de la même origine. Les jetons d'API et les appels d'outils internes
ne donnent pas accès à cette commande. Les autres installations utilisent
leur lanceur ou leur gestionnaire de services habituel.

Développements existants consultés :

- https://github.com/odysseus-dev/odysseus/issues/5561
- https://github.com/odysseus-dev/odysseus/pull/5494 (fermée, non intégrée)
- https://github.com/odysseus-dev/odysseus/issues/5897

Cet ajout local ne remplace pas le lanceur ni les dossiers de données.
