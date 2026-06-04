import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "hello@synqio.com")
APP_URL   = os.getenv("APP_URL", "https://synqio.com")


def _can_send() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


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


def send_welcome(to_email: str):
    subject = "🎉 Bienvenue sur SynqIO — vos accès sont prêts"
    text = f"""Bonjour,

Votre compte SynqIO est activé. Voici comment démarrer en 3 étapes :

1. Importez vos produits — CSV ou saisie manuelle dans l'onglet Générer
2. Générez vos fiches — titre, bullets, keywords optimisés Amazon en un clic
3. Exportez vers Seller Central — Catalogue → Ajouter des produits via importation

Accédez à votre espace : {APP_URL}

Besoin d'aide ? Répondez directement à cet email.

— L'équipe SynqIO
"""
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
</body></html>"""
    _send(to_email, subject, text, html)


def send_batch_complete(to_email: str, count: int, plan_label: str):
    subject = f"✅ SynqIO — {count} fiche(s) générée(s)"
    text = f"""Bonjour,

Votre génération de {count} fiche(s) est terminée.

Plan : {plan_label}
Accédez à vos fiches : {APP_URL}

— L'équipe SynqIO
"""
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
</body></html>"""
    _send(to_email, subject, text, html)
