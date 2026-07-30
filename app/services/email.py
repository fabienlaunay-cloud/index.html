import os
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "hello@synqio.com")
APP_URL   = os.getenv("APP_URL", "https://synqio.io")


def _can_send() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def _unsubscribe_url(email: str) -> str:
    """Generate a GDPR unsubscribe URL for the given email address.
    Uses the same token logic as _unsubscribe_token() in app/routes/auth.py."""
    import urllib.parse
    secret = os.getenv("SMTP_PASS", os.getenv("JWT_SECRET", ""))
    token = hashlib.sha256(f"{email}{secret}".encode("utf-8")).hexdigest()[:16]
    return f"{APP_URL}/?unsubscribe={token}&email={urllib.parse.quote(email)}"


def _is_unsubscribed(to_email: str) -> bool:
    """Check whether the user has unsubscribed from marketing emails."""
    try:
        from app.db import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT email_unsubscribed FROM users WHERE email = ?", (to_email.lower(),)
        ).fetchone()
        conn.close()
        if row and int(row["email_unsubscribed"] or 0):
            return True
    except Exception:
        pass
    return False


def _send(to: str, subject: str, text: str, html: str):
    if not _can_send():
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html",  "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(SMTP_USER, SMTP_PASS)
            srv.sendmail(SMTP_FROM, [to], msg.as_string())
    except Exception:
        pass  # never block user flow due to email failure


def _unsubscribe_footer_html(email: str) -> str:
    url = _unsubscribe_url(email)
    return (
        f'<p style="color:#d1d5db;font-size:11px;margin-top:20px;text-align:center">'
        f'<a href="{url}" style="color:#d1d5db;text-decoration:underline">Se désinscrire des emails</a>'
        f'</p>'
    )


def _unsubscribe_footer_text(email: str) -> str:
    url = _unsubscribe_url(email)
    return f"\n\nPour vous désinscrire : {url}"


def send_invite(to_email: str, invite_url: str, name: str = ""):
    if _is_unsubscribed(to_email):
        return
    greeting = f"Bonjour {name}," if name else "Bonjour,"
    subject = "Votre accès SynqIO — activez votre compte"
    text = f"""{greeting}

Vous avez été invité(e) à rejoindre SynqIO, la plateforme d'optimisation de fiches Amazon.

Activez votre compte en cliquant sur ce lien (valable 72h) :
{invite_url}

Ce lien est personnel et à usage unique.

— L'équipe SynqIO
{_unsubscribe_footer_text(to_email)}"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">Votre invitation est prête</p>
  </div>
  <p style="font-size:15px;margin-bottom:8px">{greeting}</p>
  <p style="font-size:15px;color:#374151;margin-bottom:24px">
    Vous avez été invité(e) à rejoindre <strong>SynqIO</strong>, la plateforme d'optimisation de fiches Amazon alimentée par l'IA.
  </p>
  <div style="background:#f5f3ff;border-radius:12px;padding:20px;margin-bottom:24px;text-align:center">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px">Ce lien est valable <strong>72 heures</strong> et à usage unique.</p>
    <a href="{invite_url}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 32px;border-radius:12px;font-weight:700;text-decoration:none;font-size:15px">
      Activer mon compte →
    </a>
  </div>
  <div style="background:#f9fafb;border-radius:10px;padding:16px;margin-bottom:24px">
    <p style="font-size:12px;color:#9ca3af;margin:0 0 6px">Ou copiez ce lien dans votre navigateur :</p>
    <p style="font-size:11px;color:#6b7280;word-break:break-all;margin:0">{invite_url}</p>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:32px;text-align:center">
    Besoin d'aide ? Répondez à cet email.<br>— L'équipe SynqIO
  </p>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)


def send_welcome(to_email: str):
    if _is_unsubscribed(to_email):
        return
    subject = "🎉 Bienvenue sur SynqIO — vos accès sont prêts"
    text = f"""Bonjour,

Votre compte SynqIO est activé. Voici comment démarrer en 3 étapes :

1. Importez vos produits — CSV ou saisie manuelle dans l'onglet Générer
2. Générez vos fiches — titre, bullets, keywords optimisés Amazon en un clic
3. Exportez vers Seller Central — Catalogue → Ajouter des produits via importation

Accédez à votre espace : {APP_URL}

Besoin d'aide ? Répondez directement à cet email.

— L'équipe SynqIO
{_unsubscribe_footer_text(to_email)}"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">Votre compte est activé 🎉</p>
  </div>
  <p style="font-size:15px;margin-bottom:20px">Démarrez en 3 étapes :</p>
  <table style="width:100%;border-collapse:separate;border-spacing:0 10px">
    <tr><td style="background:#f5f3ff;border-radius:10px;padding:14px 16px">
      <span style="font-weight:700;color:#7c3aed">1.</span>
      <strong>Importez vos produits</strong> — CSV ou saisie manuelle dans l'onglet <em>Générer</em>
    </td></tr>
    <tr><td style="background:#f5f3ff;border-radius:10px;padding:14px 16px">
      <span style="font-weight:700;color:#7c3aed">2.</span>
      <strong>Générez vos fiches</strong> — titre, bullets, keywords optimisés Amazon en un clic
    </td></tr>
    <tr><td style="background:#f5f3ff;border-radius:10px;padding:14px 16px">
      <span style="font-weight:700;color:#7c3aed">3.</span>
      <strong>Exportez vers Seller Central</strong> — Catalogue → Ajouter des produits via importation
    </td></tr>
  </table>
  <div style="text-align:center;margin-top:28px">
    <a href="{APP_URL}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 32px;border-radius:12px;font-weight:700;text-decoration:none;font-size:15px">
      Accéder à mon espace →
    </a>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:32px;text-align:center">
    Besoin d'aide ? Répondez à cet email.<br>— L'équipe SynqIO
  </p>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)


def send_password_changed(to_email: str):
    # Transactional security alert — no unsubscribe check
    subject = "Votre mot de passe SynqIO a été modifié"
    text = f"""Bonjour,

Votre mot de passe SynqIO a bien été modifié.

Si vous n'êtes pas à l'origine de cette modification, contactez-nous immédiatement en répondant à cet email.

— L'équipe SynqIO
"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">Mot de passe modifié</p>
  </div>
  <p style="font-size:15px;margin-bottom:16px">Bonjour,</p>
  <p style="font-size:15px;color:#374151;margin-bottom:24px">
    Votre mot de passe SynqIO a bien été modifié.
  </p>
  <div style="background:#fef9c3;border:1px solid #fde047;border-radius:12px;padding:16px;margin-bottom:24px">
    <p style="font-size:14px;color:#713f12;margin:0">
      Si vous n'êtes pas à l'origine de cette modification,
      <strong>contactez-nous immédiatement</strong> en répondant à cet email.
    </p>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:32px;text-align:center">— L'équipe SynqIO</p>
</body></html>"""
    _send(to_email, subject, text, html)


def send_password_reset(to_email: str, reset_url: str):
    # Transactional email — no unsubscribe check, no unsubscribe footer
    subject = "Réinitialisation de votre mot de passe SynqIO"
    text = f"""Bonjour,

Vous avez demandé la réinitialisation de votre mot de passe SynqIO.

Cliquez sur ce lien (valable 1 heure) :
{reset_url}

Si vous n'avez pas fait cette demande, ignorez cet email — votre mot de passe reste inchangé.

— L'équipe SynqIO
"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">Réinitialisation du mot de passe</p>
  </div>
  <p style="font-size:15px;margin-bottom:20px">Vous avez demandé la réinitialisation de votre mot de passe.</p>
  <div style="background:#f5f3ff;border-radius:12px;padding:20px;margin-bottom:24px;text-align:center">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px">Ce lien est valable <strong>1 heure</strong>.</p>
    <a href="{reset_url}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 32px;border-radius:12px;font-weight:700;text-decoration:none;font-size:15px">
      Réinitialiser mon mot de passe →
    </a>
  </div>
  <p style="font-size:13px;color:#9ca3af;text-align:center">Si vous n'avez pas fait cette demande, ignorez cet email.</p>
  <p style="color:#9ca3af;font-size:13px;margin-top:24px;text-align:center">— L'équipe SynqIO</p>
</body></html>"""
    _send(to_email, subject, text, html)


def send_batch_complete(to_email: str, count: int, plan_label: str):
    if _is_unsubscribed(to_email):
        return
    subject = f"✅ SynqIO — {count} fiche(s) générée(s)"
    text = f"""Bonjour,

Votre génération de {count} fiche(s) est terminée.

Plan : {plan_label}
Accédez à vos fiches : {APP_URL}

— L'équipe SynqIO
{_unsubscribe_footer_text(to_email)}"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:24px;text-align:center;margin-bottom:24px">
    <p style="color:white;font-size:18px;font-weight:700;margin:0">✅ Génération terminée</p>
  </div>
  <p style="font-size:15px">Votre lot de <strong>{count} fiche(s)</strong> est prêt.</p>
  <div style="text-align:center;margin-top:24px">
    <a href="{APP_URL}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 28px;border-radius:12px;font-weight:700;text-decoration:none">
      Voir mes fiches →
    </a>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:28px;text-align:center">— L'équipe SynqIO</p>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)


def send_quota_alert(to_email: str, used: int, total: int, plan_label: str):
    """Send a warning email when a user has consumed 80% of their monthly quota."""
    if _is_unsubscribed(to_email):
        return
    subject = "⚠️ SynqIO — vous avez utilisé 80% de votre quota"
    text = f"""Bonjour,

Vous avez utilisé {used} SKU sur {total} ce mois-ci ({round(used/total*100) if total else 0}%) sur votre plan {plan_label}.

Il vous reste environ {total - used} SKU disponibles ce mois.
Si vous pensez dépasser votre quota, pensez à passer à l'offre supérieure.

Accédez à votre espace : {APP_URL}

— L'équipe SynqIO
{_unsubscribe_footer_text(to_email)}"""
    pct = round(used / total * 100) if total else 0
    remaining = total - used
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">⚠️ Alerte quota</p>
  </div>
  <p style="font-size:15px;margin-bottom:16px">Bonjour,</p>
  <p style="font-size:15px;color:#374151;margin-bottom:24px">
    Vous avez utilisé <strong>{used} SKU sur {total}</strong> ce mois-ci ({pct}%) sur votre plan <strong>{plan_label}</strong>.
    Il vous reste environ <strong>{remaining} SKU</strong> disponibles ce mois.
  </p>
  <div style="background:#fef9c3;border:1px solid #fde047;border-radius:12px;padding:16px;margin-bottom:24px">
    <p style="font-size:14px;color:#713f12;margin:0">
      Si vous pensez dépasser votre quota avant la fin du mois,
      pensez à passer à l'offre supérieure pour ne pas être bloqué.
    </p>
  </div>
  <div style="text-align:center;margin-top:24px">
    <a href="{APP_URL}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 32px;border-radius:12px;font-weight:700;text-decoration:none;font-size:15px">
      Voir mon espace →
    </a>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:32px;text-align:center">— L'équipe SynqIO</p>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)


def send_quota_reached(to_email: str, total: int, plan_label: str, upgrade: dict | None):
    """Sent once per period when the user hits 100% of their SKU quota —
    proposes the next plan up (or a contact link on the top plan)."""
    if _is_unsubscribed(to_email):
        return
    period_word = "cette année" if "an" in (plan_label or "").lower() else "ce mois-ci"
    if upgrade:
        offer_text = (f"Passez au plan {upgrade['label']} ({upgrade['skus']} SKU/mois, "
                      f"{upgrade['price_monthly']} €/mois) pour continuer sans attendre.")
        cta_label = f"Passer au plan {upgrade['label']} →"
        cta_url = f"{APP_URL}/#pricing"
    else:
        offer_text = ("Vous êtes sur notre plus grand plan — contactez-nous pour une "
                      "offre sur mesure adaptée à votre volume.")
        cta_label = "Demander une offre sur mesure →"
        cta_url = "https://calendly.com/fl-synqio/30min"
    subject = "🚀 SynqIO — quota atteint : continuez sans interruption"
    text = f"""Bonjour,

Bonne nouvelle : vous avez utilisé l'intégralité de vos {total} SKU ({plan_label}) — votre catalogue avance vite !

{offer_text}

{cta_url}

— L'équipe SynqIO
{_unsubscribe_footer_text(to_email)}"""
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:32px;text-align:center;margin-bottom:28px">
    <h1 style="color:white;margin:0;font-size:26px;font-weight:800">SynqIO</h1>
    <p style="color:rgba(255,255,255,0.85);margin-top:8px;font-size:16px">🚀 Quota atteint</p>
  </div>
  <p style="font-size:15px;margin-bottom:16px">Bonjour,</p>
  <p style="font-size:15px;color:#374151;margin-bottom:24px">
    Bonne nouvelle : vous avez utilisé l'intégralité de vos <strong>{total} SKU</strong> {period_word}
    sur votre plan <strong>{plan_label}</strong> — votre catalogue avance vite !
  </p>
  <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-radius:12px;padding:16px;margin-bottom:24px">
    <p style="font-size:14px;color:#5b21b6;margin:0">{offer_text}</p>
  </div>
  <div style="text-align:center;margin-top:24px">
    <a href="{cta_url}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:14px 32px;border-radius:12px;font-weight:700;text-decoration:none;font-size:15px">
      {cta_label}
    </a>
  </div>
  <p style="color:#9ca3af;font-size:13px;margin-top:32px;text-align:center">— L'équipe SynqIO</p>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)


def send_admin_new_payment(client_email: str, plan: str, amount_eur: int, found_in_db: bool):
    """Notify admin when a Stripe payment is received."""
    admin_email = os.getenv("ADMIN_EMAIL", "")
    if not admin_email:
        return
    status = "✅ Plan mis à jour" if found_in_db else "⚠️ Utilisateur introuvable en DB — action manuelle requise"
    subject = f"{'✅' if found_in_db else '⚠️'} SynqIO — Nouveau paiement : {client_email}"
    text = f"""Nouveau paiement reçu sur SynqIO.

Client : {client_email}
Plan   : {plan}
Montant: {amount_eur} €
Statut : {status}

Accédez à l'admin : {APP_URL}
"""
    # Backslashes are not allowed inside f-string expressions before Python 3.12
    warning_block = (
        '<div style="background:#fef9c3;border:1px solid #fde047;border-radius:10px;padding:14px;margin-top:16px">'
        '<p style="font-size:13px;color:#713f12;margin:0">'
        "L'utilisateur n'existe pas encore en DB. Envoyez-lui une invitation depuis le panel admin."
        '</p></div>'
    ) if not found_in_db else ''
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:{'linear-gradient(135deg,#059669,#10b981)' if found_in_db else 'linear-gradient(135deg,#d97706,#f59e0b)'};border-radius:16px;padding:24px;text-align:center;margin-bottom:24px">
    <p style="color:white;font-size:18px;font-weight:700;margin:0">{'✅ Nouveau paiement' if found_in_db else '⚠️ Paiement — action requise'}</p>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:8px 0;color:#6b7280">Client</td><td style="padding:8px 0;font-weight:600">{client_email}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Plan</td><td style="padding:8px 0;font-weight:600">{plan}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Montant</td><td style="padding:8px 0;font-weight:600">{amount_eur} €</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Statut</td><td style="padding:8px 0;font-weight:600">{status}</td></tr>
  </table>
  {warning_block}
  <div style="text-align:center;margin-top:24px">
    <a href="{APP_URL}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:12px 28px;border-radius:12px;font-weight:700;text-decoration:none;font-size:14px">
      Ouvrir l'admin →
    </a>
  </div>
</body></html>"""
    _send(admin_email, subject, text, html)


def send_contact(name: str, email: str, message: str = "") -> bool:
    """Deliver a contact-form submission to the SynqIO inbox.

    Returns True if handed to SMTP, False if email isn't configured (so the
    caller can surface a fallback like the Calendly link).
    """
    to = os.getenv("CONTACT_EMAIL", "fl.synqio@gmail.com")
    if not _can_send():
        return False
    safe_name = (name or "—").strip()[:120]
    safe_email = (email or "—").strip()[:160]
    safe_msg = (message or "").strip()[:4000]
    subject = f"📩 Contact SynqIO — {safe_name}"
    text = (
        f"Nouvelle demande de contact depuis synqio.io\n\n"
        f"Nom    : {safe_name}\n"
        f"Email  : {safe_email}\n\n"
        f"Message:\n{safe_msg or '(aucun)'}\n"
    )
    msg_block = (
        f'<tr><td style="padding:8px 0;color:#6b7280;vertical-align:top">Message</td>'
        f'<td style="padding:8px 0;font-weight:500;white-space:pre-wrap">{safe_msg}</td></tr>'
    ) if safe_msg else ''
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:22px;text-align:center;margin-bottom:24px">
    <p style="color:white;font-size:18px;font-weight:700;margin:0">📩 Nouvelle demande de contact</p>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr><td style="padding:8px 0;color:#6b7280">Nom</td><td style="padding:8px 0;font-weight:600">{safe_name}</td></tr>
    <tr><td style="padding:8px 0;color:#6b7280">Email</td><td style="padding:8px 0;font-weight:600"><a href="mailto:{safe_email}">{safe_email}</a></td></tr>
    {msg_block}
  </table>
</body></html>"""
    _send(to, subject, text, html)
    return True


def send_catalog_health(to_email: str, report: dict) -> bool:
    """Weekly Conformity Watchdog digest: catalog health score + items to fix."""
    if not _can_send() or _is_unsubscribed(to_email):
        return False
    score = report.get("score", 100)
    total = report.get("total", 0)
    non_compliant = report.get("non_compliant", 0)
    critical = report.get("critical_count", 0)
    if total == 0:
        return False  # nothing to report

    color = "#059669" if score >= 90 else ("#d97706" if score >= 70 else "#dc2626")
    app_url = os.getenv("APP_URL", "https://synqio.io")

    # Top 5 listings to fix
    rows = ""
    for item in report.get("listings", [])[:5]:
        first = (item.get("issues") or [{}])[0].get("message", "")
        rows += (
            f'<tr><td style="padding:8px 0;font-size:13px;color:#374151">'
            f'<strong>{(item.get("title") or item.get("sku") or "")[:60]}</strong><br>'
            f'<span style="color:#6b7280;font-size:12px">{first}</span></td></tr>'
        )

    subject = f"🛡️ Santé de votre catalogue : {score}/100" + (f" — {non_compliant} fiche(s) à corriger" if non_compliant else " — tout est conforme")
    text = (
        f"Santé de votre catalogue SynqIO\n\n"
        f"Score de conformité : {score}/100\n"
        f"Fiches analysées : {total}\n"
        f"À corriger : {non_compliant} (dont {critical} critiques)\n\n"
        f"Ouvrez SynqIO pour corriger : {app_url}\n"
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:16px;padding:22px;text-align:center;margin-bottom:24px">
    <p style="color:white;font-size:17px;font-weight:700;margin:0">🛡️ Gardien de conformité SynqIO</p>
    <p style="color:rgba(255,255,255,0.8);font-size:13px;margin:6px 0 0">Santé hebdomadaire de votre catalogue</p>
  </div>
  <div style="text-align:center;margin-bottom:22px">
    <div style="font-size:46px;font-weight:800;color:{color}">{score}<span style="font-size:20px;color:#9ca3af">/100</span></div>
    <p style="color:#6b7280;font-size:13px;margin:4px 0 0">{total} fiches analysées · {non_compliant} à corriger · {critical} critiques</p>
  </div>
  {'<table style="width:100%;border-collapse:collapse;border-top:1px solid #eee;margin-bottom:20px">' + rows + '</table>' if rows else '<p style="text-align:center;color:#059669;font-weight:600">✅ Tout votre catalogue est conforme.</p>'}
  <div style="text-align:center;margin-top:8px">
    <a href="{app_url}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:12px 28px;border-radius:12px;font-weight:700;text-decoration:none;font-size:14px">
      Corriger dans SynqIO →
    </a>
  </div>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)
    return True


def send_health_alert(to_email: str, report: dict, reasons: list) -> bool:
    """Proactive alert: the catalog degraded since the last check. Sent outside
    the weekly digest so the seller reacts fast (e.g. an Amazon rule change)."""
    if not _can_send() or _is_unsubscribed(to_email) or not reasons:
        return False
    app_url = os.getenv("APP_URL", "https://synqio.io")
    score = report.get("score", 0)
    reason_items = "".join(f'<li style="margin:4px 0">{r}</li>' for r in reasons)
    reason_text = "\n".join(f"- {r}" for r in reasons)

    subject = f"🚨 Alerte conformité — votre catalogue s'est dégradé (score {score}/100)"
    text = (
        f"Alerte du gardien de conformité SynqIO\n\n"
        f"Votre catalogue s'est dégradé depuis la dernière vérification :\n"
        f"{reason_text}\n\n"
        f"Score actuel : {score}/100 · {report.get('non_compliant', 0)} fiche(s) à corriger.\n\n"
        f"Ouvrez SynqIO pour corriger : {app_url}\n"
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#dc2626,#f97316);border-radius:16px;padding:22px;text-align:center;margin-bottom:22px">
    <p style="color:white;font-size:17px;font-weight:700;margin:0">🚨 Alerte conformité catalogue</p>
    <p style="color:rgba(255,255,255,0.85);font-size:13px;margin:6px 0 0">Une dégradation a été détectée</p>
  </div>
  <p style="font-size:14px;color:#374151;margin:0 0 10px">Depuis la dernière vérification, votre catalogue s'est dégradé :</p>
  <ul style="font-size:14px;color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;border-radius:12px;padding:14px 14px 14px 30px;margin:0 0 18px">
    {reason_items}
  </ul>
  <p style="font-size:13px;color:#6b7280;margin:0 0 18px">Score actuel : <strong>{score}/100</strong> · {report.get('non_compliant', 0)} fiche(s) à corriger.</p>
  <div style="text-align:center">
    <a href="{app_url}" style="display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:white;padding:12px 28px;border-radius:12px;font-weight:700;text-decoration:none;font-size:14px">
      Corriger maintenant →
    </a>
  </div>
  {_unsubscribe_footer_html(to_email)}
</body></html>"""
    _send(to_email, subject, text, html)
    return True
