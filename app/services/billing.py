"""Facturation Stripe — tunnel d'inscription self-service.

Le catalogue d'offres reproduit celui de la page de tarifs. Les montants sont
la source de vérité côté serveur : le front envoie seulement un slug de plan,
jamais un prix, pour qu'on ne puisse pas s'inscrire à un tarif choisi
côté navigateur.
"""

import os

from app.logger import log

APP_URL = os.getenv("APP_URL", "https://synqio.io")


# ── Catalogue d'offres ───────────────────────────────────────────────────────
# monthly  : prix affiché au client (toujours ramené au mois)
# amount   : montant réellement prélevé à chaque échéance
# interval : périodicité du prélèvement ("month" ou "year")
# setup    : frais uniques ajoutés à la première facture
#
# Pro / Maintenance est un abonnement ANNUEL : 99 €/mois affichés, mais
# 12 × 99 = 1 188 € encaissés en une seule fois à la souscription.
SIGNUP_PLANS: dict[str, dict] = {
    "maintenance": {
        "label": "Pro / Maintenance",
        "tagline": "Marque établie, catalogue stable",
        "monthly": 99, "amount": 1188, "setup": 0, "interval": "year",
        "skus": "100 SKU / an", "commitment_months": 12,
        "features": [
            "100 SKU par an (pool annuel)",
            "Titre, bullets, mots-clés, contenu A+",
            "Audit trimestriel + score SEO par fiche",
            "Gardien de conformité en continu",
            "Rapport email dès qu'une fiche décroche",
            "Catalogue en ligne à partager par lien",
            "Support standard",
        ],
    },
    "starter": {
        "label": "Starter",
        "tagline": "Sans engagement",
        "monthly": 390, "amount": 390, "setup": 490, "interval": "month",
        "skus": "200 SKU / mois", "commitment_months": 0,
        "features": [
            "200 SKU par mois",
            "7 visuels IA par produit",
            "Push direct Amazon (SP-API)",
            "Audit du compte + score SEO /100 par fiche",
            "Gardien de conformité en continu",
            "Rapport email dès qu'une fiche décroche",
            "Catalogue en ligne à partager par lien",
        ],
    },
    "business": {
        "label": "Business",
        "tagline": "Engagement 3 mois",
        "monthly": 790, "amount": 790, "setup": 990, "interval": "month",
        "skus": "600 SKU / mois", "commitment_months": 3,
        "recommended": True,
        "features": [
            "600 SKU par mois",
            "7 visuels IA par produit",
            "Multi-marketplace simultané",
            "Audit trimestriel + score SEO par fiche",
            "Gardien de conformité en continu",
            "Rapport email dès qu'une fiche décroche",
            "Catalogue en ligne à partager par lien",
            "Support dédié",
        ],
    },
    "scale": {
        "label": "Scale",
        "tagline": "Engagement 6 mois",
        "monthly": 1490, "amount": 1490, "setup": 1990, "interval": "month",
        "skus": "1 500 SKU / mois", "commitment_months": 6,
        "features": [
            "1 500 SKU par mois",
            "7 visuels IA par produit",
            "Multi-marketplace + connecteurs boutique",
            "Audit mensuel + score SEO par fiche",
            "Gardien de conformité en continu",
            "Rapport email dès qu'une fiche décroche",
            "Catalogue en ligne à partager par lien",
            "Support dédié",
        ],
    },
}

ACCOUNT_TYPES = {"brand", "agency", "reseller", "other"}


def _eur(n: int) -> str:
    """1188 → « 1 188 ». Espace simple : la couche bilingue du front ne
    reconnaît pas l'espace insécable étroite des locales système."""
    return f"{n:,}".replace(",", " ")


def public_plans() -> list[dict]:
    """Catalogue exposé au tunnel d'inscription (sans détail interne)."""
    out = []
    for slug, p in SIGNUP_PLANS.items():
        annual = p["interval"] == "year"
        out.append({
            "slug": slug,
            "label": p["label"],
            "tagline": p["tagline"],
            "monthly": p["monthly"],
            "amount": p["amount"],
            "setup": p["setup"],
            "interval": p["interval"],
            "skus": p["skus"],
            "commitment_months": p["commitment_months"],
            "recommended": bool(p.get("recommended")),
            "features": p["features"],
            # ce que le client règle immédiatement
            "first_payment": p["amount"] + p["setup"],
            "billing_note": (f"facturé {_eur(p['amount'])} € par an, en une fois" if annual else ""),
        })
    # ordre d'affichage : du plus léger au plus complet
    order = ["maintenance", "starter", "business", "scale"]
    out.sort(key=lambda x: order.index(x["slug"]) if x["slug"] in order else 99)
    return out


def is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY", "").strip())


def create_checkout_session(email: str, plan: str, name: str = "") -> str:
    """Crée une session Stripe Checkout et renvoie son URL.

    Abonnement récurrent + frais de setup facturés une seule fois sur la
    première facture (add_invoice_items). Lève RuntimeError si Stripe répond
    en erreur — l'appelant décide alors quoi montrer au client.
    """
    cfg = SIGNUP_PLANS.get(plan)
    if not cfg:
        raise ValueError(f"Plan inconnu : {plan}")
    if not is_configured():
        raise RuntimeError("Stripe n'est pas configuré (STRIPE_SECRET_KEY absent)")

    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()

    subscription_data = {
        "metadata": {"synqio_email": email, "synqio_plan": plan},
    }
    if cfg["setup"]:
        subscription_data["add_invoice_items"] = [{
            "price_data": {
                "currency": "eur",
                "unit_amount": cfg["setup"] * 100,
                "product_data": {"name": f"SynqIO — setup unique ({cfg['label']})"},
            },
            "quantity": 1,
        }]

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=email,
        client_reference_id=email,
        line_items=[{
            "price_data": {
                "currency": "eur",
                # amount = ce qui est prélevé par échéance : 1 188 € en une fois
                # pour l'offre annuelle, le mensuel pour les autres.
                "unit_amount": cfg["amount"] * 100,
                "recurring": {"interval": cfg["interval"]},
                "product_data": {
                    "name": f"SynqIO {cfg['label']}",
                    "description": (
                        f"{cfg['skus']} — abonnement annuel SynqIO ({cfg['monthly']} €/mois)"
                        if cfg["interval"] == "year"
                        else f"{cfg['skus']} — abonnement SynqIO"
                    ),
                },
            },
            "quantity": 1,
        }],
        subscription_data=subscription_data,
        billing_address_collection="required",
        tax_id_collection={"enabled": True},
        allow_promotion_codes=True,
        locale="auto",
        metadata={"synqio_email": email, "synqio_plan": plan, "synqio_name": name},
        success_url=f"{APP_URL}/?signup=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/?signup=cancel&email={email}",
    )
    log.info(f"[billing] checkout créé pour {email} — plan {plan} ({session.id})")
    return session.url
