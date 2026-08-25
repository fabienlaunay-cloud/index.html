"""Conformité documentaire GPSR — règlement (UE) 2023/988, applicable depuis
le 13 décembre 2024.

Ce module ne dit **jamais** si un produit est conforme : personne ne peut le
déduire d'une page web. Il constate ce qui est présent ou absent d'une annonce
en vente à distance, ce qui est l'obligation posée par l'article 19 et ce que
les places de marché contrôlent avant de suspendre une fiche.

Deux fonctions :
    extract(html)  -> dict des champs repérés sur la page publique
    check(data)    -> list[dict] des mentions manquantes, au format compliance
"""
import re
import unicodedata as _ud

CRITICAL = "critique"
WARNING = "avertissement"

# Une absence dans le HTML public n'est pas une absence dans la fiche : Amazon
# rend une partie de ces blocs en JavaScript. Tous les messages le disent, et
# aucune règle ne conclut à la non-conformité du produit lui-même.
_UNSEEN = "non détectée sur la page publique"


def _fold(text: str) -> str:
    n = _ud.normalize("NFD", (text or "").lower())
    return "".join(c for c in n if _ud.category(c) != "Mn")


def _clean(raw: str) -> str:
    import html as _html
    t = _html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", t).strip()


def _first(html_text: str, patterns, limit: int = 300) -> str:
    for pat in patterns:
        m = re.search(pat, html_text, re.S | re.I)
        if m:
            v = _clean(m.group(1))
            if 1 < len(v) <= limit:
                return v
    return ""


# ── Repérage sur la page ──────────────────────────────────────────────────────
# Amazon expose ces informations sous plusieurs gabarits selon la catégorie et
# l'ancienneté de la fiche. On tente les variantes connues, du plus explicite au
# plus général, et on s'arrête à la première qui donne un contenu plausible.

_P_GTIN = (
    # Le libellé et la valeur sont séparés par des balises (`</th><td>`), des
    # deux-points ou des espaces insécables. On saute tout ce qui ne contient
    # pas de chiffre, jusqu'au premier nombre — puis la clé GS1 tranche.
    r'\b(?:EAN|GTIN|UPC)(?:[-\s]?(?:8|12|13|14))?\b[^0-9]{0,80}([0-9][0-9\s\-]{7,19})',
    r'"(?:ean|gtin|upc)"\s*:\s*"([0-9]{8,14})"',
)
_P_MANUF = (
    r'>\s*(?:Fabricant|Manufacturer|Hersteller|Fabricante)\s*(?:</[^>]+>\s*)+'
    r'(?:<[^>]+>\s*)*([^<]{2,200})',
    r'"manufacturer"\s*:\s*"([^"]{2,200})"',
)
_P_EU_RP = (
    r'>\s*(?:Personne responsable dans l[\'’]?UE|Responsable UE|'
    r'EU Responsible Person|Responsible person in the EU|'
    r'Verantwortliche Person in der EU)\s*(?:</[^>]+>\s*)+'
    r'(?:<[^>]+>\s*)*([^<]{2,300})',
    r'"(?:euResponsiblePerson|eu_responsible_person)"\s*:\s*"([^"]{2,300})"',
)
_P_SAFETY = (
    r'>\s*(?:Informations? de sécurité|Informations? sur la sécurité|'
    r'Sécurité du produit|Safety information|Product safety|Avertissements?|'
    r'Warnings?)\s*(?:</[^>]+>\s*)+(?:<[^>]+>\s*)*([^<]{10,600})',
)
_P_MODEL = (
    r'>\s*(?:Numéro du modèle|Référence|Model number|Modellnummer)\s*'
    r'(?:</[^>]+>\s*)+(?:<[^>]+>\s*)*([^<]{1,120})',
)

# Marquage CE : « CE » suivi, pour les catégories qui l'exigent, du numéro à
# quatre chiffres de l'organisme notifié. On ne juge pas de sa validité — un
# logo s'imprime — on note seulement s'il est mentionné et sous quelle forme.
_P_CE_NB = re.compile(r"\bCE\s*[–—-]?\s*(\d{4})\b")
_P_CE = re.compile(r"(?<![A-Za-z])CE(?![A-Za-z])")


def extract(html_text: str) -> dict:
    """Champs GPSR repérés sur une page produit publique."""
    html_text = html_text or ""
    gtin = re.sub(r"[^0-9]", "", _first(html_text, _P_GTIN, limit=40))
    text = _clean(html_text)[:400_000]
    nb = _P_CE_NB.search(text)
    return {
        "gtin": gtin if 8 <= len(gtin) <= 14 else "",
        "manufacturer": _first(html_text, _P_MANUF),
        "eu_responsible": _first(html_text, _P_EU_RP),
        "safety_info": _first(html_text, _P_SAFETY, limit=600),
        "model": _first(html_text, _P_MODEL, limit=120),
        "ce_mark": bool(_P_CE.search(text)),
        "ce_notified_body": nb.group(1) if nb else "",
    }


# ── Validation d'un GTIN ─────────────────────────────────────────────────────
def gtin_checksum_ok(gtin: str) -> bool:
    """Clé de contrôle GS1 — un GTIN mal recopié se repère sans base externe."""
    d = re.sub(r"[^0-9]", "", gtin or "")
    if len(d) not in (8, 12, 13, 14):
        return False
    body, check = d[:-1], int(d[-1])
    # Le poids 3 s'applique en partant de la droite du corps du code.
    total = sum(int(c) * (3 if i % 2 == 0 else 1)
                for i, c in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


# ── Règles ───────────────────────────────────────────────────────────────────
def _finding(code, severity, field, message):
    return {"code": code, "severity": severity, "field": field,
            "message": message, "fixable": False, "source": "gpsr"}


# Catégories couvertes par une réglementation sectorielle (dispositifs médicaux,
# cosmétiques, jouets…) : le GPSR y est subsidiaire et d'autres mentions
# s'ajoutent. On le signale plutôt que d'appliquer un jeu de règles incomplet.
_SECTOR_HINTS = {
    "dispositif médical": ("preservatif", "condom", "thermometre", "tensiometre",
                           "test antigenique", "autotest", "seringue", "lentille"),
    "cosmétique": ("creme", "shampoing", "parfum", "maquillage", "deodorant",
                   "dentifrice"),
    "jouet": ("jouet", "peluche", "lego", "puzzle", "figurine"),
    "denrée alimentaire": ("complement alimentaire", "vitamine", "proteine"),
    "équipement électrique": ("chargeur", "batterie", "adaptateur secteur",
                              "rallonge", "multiprise"),
}


def sector_hint(title: str) -> str:
    """Catégorie sectorielle probable, déduite du titre. Indicatif uniquement."""
    hay = _fold(title)
    for label, needles in _SECTOR_HINTS.items():
        if any(n in hay for n in needles):
            return label
    return ""


def check(data: dict, title: str = "") -> list[dict]:
    """Mentions de l'article 19 non repérées sur l'annonce.

    Aucune de ces règles ne conclut à la non-conformité du produit : elles
    constatent l'absence d'une mention obligatoire dans l'offre en ligne, qui
    est le motif de suspension le plus courant sur les places de marché."""
    out: list[dict] = []

    if not (data.get("manufacturer") or "").strip():
        out.append(_finding(
            "GPSR_FABRICANT", CRITICAL, "fabricant",
            f"Identité du fabricant {_UNSEEN} — mention exigée dans toute offre "
            "en vente à distance (art. 19 GPSR)."))

    if not (data.get("eu_responsible") or "").strip():
        out.append(_finding(
            "GPSR_RESPONSABLE_UE", CRITICAL, "responsable_ue",
            f"Personne responsable établie dans l'UE {_UNSEEN} — obligatoire dès "
            "lors que le fabricant est hors Union."))

    if not (data.get("safety_info") or "").strip():
        out.append(_finding(
            "GPSR_SECURITE", WARNING, "securite",
            f"Avertissements et informations de sécurité {_UNSEEN} — ils doivent "
            "figurer dans l'offre, en français."))

    gtin = (data.get("gtin") or "").strip()
    if not gtin:
        out.append(_finding(
            "GPSR_IDENTIFIANT", WARNING, "gtin",
            f"Identifiant produit (EAN/GTIN) {_UNSEEN} — sans lui, aucun "
            "croisement possible avec les rappels officiels."))
    elif not gtin_checksum_ok(gtin):
        out.append(_finding(
            "GPSR_GTIN_INVALIDE", WARNING, "gtin",
            f"L'identifiant « {gtin} » ne passe pas la clé de contrôle GS1 : "
            "probable erreur de saisie."))

    # Marquage CE : uniquement pour les catégories qui l'imposent, et sans
    # jamais préjuger de sa validité.
    sector = sector_hint(title)
    if sector == "dispositif médical":
        if not data.get("ce_notified_body"):
            out.append(_finding(
                "GPSR_CE_ORGANISME", WARNING, "marquage_ce",
                "Aucun numéro d'organisme notifié à quatre chiffres repéré à côté "
                f"du marquage CE ({sector}). À vérifier sur le conditionnement : "
                "sa présence est requise, sa validité se contrôle dans NANDO."))

    return out
