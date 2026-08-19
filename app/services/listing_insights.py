"""Analyse détaillée d'une fiche produit publique.

Tout est déterministe : ni appel IA, ni coût, ni latence notable. On ne
mesure que ce qu'une page publique laisse voir — titre, bullets, description
et images. Les mots-clés backend restent invisibles, donc on les *déduit*
plutôt que de prétendre les connaître.
"""

import re
import unicodedata
from typing import Optional

# Mots-outils écartés du comptage : ils sont fréquents et sans valeur SEO.
_STOPWORDS = {
    # français
    "avec", "sans", "pour", "dans", "chez", "sous", "vers", "plus", "tout",
    "tous", "toute", "toutes", "cette", "votre", "notre", "leur", "leurs",
    "elle", "elles", "nous", "vous", "ils", "des", "les", "une", "aux", "que",
    "qui", "est", "sont", "être", "avoir", "fait", "faire", "peut", "peuvent",
    "aussi", "ainsi", "donc", "mais", "comme", "très", "bien", "entre", "par",
    "sur", "pas", "ses", "son", "sa", "ce", "cet", "de", "du", "la", "le",
    "et", "ou", "un", "en", "au", "il", "on", "ne", "se", "si", "y", "a",
    # anglais
    "with", "without", "for", "from", "this", "that", "these", "those",
    "your", "our", "their", "they", "you", "and", "the", "are", "can",
    "will", "not", "but", "also", "more", "most", "all", "any", "each",
    "into", "onto", "out", "off", "per", "its", "it", "is", "of", "to", "in",
    "on", "as", "at", "by", "or", "an", "be",
}

_TOKEN_RE = re.compile(r"[a-zà-öø-ÿ0-9]+", re.IGNORECASE)


def _fold(text: str) -> str:
    """Minuscules sans accents — pour comparer « Filtré » et « filtre »."""
    n = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(_fold(text)) if len(t) >= 3]


def _is_useful(tok: str) -> bool:
    return tok not in _STOPWORDS and len(tok) >= 4 and not tok.isdigit()


def extract_keywords(title: str, bullets: list[str], description: str = "",
                     limit: int = 12) -> dict:
    """Déduit les mots-clés réellement travaillés dans la fiche.

    Renvoie les termes les plus présents (mots seuls et paires de mots), en
    indiquant lesquels figurent dans le titre. Les termes martelés dans les
    bullets mais absents du titre sont le signal le plus actionnable : c'est
    du volume de recherche laissé sur la table.
    """
    title_folded = _fold(title or "")
    # On compte segment par segment — titre, chaque bullet, chaque phrase de la
    # description — pour que les paires de mots ne se forment jamais à cheval
    # sur deux phrases sans rapport.
    segments = [title or ""]
    segments += [b for b in (bullets or []) if b]
    segments += re.split(r"[.;!?\n]+", description or "")

    counts: dict[str, int] = {}
    for source in segments:
        toks = _tokens(source)
        for t in toks:
            if _is_useful(t):
                counts[t] = counts.get(t, 0) + 1
        # paires de mots consécutifs, tous deux porteurs de sens
        for a, b in zip(toks, toks[1:]):
            if _is_useful(a) and _is_useful(b):
                pair = f"{a} {b}"
                counts[pair] = counts.get(pair, 0) + 1

    # une paire éclipse ses deux composants quand elle est aussi fréquente
    for pair in [k for k in counts if " " in k]:
        a, b = pair.split(" ", 1)
        if counts.get(a) == counts[pair]:
            counts.pop(a, None)
        if counts.get(b) == counts[pair]:
            counts.pop(b, None)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:limit]
    top = [{"term": t, "count": c, "in_title": t in title_folded} for t, c in ranked]
    missing = [k["term"] for k in top if not k["in_title"] and k["count"] >= 2]
    return {"top": top, "missing_in_title": missing[:6]}


# ── Score SEO de la fiche ────────────────────────────────────────────────────
# Barème lisible et assumé : une anomalie critique coûte 35 points, un
# avertissement 12. Une fiche sans anomalie vaut 100. Le score global du
# catalogue, lui, reste calculé par le moteur de conformité.
_CRITICAL = "critique"


def score_listing(issues: list[dict]) -> int:
    penalty = sum(35 if i.get("severity") == _CRITICAL else 12 for i in (issues or []))
    return max(0, 100 - penalty)


# ── Contrôle de l'image principale ───────────────────────────────────────────

def inspect_hero_image(image_bytes: bytes) -> Optional[dict]:
    """Vérifie l'image principale au regard des règles Amazon : fond blanc pur
    et définition suffisante pour le zoom. Renvoie None si l'image est
    illisible."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        w, h = img.size
        rgb = img.convert("RGB")
        # Échantillonnage des quatre coins et du milieu de chaque bord :
        # le fond d'une photo principale conforme y est blanc partout.
        pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
               (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
        samples = [rgb.getpixel(p) for p in pts]
        # Tolérance 250 et non 255 : la compression JPEG décale légèrement le
        # blanc pur, l'exiger au pixel près produirait de faux positifs.
        white = all(all(c >= 250 for c in px) for px in samples)
        darkest = min(min(px) for px in samples)
        return {
            "width": w, "height": h,
            "white_bg": white,
            "edge_min": darkest,
            "zoom_ok": min(w, h) >= 1000,
        }
    except Exception:
        return None


# ── Analyse d'un jeu d'images ────────────────────────────────────────────────

def _ahash(img) -> int:
    """Empreinte perceptuelle 64 bits — repère les visuels identiques ou
    quasi identiques republiés d'un slot à l'autre."""
    small = img.convert("L").resize((8, 8))
    px = list(small.getdata())
    avg = sum(px) / 64.0
    bits = 0
    for i, v in enumerate(px):
        if v > avg:
            bits |= 1 << i
    return bits


def analyse_image(image_bytes: bytes, is_main: bool = False) -> Optional[dict]:
    """Mesure une image produit : blancheur du fond, part du cadre occupée par
    le produit, définition. Rien n'est deviné — tout est mesuré au pixel."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        w, h = img.size
        fingerprint = _ahash(img)

        # Travail sur une vignette : même verdict, coût dérisoire.
        work = img.convert("RGB")
        work.thumbnail((320, 320))
        tw, th = work.size
        gray = work.convert("L")

        # Blancheur du fond : proportion de pixels blancs sur le pourtour.
        border = []
        for x in range(tw):
            border.append(gray.getpixel((x, 0)))
            border.append(gray.getpixel((x, th - 1)))
        for y in range(th):
            border.append(gray.getpixel((0, y)))
            border.append(gray.getpixel((tw - 1, y)))
        white_ratio = sum(1 for v in border if v >= 250) / max(len(border), 1)

        # Part du cadre occupée : boîte englobante de ce qui n'est pas blanc.
        mask = gray.point(lambda v: 255 if v < 245 else 0)
        box = mask.getbbox()
        fill = 0.0
        if box:
            # Amazon parle du produit qui « occupe 85 % du cadre » : la mesure
            # d'usage est linéaire, sur la plus grande dimension de la boîte
            # englobante. La mesurer en surface serait bien plus sévère et
            # ferait échouer des packshots parfaitement conformes.
            fill = max((box[2] - box[0]) / float(tw), (box[3] - box[1]) / float(th))

        kind = ("packshot" if white_ratio >= 0.9
                else "mise en situation" if white_ratio < 0.5
                else "mixte")
        out = {
            "width": w, "height": h,
            "white_ratio": round(white_ratio, 3),
            "fill": round(fill, 3),
            "kind": kind,
            "zoom_ok": min(w, h) >= 1000,
            "square": abs(w - h) <= max(w, h) * 0.02,
            "hash": fingerprint,
        }
        if is_main:
            # Règles du slot principal : fond blanc pur, produit à 85 % du cadre.
            out["main_white_ok"] = white_ratio >= 0.98
            out["main_fill_ok"] = fill >= 0.85
        return out
    except Exception:
        return None


def summarise_images(images: list[dict]) -> dict:
    """Synthèse d'un jeu d'images : variété des types, doublons, définition."""
    ok = [i for i in images if i]
    if not ok:
        return {}
    kinds: dict[str, int] = {}
    for i in ok:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    hashes = [i["hash"] for i in ok]
    dupes = len(hashes) - len(set(hashes))
    return {
        "analysed": len(ok),
        "kinds": kinds,
        "duplicates": dupes,
        "low_res": sum(1 for i in ok if not i["zoom_ok"]),
        "has_lifestyle": kinds.get("mise en situation", 0) > 0,
    }
