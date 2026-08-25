"""Croisement avec les rappels officiels — RappelConso (DGCCRF), open data.

On ne juge rien : on relaie une information publiée par l'administration, avec
sa date et son lien. C'est la partie la moins risquée juridiquement de l'audit
de conformité, et la plus utile au vendeur.

Point critique de conception : « je n'ai pas pu interroger l'API » et « aucun
rappel » sont deux réponses différentes. Toute fonction renvoie donc un statut,
jamais une liste vide ambiguë — afficher « aucun rappel » alors que l'appel a
échoué serait pire que de ne rien afficher.
"""
import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger("synqio.rappelconso")

_BASE = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets"
# Le jeu trié par GTIN éclate les codes-barres en lignes distinctes ; c'est lui
# qui permet une recherche exacte par identifiant produit.
_DS_GTIN = "rappelconso-v2-gtin-espaces"
_DS_ALL = "rappelconso-v2"

_TIMEOUT = 8.0
_TTL = 3600.0          # les rappels sont publiés au fil de l'eau, pas à la seconde
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 500

OK, UNAVAILABLE, SKIPPED = "ok", "unavailable", "skipped"

# Coupe-circuit : sans lui, un audit de 22 fiches face à une API injoignable
# attendrait 22 × 3 × 8 s. Après quelques échecs d'affilée on cesse d'appeler
# pendant une minute, et on répond « indisponible » immédiatement.
_FAIL_MAX = 3
_COOLDOWN = 60.0
_breaker = {"fails": 0, "until": 0.0}


def _breaker_open() -> bool:
    return time.time() < _breaker["until"]


def _note_failure():
    _breaker["fails"] += 1
    if _breaker["fails"] >= _FAIL_MAX:
        _breaker["until"] = time.time() + _COOLDOWN
        _breaker["fails"] = 0
        log.warning("[rappelconso] injoignable — appels suspendus %.0fs", _COOLDOWN)


def _note_success():
    _breaker["fails"] = 0
    _breaker["until"] = 0.0


def _unavailable() -> dict:
    return {"status": UNAVAILABLE, "recalls": [],
            "error": "RappelConso n'a pas répondu — absence de rappel non vérifiée"}


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _store(key: str, value: dict):
    if len(_CACHE) >= _CACHE_MAX:
        # Purge la moitié la plus ancienne : borne la mémoire sans dépendance.
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 2]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time(), value)


def _pick(rec: dict, *names: str) -> str:
    """Premier champ non vide parmi plusieurs noms possibles.

    Le schéma d'un jeu open data évolue ; se lier à un seul nom de colonne, c'est
    casser en silence à la première refonte."""
    for n in names:
        v = rec.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _normalise(rec: dict) -> dict:
    return {
        "libelle": _pick(rec, "libelle", "nom_du_produit", "modeles_ou_references"),
        "marque": _pick(rec, "nom_de_la_marque_du_produit", "marque", "marque_produit"),
        "motif": _pick(rec, "motif_du_rappel", "motif"),
        "risques": _pick(rec, "risques_encourus_par_le_consommateur", "risques"),
        "date": _pick(rec, "date_de_publication", "date_publication",
                      "date_de_debut_de_la_commercialisation"),
        "lien": _pick(rec, "lien_vers_la_fiche_rappel", "lien_vers_la_liste_des_produits",
                      "liens_vers_les_images"),
        "identification": _pick(rec, "identification_des_produits",
                                "identification_des_lots"),
        "distributeurs": _pick(rec, "distributeurs"),
    }


async def _query(client: httpx.AsyncClient, dataset: str,
                 where: str, limit: int) -> list[dict] | None:
    """Une tentative. None = l'API n'a pas répondu correctement."""
    try:
        r = await client.get(f"{_BASE}/{dataset}/records",
                             params={"where": where, "limit": limit})
        if r.status_code != 200:
            log.info("[rappelconso] %s -> HTTP %s (%s)", dataset, r.status_code, where)
            return None
        return (r.json() or {}).get("results") or []
    except Exception as exc:                      # réseau, JSON, timeout
        log.info("[rappelconso] %s -> %s", dataset, type(exc).__name__)
        return None


def _escape(value: str) -> str:
    """Neutralise les guillemets : une valeur scrapée ne doit pas pouvoir
    refermer la chaîne et modifier la requête ODSQL."""
    return re.sub(r'["\\]', "", value or "")[:120]


async def by_gtin(gtin: str) -> dict:
    """Rappels officiels portant sur ce code-barres."""
    digits = re.sub(r"[^0-9]", "", gtin or "")
    if not 8 <= len(digits) <= 14:
        return {"status": SKIPPED, "recalls": [], "error": "identifiant absent ou invalide"}

    key = f"gtin:{digits}"
    if (hit := _cached(key)) is not None:
        return hit
    if _breaker_open():
        return _unavailable()

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        # Plusieurs formulations : le nom de la colonne GTIN n'est pas garanti
        # stable, et `search(*)` reste le filet de sécurité.
        for where in (f'search(*, "{digits}")',
                      f'gtin like "{digits}"',
                      f'identification_des_produits like "{digits}"'):
            rows = await _query(client, _DS_GTIN, where, 20)
            if rows is None:
                continue
            _note_success()
            out = {"status": OK, "recalls": [_normalise(r) for r in rows], "error": ""}
            _store(key, out)
            return out

    _note_failure()
    return _unavailable()   # jamais mis en cache : un échec réseau est passager


async def by_brand(brand: str, limit: int = 20) -> dict:
    """Rappels portant sur une marque. Indicatif : les homonymies existent."""
    b = _escape((brand or "").strip())
    if len(b) < 3:
        return {"status": SKIPPED, "recalls": [], "error": "marque trop courte"}

    key = f"marque:{b.lower()}"
    if (hit := _cached(key)) is not None:
        return hit
    if _breaker_open():
        return _unavailable()

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for where in (f'search(nom_de_la_marque_du_produit, "{b}")',
                      f'search(*, "{b}")'):
            rows = await _query(client, _DS_ALL, where, limit)
            if rows is None:
                continue
            _note_success()
            out = {"status": OK, "recalls": [_normalise(r) for r in rows], "error": ""}
            _store(key, out)
            return out

    _note_failure()
    return _unavailable()


async def for_listing(gtin: str = "", brand: str = "") -> dict:
    """Croisement d'une fiche : par code-barres si on l'a, sinon par marque.

    Le code-barres est fiable, la marque ne l'est pas — d'où le champ `match`,
    que l'interface doit refléter au lieu d'annoncer un rappel certain."""
    if gtin:
        res = await by_gtin(gtin)
        if res["status"] == OK:
            return {**res, "match": "gtin"}
        if res["status"] == UNAVAILABLE:
            return {**res, "match": "gtin"}
    if brand:
        res = await by_brand(brand)
        return {**res, "match": "marque"}
    return {"status": SKIPPED, "recalls": [], "match": "",
            "error": "ni identifiant produit ni marque exploitables"}
