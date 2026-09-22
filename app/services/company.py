"""Identité légale d'une marque — API publique Recherche d'entreprises.

Source : recherche-entreprises.api.gouv.fr, celle qui alimente
annuaire-entreprises.data.gouv.fr. Ouverte, sans clé, sans quota déclaré.

Même principe que le client RappelConso : « je n'ai pas pu chercher » et « je
n'ai rien trouvé » sont deux réponses distinctes. Un prospect sans société
identifiée n'est pas la même chose qu'un prospect qu'on n'a pas pu vérifier.
"""
import logging
import re
import time

import httpx

log = logging.getLogger("synqio.company")

_BASE = "https://recherche-entreprises.api.gouv.fr/search"
_TIMEOUT = 8.0
_TTL = 86_400.0        # une immatriculation ne bouge pas dans la journée
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_MAX = 300

OK, UNAVAILABLE, SKIPPED, NOT_FOUND = "ok", "unavailable", "skipped", "not_found"

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
        log.warning("[company] injoignable — appels suspendus %.0fs", _COOLDOWN)


def _unavailable(err: str = "") -> dict:
    return {"status": UNAVAILABLE, "company": None,
            "error": err or "Annuaire des entreprises injoignable — "
                            "identité non vérifiée"}


def _pick(d: dict, *names, default=""):
    """Premier champ non vide parmi plusieurs noms possibles.

    Le schéma d'une API publique évolue ; se lier à un seul nom de colonne,
    c'est casser en silence à la première refonte."""
    for n in names:
        v = d.get(n)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return v
    return default


def _effectif(code: str) -> str:
    """Tranche d'effectif INSEE — un code à deux chiffres, illisible tel quel."""
    return {
        "NN": "non renseigné", "00": "0 salarié", "01": "1 à 2",
        "02": "3 à 5", "03": "6 à 9", "11": "10 à 19", "12": "20 à 49",
        "21": "50 à 99", "22": "100 à 199", "31": "200 à 249",
        "32": "250 à 499", "41": "500 à 999", "42": "1 000 à 1 999",
        "51": "2 000 à 4 999", "52": "5 000 à 9 999", "53": "10 000 et plus",
    }.get(str(code or "").strip(), "")


def _normalise(r: dict) -> dict:
    siege = r.get("siege") if isinstance(r.get("siege"), dict) else {}
    dirs = []
    for d in (r.get("dirigeants") or [])[:3]:
        if not isinstance(d, dict):
            continue
        # Une personne physique arrive en capitales : « MARIE DUPONT » se lit
        # mal. Une personne morale, elle, garde sa casse — « HOLDING XYZ » ne
        # doit pas devenir « Holding Xyz ».
        personne = " ".join(x for x in (
            _pick(d, "prenoms", "prenom"), _pick(d, "nom", "nom_complet")) if x).strip()
        if personne:
            nom = personne.title() if personne.isupper() else personne
        else:
            nom = _pick(d, "denomination", "nom_complet")
        if nom:
            dirs.append({"nom": nom, "qualite": _pick(d, "qualite", "fonction")})

    # Le chiffre d'affaires, quand il est publié, arrive indexé par exercice.
    ca, ca_year = None, ""
    fin = r.get("finances")
    if isinstance(fin, dict):
        for year in sorted((k for k in fin if str(k).isdigit()), reverse=True):
            block = fin.get(year)
            if isinstance(block, dict):
                val = block.get("ca") or block.get("chiffre_affaires")
                if isinstance(val, (int, float)):
                    ca, ca_year = val, str(year)
                    break

    ville = " ".join(x for x in (
        str(_pick(siege, "code_postal")), _pick(siege, "libelle_commune", "commune")
    ) if x).strip()

    return {
        "nom": _pick(r, "nom_complet", "nom_raison_sociale", "denomination"),
        "siren": _pick(r, "siren"),
        "date_creation": _pick(r, "date_creation"),
        "activite": _pick(r, "libelle_activite_principale",
                          "activite_principale"),
        "effectif": _effectif(_pick(r, "tranche_effectif_salarie")),
        "dirigeants": dirs,
        "ca": ca,
        "ca_exercice": ca_year,
        "ville": ville,
        # « A » = active. Une société cessée n'est pas un prospect.
        "active": str(_pick(r, "etat_administratif", default="A")).upper() == "A",
    }


def _cached(key: str):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _store(key: str, value: dict):
    if len(_CACHE) >= _CACHE_MAX:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 2]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time(), value)


async def lookup(name: str) -> dict:
    """Société correspondant le mieux à ce nom de marque.

    Le rapprochement nom de marque → société est **indicatif** : une marque
    commerciale ne porte pas toujours le nom de sa société, et deux sociétés
    peuvent porter le même nom. Le champ `confidence` le dit, l'interface doit
    le refléter au lieu d'affirmer."""
    q = re.sub(r"\s+", " ", (name or "").strip())
    if len(q) < 3:
        return {"status": SKIPPED, "company": None,
                "error": "nom de marque trop court"}

    key = q.lower()
    if (hit := _cached(key)) is not None:
        return hit
    if _breaker_open():
        return _unavailable()

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(_BASE, params={"q": q, "per_page": 5})
        if r.status_code != 200:
            log.info("[company] HTTP %s pour %r", r.status_code, q)
            _note_failure()
            return _unavailable(f"réponse HTTP {r.status_code}")
        payload = r.json() or {}
    except Exception as exc:
        log.info("[company] %s pour %r", type(exc).__name__, q)
        _note_failure()
        return _unavailable()

    _breaker.update(fails=0, until=0.0)
    results = payload.get("results") or []
    if not results:
        out = {"status": NOT_FOUND, "company": None, "candidates": [],
               "error": "aucune société ne correspond à ce nom"}
        _store(key, out)
        return out

    rows = [_normalise(x) for x in results if isinstance(x, dict)]
    best = rows[0] if rows else None
    # Une société radiée n'est pas un prospect — mais la marque lui a souvent
    # survécu dans une structure neuve. Quand le premier résultat est cessé et
    # qu'un autre est actif, c'est ce dernier qui intéresse l'utilisateur.
    successor = None
    if best and not best["active"]:
        successor = next((r for r in rows[1:] if r["active"]), None)
        if successor:
            best, successor = successor, best
    # Confiance : le nom de la société contient-il celui de la marque ?
    folded = re.sub(r"[^a-z0-9]+", "", q.lower())
    cand = re.sub(r"[^a-z0-9]+", "", (best or {}).get("nom", "").lower())
    confidence = "haute" if folded and folded in cand else "à confirmer"

    out = {
        "status": OK,
        "company": best,
        "confidence": confidence,
        # La société cessée qu'on a écartée : la mentionner évite de laisser
        # croire qu'on n'a rien vu, et explique le changement de structure.
        "ceased": successor,
        "total": payload.get("total_results") or len(results),
        "candidates": [r for r in rows[1:4] if r is not best],
        "error": "",
    }
    _store(key, out)
    return out
