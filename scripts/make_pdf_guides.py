"""
Builds two branded PDFs into docs/:

  SETUP_GUIDE.pdf   - visual production + email setup guide (Dokploy)
  VIDEO_SCRIPT.pdf  - point-by-point tutorial video script (admin + visitor)

Run:  .venv/Scripts/python.exe scripts/make_pdf_guides.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    Preformatted, KeepTogether, PageBreak,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "docs")

# --- Brand palette (from static/css/site.css) --------------------------------
NAVY = colors.HexColor("#16324B")
NAVY2 = colors.HexColor("#2A4A67")
TEAL = colors.HexColor("#2F7566")
TEAL_BG = colors.HexColor("#E1EFEC")
GOLD = colors.HexColor("#C0872E")
GOLD_BG = colors.HexColor("#F3E3C7")
RED = colors.HexColor("#A33A3A")
RED_BG = colors.HexColor("#F6E4E4")
INK = colors.HexColor("#3A4550")
MUTED = colors.HexColor("#6B7684")
BG = colors.HexColor("#F3F5F6")
LINE = colors.HexColor("#DEE3E7")

PAGE_W, PAGE_H = A4
MARGIN = 16 * mm
W = PAGE_W - 2 * MARGIN

# --- Styles -------------------------------------------------------------------
S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26,
                            leading=30, textColor=colors.white),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=12,
                               leading=16, textColor=colors.HexColor("#C4DED7")),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20,
                         textColor=NAVY, spaceBefore=6, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, leading=16,
                         textColor=TEAL, spaceBefore=10, spaceAfter=4),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13.5,
                           textColor=INK, spaceAfter=5),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.5, leading=13.5,
                             textColor=INK, leftIndent=10, spaceAfter=2.5),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=11,
                            textColor=MUTED),
    "steptitle": ParagraphStyle("steptitle", fontName="Helvetica-Bold", fontSize=11,
                                leading=14, textColor=NAVY, spaceAfter=3),
    "mono": ParagraphStyle("mono", fontName="Courier-Bold", fontSize=8.5, leading=12,
                           textColor=colors.HexColor("#E7D3AE")),
    "calltitle": ParagraphStyle("calltitle", fontName="Helvetica-Bold", fontSize=9.5,
                                leading=12, spaceAfter=2),
    "callbody": ParagraphStyle("callbody", fontName="Helvetica", fontSize=9,
                               leading=12.5),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=11.5,
                           textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.5,
                            leading=11.5, textColor=NAVY),
    "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8.5, leading=11.5,
                         textColor=colors.white),
    "vo": ParagraphStyle("vo", fontName="Helvetica-Oblique", fontSize=8.5,
                         leading=12, textColor=NAVY2),
    "screen": ParagraphStyle("screen", fontName="Helvetica", fontSize=8.5,
                             leading=12, textColor=INK),
    "boxlabel": ParagraphStyle("boxlabel", fontName="Helvetica-Bold", fontSize=8,
                               leading=10.5, textColor=colors.white, alignment=1),
    "num": ParagraphStyle("num", fontName="Helvetica-Bold", fontSize=10,
                          textColor=colors.white, alignment=1, leading=13),
}


def P(text, style="body"):
    return Paragraph(text, S[style])

def step_card(num, title, flow):
    """A numbered step card with a teal number chip + gold left bar."""
    chip = Table([[P(str(num), "num")]], colWidths=[9 * mm], rowHeights=[9 * mm])
    chip.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TEAL),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    head = Table([[chip, P(title, "steptitle")]], colWidths=[12 * mm, None])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 2),
    ]))
    card = Table([[head], [flow]], colWidths=[W])
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.75, LINE),
        ("LINEBEFORE", (0, 0), (0, -1), 3, GOLD),
        ("TOPPADDING", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([card, Spacer(1, 5)])


def code(lines):
    body = Preformatted("\n".join(lines), S["mono"])
    t = Table([[body]], colWidths=[W - 4])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def callout(kind, title, text):
    col = {"tip": (TEAL, TEAL_BG), "warn": (GOLD, GOLD_BG), "stop": (RED, RED_BG)}[kind]
    t = Table([[P(title, "calltitle")], [P(text, "callbody")]], colWidths=[W - 4])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), col[1]),
        ("LINEBEFORE", (0, 0), (0, -1), 3, col[0]),
        ("TEXTCOLOR", (0, 0), (0, 0), col[0]),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([t, Spacer(1, 5)])

def data_table(header, rows, widths):
    data = [[P(h, "th") for h in header]] + [
        [(P(c, "cellb") if i == 0 else P(c, "cell")) for c in row]
        for i, row in enumerate(rows)
    ]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def checklist(items):
    rows = [[P("[  ]", "cellb"), P(text, "cell")] for text in items]
    t = Table(rows, colWidths=[8 * mm, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def section(title):
    bar = Table([[""]], colWidths=[W], rowHeights=[2.2])
    bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TEAL)]))
    return KeepTogether([Spacer(1, 8), P(title.upper(), "h1"), bar, Spacer(1, 6)])


def cover(title, subtitle, tagline):
    box = Table(
        [[P(title, "title")], [Spacer(1, 6)], [P(subtitle, "subtitle")],
         [Spacer(1, 14)], [P(tagline, "small")]],
        colWidths=[W],
    )
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LINEBEFORE", (0, 0), (0, -1), 4, GOLD),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
    ]))
    return [Spacer(1, 30 * mm), box,
            P(f"TravelDoor Connect  |  generated {date.today():%d %b %Y}  |  internal documentation",
              "small")]


def make_doc(path, story):
    doc = BaseDocTemplate(path, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=14 * mm, bottomMargin=14 * mm,
                          title=os.path.basename(path))

    def on_page(canv, doc_):
        canv.saveState()
        canv.setFillColor(NAVY)
        canv.rect(0, PAGE_H - 6 * mm, PAGE_W, 6 * mm, stroke=0, fill=1)
        canv.setFillColor(GOLD)
        canv.rect(0, PAGE_H - 7.2 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)
        canv.setFont("Helvetica", 7.5)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN, 8 * mm, "TravelDoor Connect")
        canv.drawRightString(PAGE_W - MARGIN, 8 * mm, "Page %d" % doc_.page)
        canv.restoreState()

    frame = Frame(MARGIN, 12 * mm, W, PAGE_H - 26 * mm, id="main")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=on_page)])
    doc.build(story)

def box_flow_diagram():
    """Simple visual of the production stack using styled table cells."""
    def wbox(text, bg, w=34 * mm):
        t = Table([[P(text, "boxlabel")]], colWidths=[w], rowHeights=[13 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t

    def arrow():
        return P("-->", "num")

    row1 = Table([[wbox("VISITOR<br/>browser", NAVY2), arrow(),
                   wbox("TRAEFIK<br/>HTTPS + domain", NAVY), arrow(),
                   wbox("WEB CONTAINER<br/>gunicorn + static", TEAL), arrow(),
                   wbox("POSTGRES<br/>data volume", GOLD)]],
                 colWidths=[34 * mm, 12 * mm, 34 * mm, 12 * mm, 34 * mm, 12 * mm, 34 * mm])
    row1.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    row2 = Table([[wbox("ADMIN<br/>browser", NAVY2), P("", "cell"),
                   wbox("SCHEDULER CONTAINER<br/>reminders + retries", TEAL), arrow(),
                   wbox("SMTP SERVER<br/>outgoing email", RED)]],
                 colWidths=[34 * mm, 12 * mm, 58 * mm, 12 * mm, 50 * mm])
    row2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    dia = Table([[row1], [P("visitors book at  /b/&lt;event&gt;/   -   admins work at  /panel/   -   both share one Postgres", "small")], [row2]],
                colWidths=[W])
    dia.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BG),
        ("BOX", (0, 0), (-1, -1), 0.75, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return dia


def build_setup_guide():
    st = []
    st += cover("Production Setup Guide",
                "Deploy TravelDoor Connect on Dokploy and switch on real email",
                "Covers: push code  ->  Dokploy deploy  ->  first login  ->  SMTP mail  ->  go-live checks")
    st.append(Spacer(1, 10))

    st.append(section("What you are deploying"))
    st.append(P("Two containers from one image, plus a managed PostgreSQL and your company mailbox:", "body"))
    st.append(box_flow_diagram())
    st.append(Spacer(1, 4))
    st.append(P("<b>web</b> runs migrations, collects static files and serves the app (gunicorn + WhiteNoise). "
                "<b>scheduler</b> sends reminder emails and retries failed deliveries every 30 seconds. "
                "TLS certificates, domain routing and the database are provided by Dokploy.", "body"))

    st.append(callout("tip", "BEFORE YOU START",
                      "Everything below assumes the code is final and committed. Run the test suite first: "
                      "python manage.py test  (160 tests must pass), then continue."))

    st.append(section("Deploy on Dokploy - 6 steps"))
    st.append(step_card(1, "Commit and push the code", [
        P("Every uncommitted file (especially migrations) must be in Git, or the production database "
          "schema will not match the code.", "body"),
        code(["git add -A",
              'git commit -m "Prepare for production deploy"',
              "git push origin main"]),
    ]))
    st.append(step_card(2, "Point DNS at the server", [
        P("In your domain's DNS panel add an <b>A record</b> (or CNAME) for e.g. "
          "<b>booking.yourcompany.com</b> pointing at the Dokploy server IP. If a wildcard "
          "*.yourcompany.com already points there, skip this step.", "body"),
    ]))
    st.append(step_card(3, "Create the database", [
        P("Dokploy -&gt; your project -&gt; <b>Databases -&gt; PostgreSQL -&gt; Create</b>. Wait for it to become healthy. "
          "Open its <b>Connection</b> tab and copy the <b>Internal Connection URL</b> - you will paste it as "
          "DATABASE_URL in step 4.", "body"),
    ]))
    st.append(step_card(4, "Create the app + set environment variables", [
        P("Dokploy -&gt; <b>Create -&gt; Docker Compose</b>, source <b>Git</b>, pick the repository, compose file "
          "<b>docker-compose.dokploy.yml</b>. Then open <b>Environment</b> and add exactly these "
          "(raw values, no quotes, no &lt; &gt; brackets):", "body"),
        data_table(["Variable", "Value", "Why it matters"],
                   [["DATABASE_URL", "internal Postgres URL from step 3", "the app's database"],
                    ["DJANGO_SECRET_KEY", "random string (command below)", "signs sessions; ALSO encrypts the saved SMTP password - never change it later"],
                    ["DJANGO_DEBUG", "false", "false = production hardening (HTTPS redirect, secure cookies)"],
                    ["DJANGO_ALLOWED_HOSTS", "booking.yourcompany.com", "the domain from step 2, no https:// prefix"],
                    ["PUBLIC_BASE_URL", "https://booking.yourcompany.com", "builds every link inside every email"]],
                   [34 * mm, 52 * mm, None]),
        Spacer(1, 3),
        code(['# generate the secret key (any machine with Python):',
              'python -c "import secrets; print(secrets.token_urlsafe(48))"']),
    ]))
    st.append(callout("stop", "DO NOT put SMTP settings in the environment",
                      "Email credentials belong on the in-app Settings screen (step 8), where the password is "
                      "encrypted at rest and survives redeploys. Env-var SMTP is a dev fallback only."))

    st.append(step_card(5, "Add the domain + TLS certificate", [
        P("Dokploy resource -&gt; <b>Domains -&gt; Add domain</b> -&gt; <b>booking.yourcompany.com</b>, service "
          "<b>web</b>, port <b>8000</b>. Issue the Let's Encrypt certificate and keep the HTTPS redirect on.", "body"),
        P("<b>Redeploy after adding the domain</b> - domains are applied as Traefik labels and need one extra "
          "deploy to take effect.", "body"),
    ]))
    st.append(step_card(6, "Deploy and watch the build", [
        P("Click <b>Deploy</b>. First build takes a few minutes. On every boot the web container automatically "
          "runs <b>migrate</b> and <b>collectstatic</b>, then starts gunicorn; the scheduler starts once web is "
          "healthy. When the web service shows <b>healthy</b>, open <b>https://booking.yourcompany.com/health/</b> "
          "- it must answer with  {\"status\": \"ok\"}.", "body"),
    ]))

    st.append(section("First login on production"))
    st.append(step_card(7, "Create the first admin account", [
        P("Production starts with an <b>empty database</b> - your local logins do not exist there. "
          "Dokploy -&gt; web service -&gt; <b>Terminal</b>, then:", "body"),
        code(["python manage.py createsuperuser"]),
        P("Choose a username, <b>your real email address</b> (the test-email button sends there) and a strong "
          "password. Then log in at <b>https://booking.yourcompany.com/accounts/login/</b>.", "body"),
    ]))
    st.append(PageBreak())
    st.append(section("Turning on real email (SMTP)"))
    st.append(P("Everything outbound - booking confirmations, reminders, cancellations, admin welcome mails, "
                "password resets - flows through one SMTP mailbox you configure once on the "
                "<b>Settings screen (/settings/, superuser only)</b>. Until then, nothing is delivered: the app "
                "prints emails to the server console instead.", "body"))

    st.append(step_card(8, "Fill in the Settings screen", [
        P("Log in as superuser -&gt; <b>Settings</b> in the top navigation. Fill in, top to bottom:", "body"),
        data_table(["Field", "What to enter", "Example"],
                   [["Company name", "branding shown in emails + footer", "TravelDoor"],
                    ["Public base URL", "the production address (email links)", "https://booking.yourcompany.com"],
                    ["From name / From address", "who every email is from", "TravelDoor Connect / bookings@yourcompany.com"],
                    ["SMTP server", "your mail provider's server", "smtp.gmail.com"],
                    ["Port + encryption", "587 + STARTTLS  or  465 + SSL (pick ONE)", "587, STARTTLS on, SSL off"],
                    ["SMTP username", "usually the full mailbox address", "bookings@yourcompany.com"],
                    ["SMTP password", "APP password for Gmail / M365, not the login password", "(hidden after save)"]],
                   [36 * mm, 62 * mm, None]),
        Spacer(1, 3),
        P("<b>Save.</b> Changes apply immediately - no restart, no redeploy. The password is stored encrypted; "
          "leaving the field blank keeps the saved one.", "body"),
    ]))

    st.append(P("Mail provider cheat sheet", "h2"))
    st.append(data_table(["Provider", "SMTP server", "Port / encryption", "Password to use"],
                         [["Gmail / Google Workspace", "smtp.gmail.com", "587 STARTTLS (or 465 SSL)",
                           "App Password (needs 2-Step Verification)"],
                          ["Microsoft 365 / Outlook", "smtp.office365.com", "587 STARTTLS", "App Password"],
                          ["cPanel / company mail", "mail.yourcompany.com", "587 STARTTLS (or 465 SSL)",
                           "mailbox password"]],
                         [40 * mm, 42 * mm, 38 * mm, None]))

    st.append(step_card(9, "Prove it works - Send test email", [
        P("Dashboard (<b>/</b>) -&gt; <b>Send test email</b> button (bottom of the status strip). It sends to the "
          "email on YOUR admin account and reports the raw SMTP error on failure - that message is usually the "
          "whole diagnosis.", "body"),
        P("Reminders and automatic retries also need the <b>scheduler</b> container running - the dashboard's "
          "\"background jobs\" line shows its last tick.", "body"),
    ]))
    st.append(callout("warn", "EMAILS GO TO SPAM?",
                      "Set SPF, DKIM and DMARC DNS records for your domain, and make the From address belong to "
                      "the same domain you authenticate the mailbox with. Free Gmail relays ~500 recipients/day."))

    st.append(section("Go-live checklist"))
    st.append(checklist([
        "https://booking.yourcompany.com/health/ answers {\"status\": \"ok\"}",
        "Login works; HTTP redirects to HTTPS automatically",
        "Send test email arrives in your inbox",
        "A test event is created, published, and books end-to-end",
        "Confirmation email arrives; manage link starts with https://booking.yourcompany.com/",
        "Reschedule + cancel from the manage link both work",
        "Dashboard shows a recent background-jobs tick (scheduler alive)",
        "\"Forgot password?\" on the login page delivers a reset link",
    ]))

    st.append(section("Troubleshooting quick table"))
    st.append(data_table(["Symptom", "Likely cause", "Fix"],
        [["Test email: (535, authentication)", "wrong SMTP user/password; Gmail needs an App Password",
          "re-enter credentials in /settings/"],
         ["Test email hangs, then times out", "port/encryption mismatch",
          "587 = STARTTLS on; 465 = SSL on (never both)"],
         ["Bad gateway / DisallowedHost page", "DJANGO_ALLOWED_HOSTS missing the domain",
          "add the domain, redeploy"],
         ["CSRF error when booking", "PUBLIC_BASE_URL differs from the browser address",
          "exact https:// URL, no trailing slash"],
         ["Redirect loop over http", "DJANGO_DEBUG=false forces HTTPS", "use the https:// URL"],
         ["Reminders never arrive", "scheduler stopped or SMTP unset",
          "check scheduler logs; heartbeat on dashboard"],
         ["Saved SMTP password gone", "DJANGO_SECRET_KEY was changed (by design)",
          "re-enter the password in /settings/"],
         ["migrate crashes on boot", "DATABASE_URL contains &lt; &gt; placeholders or quotes",
          "paste the raw internal URL"]],
        [46 * mm, 56 * mm, None]))
    return st
def scene(num, title, duration, screen, voice):
    """One video scene: header row + ON SCREEN actions + VOICEOVER text."""
    head = Table([[P("SCENE %s" % num, "num"), P(title, "steptitle"),
                   P(duration, "cell")]], colWidths=[20 * mm, None, 18 * mm])
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), NAVY),
        ("BACKGROUND", (1, 0), (1, 0), BG),
        ("BACKGROUND", (2, 0), (2, 0), GOLD_BG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    scr = [P("ON SCREEN", "h2")] + [P("- " + s, "screen") for s in screen]
    vo = [P("VOICEOVER", "h2"), P('"%s"' % voice, "vo")]
    body = Table([[scr, vo]], colWidths=[92 * mm, None])
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (1, 0), (1, 0), TEAL_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.75, LINE),
    ]))
    return KeepTogether([head, body, Spacer(1, 6)])


def build_video_script():
    st = []
    st += cover("Tutorial Video Script",
                "Point-by-point script for the screencast - admin side and visitor side",
                "Format: each scene has ON SCREEN (what you record) and VOICEOVER (what you say). "
                "Target length 6-7 minutes at normal pace. Say the lines naturally, do not read.")
    st.append(Spacer(1, 8))

    st.append(section("Before you hit record"))
    st.append(checklist([
        "Create a throwaway event named e.g. 'Demo - Bike Show 2026' (delete it after recording)",
        "Have a second browser window (or incognito) ready for the visitor side - do not log out of admin",
        "Prepare a real inbox you can open, to show the confirmation email",
        "Record at 1920x1080, browser zoom 100%, hide bookmarks bar",
        "Slow down mouse movements; pause 1 second after each click (easier to edit)",
    ]))
    st.append(Spacer(1, 4))

    st.append(section("Part 1 - Intro (about 30 seconds)"))
    st.append(scene(1, "Welcome + what the app is", "0:00 - 0:30",
                    ["Show the login page https://booking.yourcompany.com/accounts/login/",
                     "Log in, land on the Dashboard, let it sit on screen"],
                    "This is TravelDoor Connect - a booking platform for meetings and events. "
                    "Customers pick a time on a public page, everyone gets automatic confirmation and "
                    "reminder emails, and the team manages everything from this back office. "
                    "Let me walk you through both sides, starting as the admin."))

    st.append(section("Part 2 - Admin side: create a bookable event (about 2 minutes)"))
    st.append(scene(2, "Dashboard tour", "0:30 - 0:50",
                    ["Point at the top navigation: Dashboard, Events, People, Bookings, Notifications, Audit log, Users, Settings",
                     "Highlight the stat cards: events, live events, hosts, bookings, flags, failed emails"],
                    "The dashboard is your morning overview - how many events are live, what's booked, "
                    "and whether anything needs attention. All the tools live in this top bar."))
    st.append(scene(3, "Create the event", "0:50 - 1:20",
                    ["Events -> New event -> choose type: In-person or Online",
                     "Fill the form: name, dates, venue or video provider + meeting link",
                     "Save -> land on the event page (status: draft)"],
                    "First, create the event. Choose in-person for a venue, or online for a video call - "
                    "the emails automatically include either the venue details or the join link. "
                    "For now it is a draft; visitors cannot see it yet."))
    st.append(scene(4, "Add the team", "1:20 - 1:45",
                    ["On the event page -> Team -> add hosts from the People directory",
                     "Show a person card: name, role, email, photo",
                     "Optional: quickly open People in the top bar to show the directory"],
                    "Next, add the hosts people can meet. The People directory keeps names, roles, "
                    "photos and emails - reuse them across every event."))
    st.append(scene(5, "Build the time slots", "1:45 - 2:20",
                    ["Slots -> Build slots: pick host, date range, weekdays, start time, duration",
                     "Preview the generated slots on the event page",
                     "Optional: show skipping a whole day (day calendar toggle)"],
                    "Now the calendar. Instead of adding times one by one, the slot builder generates "
                    "them for you - pick the host, the date range, the working days, and the meeting "
                    "length. Need a holiday off? Just skip that day."))
    st.append(scene(6, "Publish and share", "2:20 - 2:40",
                    ["Click Publish -> status changes to LIVE",
                     "Copy the public link ( /b/bike-show-2026/ ) - show where it appears"],
                    "One click, and the event is live. This link is what you send to customers - "
                    "put it in an email, a website, anywhere. Time to switch to the visitor side."))
    st.append(section("Part 3 - Visitor side: booking a meeting (about 2 minutes)"))
    st.append(scene(7, "Open the public page", "2:40 - 3:00",
                    ["Second browser window (incognito), paste the public link",
                     "Show the event landing page with the hosts and their photos"],
                    "This is what your customers see - no login, nothing to install. "
                    "They see the event and the people they can meet."))
    st.append(scene(8, "Pick a host and a time", "3:00 - 3:30",
                    ["Click a host -> their calendar of free slots",
                     "Pick a day, click a free time"],
                    "Choose who to meet, and the calendar shows only the times that are actually free - "
                    "double bookings are impossible by design."))
    st.append(scene(9, "Book it", "3:30 - 3:55",
                    ["Fill name + email, confirm",
                     "Show the confirmation screen"],
                    "A name and an email is all it takes. The time is now reserved instantly."))
    st.append(scene(10, "The confirmation email", "3:55 - 4:25",
                    ["Switch to the inbox, open the confirmation email",
                     "Point at: date and time, host, venue or join link, Google / Outlook calendar buttons",
                     "Point at the manage link at the bottom"],
                    "Within seconds this email arrives. It has everything - when, who, where, or how to "
                    "join the call - plus one-click calendar buttons for Google and Outlook. "
                    "And this manage link lets the customer change or cancel without contacting us."))
    st.append(scene(11, "Self-service reschedule + cancel", "4:25 - 4:55",
                    ["Open the manage link",
                     "Reschedule: pick a new free slot -> confirmation email updates",
                     "Then Cancel -> cancellation email arrives, slot becomes bookable again"],
                    "Customers reschedule or cancel on their own. The old slot frees up instantly, "
                    "and a new email confirms every change. No phone calls, no admin work."))

    st.append(section("Part 4 - Admin side: running the day (about 1.5 minutes)"))
    st.append(scene(12, "Bookings overview", "4:55 - 5:15",
                    ["Bookings in the top bar -> filter list",
                     "Open one booking: visitor details, status, resend confirmation button",
                     "Point at the CSV export button"],
                    "Back as admin, every booking is here - filterable, exportable to a spreadsheet, "
                    "and you can resend a confirmation with one click if someone lost the email."))
    st.append(scene(13, "Attention flags", "5:15 - 5:35",
                    ["Remove a host from the event team",
                     "Show the booking now flagged 'needs attention' on dashboard / bookings",
                     "Click Clear flag"],
                    "If a host drops out, affected bookings raise a red flag on the dashboard - "
                    "so you can call the customer and rebook, then clear the flag. Nothing slips through."))
    st.append(scene(14, "Notifications log + reminders", "5:35 - 5:55",
                    ["Notifications screen: every email logged (sent / failed + error)",
                     "Back on dashboard: point at 'background jobs' heartbeat and email status"],
                    "Every email the system sends is logged here - and if one fails, it retries "
                    "automatically. The background worker also sends reminder emails before each "
                    "meeting, twenty-four hours and one hour by default."))
    st.append(scene(15, "Settings + users", "5:55 - 6:20",
                    ["Settings: company name, public URL, SMTP mail block (mention the Send test email button)",
                     "Users: create an admin -> mention the welcome email with a one-time password"],
                    "Mail runs on your own SMTP account, configured here - no developer needed. "
                    "And you invite colleagues as admins from the Users screen; they receive their "
                    "login details by email and set their own password."))
    st.append(scene(16, "Outro", "6:20 - 6:40",
                    ["Audit log: one line per action, who did what",
                     "Back to dashboard, end on the overview"],
                    "Everything admins do is recorded in the audit log. That's the whole loop - "
                    "publish an event, share one link, and the bookings, emails and reminders "
                    "run themselves. Thanks for watching."))

    st.append(section("Recording tips"))
    st.append(data_table(["Tip", "Why"],
        [["Record admin and visitor parts in separate clips", "easier to reorder; incognito = clean visitor view"],
         ["Do each scene 2-3 times, keep the best take", "voiceover sounds more natural when relaxed"],
         ["Zoom the browser (Ctrl +) to 110% while recording", "text stays readable after video compression"],
         ["Blur real customer names/emails in post", "privacy"],
         ["Record voiceover AFTER the screen capture", "you can pace it to the edits, re-record fluffed lines"]],
        [62 * mm, None]))
    return st
if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    guide = os.path.join(OUT_DIR, "SETUP_GUIDE.pdf")
    script = os.path.join(OUT_DIR, "VIDEO_SCRIPT.pdf")
    make_doc(guide, build_setup_guide())
    make_doc(script, build_video_script())
    for f in (guide, script):
        size = os.path.getsize(f)
        with open(f, "rb") as fh:
            data = fh.read()
        pages = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
        print("%s  ->  %.1f KB, %d pages" % (os.path.basename(f), size / 1024, pages))









