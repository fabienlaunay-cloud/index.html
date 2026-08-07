"""Amazon restricted-phrase engine — deterministic, zero-cost.

Two severity levels, FR + EN:
- BANNED  : phrases that get a listing suppressed or investigated (medical
            claims, pesticide claims, certification claims, urgency spam).
- CAUTION : phrases Amazon's style guides prohibit or that trigger manual
            review (superlatives, unverifiable claims, promo language).

check_restricted(text) -> [{"phrase", "level"}] — case/accent-insensitive,
word-boundary aware so 'promo' doesn't fire inside 'promontoire'.
"""
import re
import unicodedata
from functools import lru_cache

BANNED = [
    # Medical / health claims (FR)
    "guérit", "guérison", "soigne", "traite le cancer", "anti-cancer", "anticancéreux",
    "anti-inflammatoire", "antibactérien", "anti-bactérien", "antimicrobien",
    "antifongique", "antiviral", "désinfecte", "désinfectant", "stérilise",
    "tue les bactéries", "tue les virus", "élimine les virus", "covid", "coronavirus",
    "vertus thérapeutiques", "effet thérapeutique", "propriétés curatives",
    "soulage la douleur", "anti-douleur", "antidouleur", "traite l'anxiété",
    "antidépresseur", "perte de poids garantie", "brûle les graisses",
    # Medical / health claims (EN)
    "cures", "cure-all", "heals", "anti-cancer", "anti-inflammatory", "antibacterial",
    "antimicrobial", "antifungal", "antiviral", "kills bacteria", "kills viruses",
    "kills germs", "sanitizes", "disinfects", "fda approved", "fda cleared",
    "clinically proven", "cliniquement prouvé", "scientifiquement prouvé",
    "scientifically proven", "treats anxiety", "weight loss guaranteed",
    # Pesticide-treated claims (Amazon pesticide policy)
    "insecticide", "pesticide", "repousse les insectes", "anti-moustique",
    "anti-acarien", "acaricide", "mosquito repellent", "insect repellent",
    "répulsif",
    # Certification / regulatory claims Amazon rejects without proof
    "certifié ce médical", "dispositif médical", "medical device",
    "approuvé par les autorités", "homologué par l'état",
    # Environmental claims banned without certification (US FTC / Amazon)
    "biodégradable", "biodegradable", "compostable", "écologiquement sûr",
    "environmentally safe", "respectueux de l'environnement à 100",
    # Counterfeit-adjacent
    "copie conforme", "réplique de", "compatible genuine", "authentique garanti",
]

CAUTION = [
    # Superlatives & unverifiable claims
    "le meilleur", "la meilleure", "les meilleurs", "meilleur du marché",
    "meilleure qualité", "n°1", "no 1", "numéro 1", "numero 1", "top vente",
    "best seller", "bestseller", "best-seller", "#1", "number one", "the best",
    "world's best", "unique au monde", "incomparable", "inégalé", "imbattable",
    "révolutionnaire", "revolutionary", "miracle", "miraculeux", "parfait",
    "perfection", "incroyable", "extraordinaire", "exceptionnel",
    "qualité supérieure garantie", "premium quality guaranteed",
    # Promo / urgency language (forbidden in Amazon content)
    "promotion", "promo", "soldes", "prix cassé", "offre limitée", "vente flash",
    "déstockage", "remise", "réduction", "discount", "sale", "limited time offer",
    "flash sale", "clearance", "hot deal", "meilleur prix", "best price",
    "prix le plus bas", "lowest price", "livraison gratuite", "free shipping",
    "satisfait ou remboursé", "money back guarantee", "moneyback",
    "achetez maintenant", "buy now", "commandez maintenant", "order now",
    "dépêchez-vous", "hurry", "stock limité", "limited stock",
    # Guarantee claims (Amazon restricts unverifiable guarantees)
    "garantie à vie", "lifetime warranty", "lifetime guarantee", "garanti 100",
    "100% garanti", "100% guaranteed", "résultats garantis", "results guaranteed",
    # Unverifiable eco/safety claims
    "non toxique", "non-toxique", "non toxic", "non-toxic", "sans produits chimiques",
    "chemical free", "chemical-free", "hypoallergénique", "hypoallergenic",
    "testé dermatologiquement", "dermatologist tested", "sans danger pour les enfants",
    "eco-friendly", "écologique", "vert et durable",
    # Review manipulation
    "laissez un avis", "leave a review", "5 étoiles", "5 stars", "avis vérifié",
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def _compiled():
    def rx(phrases):
        # word-ish boundaries; phrases are matched on the accent-stripped text
        parts = [r"(?<![a-z0-9])" + re.escape(_norm(p)).replace(r"\ ", r"[\s\-]+") + r"(?![a-z0-9])"
                 for p in phrases]
        return [(p, re.compile(rp)) for p, rp in zip(phrases, parts)]
    return rx(BANNED), rx(CAUTION)


def check_restricted(text: str) -> list:
    """Return [{'phrase','level'}] for every banned/caution phrase found."""
    if not text:
        return []
    t = _norm(text)
    banned_rx, caution_rx = _compiled()
    out, seen = [], set()
    for phrase, rx_ in banned_rx:
        if phrase not in seen and rx_.search(t):
            out.append({"phrase": phrase, "level": "banned"})
            seen.add(phrase)
    for phrase, rx_ in caution_rx:
        if phrase not in seen and rx_.search(t):
            out.append({"phrase": phrase, "level": "caution"})
            seen.add(phrase)
    return out


def check_listing_restricted(listing: dict) -> dict:
    """Scan title/bullets/description of a listing dict.
    Returns {'banned': [...], 'caution': [...]} with field context."""
    fields = [("titre", listing.get("title") or "")]
    bullets = listing.get("bullet_points") or listing.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    for i, b in enumerate(bullets, 1):
        fields.append((f"bullet {i}", b or ""))
    fields.append(("description", listing.get("description") or ""))
    banned, caution = [], []
    for fname, text in fields:
        for hit in check_restricted(text):
            entry = {"phrase": hit["phrase"], "field": fname}
            (banned if hit["level"] == "banned" else caution).append(entry)
    return {"banned": banned, "caution": caution}
