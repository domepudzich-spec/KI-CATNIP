import os
import re
import json
import io
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict, deque
from urllib.parse import quote

import discord
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types
import httpx
import tempfile
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
BOT_NAME = os.getenv("BOT_NAME", "KI-Catnip")
DEFAULT_SPOILER_LEVEL = os.getenv("DEFAULT_SPOILER_LEVEL", "Dawntrail")
WEB_SEARCH = os.getenv("WEB_SEARCH", "true").lower() in {"1", "true", "yes", "ja"}
GEMINI_FREE_TIER = os.getenv("GEMINI_FREE_TIER", "true").lower() in {"1", "true", "yes", "ja"}

# ---------------------------------------------------------------------------
# Lokales Monatsbudget
# ---------------------------------------------------------------------------
MONTHLY_BUDGET_EUR = float(os.getenv("MONTHLY_BUDGET_EUR", "20.00"))

# Wechselkurs als Konfigurationswert. Stand 07.08.2026 ungefähr:
# 1 USD ≈ 0,867 EUR. Dieser Wert kann jederzeit in .env angepasst werden.
EUR_PER_USD = float(os.getenv("EUR_PER_USD", "0.867"))

# Gemini 2.5 Flash-Lite: Paid-Tier-Äquivalent (nur für lokale Schätzung).
# Im kostenlosen Tier entstehen für unterstützte Nutzung tatsächlich 0 € API-Kosten.
INPUT_USD_PER_M = float(os.getenv("INPUT_USD_PER_M", "0.10"))
CACHED_INPUT_USD_PER_M = float(os.getenv("CACHED_INPUT_USD_PER_M", "0.01"))
OUTPUT_USD_PER_M = float(os.getenv("OUTPUT_USD_PER_M", "0.40"))

# Konservative Paid-Tier-Schätzung für Such-Grounding nach Freikontingenten.
WEB_SEARCH_USD_PER_CALL = float(os.getenv("WEB_SEARCH_USD_PER_CALL", "0.035"))

BUDGET_FILE = Path(os.getenv("BUDGET_FILE", "budget_usage.json"))
BUDGET_WARNING_EUR = float(os.getenv("BUDGET_WARNING_EUR", "15.00"))
BUDGET_SAFETY_RESERVE_EUR = float(os.getenv("BUDGET_SAFETY_RESERVE_EUR", "0.50"))
BUDGET_ADMIN_USER_ID = int(os.getenv("BUDGET_ADMIN_USER_ID", "731192061294018641"))

EVENT_ADMIN_USER_IDS = {
    BUDGET_ADMIN_USER_ID,
    215178140484501504,
    231547593082011649,
    1045068890524373082,
    1010240833225765008,
}


PRIVATE_CHANNELS_ENABLED = os.getenv("PRIVATE_CHANNELS_ENABLED", "true").lower() in {"1", "true", "yes", "ja"}
PRIVATE_CATEGORY_NAME = os.getenv("PRIVATE_CATEGORY_NAME", "Private FFXIV-Anfragen")
DELETE_PRIVATE_CHANNEL_ON_LEAVE = os.getenv("DELETE_PRIVATE_CHANNEL_ON_LEAVE", "true").lower() in {"1", "true", "yes", "ja"}
RETURN_GREETING_ENABLED = os.getenv("RETURN_GREETING_ENABLED", "true").lower() in {"1", "true", "yes", "ja"}
RETURN_GREETING_HOURS = float(os.getenv("RETURN_GREETING_HOURS", "12"))


# Optional: Discord-Rollen-ID, die zusätzlich Zugriff auf alle privaten
# FFXIV-Channels erhält. 0 = keine zusätzliche Rolle.
PRIVATE_ADMIN_ROLE_ID = int(os.getenv("PRIVATE_ADMIN_ROLE_ID", "0"))


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN fehlt in der .env-Datei.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY fehlt in der .env-Datei.")

ai = genai.Client(api_key=GEMINI_API_KEY)
http = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Schattenflauscher-FFXIV-Bot/1.0"})

# Getrennter Gesprächskontext pro Discord-Channel
history = defaultdict(lambda: deque(maxlen=10))

# Spoiler sind standardmäßig vollständig gesperrt.
# Nur Channels, die explizit per /spoiler an freigeschaltet wurden, stehen hier.
spoiler_enabled_channels = set()

# Formulierungen, die Spoiler nur für EINE konkrete Anfrage erlauben.
SINGLE_REQUEST_SPOILER_PHRASES = (
    "du darfst spoilern",
    "spoiler erlaubt",
    "mit spoilern",
    "inklusive spoilern",
    "volle story",
    "komplette story",
    "erzähl alles",
    "erzaehl alles",
    "ohne rücksicht auf spoiler",
    "ohne ruecksicht auf spoiler",
)


SPOILER_PROGRESS = {
    "spoilerfrei": {
        "label": "Spoilerfrei",
        "instruction": "Keine wichtigen Storyspoiler aus irgendeiner Erweiterung verraten.",
    },
    "arr": {
        "label": "A Realm Reborn",
        "instruction": "Storydetails bis einschließlich A Realm Reborn sind erlaubt. Alles ab Heavensward bleibt geschützt.",
    },
    "heavensward": {
        "label": "Heavensward",
        "instruction": "Storydetails bis einschließlich Heavensward sind erlaubt. Alles ab Stormblood bleibt geschützt.",
    },
    "stormblood": {
        "label": "Stormblood",
        "instruction": "Storydetails bis einschließlich Stormblood sind erlaubt. Alles ab Shadowbringers bleibt geschützt.",
    },
    "shadowbringers": {
        "label": "Shadowbringers",
        "instruction": "Storydetails bis einschließlich Shadowbringers sind erlaubt. Alles ab Endwalker bleibt geschützt.",
    },
    "endwalker": {
        "label": "Endwalker",
        "instruction": "Storydetails bis einschließlich Endwalker sind erlaubt. Alles ab Dawntrail bleibt geschützt.",
    },
    "dawntrail": {
        "label": "Dawntrail",
        "instruction": "Storydetails bis einschließlich Dawntrail sind erlaubt. Spätere noch nicht freigegebene Storyinhalte bleiben geschützt.",
    },
}

DEFAULT_PROGRESS_KEY = "spoilerfrei"
PROGRESS_TOPIC_KEY = "FFXIV_SPOILER_LEVEL"


# Letzte Aktivität pro privatem User-Channel.
# Wird nur im Arbeitsspeicher gehalten; nach einem Bot-Neustart beginnt die
# Rückkehr-Erkennung neu.
private_user_last_activity = {}



# ============================================================
# STUFE 3 — KI-CATNIP PERSÖNLICHKEIT
# ============================================================

CATNIP_CHARACTER_PROFILE = """
IDENTITÄT & CHARAKTER
- Du bist KI-Catnip, der digitale Eorzea-Assistent der Discord-Gemeinschaft „Schattenflauscher“.
- Deine Persönlichkeit ist freundlich, neugierig, hilfsbereit, leicht frech und gelegentlich verspielt.
- Du wirkst wie ein vertrauter FC-Begleiter, nicht wie ein steriler Kundendienst-Bot.
- Dezente Katzen-Anspielungen und kleine FFXIV-Anspielungen sind willkommen, aber nur gelegentlich.
- Humor ist erlaubt, aber eine hilfreiche und korrekte Antwort hat immer Vorrang.
- Verwende nicht in jeder Antwort dieselbe Begrüßung oder denselben Abschlusssatz.
- Bei einfachen Wissensfragen kommst du schnell zur Sache.
- Wenn du etwas nicht sicher weißt, gib es offen zu und erfinde nichts.
- Bei Erfolgen darfst du dich kurz mitfreuen.
- Bei Bossmechaniken darfst du leicht über AoEs oder Bodenmarkierungen scherzen.
- Bleibe familienfreundlich und respektvoll.
- Rollenspiel-Flair darf Fakten niemals verfälschen.

WIEDERERKENNUNGSWERT
- Gelegentlich sind Formulierungen wie „Catnip ist zur Stelle“, „die Pfoten sind bereit“ oder „Mögen die AoEs woanders liegen“ erlaubt.
- Nutze solche Formulierungen abwechslungsreich und sparsam.
- Vermeide übertriebenes Katzen-Rollenspiel, Baby-Sprache und dauerndes „Miau“.
""".strip()

SYSTEM_PROMPT = f"""
Du heißt {BOT_NAME}. Wenn Nutzer dich mit "{BOT_NAME}", "Catnip" oder "KI-Catnip" ansprechen, erkennst du dies als deinen Namen. Du bist ein spezialisierter deutschsprachiger Wissensassistent
für FINAL FANTASY XIV Online (FFXIV) und die Eorzea-Enzyklopädie
der Discord-Gemeinschaft „Schattenflauscher“.

{CATNIP_CHARACTER_PROFILE}

DEIN AUFGABENGEBIET
Beantworte möglichst zuverlässig Fragen zu:
- MSQ, Nebenquests, Lore, Figuren, Fraktionen und Orten
- Jobs, Klassen, Skills, Rollen, Ressourcen und Rotationen
- Dungeons, Prüfungen, Raids, Allianz-Raids, Savage und Ultimate
- Bossmechaniken und Encounter-Strategien
- Crafting und Gathering
- Items, Ausrüstung, Währungen und Händler
- Mounts, Minions, Glamour und Housing
- Reliktwaffen und Fortschrittssystemen
- PvP und Crystalline Conflict
- Deep Dungeons, Variant/Criterion und Spezialinhalten
- Abkürzungen, Systemen und Einsteigerfragen

NICHT DEIN AUFGABENGEBIET
- Keine automatischen News-Zusammenfassungen.
- Kein Serverstatus-Monitoring.
Wenn ein Nutzer ausdrücklich eine einzelne Patch- oder aktuelle Inhaltsfrage stellt,
darfst du sie natürlich beantworten.

QUELLEN & AKTUALITÄT
- Nutze Websuche für aktuelle, patchabhängige oder unsichere Angaben.
- Bevorzuge offizielle Quellen: FFXIV Lodestone, Patch Notes, Job Guide,
  Play Guide, Eorzea Database und Square Enix.
- Community-Quellen sind ergänzend für Strategien und Rotationen erlaubt.
- Erfinde keine Potencies, Drop-Orte, Questvoraussetzungen oder Patchstände.
- Marktpreise stammen bei speziellen Markt-Anfragen aus Universalis und sind
  Community-Daten, keine Garantie für den Preis im Spiel.
- Charakterprofile müssen nach Möglichkeit auf dem offiziellen Lodestone geprüft werden.

SPOILER-SCHUTZ — HÖCHSTE PRIORITÄT
- Spoiler sind standardmäßig VERBOTEN, außer innerhalb des ausdrücklich gespeicherten persönlichen Story-Fortschritts.
- Ohne Vollfreigabe darfst du nur Storydetails nennen, die innerhalb des persönlichen Story-Fortschritts liegen.
- Dazu zählen insbesondere: Tode, Opfer, Verrat, geheime Identitäten, wahre Herkunft,
  spätere Bündnisse/Feindschaften, Bossidentitäten, spätere Formen, Enden von Handlungsbögen,
  zentrale Wendungen, Schicksale von Figuren und überraschende Rückkehrer.
- Auch wenn der Nutzer nach einer Figur fragt, gib ohne Freigabe nur spoilerarme Basisinformationen:
  Wer ist die Figur grundsätzlich? Wo/ungefähr wann begegnet man ihr? Welche spoilerfreie Rolle hat sie?
- Ein bloßer Name oder eine allgemeine Frage ist NIEMALS eine Spoilerfreigabe.
- Wenn eine Frage ohne Spoiler nicht sinnvoll beantwortbar ist, sage kurz, dass die Antwort Spoiler
  enthalten würde, und bitte um ausdrückliche Freigabe.
- Wenn Spoiler für die aktuelle Anfrage ausdrücklich erlaubt sind, markiere größere Enthüllungen
  weiterhin mit **⚠️ SPOILER**.
- Bei Unsicherheit immer die spoilerärmere Antwort wählen.


ANTWORTSTIL
- Standardmäßig Deutsch.
- Antworte standardmäßig kompakt: meist 120 bis 250 Wörter.
- Nur wenn der Nutzer ausdrücklich Details möchte, darf die Antwort deutlich länger werden.
- Direkt antworten, danach nur die wichtigsten Details.
- Bei Jobs: Rolle → Kernmechanik → Ressourcen → Spielweise → typische Fehler.
- Bei Bossen: Mechanik → Erkennungszeichen → Reaktion.
- Bei Items/Mounts/Minions: Herkunft → Voraussetzungen → Verwendung/Besonderheiten.
- Fakten und Empfehlungen klar trennen.
- Discord-Markdown verwenden, aber keine unnötigen Textwände.
- Wiederhole die Nutzerfrage nicht.
- Vermeide lange Einleitungen und unnötige Zusammenfassungen.


EVENTS & SPIELLEITER-MODUS
- Du kannst komplette FFXIV-inspirierte Discord-/Gildenevents erstellen.
- Offizielle FFXIV-Fakten und frei erfundene Event-Lore strikt trennen.
- Quizfragen müssen genau eine richtige Antwort haben.
- Bei Quizfragen immer eine klar getrennte Spielleiter-Lösung angeben.
- Bei Boss-Events: Phasen, Mechaniken, Erfolg/Misserfolg, Schaden/Konsequenzen und Finale/Enrage strukturieren.
- Bei Rätseln: eindeutige Lösung, bis zu drei Hinweise und Spielleiter-Lösung.
- Inhalte sollen direkt in Discord, PowerPoint oder einer späteren PDF nutzbar sein.

""".strip()



def current_month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_budget():
    month = current_month_key()
    default = {
        "month": month,
        "estimated_eur": 0.0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "web_search_calls": 0,
        "requests": 0,
        "warnings_sent": [],
    }

    if not BUDGET_FILE.exists():
        return default

    try:
        data = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        if data.get("month") != month:
            return default
        for key, value in default.items():
            data.setdefault(key, value)
        return data
    except Exception:
        return default


def save_budget(data):
    BUDGET_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def budget_remaining():
    data = load_budget()
    return max(0.0, MONTHLY_BUDGET_EUR - float(data["estimated_eur"]))


def budget_exhausted():
    # Im Gemini Free Tier gibt es keine kostenpflichtige API-Nutzung.
    # Das lokale 20-Euro-Limit bleibt als Fallback für einen späteren
    # Wechsel auf einen kostenpflichtigen Gemini-Tarif erhalten.
    if GEMINI_FREE_TIER:
        return False
    return budget_remaining() <= BUDGET_SAFETY_RESERVE_EUR


def count_web_search_calls(response):
    """Schätzt, ob Gemini Google Search Grounding verwendet hat."""
    try:
        candidate = response.candidates[0]
        metadata = getattr(candidate, "grounding_metadata", None)
        queries = getattr(metadata, "web_search_queries", None) if metadata else None
        return len(queries or [])
    except Exception:
        return 0

def record_api_usage(response):
    """
    Speichert Token-Nutzung. Im Free Tier werden reale API-Kosten als 0 €
    behandelt; zusätzlich wird ein Paid-Tier-Äquivalent berechnet, falls
    GEMINI_FREE_TIER=false gesetzt wird.
    """
    data = load_budget()
    usage = getattr(response, "usage_metadata", None)

    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    cached_tokens = int(getattr(usage, "cached_content_token_count", 0) or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    web_calls = count_web_search_calls(response)

    usd_equivalent = (
        uncached_tokens / 1_000_000 * INPUT_USD_PER_M
        + cached_tokens / 1_000_000 * CACHED_INPUT_USD_PER_M
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_M
        + web_calls * WEB_SEARCH_USD_PER_CALL
    )

    eur = 0.0 if GEMINI_FREE_TIER else usd_equivalent * EUR_PER_USD

    data["estimated_eur"] = round(float(data["estimated_eur"]) + eur, 6)
    data["input_tokens"] += input_tokens
    data["cached_input_tokens"] += cached_tokens
    data["output_tokens"] += output_tokens
    data["web_search_calls"] += web_calls
    data["requests"] += 1
    save_budget(data)

    return eur, data

def budget_status_text():
    data = load_budget()
    spent = float(data["estimated_eur"])

    if GEMINI_FREE_TIER:
        return (
            "🆓 **Gemini Free Tier ist aktiv**\n"
            f"**KI-Anfragen diesen Monat:** {data['requests']}\n"
            f"**Input-Tokens:** {data['input_tokens']:,}\n"
            f"**Output-Tokens:** {data['output_tokens']:,}\n"
            f"**Google-Suchen:** {data['web_search_calls']}\n"
            "Aktuell werden vom Bot keine kostenpflichtigen Gemini-API-Kosten "
            "angesetzt. Google-Ratenlimits gelten trotzdem."
        )

    remaining = max(0.0, MONTHLY_BUDGET_EUR - spent)
    percent = min(100.0, (spent / MONTHLY_BUDGET_EUR * 100) if MONTHLY_BUDGET_EUR else 100)
    return (
        f"💶 **Monatsbudget:** {spent:.2f} € / {MONTHLY_BUDGET_EUR:.2f} € ({percent:.0f} %)\n"
        f"**Verbleibend:** ca. {remaining:.2f} €\n"
        f"**KI-Anfragen:** {data['requests']} · **Google-Suchen:** {data['web_search_calls']}\n"
        "_Lokale Gemini-Kostenschätzung; die Anbieterabrechnung ist maßgeblich._"
    )


async def notify_budget_admin_if_needed(data):
    """
    Sendet Budgetwarnungen ausschließlich per DM an den konfigurierten Admin.
    Warnungen werden pro Monat/Stufe nur einmal gesendet.
    """
    if GEMINI_FREE_TIER:
        return

    spent = float(data.get("estimated_eur", 0.0))
    month = data.get("month", current_month_key())

    # Persistierte Warnstufen
    warned = set(data.get("warnings_sent", []))

    thresholds = [
        ("15", BUDGET_WARNING_EUR, "⚠️ Das API-Budget hat die Warnschwelle erreicht."),
        ("18", 18.00, "⚠️ Das API-Budget nähert sich dem Monatslimit."),
        ("limit", MONTHLY_BUDGET_EUR - BUDGET_SAFETY_RESERVE_EUR,
         "🛑 Das API-Budget ist praktisch ausgeschöpft."),
    ]

    for key, threshold, headline in thresholds:
        token = f"{month}:{key}"
        if spent >= threshold and token not in warned:
            try:
                user = client.get_user(BUDGET_ADMIN_USER_ID)
                if user is None:
                    user = await client.fetch_user(BUDGET_ADMIN_USER_ID)

                remaining = max(0.0, MONTHLY_BUDGET_EUR - spent)
                await user.send(
                    f"{headline}\n"
                    f"**Verbraucht:** ca. {spent:.2f} € / {MONTHLY_BUDGET_EUR:.2f} €\n"
                    f"**Verbleibend:** ca. {remaining:.2f} €\n"
                    f"**Anfragen:** {data.get('requests', 0)} · "
                    f"**Websuchen:** {data.get('web_search_calls', 0)}"
                )
                warned.add(token)
            except Exception as exc:
                print(f"Budget-DM konnte nicht gesendet werden: {exc}")

    data["warnings_sent"] = sorted(warned)
    save_budget(data)



class EorzeaBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()

        # Wichtig für @Nyx-Nachrichten.
        # Im Discord Developer Portal muss "Message Content Intent"
        # ebenfalls aktiviert werden.
        intents.message_content = True

        # Für automatische Channels beim Serverbeitritt.
        # Im Discord Developer Portal muss zusätzlich "Server Members Intent"
        # aktiviert werden.
        intents.members = True

        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("✓ Slash-Commands synchronisiert.")

    async def close(self):
        await http.aclose()
        await super().close()


client = EorzeaBot()


def split_message(text: str, limit: int = 1900):
    text = (text or "").strip()
    if not text:
        return ["*Der Äther schweigt.*"]
    if len(text) <= limit:
        return [text]

    parts = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break

        cut = rest.rfind("\n\n", 0, limit)
        if cut < 600:
            cut = rest.rfind("\n", 0, limit)
        if cut < 600:
            cut = rest.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit

        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()

    return parts


def extract_sources(response, max_sources=5):
    sources = []
    seen = set()
    try:
        candidate = response.candidates[0]
        metadata = getattr(candidate, "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) if metadata else None
        for chunk in chunks or []:
            web = getattr(chunk, "web", None)
            url = getattr(web, "uri", None) if web else None
            title = getattr(web, "title", None) if web else None
            if url and url not in seen:
                seen.add(url)
                sources.append((title or "Quelle", url))
            if len(sources) >= max_sources:
                break
    except Exception:
        pass
    return sources

def append_sources(answer: str, sources):
    if not sources:
        return answer
    lines = ["", "**Quellen:**"]
    for title, url in sources:
        title = title.replace("[", "").replace("]", "")
        lines.append(f"• [{title}]({url})")
    return answer + "\n".join(lines)



def should_use_web(question: str) -> bool:
    """
    Kostensparende Heuristik:
    Websuche nur bei klar aktuellen, markt-, patch- oder detailabhängigen Fragen.
    """
    q = question.lower()
    triggers = (
        "aktuell", "heute", "neuester", "neueste", "neuesten", "patch",
        "hotfix", "potency", "potencies", "skillwert", "drop rate",
        "droprate", "markt", "preis", "marketboard", "marktbrett",
        "wo bekomme ich", "fundort", "händler", "vendor",
        "questvoraussetzung", "freischalten", "unlock",
        "level 100 rotation", "bis", "best in slot"
    )
    return any(term in q for term in triggers)



def request_explicitly_allows_spoilers(question: str) -> bool:
    q = question.lower()
    return any(phrase in q for phrase in SINGLE_REQUEST_SPOILER_PHRASES)


def spoilers_allowed_for_request(channel_id: int, question: str) -> bool:
    # Persistente Channel-Freigabe ODER ausdrückliche Freigabe nur für diese Anfrage.
    return channel_id in spoiler_enabled_channels or request_explicitly_allows_spoilers(question)


def spoiler_status_text(channel_id: int) -> str:
    progress = get_spoiler_progress(channel_id)
    label = progress_label(progress)

    if channel_id in spoiler_enabled_channels:
        return (
            "🔓 **Vollständige Spoilerfreigabe ist derzeit AKTIV.**\n"
            f"Dein gespeicherter Story-Fortschritt ist **{label}**.\n"
            "Mit `/spoiler aus` kehrst du wieder zu dieser persönlichen Spoilergrenze zurück."
        )

    return (
        "🔒 **Persönlicher Spoilerschutz ist AKTIV.**\n"
        f"Dein Story-Fortschritt: **{label}**\n"
        "KI-Catnip darf nur bis zu diesem Stand spoilern. "
        "Mit `/fortschritt` kannst du den Stand ändern oder mit `/spoiler an` "
        "vorübergehend alles freigeben."
    )


async def ask_ai(channel_id: int, username: str, question: str, *, remember=True, force_web=False):
    if budget_exhausted():
        raise RuntimeError(
            f"MONATSBUDGET_ERREICHT:{MONTHLY_BUDGET_EUR:.2f}"
        )

    contents = []

    if remember:
        recent_history = list(history[channel_id])[-6:]
        for item in recent_history:
            role = "model" if item["role"] == "assistant" else "user"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=item["content"])]
                )
            )

    allow_spoilers = spoilers_allowed_for_request(channel_id, question)
    progress_key = get_spoiler_progress(channel_id)
    progress = SPOILER_PROGRESS.get(
        progress_key, SPOILER_PROGRESS[DEFAULT_PROGRESS_KEY]
    )

    if allow_spoilers:
        spoiler_instruction = (
            "SPOILERFREIGABE FÜR DIESE ANFRAGE: JA. "
            "Alle Storybereiche dürfen beantwortet werden. "
            "Größere Enthüllungen weiterhin deutlich mit **⚠️ SPOILER** markieren."
        )
    else:
        spoiler_instruction = (
            "SPOILERFREIGABE FÜR DIESE ANFRAGE: NEIN. "
            f"PERSÖNLICHER STORY-FORTSCHRITT: {progress['label']}. "
            f"{progress['instruction']} "
            "Tode, Verrat, Identitätsenthüllungen, Bossidentitäten, Schicksale und "
            "Storywendungen jenseits dieser Grenze niemals verraten. "
            "Bei Unsicherheit die spoilerärmere Antwort wählen."
        )

    user_text = f"{username}: {question}\n\n{spoiler_instruction}"
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_text)]
        )
    )

    use_web = force_web or (WEB_SEARCH and should_use_web(question))

    tools = None
    if use_web:
        tools = [
            types.Tool(
                google_search=types.GoogleSearch()
            )
        ]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=tools,
        temperature=0.35,
        max_output_tokens=1800,
    )

    print(
        f"Gemini-Anfrage: user={username}, model={GEMINI_MODEL}, "
        f"web={'ja' if use_web else 'nein'}"
    )

    response = await ai.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=config,
    )

    request_cost_eur, budget_data = record_api_usage(response)
    await notify_budget_admin_if_needed(budget_data)

    answer = (response.text or "").strip()
    if not answer:
        answer = "Ich konnte dazu gerade keine Textantwort erzeugen."

    answer = append_sources(answer, extract_sources(response))

    if remember:
        history[channel_id].append({"role": "user", "content": user_text})
        history[channel_id].append({"role": "assistant", "content": answer})

    return answer


async def send_interaction(interaction, prompt, *, remember=False, force_web=False):
    await interaction.response.defer(thinking=True)

    try:
        answer = await ask_ai(
            interaction.channel_id,
            interaction.user.display_name,
            prompt,
            remember=remember,
            force_web=force_web,
        )
        for part in split_message(answer):
            await interaction.followup.send(part)
    except Exception as exc:
        print(f"KI-Fehler: {type(exc).__name__}: {exc}")
        if str(exc).startswith("MONATSBUDGET_ERREICHT:"):
            await interaction.followup.send(
                "⚠️ **Das monatliche Nutzungslimit des Bots wurde erreicht.**\n"
                "Weitere KI-Anfragen sind bis zum nächsten Kalendermonat gesperrt."
            )
        else:
            await interaction.followup.send(
                "⚠️ Die Anfrage konnte gerade nicht beantwortet werden. "
                "Prüfe die Bot-Konsole und API-Einstellungen."
            )


async def send_channel(channel, text):
    for part in split_message(text):
        await channel.send(part)




EVENT_ADMIN_ROLE_NAME = os.getenv("EVENT_ADMIN_ROLE_NAME", "KI-Catnip Event-Admin")


async def get_or_create_event_admin_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name=EVENT_ADMIN_ROLE_NAME)
    if role:
        return role

    # Deliberately no global Discord "Administrator" permission.
    # The role identifies trusted KI-Catnip event admins and can be granted
    # access to Catnip-managed private channels.
    return await guild.create_role(
        name=EVENT_ADMIN_ROLE_NAME,
        permissions=discord.Permissions.none(),
        reason="KI-Catnip Event-Administratoren",
    )


async def sync_event_admin_role(guild: discord.Guild):
    try:
        role = await get_or_create_event_admin_role(guild)
        for user_id in EVENT_ADMIN_USER_IDS:
            member = guild.get_member(user_id)
            if member and role not in member.roles:
                await member.add_roles(role, reason="KI-Catnip Event-Admin Whitelist")
    except discord.Forbidden:
        print(
            "Event-Admin-Rolle konnte nicht synchronisiert werden. "
            "Dem Bot fehlt vermutlich 'Manage Roles'."
        )
    except Exception as exc:
        print(f"Fehler bei Event-Admin-Rollensynchronisierung: {exc}")



# ===========================================================================
# PRIVATE FFXIV-CHANNELS
# ===========================================================================

def private_channel_topic(member: discord.Member) -> str:
    return (
        f"FFXIV_PRIVAT_USER_ID={member.id} | "
        f"{PROGRESS_TOPIC_KEY}={DEFAULT_PROGRESS_KEY}"
    )


def get_spoiler_progress_from_channel(channel) -> str:
    if not isinstance(channel, discord.TextChannel) or not channel.topic:
        return DEFAULT_PROGRESS_KEY

    match = re.search(
        rf"{re.escape(PROGRESS_TOPIC_KEY)}=([a-z]+)",
        channel.topic,
        flags=re.IGNORECASE,
    )
    if not match:
        return DEFAULT_PROGRESS_KEY

    key = match.group(1).lower()
    return key if key in SPOILER_PROGRESS else DEFAULT_PROGRESS_KEY


def get_spoiler_progress(channel_id: int) -> str:
    return get_spoiler_progress_from_channel(client.get_channel(channel_id))


def progress_label(progress_key: str) -> str:
    return SPOILER_PROGRESS.get(
        progress_key, SPOILER_PROGRESS[DEFAULT_PROGRESS_KEY]
    )["label"]


async def set_spoiler_progress(channel: discord.TextChannel, progress_key: str):
    if progress_key not in SPOILER_PROGRESS:
        progress_key = DEFAULT_PROGRESS_KEY

    topic = channel.topic or ""

    if re.search(
        rf"{re.escape(PROGRESS_TOPIC_KEY)}=[a-z]+",
        topic,
        flags=re.IGNORECASE,
    ):
        topic = re.sub(
            rf"{re.escape(PROGRESS_TOPIC_KEY)}=[a-z]+",
            f"{PROGRESS_TOPIC_KEY}={progress_key}",
            topic,
            flags=re.IGNORECASE,
        )
    else:
        topic = (
            f"{topic} | {PROGRESS_TOPIC_KEY}={progress_key}"
            if topic
            else f"{PROGRESS_TOPIC_KEY}={progress_key}"
        )

    await channel.edit(
        topic=topic[:1024],
        reason="KI-Catnip: persönlicher FFXIV-Storyfortschritt aktualisiert",
    )


def sanitize_channel_name(name: str) -> str:
    """
    Erzeugt einen Discord-tauglichen Channelnamen.
    Die User-ID am Ende sorgt dafür, dass Namen eindeutig bleiben.
    """
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9äöüß_-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "mitglied"
    return name[:50]



def private_channel_owner_id(channel: discord.TextChannel):
    if not channel.topic:
        return None
    match = re.search(r"FFXIV_PRIVAT_USER_ID=(\d+)", channel.topic)
    return int(match.group(1)) if match else None


async def maybe_send_return_greeting(message: discord.Message):
    """
    Begrüßt den Besitzer eines privaten FFXIV-Channels nach längerer Pause.
    Discord meldet nicht, wenn ein Textchannel nur geöffnet wird; deshalb
    erfolgt die Rückkehr-Begrüßung bei der ersten neuen Nachricht des Users.
    """
    if not RETURN_GREETING_ENABLED:
        return

    if not isinstance(message.channel, discord.TextChannel):
        return

    owner_id = private_channel_owner_id(message.channel)
    if owner_id is None or message.author.id != owner_id:
        return

    now = datetime.now(timezone.utc)
    last = private_user_last_activity.get(message.channel.id)
    private_user_last_activity[message.channel.id] = now

    # Beim allerersten Schreiben nach Botstart keine zusätzliche Rückkehr-
    # Begrüßung; der Channel besitzt bereits die Willkommensnachricht.
    if last is None:
        return

    if now - last >= timedelta(hours=RETURN_GREETING_HOURS):
        await message.channel.send(
            f"🐱 **Willkommen zurück, {message.author.display_name}!** "
            f"Wie kann ich dir heute bei **FINAL FANTASY XIV** helfen? "
            f"Du kannst mich einfach mit `@{BOT_NAME}` ansprechen. ✨"
        )


def find_private_channel(guild: discord.Guild, member_id: int):
    token = f"FFXIV_PRIVAT_USER_ID={member_id}"
    for channel in guild.text_channels:
        if channel.topic and token in channel.topic:
            return channel
    return None


async def get_or_create_private_category(guild: discord.Guild):
    existing = discord.utils.get(guild.categories, name=PRIVATE_CATEGORY_NAME)
    if existing:
        return existing

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        ),
    }

    admin_role = guild.get_role(PRIVATE_ADMIN_ROLE_ID) if PRIVATE_ADMIN_ROLE_ID else None
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    event_admin_role = discord.utils.get(guild.roles, name=EVENT_ADMIN_ROLE_NAME)
    if event_admin_role:
        overwrites[event_admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )

    return await guild.create_category(
        PRIVATE_CATEGORY_NAME,
        overwrites=overwrites,
        reason="Kategorie für private FFXIV-Anfragen"
    )


async def create_private_ffxiv_channel(member: discord.Member):
    if not PRIVATE_CHANNELS_ENABLED or member.bot:
        return None

    existing = find_private_channel(member.guild, member.id)
    if existing:
        return existing

    category = await get_or_create_private_category(member.guild)

    overwrites = {
        member.guild.default_role: discord.PermissionOverwrite(
            view_channel=False
        ),
        member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        member.guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        ),
    }

    admin_role = (
        member.guild.get_role(PRIVATE_ADMIN_ROLE_ID)
        if PRIVATE_ADMIN_ROLE_ID else None
    )
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )

    event_admin_role = discord.utils.get(member.guild.roles, name=EVENT_ADMIN_ROLE_NAME)
    if event_admin_role:
        overwrites[event_admin_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )

    base = sanitize_channel_name(member.display_name)
    channel_name = f"ffxiv-{base}-{str(member.id)[-4:]}"

    channel = await member.guild.create_text_channel(
        channel_name,
        category=category,
        topic=private_channel_topic(member),
        overwrites=overwrites,
        reason=f"Privater FFXIV-Channel für {member}",
    )

    is_admin_member = member.id in EVENT_ADMIN_USER_IDS

    if is_admin_member:
        embed = discord.Embed(
            title=f"🐱 Willkommen bei KI-Catnip, {member.display_name}!",
            description=(
                "Du bist als **KI-Catnip-Administrator** freigeschaltet.\n\n"
                f"Frag mich einfach mit `@{BOT_NAME}` oder nutze einen der Slash-Commands. "
                "Hier bekommst du einmalig die vollständige Funktionsübersicht."
            ),
        )
        embed.add_field(
            name="💬 Fragen & Wissen",
            value=(
                f"`@{BOT_NAME} deine Frage`\n"
                "`/ffxiv` – allgemeine FFXIV-Frage\n"
                "`/lore` – Lore, Figuren und Orte\n"
                "`/job` – Jobs, Skills und Spielweise\n"
                "`/kampf` – Dungeon-, Trial- und Raidmechaniken\n"
                "`/quest` – Hilfe bei Quests\n"
                "`/begriff` – Begriffe und Abkürzungen\n"
                "`/einsteiger` – einfache Erklärungen"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔎 Suche & Nachschlagen",
            value=(
                "`/charakter` – einfache Charaktersuche\n`/spielersuche` – erweiterte Lodestone-Spielersuche\n"
                "`/mount` – Mount-Herkunft und Freischaltung\n"
                "`/minion` – Minion-Herkunft und Freischaltung\n"
                "`/markt` – Marktbrettdaten über Universalis\n"
                "`/wissen` – Schattenpfoten-Wissensdatenbank durchsuchen\n"
                "`/wissensliste` – Wissenseinträge anzeigen"
            ),
            inline=False,
        )
        embed.add_field(
            name="📖 Persönlich & RP",
            value=(
                "`/fortschritt` – persönlicher Story-Stand\n"
                "`/spoiler` – Spoilerschutz verwalten\n"
                "`/profil` – Punkte-/Titelprofil\n"
                "`/rangliste` – Rangliste\n"
                "`/charaktererstellen` – RP-Charakter anlegen\n"
                "`/charakterprofil` – RP-Charakter anzeigen\n"
                "`/charakterbearbeiten` – RP-Charakter ändern\n"
                "`/rp` – persönliche RP-Szene\n"
                "`/rpquest` – persönliche Quest\n"
                "`/rpgruppe` – Gruppen-RP"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎭 Events & Spielleitung",
            value=(
                "`/quiz` – FFXIV-Quiz erstellen\n"
                "`/event` – Gildenevent planen\n"
                "`/boss` – Event-Boss entwerfen\n"
                "`/raetsel` – einzelnes Rätsel erstellen\n"
                "`/eventerstellen` – Event-Anmeldung\n"
                "`/eventadmin` – Event-Admin-Zentrale\n"
                "`/bossgruppe` – Kampfgruppe eröffnen\n"
                "`/bossstart` – interaktiven Bosskampf starten\n"
                "`/raetselevent` – mehrstufiges Rätsel-Event starten"
            ),
            inline=False,
        )
        embed.add_field(
            name="🐾 Schattenpfoten-Wissen",
            value=(
                "`/wissensadmin` – Wissensdatenbank-Adminmenü\n"
                "`/wissenhinzufuegen` – Wissen speichern\n"
                "`/wissenbearbeiten` – Wissen ändern\n"
                "`/wissenloeschen` – Wissen löschen"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔧 Verwaltung & Diagnose",
            value=(
                "`/admin` – Bot-Admin-Dashboard\n"
                "`/diagnose` – Systemdiagnose\n"
                "`/eventadmins` – Event-Admins anzeigen\n"
                "`/punkte` – Punkte verwalten\n"
                "`/budget` – Nutzung anzeigen\n"
                "`/reset` – Gesprächskontext löschen\n"
                "`/botinfo` – Funktionsübersicht"
            ),
            inline=False,
        )
        embed.add_field(
            name="📄 Guides",
            value="`/pdf` – erstellt einen FFXIV-Guide als PDF-Datei",
            inline=False,
        )
        embed.set_footer(
            text="Admin-Begrüßung • vollständige KI-Catnip-Funktionsübersicht"
        )

    else:
        embed = discord.Embed(
            title=f"🐱 Willkommen bei KI-Catnip, {member.display_name}!",
            description=(
                f"Schön, dass du da bist! **Wie kann ich dir bei FINAL FANTASY XIV helfen?**\n\n"
                f"Frag mich einfach mit `@{BOT_NAME}` oder nutze die Such- und Fragebefehle unten."
            ),
        )
        embed.add_field(
            name="💬 KI-Catnip fragen",
            value=(
                f"`@{BOT_NAME} Wie funktioniert Viper?`\n"
                "`/ffxiv` – allgemeine FFXIV-Frage\n"
                "`/lore` – Lore, Figuren und Orte\n"
                "`/job` – Jobs, Skills und Spielweise\n"
                "`/kampf` – Dungeon-, Trial- und Raidmechaniken\n"
                "`/quest` – Hilfe bei Quests\n"
                "`/begriff` – Begriffe und Abkürzungen\n"
                "`/einsteiger` – einfache Erklärungen"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔎 Suchen & Nachschlagen",
            value=(
                "`/charakter` – einfache Charaktersuche\n`/spielersuche` – erweiterte Lodestone-Spielersuche\n"
                "`/mount` – Mount-Herkunft und Freischaltung\n"
                "`/minion` – Minion-Herkunft und Freischaltung\n"
                "`/markt` – Marktbrettdaten über Universalis\n"
                "`/wissen` – Schattenpfoten-Wissen durchsuchen\n"
                "`/wissensliste` – Wissenseinträge anzeigen"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔒 Spoilerschutz",
            value=(
                "`/fortschritt` – persönlichen Story-Stand festlegen\n"
                "`/spoiler status` – aktuellen Schutz prüfen\n"
                "`/spoiler an` – Spoiler ausdrücklich erlauben\n"
                "`/spoiler aus` – wieder auf persönlichen Fortschritt begrenzen"
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Normale Nutzer sehen hier nur Fragen, Suche und Spoilerschutz. "
                "Admin-/Eventfunktionen werden nicht in dieser Begrüßung angezeigt."
            )
        )

    await channel.send(member.mention, embed=embed)
    return channel


@client.event
async def on_member_join(member: discord.Member):
    try:
        if member.id in EVENT_ADMIN_USER_IDS:
            await sync_event_admin_role(member.guild)
        await create_private_ffxiv_channel(member)
    except discord.Forbidden:
        print(
            f"Private Channel konnte für {member} nicht erstellt werden: "
            "Dem Bot fehlen 'Manage Channels'-Rechte."
        )
    except Exception as exc:
        print(f"Fehler beim Erstellen des privaten Channels für {member}: {exc}")


@client.event
async def on_member_remove(member: discord.Member):
    if not PRIVATE_CHANNELS_ENABLED or not DELETE_PRIVATE_CHANNEL_ON_LEAVE:
        return

    channel = find_private_channel(member.guild, member.id)
    if not channel:
        return

    try:
        # Gesprächskontext ebenfalls entfernen.
        history.pop(channel.id, None)
        private_user_last_activity.pop(channel.id, None)
        spoiler_enabled_channels.discard(channel.id)
        await channel.delete(
            reason=f"Mitglied {member} hat den Server verlassen"
        )
    except discord.Forbidden:
        print(
            f"Privater Channel von {member} konnte nicht gelöscht werden: "
            "Dem Bot fehlen 'Manage Channels'-Rechte."
        )
    except Exception as exc:
        print(f"Fehler beim Löschen des privaten Channels für {member}: {exc}")


@client.tree.command(
    name="privatchat",
    description="Erstellt deinen privaten FFXIV-Channel, falls noch keiner existiert."
)
async def privatchat(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
        await interaction.response.send_message(
            "Dieser Befehl funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    if not PRIVATE_CHANNELS_ENABLED:
        await interaction.response.send_message(
            "Private FFXIV-Channels sind derzeit deaktiviert.",
            ephemeral=True,
        )
        return

    existing = find_private_channel(interaction.guild, interaction.user.id)
    if existing:
        await interaction.response.send_message(
            f"🌙 Du hast bereits einen privaten FFXIV-Channel: {existing.mention}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        channel = await create_private_ffxiv_channel(interaction.user)
        await interaction.followup.send(
            f"✅ Dein privater FFXIV-Channel wurde erstellt: {channel.mention}",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ Ich darf keine Channels erstellen. Ein Administrator muss mir "
            "die Berechtigung **Kanäle verwalten / Manage Channels** geben.",
            ephemeral=True,
        )


# ===========================================================================
# @MENTION: Einfach @Nyx im normalen Chat ansprechen
# ===========================================================================

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(
        f"Discord-Nachricht empfangen: user={message.author} "
        f"channel={message.channel.id} "
        f"user_mentions={len(message.mentions)} "
        f"role_mentions={len(getattr(message, 'raw_role_mentions', []) or [])}"
    )

    # Rückkehr-Begrüßung im persönlichen FFXIV-Channel.
    await maybe_send_return_greeting(message)

    if not client.user:
        return

    # Robuste Mention-Erkennung:
    # 1) Bot-User direkt erwähnt
    # 2) rohe <@ID>/<@!ID>-Erwähnung
    # 3) Discord-verwaltete Bot-Rolle erwähnt (z.B. @KI-CATNIP)
    raw_user_mention = (
        f"<@{client.user.id}>" in message.content
        or f"<@!{client.user.id}>" in message.content
    )

    user_mentioned = (
        client.user.mentioned_in(message)
        or client.user in message.mentions
        or raw_user_mention
    )

    bot_role_ids = set()
    if message.guild and message.guild.me:
        for role in message.guild.me.roles:
            try:
                if role.is_bot_managed():
                    bot_role_ids.add(role.id)
            except Exception:
                pass

    mentioned_bot_role_ids = set(getattr(message, "raw_role_mentions", []) or [])
    role_mentioned = bool(bot_role_ids & mentioned_bot_role_ids)

    mentioned = user_mentioned or role_mentioned

    if not mentioned:
        return

    mention_kind = "Bot-User" if user_mentioned else "Bot-Rolle"
    print(
        f"@Mention erkannt ({mention_kind}): user={message.author} "
        f"channel={message.channel.id}"
    )

    # Bot-User- und Bot-Rollen-Erwähnungen aus der eigentlichen Frage entfernen.
    question = message.content
    question = question.replace(f"<@{client.user.id}>", "")
    question = question.replace(f"<@!{client.user.id}>", "")

    for role_id in bot_role_ids:
        question = question.replace(f"<@&{role_id}>", "")

    question = question.strip()

    if not question:
        await message.reply(
            f"🐱 Frag mich einfach etwas zu **FINAL FANTASY XIV**.\n"
            f"Zum Beispiel: `@{BOT_NAME} Wie funktioniert Viper?`"
        )
        return

    async with message.channel.typing():
        try:
            print(
                f"@Mention-Frage wird verarbeitet: "
                f"user={message.author.display_name}"
            )
            answer = await ask_ai(
                message.channel.id,
                message.author.display_name,
                question,
                remember=True,
            )

            print("@Mention-Antwort erfolgreich erzeugt.")

            for part in split_message(answer):
                await message.channel.send(part)

        except Exception as exc:
            print(
                f"Mention-Fehler: {type(exc).__name__}: {exc}"
            )

            if str(exc).startswith("MONATSBUDGET_ERREICHT:"):
                await message.reply(
                    "⚠️ Das monatliche Nutzungslimit des Bots wurde erreicht. "
                    "Weitere KI-Anfragen sind bis zum nächsten Kalendermonat gesperrt."
                )
            else:
                await message.reply(
                    "⚠️ Ich konnte deine Frage gerade nicht beantworten. "
                    "Bitte versuche es noch einmal oder nutze `/ffxiv`."
                )


# ===========================================================================
# UNIVERSALER WISSENSCHAT
# ===========================================================================

@client.tree.command(name="ffxiv", description="Stelle eine beliebige Frage zu FINAL FANTASY XIV.")
@app_commands.describe(frage="Deine FFXIV-Frage")
async def ffxiv(interaction: discord.Interaction, frage: str):
    await send_interaction(interaction, frage, remember=True)


@client.tree.command(name="lore", description="Frage nach FFXIV-Lore, Figuren, Orten oder Ereignissen.")
@app_commands.describe(thema="Deine Lore-Frage")
async def lore(interaction: discord.Interaction, thema: str):
    await send_interaction(
        interaction,
        f"Beantworte diese FFXIV-Lorefrage spoilerbewusst: {thema}",
    )


@client.tree.command(name="job", description="Erklärt einen FFXIV-Job oder beantwortet eine Jobfrage.")
@app_commands.describe(job="Jobname", frage="Optional: konkrete Frage")
async def job(interaction: discord.Interaction, job: str, frage: str = ""):
    prompt = (
        f"FFXIV-Job: {job}. "
        + (f"Frage: {frage}" if frage else
           "Gib einen aktuellen Überblick über Rolle, Kernmechanik, Ressourcen, "
           "Spielweise, Grundrotation und häufige Fehler.")
    )
    await send_interaction(interaction, prompt)


@client.tree.command(name="kampf", description="Erklärt einen Dungeon-, Trial- oder Raidkampf.")
@app_commands.describe(inhalt="Inhalt", boss="Optional: Boss", schwierigkeit="Normal/Extreme/Savage/Ultimate")
async def kampf(
    interaction: discord.Interaction,
    inhalt: str,
    boss: str = "",
    schwierigkeit: str = "",
):
    await send_interaction(
        interaction,
        f"Erkläre den FFXIV-Kampf '{inhalt}'. Boss: {boss or 'nicht angegeben'}. "
        f"Schwierigkeit: {schwierigkeit or 'nicht angegeben'}. "
        "Strukturiere jede Mechanik als Erkennungszeichen → richtige Reaktion.",
    )


@client.tree.command(name="quest", description="Hilft bei einer FFXIV-Quest.")
@app_commands.describe(quest="Questname", problem="Wo hängst du fest?")
async def quest(interaction: discord.Interaction, quest: str, problem: str = ""):
    await send_interaction(
        interaction,
        f"FFXIV-Quest: {quest}. Problem: {problem or 'allgemeine Hilfe'}. "
        "Nenne Start-NPC, Gebiet, Voraussetzungen und nächsten Schritt nur, "
        "wenn du sie zuverlässig bestimmen kannst. Vermeide unnötige Spoiler.",
        force_web=True,
    )


@client.tree.command(name="begriff", description="Erklärt einen FFXIV-Begriff oder eine Abkürzung.")
@app_commands.describe(begriff="z.B. GCD, oGCD, BiS, PF, LB3")
async def begriff(interaction: discord.Interaction, begriff: str):
    await send_interaction(
        interaction,
        f"Erkläre den FFXIV-Begriff '{begriff}' verständlich mit einem Praxisbeispiel.",
    )


@client.tree.command(name="einsteiger", description="Erklärt ein FFXIV-Thema besonders einfach.")
@app_commands.describe(frage="Was möchtest du verstehen?")
async def einsteiger(interaction: discord.Interaction, frage: str):
    await send_interaction(
        interaction,
        f"Erkläre diese FFXIV-Frage besonders einsteigerfreundlich: {frage}",
    )



# ============================================================
# STUFE 10.1 — ERWEITERTE FFXIV-SPIELERSUCHE
# ============================================================

@client.tree.command(
    name="spielersuche",
    description="Sucht einen öffentlichen FFXIV-Spielercharakter über den Lodestone."
)
@app_commands.describe(
    name="Vor- und Nachname des FFXIV-Charakters",
    welt="Optional: Heimatwelt/Server, z. B. Shiva, Odin oder Phoenix",
)
async def spielersuche(
    interaction: discord.Interaction,
    name: str,
    welt: str = "",
):
    prompt = f"""
Suche nach einem öffentlich sichtbaren FINAL FANTASY XIV Spielercharakter.

CHARAKTERNAME:
{name}

HEIMATWELT:
{welt or "nicht angegeben"}

WICHTIGE REGELN:
- Nutze vorrangig bzw. ausschließlich den offiziellen FINAL FANTASY XIV Lodestone.
- Wenn keine eindeutige Übereinstimmung gefunden wird, sage das klar.
- Wenn mehrere Charaktere mit diesem Namen existieren, liste die plausibelsten Treffer
  mit Charaktername und Heimatwelt auf, statt einen davon willkürlich auszuwählen.
- Erfinde keine Charakterdaten.
- Nutze ausschließlich öffentlich sichtbare Informationen.
- Keine Rückschlüsse auf Square-Enix-Account, echte Identität, E-Mail, Wohnort
  oder andere nicht öffentlich sichtbare Kontodaten.
- Wenn eine Heimatwelt angegeben wurde, priorisiere exakt diese Welt.

WENN EIN EINDEUTIGER TREFFER GEFUNDEN WIRD:
Gib die öffentlich sichtbaren Informationen möglichst kompakt in dieser Reihenfolge aus:

1. **Charaktername**
2. **Heimatwelt**
3. **Datenzentrum**, falls zuverlässig bestimmbar
4. **Volk / Stamm**, sofern öffentlich sichtbar
5. **Stadtstaat**, sofern öffentlich sichtbar
6. **Staatliche Gesellschaft**, sofern öffentlich sichtbar
7. **Freie Gesellschaft**, sofern öffentlich sichtbar
8. **Jobs / Klassen**, nur soweit öffentlich sichtbar und zuverlässig
9. **Lodestone-Hinweis**, dass die Daten aus dem öffentlichen Profil stammen

Verlinke, wenn zuverlässig gefunden, das offizielle Lodestone-Profil als Quelle.

Antworte auf Deutsch.
""".strip()

    await send_interaction(
        interaction,
        prompt,
        remember=False,
        force_web=True,
    )


@client.tree.command(
    name="spielersuchehilfe",
    description="Zeigt Beispiele für die KI-Catnip-Spielersuche."
)
async def spielersuchehilfe(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔎 KI-Catnip — FFXIV-Spielersuche",
        description=(
            "Suche nach öffentlich sichtbaren Spielercharakteren über den "
            "**FINAL FANTASY XIV Lodestone**."
        ),
    )
    embed.add_field(
        name="Beispiel",
        value=(
            "`/spielersuche name:Vorname Nachname`\n"
            "`/spielersuche name:Vorname Nachname welt:Shiva`"
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Tipp",
        value=(
            "Wenn ein Name mehrfach vorkommt, gib die **Heimatwelt** mit an. "
            "Damit kann KI-Catnip den richtigen Charakter deutlich besser eingrenzen."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Datenschutz",
        value=(
            "KI-Catnip verwendet nur öffentlich sichtbare Charakterinformationen "
            "und versucht nicht, private Account- oder Identitätsdaten zu ermitteln."
        ),
        inline=False,
    )
    embed.set_footer(text="Stufe 10.1 • Erweiterte FFXIV-Spielersuche")
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ===========================================================================
# CHARAKTER-SUCHE (offizieller Lodestone über Websuche)
# ===========================================================================

@client.tree.command(name="charakter", description="Sucht einen FFXIV-Charakter auf dem Lodestone.")
@app_commands.describe(name="Charaktername", welt="Optional: Heimatwelt/Server")
async def charakter(interaction: discord.Interaction, name: str, welt: str = ""):
    prompt = f"""
Suche nach dem FFXIV-Spielercharakter "{name}".
Heimatwelt, falls bekannt: "{welt or 'nicht angegeben'}".

Bevorzuge ausschließlich bzw. vorrangig den offiziellen FINAL FANTASY XIV Lodestone.
Wenn mehrere Treffer existieren, sage das klar und liste die plausibelsten Treffer.
Nenne nur öffentlich sichtbare Profilinformationen.
Erfinde keine Charakterdaten.
""".strip()
    await send_interaction(interaction, prompt, force_web=True)


# ===========================================================================
# MOUNTS & MINIONS
# ===========================================================================

@client.tree.command(name="mount", description="Sucht Herkunft und Freischaltung eines FFXIV-Mounts.")
@app_commands.describe(name="Name des Mounts")
async def mount(interaction: discord.Interaction, name: str):
    await send_interaction(
        interaction,
        f"FFXIV-Mount: '{name}'. Prüfe den aktuellen Stand und erkläre: "
        "Herkunft, Freischaltbedingungen, benötigte Währung/Item/Quest, "
        "ob es handelbar ist und besondere Hinweise.",
        force_web=True,
    )


@client.tree.command(name="minion", description="Sucht Herkunft eines FFXIV-Begleiters/Minions.")
@app_commands.describe(name="Name des Begleiters")
async def minion(interaction: discord.Interaction, name: str):
    await send_interaction(
        interaction,
        f"FFXIV-Minion/Begleiter: '{name}'. Prüfe den aktuellen Stand und erkläre "
        "Herkunft, Freischaltbedingungen, Handelbarkeit und besondere Hinweise.",
        force_web=True,
    )


# ===========================================================================
# MARKTBRETT: XIVAPI v2 + Universalis
# ===========================================================================

async def xivapi_find_item(name: str):
    # Erst deutsche Suche, danach englische Suche als Fallback.
    for lang in ("de", "en"):
        params = {
            "sheets": "Item",
            "fields": "Name",
            "query": f'Name~"{name}"',
            "language": lang,
            "limit": 5,
        }
        response = await http.get("https://v2.xivapi.com/api/search", params=params)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if results:
            return results
    return []


async def universalis_market(world: str, item_id: int):
    url = f"https://universalis.app/api/v2/{quote(world)}/{item_id}"
    response = await http.get(url, params={"listings": 10, "entries": 10})
    response.raise_for_status()
    return response.json()


@client.tree.command(name="markt", description="Prüft aktuelle Marktbrettdaten über Universalis.")
@app_commands.describe(
    item="Name des Gegenstands",
    welt="Welt, Datenzentrum oder Region, z.B. Shiva oder Light"
)
async def markt(interaction: discord.Interaction, item: str, welt: str):
    await interaction.response.defer(thinking=True)

    try:
        matches = await xivapi_find_item(item)
        if not matches:
            await interaction.followup.send(
                f"❌ Ich konnte **{item}** in den FFXIV-Spieldaten nicht finden."
            )
            return

        # Nimmt den relevantesten XIVAPI-Suchtreffer.
        selected = matches[0]
        item_id = selected.get("row_id")
        fields = selected.get("fields", {})
        item_name = fields.get("Name") or item

        data = await universalis_market(welt, item_id)
        listings = data.get("listings", [])
        recent = data.get("recentHistory", [])

        if not listings:
            await interaction.followup.send(
                f"📉 Für **{item_name}** wurden auf **{welt}** derzeit keine "
                "aktuellen Universalis-Angebote gefunden.\n"
                "*Universalis basiert auf von Spielern hochgeladenen Marktdaten.*"
            )
            return

        prices = [x.get("pricePerUnit", 0) for x in listings if x.get("pricePerUnit")]
        quantities = [x.get("quantity", 0) for x in listings]

        lowest = min(prices) if prices else 0
        top = sorted(listings, key=lambda x: x.get("pricePerUnit", 10**18))[:5]

        embed = discord.Embed(
            title=f"💰 {item_name}",
            description=f"Marktbrettdaten für **{welt}**",
        )

        embed.add_field(
            name="Günstigster Preis",
            value=f"**{lowest:,} Gil** pro Stück".replace(",", "."),
            inline=False,
        )

        listing_lines = []
        for entry in top:
            price = entry.get("pricePerUnit", 0)
            qty = entry.get("quantity", 0)
            hq = " HQ" if entry.get("hq") else ""
            listing_lines.append(
                f"• {price:,} Gil × {qty}{hq}".replace(",", ".")
            )

        embed.add_field(
            name="Aktuelle Angebote",
            value="\n".join(listing_lines) if listing_lines else "Keine",
            inline=False,
        )

        if recent:
            sale_prices = [
                x.get("pricePerUnit", 0)
                for x in recent[:10]
                if x.get("pricePerUnit")
            ]
            if sale_prices:
                avg = round(sum(sale_prices) / len(sale_prices))
                embed.add_field(
                    name="Ø letzte Verkäufe",
                    value=f"ca. **{avg:,} Gil**".replace(",", "."),
                    inline=False,
                )

        embed.add_field(
            name="Hinweis",
            value=(
                "Universalis-Daten werden von Spielern hochgeladen und können "
                "gegenüber dem Marktbrett im Spiel verzögert sein."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Item-ID: {item_id} · Daten: XIVAPI v2 + Universalis")

        await interaction.followup.send(embed=embed)

    except httpx.HTTPStatusError as exc:
        print(f"Markt HTTP-Fehler: {exc}")
        await interaction.followup.send(
            "⚠️ Marktbrettdaten konnten nicht geladen werden. "
            "Prüfe bitte den Namen von Welt/Datenzentrum und Gegenstand."
        )
    except Exception as exc:
        print(f"Markt-Fehler: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            "⚠️ Bei der Marktbrettsuche ist ein Fehler aufgetreten."
        )



# ===========================================================================
# EVENTS & SPIELLEITER


def is_event_admin(user_id: int) -> bool:
    return user_id in EVENT_ADMIN_USER_IDS


async def require_event_admin(interaction: discord.Interaction) -> bool:
    if is_event_admin(interaction.user.id):
        return True

    if interaction.response.is_done():
        await interaction.followup.send(
            "🔒 Diese Event-Funktion ist nur für freigeschaltete Event-Administratoren verfügbar.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "🔒 Diese Event-Funktion ist nur für freigeschaltete Event-Administratoren verfügbar.",
            ephemeral=True,
        )
    return False



# ===========================================================================


@client.tree.command(name="eventadmins", description="Zeigt die freigeschalteten Event-Administratoren.")
async def eventadmins(interaction: discord.Interaction):
    if not is_event_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Dieser Befehl ist nur für Event-Administratoren verfügbar.",
            ephemeral=True,
        )
        return

    lines = []
    for user_id in sorted(EVENT_ADMIN_USER_IDS):
        member = interaction.guild.get_member(user_id) if interaction.guild else None
        if member:
            lines.append(f"• {member.mention} (`{user_id}`)")
        else:
            lines.append(f"• `{user_id}` – noch nicht auf diesem Server / nicht im Cache")

    await interaction.response.send_message(
        "🎭 **Freigeschaltete Event-Administratoren**\n" + "\n".join(lines),
        ephemeral=True,
    )


@client.tree.command(name="quiz", description="Erstellt ein FFXIV-Quiz für ein Event.")
@app_commands.describe(
    thema="Thema des Quiz, z.B. Heavensward, Jobs oder Raids",
    fragen="Anzahl der Fragen",
    schwierigkeit="Schwierigkeitsgrad",
)
@app_commands.choices(schwierigkeit=[
    app_commands.Choice(name="Leicht", value="Leicht"),
    app_commands.Choice(name="Mittel", value="Mittel"),
    app_commands.Choice(name="Schwer", value="Schwer"),
    app_commands.Choice(name="Extrem", value="Extrem"),
])
async def quiz(
    interaction: discord.Interaction,
    thema: str,
    fragen: app_commands.Range[int, 5, 30] = 10,
    schwierigkeit: app_commands.Choice[str] | None = None,
):
    if not await require_event_admin(interaction):
        return

    level = schwierigkeit.value if schwierigkeit else "Mittel"
    prompt = f"""
Erstelle ein spielbares FINAL FANTASY XIV Quiz für ein Discord-/Gildenevent.

Thema: {thema}
Anzahl Fragen: {fragen}
Schwierigkeit: {level}

Anforderungen:
- Jede Frage hat genau 4 Antwortmöglichkeiten: A, B, C, D.
- Genau eine Antwort ist richtig.
- Nutze nur ausreichend sichere offizielle FFXIV-Fakten.
- Keine erfundenen Fakten als Quizwissen.
- Die Fragen sollen sich nicht wiederholen.
- Am Ende eine separate Spielleiter-Lösung mit Nummer + richtiger Antwort + kurzer Erklärung.
- Ergänze eine einfache Punktewertung und bei Bedarf eine Stichfrage für Gleichstand.
- Discord-tauglich und übersichtlich.
""".strip()
    await send_interaction(interaction, prompt, force_web=False)


@client.tree.command(name="event", description="Plant ein komplettes FFXIV-Gildenevent.")
@app_commands.describe(
    thema="Thema oder Storyidee des Events",
    spieler="Ungefähre Spielerzahl",
    dauer="Gewünschte Dauer",
)
@app_commands.choices(dauer=[
    app_commands.Choice(name="30 Minuten", value="30 Minuten"),
    app_commands.Choice(name="60 Minuten", value="60 Minuten"),
    app_commands.Choice(name="90 Minuten", value="90 Minuten"),
    app_commands.Choice(name="2+ Stunden", value="2+ Stunden"),
])
async def event(
    interaction: discord.Interaction,
    thema: str,
    spieler: app_commands.Range[int, 2, 24] = 8,
    dauer: app_commands.Choice[str] | None = None,
):
    if not await require_event_admin(interaction):
        return

    duration = dauer.value if dauer else "60 Minuten"
    prompt = f"""
Plane ein vollständiges FFXIV-inspiriertes Discord-/Gildenevent.

Thema: {thema}
Spielerzahl: ungefähr {spieler}
Dauer: ungefähr {duration}

Liefere:
1. Eventtitel
2. kurze Vorlese-Einleitung
3. Ziel der Gruppe
4. Ablauf in 3 bis 5 Abschnitten
5. mindestens ein Rätsel oder Quizmoment
6. mindestens eine Gruppenentscheidung
7. ein Finale oder einen Boss
8. Siegtext
9. optionalen Niederlagen-/Fehlschlagtext
10. Belohnungsidee
11. Hinweise für den Spielleiter

Kennzeichne frei erfundene Inhalte deutlich als **Event-Lore**.
Offizielle FFXIV-Fakten dürfen nicht verfälscht werden.
""".strip()
    await send_interaction(interaction, prompt)


@client.tree.command(name="boss", description="Erstellt einen FFXIV-inspirierten Event-Bosskampf.")
@app_commands.describe(
    thema="Boss, Ort oder Thema",
    phasen="Anzahl der Phasen",
    schwierigkeit="Schwierigkeitsgrad",
)
@app_commands.choices(schwierigkeit=[
    app_commands.Choice(name="Normal", value="Normal"),
    app_commands.Choice(name="Schwer", value="Schwer"),
    app_commands.Choice(name="Brutal", value="Brutal"),
])
async def boss(
    interaction: discord.Interaction,
    thema: str,
    phasen: app_commands.Range[int, 1, 5] = 3,
    schwierigkeit: app_commands.Choice[str] | None = None,
):
    if not await require_event_admin(interaction):
        return

    level = schwierigkeit.value if schwierigkeit else "Schwer"
    prompt = f"""
Erstelle einen spielbaren FFXIV-inspirierten Bosskampf für ein Discord-Event.

Thema/Boss: {thema}
Phasen: {phasen}
Schwierigkeit: {level}

Für jede Phase:
- Phasenname
- 2 bis 3 Attacken
- Was die Spieler sehen
- Mechanik oder Frage
- Was die Gruppe tun muss
- Konsequenz bei Fehler
- Spielleiter-Lösung

Am Ende:
- Finale oder Enrage
- Siegtext
- kurze Belohnungsidee

Wenn offizielle FFXIV-Lore vorkommt, bleibe faktisch korrekt.
Frei erfundene Teile klar als **Event-Lore** kennzeichnen.
""".strip()
    await send_interaction(interaction, prompt)


@client.tree.command(name="raetsel", description="Erstellt ein FFXIV-Rätsel für ein Event.")
@app_commands.describe(
    thema="Thema des Rätsels",
    schwierigkeit="Schwierigkeitsgrad",
)
@app_commands.choices(schwierigkeit=[
    app_commands.Choice(name="Leicht", value="Leicht"),
    app_commands.Choice(name="Mittel", value="Mittel"),
    app_commands.Choice(name="Schwer", value="Schwer"),
    app_commands.Choice(name="Extrem", value="Extrem"),
])
async def raetsel(
    interaction: discord.Interaction,
    thema: str,
    schwierigkeit: app_commands.Choice[str] | None = None,
):
    if not await require_event_admin(interaction):
        return

    level = schwierigkeit.value if schwierigkeit else "Schwer"
    prompt = f"""
Erstelle EIN spielbares FFXIV-Rätsel für ein Event.

Thema: {thema}
Schwierigkeit: {level}

Format:
**Rätsel**
Text für die Spieler.

**Hinweise**
Genau 3 Hinweise, zunehmend deutlicher.

**Spielleiter-Lösung**
Eine eindeutige Lösung mit kurzer Erklärung.

Falls echte FFXIV-Lore verwendet wird, muss sie korrekt sein.
Frei erfundene Inhalte als **Event-Lore** kennzeichnen.
""".strip()
    await send_interaction(interaction, prompt)



# ===========================================================================
# PDF-GUIDES
# ===========================================================================

def build_pdf_file(title: str, body: str, author: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß_-]+", "_", title).strip("_")[:60] or "FFXIV_Guide"
    path = Path(tempfile.gettempdir()) / f"KI-Catnip_{safe_name}.pdf"

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18*mm,
        rightMargin=18*mm,
        topMargin=17*mm,
        bottomMargin=17*mm,
        title=title,
        author="KI-Catnip",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CatnipTitle",
        parent=styles["Title"],
        fontSize=21,
        leading=26,
        alignment=TA_CENTER,
        spaceAfter=14,
    )
    h_style = ParagraphStyle(
        "CatnipHeading",
        parent=styles["Heading2"],
        fontSize=13,
        leading=17,
        spaceBefore=9,
        spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "CatnipBody",
        parent=styles["BodyText"],
        fontSize=9.7,
        leading=14,
        spaceAfter=5,
    )

    story = [
        Paragraph(title, title_style),
        Paragraph(f"Erstellt von KI-Catnip für {author}", body_style),
        Spacer(1, 6),
    ]

    def clean(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
        )

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue

        if line.startswith("### "):
            story.append(Paragraph(clean(line[4:]), h_style))
        elif line.startswith("## "):
            story.append(Paragraph(clean(line[3:]), h_style))
        elif line.startswith("# "):
            story.append(Paragraph(clean(line[2:]), h_style))
        elif line.startswith(("- ", "• ")):
            story.append(Paragraph("• " + clean(line[2:]), body_style))
        else:
            # Basic Markdown bold -> ReportLab bold
            escaped = clean(line)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
            story.append(Paragraph(escaped, body_style))

    doc.build(story)
    return path


@client.tree.command(name="pdf", description="Erstellt einen FFXIV-Guide als PDF-Datei.")
@app_commands.describe(
    thema="Thema des Guides",
    titel="Optionaler Titel der PDF",
    aktuell="Aktuelle/patchabhängige Informationen mit Google-Suche prüfen?"
)
async def pdf(
    interaction: discord.Interaction,
    thema: str,
    titel: str = "",
    aktuell: bool = False,
):
    await interaction.response.defer(thinking=True)

    prompt = f"""
Erstelle einen gut strukturierten FINAL FANTASY XIV Guide als PDF-Grundlage.

Thema: {thema}

Anforderungen:
- verständliches Deutsch
- klare Überschriften
- praktische Schritte
- wichtige Voraussetzungen
- häufige Fehler oder Hinweise, wenn relevant
- keine unnötigen Wiederholungen
- bei Storythemen Spoiler klar kennzeichnen
- erfinde keine FFXIV-Fakten
- ungefähr 700 bis 1400 Wörter, je nach Thema
""".strip()

    try:
        answer = await ask_ai(
            interaction.channel_id,
            interaction.user.display_name,
            prompt,
            remember=False,
            force_web=aktuell,
        )

        pdf_title = titel.strip() or f"FFXIV Guide – {thema}"
        file_path = build_pdf_file(
            pdf_title,
            answer,
            interaction.user.display_name,
        )

        try:
            await interaction.followup.send(
                f"🐱📄 **Dein Guide ist fertig:** {pdf_title}",
                file=discord.File(str(file_path), filename=file_path.name),
            )
        finally:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass

    except Exception as exc:
        print(f"PDF-Fehler: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            "⚠️ KI-Catnip konnte die PDF gerade nicht erstellen. "
            "Prüfe die Railway-Logs."
        )



# ===========================================================================
# RESET / INFO
# ===========================================================================


@client.tree.command(name="budget", description="Zeigt das geschätzte API-Monatsbudget.")
async def budget(interaction: discord.Interaction):
    if interaction.user.id != BUDGET_ADMIN_USER_ID:
        await interaction.response.send_message(
            "🔒 Dieser Befehl ist nur für den Bot-Administrator verfügbar.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        budget_status_text(),
        ephemeral=True,
    )




@client.tree.command(
    name="fortschritt",
    description="Legt deinen persönlichen FFXIV-Storyfortschritt für den Spoilerschutz fest."
)
@app_commands.describe(
    stand="Bis zu welcher Erweiterung darf KI-Catnip Storydetails nennen?"
)
@app_commands.choices(stand=[
    app_commands.Choice(name="Komplett spoilerfrei", value="spoilerfrei"),
    app_commands.Choice(name="A Realm Reborn", value="arr"),
    app_commands.Choice(name="Heavensward", value="heavensward"),
    app_commands.Choice(name="Stormblood", value="stormblood"),
    app_commands.Choice(name="Shadowbringers", value="shadowbringers"),
    app_commands.Choice(name="Endwalker", value="endwalker"),
    app_commands.Choice(name="Dawntrail", value="dawntrail"),
])
async def fortschritt(
    interaction: discord.Interaction,
    stand: app_commands.Choice[str],
):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "🔒 Deinen Story-Fortschritt kannst du nur in einem Textchannel festlegen.",
            ephemeral=True,
        )
        return

    owner_id = private_channel_owner_id(interaction.channel)
    if owner_id is None:
        await interaction.response.send_message(
            "🔒 Bitte verwende `/fortschritt` in deinem persönlichen KI-Catnip-Channel.",
            ephemeral=True,
        )
        return

    if interaction.user.id != owner_id and not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Du kannst nur deinen eigenen Story-Fortschritt ändern.",
            ephemeral=True,
        )
        return

    try:
        await set_spoiler_progress(interaction.channel, stand.value)
        spoiler_enabled_channels.discard(interaction.channel_id)

        await interaction.response.send_message(
            f"📖 **Story-Fortschritt gespeichert: {progress_label(stand.value)}**\\n"
            "🔒 Die vollständige Spoilerfreigabe wurde ausgeschaltet. "
            "KI-Catnip schützt jetzt automatisch alles, was nach diesem Stand liegt.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ Ich kann den Fortschritt nicht dauerhaft speichern. "
            "Mir fehlt die Berechtigung **Kanäle verwalten / Manage Channels**.",
            ephemeral=True,
        )


@client.tree.command(
    name="spoiler",
    description="Schaltet Story-Spoiler für diesen privaten Channel an oder aus."
)
@app_commands.describe(modus="Spoiler erlauben, sperren oder Status anzeigen")
@app_commands.choices(modus=[
    app_commands.Choice(name="Aus – Spoilerschutz aktiv", value="aus"),
    app_commands.Choice(name="An – Spoiler erlauben", value="an"),
    app_commands.Choice(name="Status anzeigen", value="status"),
])
async def spoiler(
    interaction: discord.Interaction,
    modus: app_commands.Choice[str],
):
    channel_id = interaction.channel_id

    if modus.value == "an":
        spoiler_enabled_channels.add(channel_id)
        await interaction.response.send_message(
            "🔓 **Vollständige Spoilerfreigabe aktiviert.**\n"
            "KI-Catnip darf jetzt auch Inhalte nach deinem gespeicherten Story-Fortschritt nennen. "
            "Mit `/spoiler aus` kehrst du wieder zu deiner persönlichen Spoilergrenze zurück.",
            ephemeral=True,
        )
        return

    if modus.value == "aus":
        spoiler_enabled_channels.discard(channel_id)
        await interaction.response.send_message(
            f"🔒 **Persönlicher Spoilerschutz aktiviert.**\n"
            f"KI-Catnip begrenzt Storydetails wieder auf **{progress_label(get_spoiler_progress(channel_id))}**.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        spoiler_status_text(channel_id),
        ephemeral=True,
    )


@client.tree.command(name="reset", description="Löscht den Gesprächsverlauf dieses Channels.")
async def reset(interaction: discord.Interaction):
    history.pop(interaction.channel_id, None)
    spoiler_enabled_channels.discard(interaction.channel_id)
    await interaction.response.send_message(
        f"🧹 **Gesprächsverlauf gelöscht.**\n"
        f"🔒 Vollständige Spoilerfreigabe ist aus. Dein gespeicherter Fortschritt "
        f"bleibt **{progress_label(get_spoiler_progress(interaction.channel_id))}**.",
        ephemeral=True,
    )



# ===========================================================================
# ADMIN-DASHBOARD
# ===========================================================================


def is_bot_admin(user_id: int) -> bool:
    """Bot-Admin oder freigeschalteter Event-Admin."""
    return user_id == BUDGET_ADMIN_USER_ID or user_id in EVENT_ADMIN_USER_IDS


def admin_status_embed(channel_id: int) -> discord.Embed:
    data = load_budget()
    embed = discord.Embed(
        title=f"🛠️ {BOT_NAME} — Admin-Menü",
        description=(
            "Zentrale Übersicht für KI-Catnip. Die Schalter gelten nur bis zum "
            "nächsten Bot-Neustart; dauerhafte Standardwerte bleiben die Railway-Variablen."
        ),
    )
    embed.add_field(name="🤖 Bot", value="🟢 Online", inline=True)
    embed.add_field(name="🧠 Modell", value=f"`{GEMINI_MODEL}`", inline=True)
    embed.add_field(
        name="🌐 Websuche",
        value="🟢 Aktiv" if WEB_SEARCH else "🔴 Deaktiviert",
        inline=True,
    )
    progress = get_spoiler_progress(channel_id)
    spoiler_value = (
        f"🔓 Vollfreigabe · gespeichert: {progress_label(progress)}"
        if channel_id in spoiler_enabled_channels
        else f"🔒 Bis {progress_label(progress)}"
    )
    embed.add_field(
        name="🔒 Spoilerschutz (dieser Channel)",
        value=spoiler_value,
        inline=True,
    )
    embed.add_field(
        name="💬 Private Channels",
        value="🟢 Aktiv" if PRIVATE_CHANNELS_ENABLED else "🔴 Deaktiviert",
        inline=True,
    )
    embed.add_field(
        name="🎭 Event-System",
        value=f"🟢 Aktiv · {len(EVENT_ADMIN_USER_IDS)} Admin(s)",
        inline=True,
    )
    if GEMINI_FREE_TIER:
        budget_value = (
            f"🆓 Free Tier · {data.get('requests', 0)} Anfrage(n) · "
            f"{data.get('web_search_calls', 0)} Suche(n)"
        )
    else:
        spent = float(data.get("estimated_eur", 0.0))
        budget_value = f"{spent:.2f} € / {MONTHLY_BUDGET_EUR:.2f} €"
    embed.add_field(name="💶 API-Nutzung", value=budget_value, inline=False)
    embed.set_footer(text="Nur für freigeschaltete KI-Catnip-Administratoren sichtbar.")
    return embed


class AdminView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=300)
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_bot_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "🔒 Dieses Menü ist nur für freigeschaltete KI-Catnip-Administratoren verfügbar.",
            ephemeral=True,
        )
        return False

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=admin_status_embed(self.channel_id), view=self
        )

    @discord.ui.button(label="Aktualisieren", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.refresh(interaction)

    @discord.ui.button(label="Vollspoiler umschalten", emoji="🔒", style=discord.ButtonStyle.primary)
    async def spoiler_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.channel_id in spoiler_enabled_channels:
            spoiler_enabled_channels.discard(self.channel_id)
        else:
            spoiler_enabled_channels.add(self.channel_id)
        await self.refresh(interaction)

    @discord.ui.button(label="Websuche umschalten", emoji="🌐", style=discord.ButtonStyle.primary)
    async def web_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global WEB_SEARCH
        WEB_SEARCH = not WEB_SEARCH
        await self.refresh(interaction)

    @discord.ui.button(label="Private Channels umschalten", emoji="💬", style=discord.ButtonStyle.primary)
    async def private_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global PRIVATE_CHANNELS_ENABLED
        PRIVATE_CHANNELS_ENABLED = not PRIVATE_CHANNELS_ENABLED
        await self.refresh(interaction)

    @discord.ui.button(label="Budgetdetails", emoji="💶", style=discord.ButtonStyle.secondary)
    async def budget_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(budget_status_text(), ephemeral=True)


@client.tree.command(name="admin", description="Öffnet das private KI-Catnip Admin-Menü.")
async def admin(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Dieser Befehl ist nur für freigeschaltete KI-Catnip-Administratoren verfügbar.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=admin_status_embed(interaction.channel_id),
        view=AdminView(interaction.channel_id),
        ephemeral=True,
    )


@client.tree.command(name="botinfo", description="Zeigt die Funktionen der Eorzea-Enzyklopädie.")
async def botinfo(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"🌙 {BOT_NAME} — Eorzea-Enzyklopädie",
        description=(
            "FFXIV-Wissensbot für Lore, Gameplay, Charaktere, Mounts, Minions, "
            "Marktbrett und allgemeine Fragen."
        ),
    )
    embed.add_field(
        name="💬 Einfach fragen",
        value=f"`@{BOT_NAME} deine Frage` oder `/ffxiv`",
        inline=False,
    )
    embed.add_field(
        name="🔒 Persönlicher Spoilerschutz",
        value="`/fortschritt` · `/spoiler an|aus|status`",
        inline=False,
    )
    embed.add_field(
        name="📚 Wissen",
        value="`/lore` · `/job` · `/kampf` · `/quest` · `/begriff` · `/einsteiger`",
        inline=False,
    )
    embed.add_field(
        name="🔎 Nachschlagen",
        value="`/charakter` · `/mount` · `/minion` · `/markt`",
        inline=False,
    )
    embed.add_field(
        name="🎭 Events",
        value="`/quiz` · `/event` · `/boss` · `/raetsel`",
        inline=False,
    )
    embed.add_field(
        name="🔒 Privater Chat",
        value="Für neue Mitglieder wird automatisch ein privater FFXIV-Channel erstellt. "
              "Falls nötig: `/privatchat`",
        inline=False,
    )
    embed.add_field(
        name="💶 Nutzungslimit",
        value="Aktiv · Budgetdetails sind nur für den Bot-Administrator sichtbar.",
        inline=False,
    )
    embed.add_field(
        name="🚫 Bewusst nicht enthalten",
        value="Automatische News und Serverstatus",
        inline=False,
    )
    embed.add_field(
        name="🌐 Websuche",
        value="Aktiv" if WEB_SEARCH else "Deaktiviert",
        inline=True,
    )
    embed.add_field(
        name="🔒 Spoilerschutz",
        value="Standardmäßig aktiv · `/spoiler an|aus|status`",
        inline=False,
    )
    embed.set_footer(text=f"Modell: {GEMINI_MODEL}")
    await interaction.response.send_message(embed=embed)









# ============================================================
# STUFE 8.1 — EVENT-ANMELDUNG & TEILNEHMERVERWALTUNG
# ============================================================

active_event_signups = {}

EVENT_ROLE_ICONS = {
    "Tank": "🛡️",
    "Heiler": "💚",
    "DPS": "⚔️",
    "Dabei": "✅",
    "Ersatzbank": "🪑",
}


def _event_key(guild_id: int, channel_id: int):
    return (guild_id, channel_id)


def _event_signup_counts(signups: dict):
    counts = {role: 0 for role in EVENT_ROLE_ICONS}
    for data in signups.values():
        role = data.get("role")
        if role in counts:
            counts[role] += 1
    return counts


def _event_signup_lines(signups: dict, role: str):
    rows = []
    for user_id, data in signups.items():
        if data.get("role") == role:
            rows.append(f"• <@{user_id}>")
    return "\n".join(rows) if rows else "—"


def event_signup_embed(state: dict) -> discord.Embed:
    signups = state["signups"]
    counts = _event_signup_counts(signups)

    embed = discord.Embed(
        title=f"📅 {state['title']}",
        description=state.get("description") or "KI-Catnip verwaltet eure Anmeldung. 🐾",
    )

    embed.add_field(
        name="🕒 Termin",
        value=state.get("when") or "Nicht angegeben",
        inline=False,
    )

    if state.get("max_players", 0) > 0:
        active_count = sum(
            1 for entry in signups.values()
            if entry.get("role") != "Ersatzbank"
        )
        embed.add_field(
            name="👥 Plätze",
            value=f"**{active_count}/{state['max_players']}**",
            inline=True,
        )
    else:
        embed.add_field(
            name="👥 Plätze",
            value="Keine feste Begrenzung",
            inline=True,
        )

    embed.add_field(
        name="🛡️ Tank",
        value=f"**{counts['Tank']}**\n{_event_signup_lines(signups, 'Tank')}",
        inline=True,
    )
    embed.add_field(
        name="💚 Heiler",
        value=f"**{counts['Heiler']}**\n{_event_signup_lines(signups, 'Heiler')}",
        inline=True,
    )
    embed.add_field(
        name="⚔️ DPS",
        value=f"**{counts['DPS']}**\n{_event_signup_lines(signups, 'DPS')}",
        inline=True,
    )
    embed.add_field(
        name="✅ Dabei",
        value=f"**{counts['Dabei']}**\n{_event_signup_lines(signups, 'Dabei')}",
        inline=True,
    )
    embed.add_field(
        name="🪑 Ersatzbank",
        value=f"**{counts['Ersatzbank']}**\n{_event_signup_lines(signups, 'Ersatzbank')}",
        inline=True,
    )

    embed.add_field(
        name="📌 Anmeldung",
        value=(
            "Wähle unten deine Rolle oder **Dabei**.\n"
            "Mit **Abmelden** entfernst du dich wieder."
        ),
        inline=False,
    )

    embed.set_footer(
        text=f"Erstellt von {state['creator_name']} • KI-Catnip Event-Anmeldung"
    )
    return embed


def _active_signup_count(state: dict) -> int:
    return sum(
        1 for entry in state["signups"].values()
        if entry.get("role") != "Ersatzbank"
    )


class EventSignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _set_role(
        self,
        interaction: discord.Interaction,
        role: str,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Event-Anmeldungen funktionieren nur auf einem Server.",
                ephemeral=True,
            )
            return

        key = _event_key(interaction.guild.id, interaction.channel_id)
        state = active_event_signups.get(key)

        if not state:
            await interaction.response.send_message(
                "📅 Diese Event-Anmeldung ist nicht mehr aktiv.",
                ephemeral=True,
            )
            return

        current = state["signups"].get(interaction.user.id)
        max_players = int(state.get("max_players", 0))

        # Ersatzbank zählt nicht als aktiver Platz.
        wants_active_slot = role != "Ersatzbank"
        already_active = current and current.get("role") != "Ersatzbank"

        if (
            wants_active_slot
            and not already_active
            and max_players > 0
            and _active_signup_count(state) >= max_players
        ):
            await interaction.response.send_message(
                "⚠️ Die aktiven Plätze sind bereits voll. "
                "Du kannst dich noch auf die **Ersatzbank** setzen.",
                ephemeral=True,
            )
            return

        state["signups"][interaction.user.id] = {
            "name": interaction.user.display_name,
            "role": role,
        }

        await interaction.response.edit_message(
            embed=event_signup_embed(state),
            view=self,
        )

    @discord.ui.button(label="Tank", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_role(interaction, "Tank")

    @discord.ui.button(label="Heiler", emoji="💚", style=discord.ButtonStyle.success, row=0)
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_role(interaction, "Heiler")

    @discord.ui.button(label="DPS", emoji="⚔️", style=discord.ButtonStyle.danger, row=0)
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_role(interaction, "DPS")

    @discord.ui.button(label="Dabei", emoji="✅", style=discord.ButtonStyle.secondary, row=1)
    async def dabei(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_role(interaction, "Dabei")

    @discord.ui.button(label="Ersatzbank", emoji="🪑", style=discord.ButtonStyle.secondary, row=1)
    async def reserve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_role(interaction, "Ersatzbank")

    @discord.ui.button(label="Abmelden", emoji="❌", style=discord.ButtonStyle.secondary, row=1)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Event-Anmeldungen funktionieren nur auf einem Server.",
                ephemeral=True,
            )
            return

        key = _event_key(interaction.guild.id, interaction.channel_id)
        state = active_event_signups.get(key)

        if not state:
            await interaction.response.send_message(
                "📅 Diese Event-Anmeldung ist nicht mehr aktiv.",
                ephemeral=True,
            )
            return

        state["signups"].pop(interaction.user.id, None)

        await interaction.response.edit_message(
            embed=event_signup_embed(state),
            view=self,
        )


@client.tree.command(
    name="eventerstellen",
    description="Erstellt eine KI-Catnip-Event-Anmeldung mit Rollen-Buttons."
)
@app_commands.describe(
    titel="Name des Events",
    termin="Datum/Uhrzeit oder freie Terminangabe",
    beschreibung="Kurze Eventbeschreibung",
    max_spieler="Optional: maximale aktive Teilnehmerzahl"
)
async def eventerstellen(
    interaction: discord.Interaction,
    titel: str,
    termin: str,
    beschreibung: str = "",
    max_spieler: app_commands.Range[int, 0, 50] = 8,
):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen Event-Anmeldungen erstellen.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "Event-Anmeldungen funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    key = _event_key(interaction.guild.id, interaction.channel_id)

    if key in active_event_signups:
        await interaction.response.send_message(
            "⚠️ In diesem Channel läuft bereits eine Event-Anmeldung. "
            "Nutze zuerst `/eventbeenden`.",
            ephemeral=True,
        )
        return

    state = {
        "title": titel.strip(),
        "when": termin.strip(),
        "description": beschreibung.strip(),
        "max_players": int(max_spieler),
        "creator_id": interaction.user.id,
        "creator_name": interaction.user.display_name,
        "signups": {},
    }

    active_event_signups[key] = state

    await interaction.response.send_message(
        embed=event_signup_embed(state),
        view=EventSignupView(),
    )


@client.tree.command(
    name="eventstatus",
    description="Zeigt den aktuellen Stand der Event-Anmeldung."
)
async def eventstatus(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Event-Anmeldungen funktionieren nur auf einem Server.",
            ephemeral=True,
        )
        return

    state = active_event_signups.get(
        _event_key(interaction.guild.id, interaction.channel_id)
    )

    if not state:
        await interaction.response.send_message(
            "📅 In diesem Channel läuft aktuell keine Event-Anmeldung.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=event_signup_embed(state),
        ephemeral=True,
    )


@client.tree.command(
    name="eventliste",
    description="Admin: zeigt eine kompakte Teilnehmerliste der aktuellen Anmeldung."
)
async def eventliste(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Dieser Befehl ist nur für KI-Catnip-Administratoren verfügbar.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "Nur auf einem Server verfügbar.",
            ephemeral=True,
        )
        return

    state = active_event_signups.get(
        _event_key(interaction.guild.id, interaction.channel_id)
    )

    if not state:
        await interaction.response.send_message(
            "📅 Hier läuft aktuell keine Event-Anmeldung.",
            ephemeral=True,
        )
        return

    lines = []
    for role in ("Tank", "Heiler", "DPS", "Dabei", "Ersatzbank"):
        members = [
            f"<@{uid}>"
            for uid, data in state["signups"].items()
            if data.get("role") == role
        ]
        lines.append(
            f"{EVENT_ROLE_ICONS[role]} **{role}:** "
            + (", ".join(members) if members else "—")
        )

    await interaction.response.send_message(
        f"📋 **Teilnehmerliste — {state['title']}**\n\n"
        + "\n".join(lines),
        ephemeral=True,
    )


@client.tree.command(
    name="eventbeenden",
    description="Beendet die Event-Anmeldung im aktuellen Channel."
)
async def eventbeenden(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen Event-Anmeldungen beenden.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "Nur auf einem Server verfügbar.",
            ephemeral=True,
        )
        return

    key = _event_key(interaction.guild.id, interaction.channel_id)
    state = active_event_signups.pop(key, None)

    if not state:
        await interaction.response.send_message(
            "📅 Hier läuft aktuell keine Event-Anmeldung.",
            ephemeral=True,
        )
        return

    counts = _event_signup_counts(state["signups"])
    total_active = _active_signup_count(state)

    embed = discord.Embed(
        title=f"📅 Anmeldung beendet — {state['title']}",
        description=(
            f"Die Anmeldung wurde geschlossen.\n\n"
            f"👥 **Aktive Teilnehmer:** {total_active}\n"
            f"🪑 **Ersatzbank:** {counts['Ersatzbank']}\n\n"
            "Die Teilnehmerliste bleibt in der letzten Event-Nachricht sichtbar."
        ),
    )

    await interaction.response.send_message(embed=embed)



# ============================================================
# STUFE 7.2 — RP-SPIELLEITER & PERSÖNLICHE QUESTS
# ============================================================

async def _character_context_for_member(guild: discord.Guild, member: discord.Member):
    data = await load_character_profile(guild, member.id)
    if not data:
        return None

    return (
        f"Charaktername: {data.get('charname', 'Unbekannt')}\n"
        f"Volk: {data.get('volk', 'Nicht angegeben')}\n"
        f"Geschlecht/Identität: {data.get('geschlecht', 'Nicht angegeben')}\n"
        f"Hauptjob: {data.get('hauptjob', 'Nicht angegeben')}\n"
        f"Herkunft: {data.get('herkunft', 'Nicht angegeben')}\n"
        f"Persönlichkeit: {data.get('persoenlichkeit', 'Nicht angegeben')}\n"
        f"Hintergrund: {data.get('hintergrund', 'Nicht angegeben')}"
    )


RP_RULES = """
RP-REGELN FÜR KI-CATNIP
- Nutze das gespeicherte Charakterprofil als Grundlage.
- Du steuerst NIEMALS ungefragt den Spielercharakter.
- Lege dem Spielercharakter keine Worte, Gefühle, Entscheidungen oder Handlungen in den Mund.
- Beschreibe nur Welt, NPCs, Umgebung, Gefahren, Hinweise und Konsequenzen auf bestätigte Spieleraktionen.
- Beende Szenen möglichst mit einer offenen Situation oder klaren Entscheidungsmöglichkeit.
- Bei Gruppenszenen alle Charaktere fair berücksichtigen.
- Offizielle FFXIV-Lore und frei erfundene Event-/RP-Lore klar trennen.
- Persönliche Hintergrundgeschichten des Nutzers nicht als offizielle FFXIV-Lore darstellen.
- Den persönlichen Spoilerstand weiterhin strikt beachten.
- Keine sexualisierten oder unangemessen intimen Inhalte erzeugen.
""".strip()


@client.tree.command(
    name="rp",
    description="Startet eine persönliche RP-Szene mit deinem gespeicherten FFXIV-Charakter."
)
@app_commands.describe(
    szene="Wunschszene, Ort oder Ausgangssituation",
    stil="Optional: Stimmung oder Stil"
)
async def rp(
    interaction: discord.Interaction,
    szene: str,
    stil: str = "",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "RP-Szenen funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    context = await _character_context_for_member(interaction.guild, interaction.user)
    if not context:
        await interaction.response.send_message(
            "📜 Du brauchst zuerst ein Charakterprofil. "
            "Nutze `/charaktererstellen`.",
            ephemeral=True,
        )
        return

    prompt = f"""
{RP_RULES}

GESPEICHERTES CHARAKTERPROFIL
{context}

GEWÜNSCHTE SZENE
{szene}

STIL
{stil or 'Atmosphärisch, FFXIV-inspiriert, kompakt'}

Aufgabe:
- Eröffne eine spielbare RP-Szene.
- 2 bis 4 Absätze.
- Nutze mindestens ein konkretes Umgebungsdetail.
- Optional 1 bis 2 NPCs.
- Beende mit einer offenen Situation oder Frage an den Spieler.
- Keine Entscheidung für den Spielercharakter treffen.
""".strip()

    await send_interaction(
        interaction,
        prompt,
        remember=True,
    )


@client.tree.command(
    name="rpquest",
    description="Erstellt eine persönliche kleine RP-Quest für deinen FFXIV-Charakter."
)
@app_commands.describe(
    thema="Thema oder Aufhänger",
    dauer="Gewünschte Länge",
    schwierigkeit="Schwierigkeitsgrad"
)
@app_commands.choices(
    dauer=[
        app_commands.Choice(name="Kurz", value="Kurz"),
        app_commands.Choice(name="Mittel", value="Mittel"),
        app_commands.Choice(name="Lang", value="Lang"),
    ],
    schwierigkeit=[
        app_commands.Choice(name="Leicht", value="Leicht"),
        app_commands.Choice(name="Mittel", value="Mittel"),
        app_commands.Choice(name="Schwer", value="Schwer"),
    ]
)
async def rpquest(
    interaction: discord.Interaction,
    thema: str,
    dauer: app_commands.Choice[str] | None = None,
    schwierigkeit: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "RP-Quests funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    context = await _character_context_for_member(interaction.guild, interaction.user)
    if not context:
        await interaction.response.send_message(
            "📜 Du brauchst zuerst ein Charakterprofil. "
            "Nutze `/charaktererstellen`.",
            ephemeral=True,
        )
        return

    duration = dauer.value if dauer else "Mittel"
    difficulty = schwierigkeit.value if schwierigkeit else "Mittel"

    prompt = f"""
{RP_RULES}

GESPEICHERTES CHARAKTERPROFIL
{context}

QUEST-THEMA
{thema}

LÄNGE
{duration}

SCHWIERIGKEIT
{difficulty}

Erstelle eine persönliche FFXIV-inspirierte RP-Quest mit:
1. Questtitel
2. kurzer Aufhänger
3. Auftraggeber oder Auslöser
4. Ziel
5. 2 bis 4 Stationen
6. mindestens einer Entscheidung
7. optionalem Rätsel oder Kampfaufhänger
8. möglicher Belohnung
9. Spielleiterhinweis

WICHTIG:
- Nicht automatisch ausspielen, sondern als spielbare Queststruktur liefern.
- Spielercharakter nicht fremdsteuern.
- Erfundene Inhalte als **Event-/RP-Lore** kennzeichnen.
""".strip()

    await send_interaction(
        interaction,
        prompt,
        remember=False,
    )


@client.tree.command(
    name="rpgruppe",
    description="Erstellt eine RP-Szene mit mehreren gespeicherten KI-Catnip-Charakteren."
)
@app_commands.describe(
    spieler2="Zweiter Spieler",
    spieler3="Optional: dritter Spieler",
    spieler4="Optional: vierter Spieler",
    szene="Gemeinsame Ausgangssituation"
)
async def rpgruppe(
    interaction: discord.Interaction,
    spieler2: discord.Member,
    szene: str,
    spieler3: discord.Member | None = None,
    spieler4: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "RP-Gruppenszenen funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    members = [interaction.user, spieler2]
    if spieler3:
        members.append(spieler3)
    if spieler4:
        members.append(spieler4)

    seen = set()
    unique_members = []
    for member in members:
        if member.id not in seen:
            seen.add(member.id)
            unique_members.append(member)

    contexts = []
    missing = []

    for member in unique_members:
        ctx = await _character_context_for_member(interaction.guild, member)
        if ctx:
            contexts.append(
                f"--- {member.display_name} ---\n{ctx}"
            )
        else:
            missing.append(member.display_name)

    if missing:
        await interaction.response.send_message(
            "📜 Für folgende Spieler fehlt noch ein Charakterprofil:\n"
            + "\n".join(f"• {name}" for name in missing),
            ephemeral=True,
        )
        return

    prompt = f"""
{RP_RULES}

GRUPPENCHARAKTERE
{chr(10).join(contexts)}

GEMEINSAME SZENE
{szene}

Aufgabe:
- Eröffne eine gemeinsame RP-Szene für diese Charaktere.
- Beschreibe Welt, NPCs und Ausgangslage.
- Beziehe die Profile sichtbar, aber nicht aufdringlich ein.
- Kein Charakter wird von dir gesteuert.
- Jeder Spieler soll eine sinnvolle Möglichkeit zum Reagieren haben.
- 3 bis 5 Absätze.
- Beende mit einer offenen Gruppensituation.
""".strip()

    await send_interaction(
        interaction,
        prompt,
        remember=True,
    )


@client.tree.command(
    name="rphilfe",
    description="Zeigt die KI-Catnip-RP-Funktionen."
)
async def rphilfe(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📜 KI-Catnip — RP-Spielleiter",
        description=(
            "KI-Catnip kann eure gespeicherten FFXIV-Charakterprofile "
            "für persönliche und gemeinsame RP-Szenen verwenden."
        ),
    )
    embed.add_field(
        name="🎭 /rp",
        value="Startet eine persönliche, offene RP-Szene.",
        inline=False,
    )
    embed.add_field(
        name="📖 /rpquest",
        value="Erstellt eine kleine persönliche Queststruktur.",
        inline=False,
    )
    embed.add_field(
        name="👥 /rpgruppe",
        value="Eröffnet eine Szene mit 2 bis 4 gespeicherten Charakteren.",
        inline=False,
    )
    embed.add_field(
        name="🛡️ Spielerautonomie",
        value=(
            "Catnip beschreibt Welt und NPCs, entscheidet aber nicht ungefragt, "
            "was dein Charakter sagt, fühlt oder tut."
        ),
        inline=False,
    )
    embed.set_footer(text="Stufe 7.2 • RP-Spielleiter")
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ============================================================
# STUFE 7.1 — DAUERHAFTE FFXIV-CHARAKTERPROFILE
# ============================================================

CHARACTER_DATA_PREFIX = "KI_CATNIP_CHARACTER_V1:"


def _character_message_prefix(user_id: int) -> str:
    return f"{CHARACTER_DATA_PREFIX}{int(user_id)}:"


async def _find_character_message(channel: discord.TextChannel, user_id: int):
    prefix = _character_message_prefix(user_id)
    async for message in channel.history(limit=200):
        if message.author == channel.guild.me and message.content.startswith(prefix):
            return message
    return None


async def load_character_profile(guild: discord.Guild, user_id: int):
    """
    Lädt ein einzelnes Charakterprofil aus dem versteckten KI-Catnip-Datenchannel.
    """
    try:
        channel = await _get_or_create_profile_data_channel(guild)
        message = await _find_character_message(channel, user_id)
        if not message:
            return None

        raw = message.content[len(_character_message_prefix(user_id)):].strip()
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print(f"Charakterprofil-Ladefehler: {type(exc).__name__}: {exc}")
        return None


async def save_character_profile(guild: discord.Guild, user_id: int, data: dict):
    """
    Speichert ein Charakterprofil als eigene Discord-Nachricht.
    So stößt das System deutlich später an Discord-Limits als bei einer gemeinsamen JSON-Datei.
    """
    channel = await _get_or_create_profile_data_channel(guild)
    message = await _find_character_message(channel, user_id)

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    content = _character_message_prefix(user_id) + payload

    if len(content) > 1950:
        raise RuntimeError(
            "Das Charakterprofil ist zu lang für die Discord-Speicherung. "
            "Kürze bitte Persönlichkeit oder Hintergrundgeschichte."
        )

    if message:
        await message.edit(content=content)
    else:
        await channel.send(content)


async def delete_character_profile(guild: discord.Guild, user_id: int):
    channel = await _get_or_create_profile_data_channel(guild)
    message = await _find_character_message(channel, user_id)
    if not message:
        return False

    await message.delete()
    return True


def character_profile_embed(data: dict, owner_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📜 {data.get('charname', 'Unbekannter Charakter')}",
        description=f"FFXIV-Charakterprofil von **{owner_name}**",
    )
    embed.add_field(
        name="🧬 Volk",
        value=data.get("volk") or "Nicht angegeben",
        inline=True,
    )
    embed.add_field(
        name="⚧ Geschlecht",
        value=data.get("geschlecht") or "Nicht angegeben",
        inline=True,
    )
    embed.add_field(
        name="⚔️ Hauptjob",
        value=data.get("hauptjob") or "Nicht angegeben",
        inline=True,
    )
    embed.add_field(
        name="🏘️ Herkunft",
        value=data.get("herkunft") or "Nicht angegeben",
        inline=False,
    )
    embed.add_field(
        name="💭 Persönlichkeit",
        value=data.get("persoenlichkeit") or "Nicht angegeben",
        inline=False,
    )
    embed.add_field(
        name="📖 Hintergrundgeschichte",
        value=data.get("hintergrund") or "Nicht angegeben",
        inline=False,
    )
    embed.add_field(
        name="🎭 RP verwenden",
        value="`/rp` · `/rpquest` · `/rpgruppe`",
        inline=False,
    )
    embed.set_footer(
        text="KI-Catnip • Charakterprofil Stufe 7.2"
    )
    return embed


@client.tree.command(
    name="charaktererstellen",
    description="Erstellt dein dauerhaftes persönliches FFXIV-Charakterprofil."
)
@app_commands.describe(
    name="Name deines FFXIV-Charakters",
    volk="Volk, z. B. Hyuran, Miqo'te, Elezen, Au Ra, Viera",
    geschlecht="Optional: Geschlecht/Identität deines Charakters",
    hauptjob="Hauptjob deines Charakters",
    herkunft="Herkunft oder Heimatort",
    persoenlichkeit="Kurze Beschreibung der Persönlichkeit",
    hintergrund="Kurze Hintergrundgeschichte",
)
async def charaktererstellen(
    interaction: discord.Interaction,
    name: str,
    volk: str,
    hauptjob: str,
    geschlecht: str = "",
    herkunft: str = "",
    persoenlichkeit: str = "",
    hintergrund: str = "",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Charakterprofile funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    existing = await load_character_profile(interaction.guild, interaction.user.id)
    if existing:
        await interaction.response.send_message(
            "📜 Du hast bereits ein Charakterprofil. "
            "Nutze `/charakterbearbeiten`, um es zu ändern.",
            ephemeral=True,
        )
        return

    data = {
        "user_id": interaction.user.id,
        "discord_name": interaction.user.display_name,
        "charname": name.strip(),
        "volk": volk.strip(),
        "geschlecht": geschlecht.strip(),
        "hauptjob": hauptjob.strip(),
        "herkunft": herkunft.strip(),
        "persoenlichkeit": persoenlichkeit.strip(),
        "hintergrund": hintergrund.strip(),
    }

    try:
        await save_character_profile(interaction.guild, interaction.user.id, data)
    except RuntimeError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return

    await interaction.response.send_message(
        "✅ **Dein FFXIV-Charakterprofil wurde gespeichert.**\n"
        "Mit `/charakterprofil` kannst du es jederzeit anzeigen.",
        ephemeral=True,
    )


@client.tree.command(
    name="charakterprofil",
    description="Zeigt dein oder das FFXIV-Charakterprofil eines anderen Spielers."
)
@app_commands.describe(
    spieler="Optional: Profil eines anderen Discord-Mitglieds anzeigen"
)
async def charakterprofil(
    interaction: discord.Interaction,
    spieler: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Charakterprofile funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    target = spieler or interaction.user
    data = await load_character_profile(interaction.guild, target.id)

    if not data:
        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "📜 Du hast noch kein Charakterprofil. "
                "Erstelle eins mit `/charaktererstellen`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"📜 **{target.display_name}** hat noch kein KI-Catnip-Charakterprofil.",
                ephemeral=True,
            )
        return

    await interaction.response.send_message(
        embed=character_profile_embed(data, target.display_name)
    )


@client.tree.command(
    name="charakterbearbeiten",
    description="Bearbeitet einzelne Angaben deines gespeicherten FFXIV-Charakterprofils."
)
@app_commands.describe(
    name="Optional: neuer Charaktername",
    volk="Optional: neues Volk",
    geschlecht="Optional: neues Geschlecht/Identität",
    hauptjob="Optional: neuer Hauptjob",
    herkunft="Optional: neue Herkunft",
    persoenlichkeit="Optional: neue Persönlichkeitsbeschreibung",
    hintergrund="Optional: neue Hintergrundgeschichte",
)
async def charakterbearbeiten(
    interaction: discord.Interaction,
    name: str = "",
    volk: str = "",
    geschlecht: str = "",
    hauptjob: str = "",
    herkunft: str = "",
    persoenlichkeit: str = "",
    hintergrund: str = "",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Charakterprofile funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    data = await load_character_profile(interaction.guild, interaction.user.id)
    if not data:
        await interaction.response.send_message(
            "📜 Du hast noch kein Charakterprofil. "
            "Erstelle zuerst eines mit `/charaktererstellen`.",
            ephemeral=True,
        )
        return

    updates = {
        "charname": name.strip(),
        "volk": volk.strip(),
        "geschlecht": geschlecht.strip(),
        "hauptjob": hauptjob.strip(),
        "herkunft": herkunft.strip(),
        "persoenlichkeit": persoenlichkeit.strip(),
        "hintergrund": hintergrund.strip(),
    }

    changed = []
    for key, value in updates.items():
        if value:
            data[key] = value
            changed.append(key)

    if not changed:
        await interaction.response.send_message(
            "🐾 Du hast keine neuen Angaben eingetragen.",
            ephemeral=True,
        )
        return

    data["discord_name"] = interaction.user.display_name

    try:
        await save_character_profile(interaction.guild, interaction.user.id, data)
    except RuntimeError as exc:
        await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
        return

    await interaction.response.send_message(
        "✅ **Charakterprofil aktualisiert.**\n"
        "Geändert: " + ", ".join(changed),
        ephemeral=True,
    )


@client.tree.command(
    name="charakterloeschen",
    description="Löscht dein dauerhaft gespeichertes KI-Catnip-Charakterprofil."
)
@app_commands.describe(
    bestaetigen="Muss auf Ja gesetzt werden, damit das Profil wirklich gelöscht wird."
)
async def charakterloeschen(
    interaction: discord.Interaction,
    bestaetigen: bool = False,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Charakterprofile funktionieren nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    if not bestaetigen:
        await interaction.response.send_message(
            "⚠️ Dein Charakterprofil wurde **nicht** gelöscht.\n"
            "Führe `/charakterloeschen bestaetigen:Ja` aus, wenn du es wirklich entfernen möchtest.",
            ephemeral=True,
        )
        return

    deleted = await delete_character_profile(
        interaction.guild,
        interaction.user.id,
    )

    if not deleted:
        await interaction.response.send_message(
            "📜 Du hast aktuell kein gespeichertes Charakterprofil.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "🗑️ **Dein KI-Catnip-Charakterprofil wurde gelöscht.**\n"
        "Deine Punkte, Titel und Event-Statistiken bleiben davon unberührt.",
        ephemeral=True,
    )



# ============================================================
# STUFE 6.1 — DAUERHAFTE SPIELERPROFILE, PUNKTE, TITEL & RANGLISTE
# ============================================================

PROFILE_DATA_CHANNEL_NAME = os.getenv("PROFILE_DATA_CHANNEL_NAME", "ki-catnip-data")
PROFILE_DATA_MESSAGE_PREFIX = "KI_CATNIP_PROFILE_DATA_V1:"
player_profiles = {}

TITLE_THRESHOLDS = [
    (0, "Frischling von Eorzea"),
    (100, "Abenteurer"),
    (250, "Mechaniken-Versteher"),
    (500, "Eorzea-Gelehrter"),
    (1000, "Prüfungsbezwinger"),
    (1750, "Ätherkenner"),
    (2500, "Stand-in-AOE-Champion"),
    (4000, "Held der Schattenflauscher"),
    (6000, "Legende von Eorzea"),
]


# Automatische Belohnungen aus Stufe 6.2
REWARD_RIDDLE_SOLVED = int(os.getenv("REWARD_RIDDLE_SOLVED", "25"))
REWARD_RIDDLE_EVENT_COMPLETE = int(os.getenv("REWARD_RIDDLE_EVENT_COMPLETE", "75"))
REWARD_BOSS_WIN = int(os.getenv("REWARD_BOSS_WIN", "150"))


def _ensure_profile_for_user_id(user_id: int, display_name: str):
    profile = player_profiles.get(user_id)
    if profile is None:
        profile = _profile_default(user_id, display_name)
        player_profiles[user_id] = profile
    else:
        profile["display_name"] = display_name
    return profile


def _award_profile_points(
    user_id: int,
    display_name: str,
    points: int,
    *,
    stat: str | None = None,
    stat_amount: int = 1,
):
    """Vergibt Punkte/Statistiken und meldet ggf. einen neu freigeschalteten Titel."""
    profile = _ensure_profile_for_user_id(user_id, display_name)

    old_points = int(profile.get("points", 0))
    old_highest = _highest_title(old_points)

    profile["points"] = max(0, old_points + int(points))

    if stat:
        profile[stat] = int(profile.get(stat, 0)) + int(stat_amount)

    new_points = int(profile["points"])
    new_highest = _highest_title(new_points)

    unlocked = new_highest if new_highest != old_highest and new_points > old_points else None

    return {
        "profile": profile,
        "old_points": old_points,
        "new_points": new_points,
        "unlocked_title": unlocked,
    }


async def _persist_rewards(guild: discord.Guild | None):
    if guild is not None:
        await save_profiles_to_discord(guild)


def _reward_line(display_name: str, result: dict, gained: int) -> str:
    line = f"🏆 **{display_name}** erhält **+{gained} Punkte**"
    if result.get("unlocked_title"):
        line += f" — ✨ neuer Titel: **{result['unlocked_title']}**"
    return line


def _profile_default(user_id: int, display_name: str = "Unbekannt"):
    return {
        "user_id": int(user_id),
        "display_name": display_name,
        "points": 0,
        "events": 0,
        "boss_wins": 0,
        "riddles_solved": 0,
        "selected_title": None,
    }


def _earned_titles(points: int):
    return [title for threshold, title in TITLE_THRESHOLDS if points >= threshold]


def _highest_title(points: int):
    titles = _earned_titles(points)
    return titles[-1] if titles else "Frischling von Eorzea"


def _profile_title(profile: dict):
    selected = profile.get("selected_title")
    earned = _earned_titles(int(profile.get("points", 0)))
    if selected in earned:
        return selected
    return _highest_title(int(profile.get("points", 0)))


def _next_title_info(points: int):
    for threshold, title in TITLE_THRESHOLDS:
        if threshold > points:
            return threshold, title
    return None, None


async def _get_or_create_profile_data_channel(guild: discord.Guild):
    existing = discord.utils.get(guild.text_channels, name=PROFILE_DATA_CHANNEL_NAME)
    if existing:
        return existing

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        ),
    }

    # Event-Admins dürfen den Datenchannel nicht automatisch sehen.
    channel = await guild.create_text_channel(
        PROFILE_DATA_CHANNEL_NAME,
        overwrites=overwrites,
        reason="Persistente KI-Catnip-Spielerprofile",
    )
    return channel


async def _find_profile_data_message(channel: discord.TextChannel):
    async for message in channel.history(limit=50, oldest_first=True):
        if message.author == channel.guild.me and message.content.startswith(PROFILE_DATA_MESSAGE_PREFIX):
            return message
    return None


async def load_profiles_from_discord(guild: discord.Guild):
    global player_profiles
    try:
        channel = await _get_or_create_profile_data_channel(guild)
        message = await _find_profile_data_message(channel)

        if not message:
            player_profiles = {}
            message = await channel.send(PROFILE_DATA_MESSAGE_PREFIX + "{}")
            try:
                await message.pin(reason="KI-Catnip Profildaten")
            except Exception:
                pass
            print(f"✓ Profildaten initialisiert auf {guild.name}")
            return

        raw = message.content[len(PROFILE_DATA_MESSAGE_PREFIX):].strip()
        data = json.loads(raw or "{}")
        player_profiles = {
            int(uid): profile for uid, profile in data.items()
            if isinstance(profile, dict)
        }
        print(f"✓ {len(player_profiles)} Spielerprofile geladen")
    except Exception as exc:
        print(f"Profil-Ladefehler: {type(exc).__name__}: {exc}")


async def save_profiles_to_discord(guild: discord.Guild):
    try:
        channel = await _get_or_create_profile_data_channel(guild)
        message = await _find_profile_data_message(channel)

        payload = json.dumps(
            {str(uid): profile for uid, profile in player_profiles.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        content = PROFILE_DATA_MESSAGE_PREFIX + payload

        # Discord-Nachrichtenlimit: 2000 Zeichen.
        if len(content) > 1950:
            raise RuntimeError(
                "Profildaten sind zu groß für die einfache Discord-Nachrichtenspeicherung. "
                "Für größere Communities sollte Stufe 6 später auf SQLite/externen Speicher wechseln."
            )

        if message:
            await message.edit(content=content)
        else:
            message = await channel.send(content)
            try:
                await message.pin(reason="KI-Catnip Profildaten")
            except Exception:
                pass
    except Exception as exc:
        print(f"Profil-Speicherfehler: {type(exc).__name__}: {exc}")
        raise


def get_or_create_profile(user):
    profile = player_profiles.get(user.id)
    if profile is None:
        profile = _profile_default(user.id, user.display_name)
        player_profiles[user.id] = profile
    else:
        profile["display_name"] = user.display_name
    return profile


def profile_embed(profile: dict) -> discord.Embed:
    points = int(profile.get("points", 0))
    title = _profile_title(profile)
    next_threshold, next_title = _next_title_info(points)

    embed = discord.Embed(
        title=f"🏆 {profile.get('display_name', 'Abenteurer')} — Spielerprofil",
        description=f"**Titel:** ✨ {title}",
    )
    embed.add_field(name="⭐ Punkte", value=f"**{points}**", inline=True)
    embed.add_field(name="🎭 Events", value=f"**{profile.get('events', 0)}**", inline=True)
    embed.add_field(name="⚔️ Boss-Siege", value=f"**{profile.get('boss_wins', 0)}**", inline=True)
    embed.add_field(name="🧩 Rätsel gelöst", value=f"**{profile.get('riddles_solved', 0)}**", inline=True)

    if next_threshold is not None:
        remaining = max(0, next_threshold - points)
        embed.add_field(
            name="📈 Nächster Titel",
            value=f"**{next_title}** bei {next_threshold} Punkten\nNoch **{remaining}** Punkte.",
            inline=False,
        )
    else:
        embed.add_field(
            name="🌟 Rang",
            value="Du hast aktuell den höchsten verfügbaren KI-Catnip-Titel erreicht.",
            inline=False,
        )

    embed.add_field(
        name="📜 FFXIV-Charakter",
        value="Mit `/charakterprofil` kannst du dein persönliches FFXIV-/RP-Profil anzeigen.",
        inline=False,
    )
    embed.set_footer(text="Stufe 7.1 • Punkteprofil + separates FFXIV-Charakterprofil")
    return embed


@client.tree.command(name="profil", description="Zeigt dein dauerhaftes KI-Catnip-Spielerprofil.")
@app_commands.describe(spieler="Optional: Profil eines anderen Spielers anzeigen")
async def profil(
    interaction: discord.Interaction,
    spieler: discord.Member | None = None,
):
    target = spieler or interaction.user
    profile = get_or_create_profile(target)
    if interaction.guild:
        await save_profiles_to_discord(interaction.guild)
    await interaction.response.send_message(embed=profile_embed(profile))


@client.tree.command(name="rangliste", description="Zeigt die KI-Catnip-Punkterangliste.")
async def rangliste(interaction: discord.Interaction):
    if not player_profiles:
        await interaction.response.send_message(
            "🏆 Noch gibt es keine Spielerprofile. Nutzt `/profil`, um eines anzulegen."
        )
        return

    ranking = sorted(
        player_profiles.values(),
        key=lambda p: int(p.get("points", 0)),
        reverse=True,
    )[:10]

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, profile in enumerate(ranking, start=1):
        icon = medals[i - 1] if i <= 3 else f"`#{i}`"
        title = _profile_title(profile)
        lines.append(
            f"{icon} **{profile.get('display_name', 'Unbekannt')}** — "
            f"**{profile.get('points', 0)} Punkte** · *{title}*"
        )

    embed = discord.Embed(
        title="🏆 KI-Catnip — Rangliste",
        description="\n".join(lines),
    )
    embed.set_footer(text="Top 10 • Schattenflauscher")
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="titel", description="Wählt einen deiner freigeschalteten KI-Catnip-Titel.")
@app_commands.describe(titel="Exakter Name eines freigeschalteten Titels")
async def titel(interaction: discord.Interaction, titel: str):
    profile = get_or_create_profile(interaction.user)
    earned = _earned_titles(int(profile.get("points", 0)))

    wanted = titel.strip().lower()
    selected = next((t for t in earned if t.lower() == wanted), None)

    if not selected:
        await interaction.response.send_message(
            "🔒 Diesen Titel hast du noch nicht freigeschaltet.\n"
            "Deine verfügbaren Titel:\n" + "\n".join(f"• {t}" for t in earned),
            ephemeral=True,
        )
        return

    profile["selected_title"] = selected
    if interaction.guild:
        await save_profiles_to_discord(interaction.guild)

    await interaction.response.send_message(
        f"✨ Dein aktiver Titel ist jetzt **{selected}**.",
        ephemeral=True,
    )


@client.tree.command(name="titelinfo", description="Zeigt alle KI-Catnip-Titel und ihre Punktegrenzen.")
async def titelinfo(interaction: discord.Interaction):
    lines = [
        f"**{threshold:>4} Punkte** — {title}"
        for threshold, title in TITLE_THRESHOLDS
    ]
    await interaction.response.send_message(
        "🏆 **KI-Catnip-Titel**\n" + "\n".join(lines),
        ephemeral=True,
    )



@client.tree.command(name="belohnungen", description="Zeigt die automatischen KI-Catnip-Punktebelohnungen.")
async def belohnungen(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🏆 KI-Catnip — Automatische Belohnungen",
        description="Punkte werden bei euren Events automatisch ins Spielerprofil geschrieben.",
    )
    embed.add_field(
        name="🧩 Rätsel lösen",
        value=f"**+{REWARD_RIDDLE_SOLVED} Punkte** pro korrekt gelöster Station",
        inline=False,
    )
    embed.add_field(
        name="🏁 Rätsel-Event abschließen",
        value=f"**+{REWARD_RIDDLE_EVENT_COMPLETE} Punkte** für beteiligte Löser",
        inline=False,
    )
    embed.add_field(
        name="⚔️ Boss besiegen",
        value=f"**+{REWARD_BOSS_WIN} Punkte** für jedes angemeldete Gruppenmitglied",
        inline=False,
    )
    embed.add_field(
        name="✨ Titel",
        value="Neue Titel werden beim Überschreiten ihrer Punktegrenze automatisch freigeschaltet.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="punkte", description="Admin: Vergibt oder entfernt KI-Catnip-Punkte.")
@app_commands.describe(
    spieler="Spieler",
    menge="Positive oder negative Punktzahl",
    grund="Optionaler Grund",
)
async def punkte(
    interaction: discord.Interaction,
    spieler: discord.Member,
    menge: app_commands.Range[int, -5000, 5000],
    grund: str = "",
):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen Punkte verändern.",
            ephemeral=True,
        )
        return

    profile = get_or_create_profile(spieler)
    old_points = int(profile.get("points", 0))
    profile["points"] = max(0, old_points + menge)
    new_points = profile["points"]

    old_title = _highest_title(old_points)
    new_title = _highest_title(new_points)

    if interaction.guild:
        await save_profiles_to_discord(interaction.guild)

    text = (
        f"🏆 **{spieler.display_name}**: {old_points} → **{new_points} Punkte**"
        + (f"\nGrund: {grund}" if grund else "")
    )

    if new_title != old_title and new_points > old_points:
        text += f"\n✨ **Neuer Titel freigeschaltet: {new_title}!**"

    await interaction.response.send_message(text)



# ============================================================
# STUFE 5.1 — MEHRSTUFIGE RÄTSEL-EVENTS
# ============================================================

active_riddle_events = {}

async def generate_ai_riddle_stations(
    channel_id: int,
    username: str,
    theme: str,
    count: int,
    difficulty: str,
):
    """
    Erzeugt neue Rätsel über Gemini.
    Erwartet strikt JSON. Bei Fehlern gibt die Funktion None zurück,
    damit auf feste Presets zurückgefallen werden kann.
    """
    prompt = f"""
Erstelle {count} spielbare FINAL FANTASY XIV Rätselstationen für ein Discord-Event.

Thema: {theme}
Schwierigkeit: {difficulty}

WICHTIG:
- Antworte ausschließlich als gültiges JSON.
- Keine Markdown-Codeblöcke.
- Keine Kommentare außerhalb des JSON.
- Genau {count} Stationen.
- Jede Station muss genau eine eindeutige Lösung haben.
- Jede Station braucht genau 3 Hinweise, zunehmend deutlicher.
- Offizielle FFXIV-Fakten nur verwenden, wenn du dir sicher bist.
- Frei erfundene Inhalte deutlich als Event-Lore behandeln.
- Keine Storyspoiler erzwingen.
- Lösungen kurz halten, damit Spieler sie tippen können.

JSON-Format:
{{
  "stations": [
    {{
      "title": "Station 1 — ...",
      "question": "...",
      "answer": "...",
      "hints": ["...", "...", "..."]
    }}
  ]
}}
""".strip()

    try:
        answer = await ask_ai(
            channel_id,
            username,
            prompt,
            remember=False,
            force_web=False,
        )

        # Quellenblock entfernen, falls ask_ai doch Quellen angehängt hat.
        answer = answer.split("\n**Quellen:**", 1)[0].strip()

        # Optional umschließende Codefences entfernen.
        answer = re.sub(r"^```(?:json)?\s*", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"\s*```$", "", answer)

        payload = json.loads(answer)
        stations = payload.get("stations", [])

        cleaned = []
        for i, station in enumerate(stations[:count], start=1):
            title = str(station.get("title", f"Station {i}")).strip()
            question = str(station.get("question", "")).strip()
            answer_value = str(station.get("answer", "")).strip()
            hints = station.get("hints", [])

            if not question or not answer_value or not isinstance(hints, list):
                continue

            hints = [str(h).strip() for h in hints if str(h).strip()][:3]
            if len(hints) != 3:
                continue

            cleaned.append(
                {
                    "title": title,
                    "question": question,
                    "answer": answer_value,
                    "hints": hints,
                }
            )

        if len(cleaned) == count:
            return cleaned

    except Exception as exc:
        print(f"KI-Rätselgenerierung fehlgeschlagen: {type(exc).__name__}: {exc}")

    return None


def _fallback_riddle_stations(theme: str, count: int):
    """
    Nutzt feste Presets als Sicherheitsnetz.
    """
    preset_name = theme if theme in RIDDLE_PRESETS else "Event-Lore"
    return [dict(s) for s in RIDDLE_PRESETS[preset_name][:count]]


async def _start_linked_endboss(
    interaction: discord.Interaction,
    boss_name: str,
):
    """
    Startet nach einem abgeschlossenen Rätsel-Event automatisch
    einen Bosskampf im selben Channel.
    """
    if interaction.guild is None:
        return False

    key = _boss_key(interaction.guild.id, interaction.channel_id)

    if key in active_boss_battles:
        return False

    if boss_name not in BOSS_TEMPLATES:
        return False

    template = BOSS_TEMPLATES[boss_name]
    state = {
        "name": boss_name,
        "hp": template["max_hp"],
        "max_hp": template["max_hp"],
        "party_hp": template["party_hp"],
        "party_max_hp": template["party_hp"],
        "wrong_damage": template["wrong_damage"],
        "phase": 0,
        "phases": template["phases"],
        "correct": 0,
        "wrong": 0,
        "answered_users": set(),
        "players": {},
    }

    lobby = boss_party_lobbies.get(key)
    if lobby and lobby["players"]:
        state["players"] = {
            uid: dict(data) for uid, data in lobby["players"].items()
        }
        state["party_max_hp"] = sum(p["max_hp"] for p in state["players"].values())
        state["party_hp"] = sum(p["hp"] for p in state["players"].values())

    active_boss_battles[key] = state

    await interaction.channel.send(
        embed=boss_embed(
            state,
            message=(
                f"⚔️ **Das letzte Siegel zerbricht!** "
                f"Aus dem Äther tritt **{boss_name}** hervor.\n"
                "Der Rätselpfad endet — der Bosskampf beginnt!"
            ),
        ),
        view=BossCombatView(),
    )
    return True



RIDDLE_PRESETS = {
    "FFXIV Wissen": [
        {
            "title": "Station 1 — Die Rolle",
            "question": "Welche der drei klassischen Gruppenrollen hält einen Boss normalerweise frontal und bindet seine Aufmerksamkeit?",
            "answer": "tank",
            "hints": [
                "Diese Rolle trägt meist besonders robuste Ausrüstung.",
                "Sie nutzt Fähigkeiten wie Provozieren und defensive Cooldowns.",
                "Die gesuchte Rolle ist der Tank.",
            ],
        },
        {
            "title": "Station 2 — Die Bodenmarkierung",
            "question": "Was sollte man in FFXIV normalerweise tun, wenn unter dem eigenen Charakter eine feindliche AoE-Fläche erscheint?",
            "answer": "raus",
            "hints": [
                "Die Fläche kündigt Schaden an.",
                "Bewegung ist meist wichtiger als noch ein zusätzlicher Angriff.",
                "Die Lösung ist: aus der AoE laufen.",
            ],
        },
        {
            "title": "Station 3 — Die Gruppe",
            "question": "Welche Rolle stellt verlorene Lebenspunkte der Gruppe wieder her?",
            "answer": "heiler",
            "hints": [
                "Sie hält die Party am Leben.",
                "Zu ihren Werkzeugen gehören Einzel- und Gruppenheilungen.",
                "Die gesuchte Rolle ist der Heiler.",
            ],
        },
    ],
    "Eorzea": [
        {
            "title": "Station 1 — Die Waldstadt",
            "question": "Welche große Stadtstaat-Region ist besonders mit dem Finsterwald verbunden?",
            "answer": "gridania",
            "hints": [
                "Sie liegt im Schwarzen Wald.",
                "Die Kan-E-Senna ist eng mit ihr verbunden.",
                "Die Lösung ist Gridania.",
            ],
        },
        {
            "title": "Station 2 — Die Wüstenstadt",
            "question": "Welche Stadt ist für Handel, Reichtum und ihre Lage in Thanalan bekannt?",
            "answer": "uldah",
            "hints": [
                "Sie liegt in Thanalan.",
                "Die Sultana Nanamo ist mit dieser Stadt verbunden.",
                "Die Lösung ist Ul'dah.",
            ],
        },
        {
            "title": "Station 3 — Die Hafenstadt",
            "question": "Welche Stadtstaat-Region ist besonders für Seefahrt und Piraten bekannt?",
            "answer": "limsa lominsa",
            "hints": [
                "Sie liegt an der Küste.",
                "Die Maelstrom-Organisation hat dort ihren Sitz.",
                "Die Lösung ist Limsa Lominsa.",
            ],
        },
    ],
    "Event-Lore": [
        {
            "title": "Station 1 — Die versiegelte Pforte",
            "question": "Vor euch glühen drei Runen: Sonne, Mond und Stern. Welche Rune würdet ihr wählen, wenn der Hinweis lautet: 'Ich leuchte nur, wenn die Nacht beginnt'?",
            "answer": "mond",
            "hints": [
                "Die Sonne scheidet aus.",
                "Gesucht ist etwas, das besonders mit der Nacht verbunden ist.",
                "Die Lösung ist Mond.",
            ],
        },
        {
            "title": "Station 2 — Der schwarze Eid",
            "question": "Ein alter Text sagt: 'Nur wer schweigt, hört das Flüstern der Tiefe.' Welche Handlung ist am naheliegendsten?",
            "answer": "schweigen",
            "hints": [
                "Der Text fordert keine Bewegung.",
                "Es geht darum, keine Geräusche zu machen.",
                "Die Lösung ist Schweigen.",
            ],
        },
        {
            "title": "Station 3 — Das letzte Siegel",
            "question": "Drei Symbole erscheinen: Klinge, Schild und Schlüssel. Welches Symbol öffnet am ehesten ein versiegeltes Tor?",
            "answer": "schlüssel",
            "hints": [
                "Eine Klinge trennt, ein Schild schützt.",
                "Gesucht ist ein Gegenstand zum Öffnen.",
                "Die Lösung ist Schlüssel.",
            ],
        },
    ],
}

def _riddle_key(guild_id: int, channel_id: int):
    return (guild_id, channel_id)

def _normalize_riddle_answer(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("'", "").replace("’", "").replace("-", " ")
    value = " ".join(value.split())

    aliases = {
        "uldah": {"ul dah", "uldah", "ul'dah"},
        "limsa lominsa": {"limsa", "limsa lominsa"},
        "heiler": {"heiler", "heal", "healing"},
        "tank": {"tank", "tanken"},
        "raus": {"raus", "ausweichen", "weg", "aus der aoe", "aoe verlassen"},
        "mond": {"mond", "der mond"},
        "schweigen": {"schweigen", "still sein", "stille"},
        "schlüssel": {"schlüssel", "schluessel", "key"},
        "gridania": {"gridania"},
    }
    for canonical, values in aliases.items():
        if value in values:
            return canonical
    return value

def riddle_event_embed(state: dict, *, message: str | None = None) -> discord.Embed:
    station = state["stations"][state["index"]]

    embed = discord.Embed(
        title=f"🧩 Rätsel-Event — {state['theme']}",
        description=message or "KI-Catnip öffnet die nächste Station. Die Pfoten bleiben von der Lösung fern. 🐾",
    )
    embed.add_field(
        name="📍 Fortschritt",
        value=f"Station **{state['index'] + 1}/{len(state['stations'])}**",
        inline=True,
    )
    embed.add_field(
        name="🏆 Punkte",
        value=f"**{state['score']}**",
        inline=True,
    )
    embed.add_field(
        name="🧠 Modus",
        value=(
            f"**{state.get('source', 'Preset')}**\n"
            f"Schwierigkeit: **{state.get('difficulty', 'Mittel')}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="❌ Fehlversuche",
        value=f"**{state['wrong']}**",
        inline=True,
    )
    embed.add_field(name=f"🧩 {station['title']}", value=station["question"], inline=False)
    embed.add_field(
        name="💡 Hinweise",
        value=(
            f"Verwendet `/raetselhinweis`, wenn ihr Hilfe braucht.\n"
            f"Bereits verwendet: **{state['hints_used_current']}/3**"
        ),
        inline=False,
    )
    embed.add_field(
        name="✍️ Antwort",
        value="Antwortet mit `/raetselantwort lösung:<eure Lösung>`.",
        inline=False,
    )
    boss_name = state.get("endboss", "none")
    boss_info = f" • Endboss: {boss_name}" if boss_name != "none" else ""
    embed.set_footer(
        text=(
            "Wertung: +100 richtige Lösung • -25 pro Hinweis • -10 pro Fehlversuch"
            + boss_info
        )
    )
    return embed

def riddle_admin_solution_text(state: dict) -> str:
    station = state["stations"][state["index"]]
    return (
        f"🔐 **Spielleiter-Lösung — {station['title']}**\n"
        f"**Antwort:** `{station['answer']}`\n"
        f"**Hinweise:**\n"
        + "\n".join(f"{i+1}. {h}" for i, h in enumerate(station["hints"]))
    )

@client.tree.command(name="raetselevent", description="Startet ein mehrstufiges KI-Catnip-Rätsel-Event.")
@app_commands.describe(
    thema="Thema oder Stichwort für das Rätsel-Event",
    stationen="Anzahl der Stationen",
    quelle="Feste Presets oder neue KI-Rätsel",
    schwierigkeit="Schwierigkeitsgrad",
    endboss="Optionaler Boss nach der letzten Station",
)
@app_commands.choices(
    quelle=[
        app_commands.Choice(name="KI-generiert", value="ki"),
        app_commands.Choice(name="Feste Presets", value="preset"),
    ],
    schwierigkeit=[
        app_commands.Choice(name="Leicht", value="Leicht"),
        app_commands.Choice(name="Mittel", value="Mittel"),
        app_commands.Choice(name="Schwer", value="Schwer"),
        app_commands.Choice(name="Extrem", value="Extrem"),
    ],
    endboss=[
        app_commands.Choice(name="Kein Endboss", value="none"),
        app_commands.Choice(name="Ifrit", value="Ifrit"),
        app_commands.Choice(name="Titan", value="Titan"),
        app_commands.Choice(name="Jupiter", value="Jupiter"),
    ],
)
async def raetselevent(
    interaction: discord.Interaction,
    thema: str,
    stationen: app_commands.Range[int, 1, 5] = 3,
    quelle: app_commands.Choice[str] | None = None,
    schwierigkeit: app_commands.Choice[str] | None = None,
    endboss: app_commands.Choice[str] | None = None,
):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur freigeschaltete KI-Catnip-Administratoren dürfen Rätsel-Events starten.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "Rätsel-Events funktionieren nur auf einem Server.",
            ephemeral=True,
        )
        return

    key = _riddle_key(interaction.guild.id, interaction.channel_id)
    if key in active_riddle_events:
        await interaction.response.send_message(
            "⚠️ In diesem Channel läuft bereits ein Rätsel-Event. Nutze zuerst `/raetselstop`.",
            ephemeral=True,
        )
        return

    source = quelle.value if quelle else "ki"
    difficulty = schwierigkeit.value if schwierigkeit else "Mittel"
    boss_name = endboss.value if endboss else "none"

    await interaction.response.defer(thinking=True)

    stations = None
    used_source = "Preset"

    if source == "ki":
        stations = await generate_ai_riddle_stations(
            interaction.channel_id,
            interaction.user.display_name,
            thema,
            stationen,
            difficulty,
        )
        if stations:
            used_source = "KI-generiert"

    if not stations:
        stations = _fallback_riddle_stations(thema, min(stationen, 3))
        used_source = "Preset-Fallback"

    state = {
        "theme": thema,
        "stations": stations,
        "index": 0,
        "score": 0,
        "wrong": 0,
        "hints_used_current": 0,
        "solved_by": [],
        "solver_ids": {},
        "source": used_source,
        "difficulty": difficulty,
        "endboss": boss_name,
    }
    active_riddle_events[key] = state

    boss_text = boss_name if boss_name != "none" else "Keiner"

    await interaction.followup.send(
        embed=riddle_event_embed(
            state,
            message=(
                f"🧩 **{thema}** beginnt!\n"
                f"Quelle: **{used_source}** • Schwierigkeit: **{difficulty}** • Endboss: **{boss_text}**"
            ),
        )
    )


@client.tree.command(name="raetselantwort", description="Beantwortet die aktuelle Station eines Rätsel-Events.")
@app_commands.describe(lösung="Eure Lösung")
async def raetselantwort(interaction: discord.Interaction, lösung: str):
    if interaction.guild is None:
        await interaction.response.send_message("Rätsel-Events funktionieren nur auf einem Server.", ephemeral=True)
        return

    key = _riddle_key(interaction.guild.id, interaction.channel_id)
    state = active_riddle_events.get(key)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft gerade kein Rätsel-Event.", ephemeral=True)
        return

    station = state["stations"][state["index"]]
    expected = _normalize_riddle_answer(station["answer"])
    given = _normalize_riddle_answer(lösung)

    if given == expected:
        gained = max(10, 100 - state["hints_used_current"] * 25)
        state["score"] += gained
        state["solved_by"].append(interaction.user.display_name)
        state.setdefault("solver_ids", {})[str(interaction.user.id)] = interaction.user.display_name

        # Stufe 6.2: dauerhafte Spielerbelohnung für jede gelöste Station.
        riddle_reward = _award_profile_points(
            interaction.user.id,
            interaction.user.display_name,
            REWARD_RIDDLE_SOLVED,
            stat="riddles_solved",
        )
        await _persist_rewards(interaction.guild)

        final = state["index"] >= len(state["stations"]) - 1
        if final:
            boss_name = state.get("endboss", "none")

            # Stufe 6.2: Abschlussbonus an jeden Spieler, der mindestens
            # eine Station dieses Events gelöst hat.
            event_reward_lines = []
            for uid_text, display_name in state.get("solver_ids", {}).items():
                uid = int(uid_text)
                result = _award_profile_points(
                    uid,
                    display_name,
                    REWARD_RIDDLE_EVENT_COMPLETE,
                    stat="events",
                )
                event_reward_lines.append(
                    _reward_line(display_name, result, REWARD_RIDDLE_EVENT_COMPLETE)
                )

            await _persist_rewards(interaction.guild)

            embed = discord.Embed(
                title="🏆 Rätsel-Event abgeschlossen!",
                description=(
                    f"Alle **{len(state['stations'])} Stationen** wurden gelöst.\n\n"
                    f"🏆 **Endpunktzahl:** {state['score']}\n"
                    f"❌ **Fehlversuche:** {state['wrong']}\n"
                    f"🧠 **Letzte Lösung durch:** {interaction.user.display_name}\n\n"
                    + (
                        f"⚔️ **Doch das letzte Siegel beginnt zu beben... {boss_name} wartet dahinter.**"
                        if boss_name != "none"
                        else "KI-Catnip schnurrt anerkennend. Kein Runensiegel ist vor euch sicher. 🐱"
                    )
                    + (
                        "\n\n**🏆 Profil-Belohnungen**\n" + "\n".join(event_reward_lines)
                        if event_reward_lines else ""
                    )
                ),
            )

            active_riddle_events.pop(key, None)
            await interaction.response.send_message(embed=embed)

            if boss_name != "none":
                started = await _start_linked_endboss(interaction, boss_name)
                if not started:
                    await interaction.channel.send(
                        "⚠️ Der konfigurierte Endboss konnte nicht automatisch gestartet werden. "
                        "Prüfe, ob bereits ein Bosskampf läuft."
                    )
            return

        state["index"] += 1
        state["hints_used_current"] = 0
        await interaction.response.send_message(
            embed=riddle_event_embed(
                state,
                message=(
                    f"✅ **Richtig!** {interaction.user.display_name} löst die Station "
                    f"und verdient **{gained} Event-Punkte**. Die nächste Pforte öffnet sich.\n"
                    + _reward_line(interaction.user.display_name, riddle_reward, REWARD_RIDDLE_SOLVED)
                ),
            )
        )
        return

    state["wrong"] += 1
    state["score"] = max(0, state["score"] - 10)
    await interaction.response.send_message(
        f"❌ **Das ist noch nicht die Lösung.** `{lösung}` passt nicht zum Siegel.\n"
        f"Die Gruppe verliert **10 Punkte**. Aktueller Stand: **{state['score']}**."
    )

@client.tree.command(name="raetselhinweis", description="Gibt den nächsten Hinweis zur aktuellen Rätsel-Station.")
async def raetselhinweis(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    key = _riddle_key(interaction.guild.id, interaction.channel_id)
    state = active_riddle_events.get(key)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft gerade kein Rätsel-Event.", ephemeral=True)
        return

    station = state["stations"][state["index"]]
    hint_index = state["hints_used_current"]

    if hint_index >= len(station["hints"]):
        await interaction.response.send_message(
            "💡 Ihr habt bereits alle Hinweise für diese Station verwendet.",
            ephemeral=True,
        )
        return

    hint = station["hints"][hint_index]
    state["hints_used_current"] += 1
    state["score"] = max(0, state["score"] - 25)

    await interaction.response.send_message(
        f"💡 **Hinweis {state['hints_used_current']}:** {hint}\n"
        f"🏆 Aktuelle Punkte: **{state['score']}**"
    )

@client.tree.command(name="raetselloesung", description="Zeigt Admins die Lösung der aktuellen Rätsel-Station.")
async def raetselloesung(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen die Spielleiter-Lösung sehen.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    state = active_riddle_events.get(_riddle_key(interaction.guild.id, interaction.channel_id))
    if not state:
        await interaction.response.send_message("🐾 Hier läuft gerade kein Rätsel-Event.", ephemeral=True)
        return

    await interaction.response.send_message(
        riddle_admin_solution_text(state),
        ephemeral=True,
    )

@client.tree.command(name="raetselstatus", description="Zeigt den aktuellen Stand des Rätsel-Events.")
async def raetselstatus(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    state = active_riddle_events.get(_riddle_key(interaction.guild.id, interaction.channel_id))
    if not state:
        await interaction.response.send_message("🐾 Hier läuft gerade kein Rätsel-Event.", ephemeral=True)
        return

    await interaction.response.send_message(embed=riddle_event_embed(state), ephemeral=True)

@client.tree.command(name="raetselstop", description="Beendet das aktuelle Rätsel-Event.")
async def raetselstop(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen Rätsel-Events beenden.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    key = _riddle_key(interaction.guild.id, interaction.channel_id)
    state = active_riddle_events.pop(key, None)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft gerade kein Rätsel-Event.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"🛑 Das Rätsel-Event **{state['theme']}** wurde beendet. "
        f"Endstand: **{state['score']} Punkte**."
    )



# ============================================================
# STUFE 4.1 — INTERAKTIVE BOSSKÄMPFE MIT BUTTONS + PARTY-HP
# ============================================================

BOSS_TEMPLATES = {
    "Ifrit": {
        "max_hp": 100,
        "party_hp": 100,
        "wrong_damage": 25,
        "phases": [
            {
                "name": "Phase 1 — Glut",
                "attack": "🔥 Höllenglut",
                "mechanic_type": "tankbuster",
                "question": "Ifrit richtet einen schweren Angriff auf sein Hauptziel. Welche Rolle sollte ihn normalerweise halten?",
                "choices": [("A", "Tank"), ("B", "Heiler"), ("C", "Fernkampf-DPS"), ("D", "Crafter")],
                "correct": "A",
            },
            {
                "name": "Phase 2 — Eruption",
                "attack": "💥 Eruption",
                "mechanic_type": "aoe",
                "question": "Unter euch erscheint eine große gefährliche Bodenfläche. Was ist die richtige Reaktion?",
                "choices": [("A", "Stehen bleiben"), ("B", "Aus der AoE laufen"), ("C", "Zum Boss laufen"), ("D", "Emote benutzen")],
                "correct": "B",
            },
            {
                "name": "Phase 3 — Inferno",
                "attack": "🌋 Inferno",
                "mechanic_type": "raidwide",
                "question": "Die gesamte Gruppe erleidet hohen Schaden. Welche Rolle reagiert besonders mit Gruppenheilung?",
                "choices": [("A", "Tank"), ("B", "Heiler"), ("C", "Melee-DPS"), ("D", "Gatherer")],
                "correct": "B",
            },
        ],
    },
    "Titan": {
        "max_hp": 120,
        "party_hp": 100,
        "wrong_damage": 25,
        "phases": [
            {
                "name": "Phase 1 — Gewicht des Landes",
                "attack": "🟤 Gewicht des Landes",
                "mechanic_type": "aoe",
                "question": "Mehrere Bodenflächen erscheinen unter der Gruppe. Was hat Vorrang?",
                "choices": [("A", "Ausweichen"), ("B", "Weitercasten"), ("C", "Zum Rand laufen"), ("D", "Limit Break")],
                "correct": "A",
            },
            {
                "name": "Phase 2 — Bergsturz",
                "attack": "🪨 Bergsturz",
                "mechanic_type": "tankbuster",
                "question": "Titan setzt einen schweren Angriff auf sein Hauptziel ein. Wer sollte ihn abfangen?",
                "choices": [("A", "Heiler"), ("B", "Tank"), ("C", "DPS"), ("D", "Alle gleichzeitig")],
                "correct": "B",
            },
            {
                "name": "Phase 3 — Erderschütterung",
                "attack": "🌍 Erderschütterung",
                "mechanic_type": "raidwide",
                "question": "Die ganze Gruppe verliert gleichzeitig HP. Was stabilisiert die Gruppe am schnellsten?",
                "choices": [("A", "Gruppenheilung"), ("B", "Sprint"), ("C", "Provozieren"), ("D", "Auto-Attack")],
                "correct": "A",
            },
        ],
    },
    "Jupiter": {
        "max_hp": 160,
        "party_hp": 100,
        "wrong_damage": 25,
        "phases": [
            {
                "name": "PHASE 1 — Erwachen des Machtpatrons",
                "attack": "🌌 Sternenbrand",
                "mechanic_type": "aoe",
                "question": "Jupiter markiert große Teile der Arena mit leuchtenden Flächen. Was ist die wichtigste Reaktion?",
                "choices": [
                    ("A", "In der Markierung stehen bleiben"),
                    ("B", "Die gefährliche Fläche verlassen"),
                    ("C", "Alle zum Tank laufen"),
                    ("D", "Nur den Boss weiter angreifen"),
                ],
                "correct": "B",
            },
            {
                "name": "PHASE 2 — Herrschaft der Sterne",
                "attack": "☄️ Zwillingsklinge",
                "mechanic_type": "tankbuster",
                "question": "Jupiter setzt einen schweren Einzelangriff auf sein Hauptziel ein. Welche Rolle sollte diesen Angriff kontrolliert abfangen?",
                "choices": [
                    ("A", "Tank"),
                    ("B", "Heiler"),
                    ("C", "Fernkampf-DPS"),
                    ("D", "Crafter"),
                ],
                "correct": "A",
            },
            {
                "name": "PHASE 3 — Jupiter, der Zerstörer",
                "attack": "⚡ Kosmische Entladung",
                "mechanic_type": "raidwide",
                "question": "Eine starke raidweite Attacke steht bevor. Was hilft der Gruppe am meisten?",
                "choices": [
                    ("A", "Alle verteilen sich ohne Grund"),
                    ("B", "Mitigation und Gruppenheilung vorbereiten"),
                    ("C", "Tank dreht den Boss zur Gruppe"),
                    ("D", "Alle stoppen ihre Aktionen"),
                ],
                "correct": "B",
            },
            {
                "name": "PHASE 4 — ENRAGE",
                "attack": "💀 Ende der Sterne",
                "mechanic_type": "enrage",
                "question": "Jupiter beginnt seinen finalen Enrage. Was ist jetzt das zentrale Ziel der Gruppe?",
                "choices": [
                    ("A", "So schnell wie möglich verbleibenden Schaden verursachen"),
                    ("B", "Die Arena verlassen"),
                    ("C", "Nur noch heilen"),
                    ("D", "Den Boss nicht mehr angreifen"),
                ],
                "correct": "A",
            },
        ],
    },
}

active_boss_battles = {}

boss_party_lobbies = {}

ROLE_ICONS = {
    "Tank": "🛡️",
    "Heiler": "💚",
    "DPS": "⚔️",
}

MECHANIC_INFO = {
    "aoe": ("🟠 AoE", "Gefährliche Fläche – rechtzeitig ausweichen."),
    "tankbuster": ("🛡️ Tankbuster", "Schwerer Treffer auf den Tank."),
    "raidwide": ("💥 Raidwide", "Die gesamte Gruppe erleidet Schaden."),
    "stack": ("🔻 Stack", "Die Gruppe muss zusammenstehen."),
    "spread": ("↔️ Spread", "Spieler müssen Abstand voneinander halten."),
    "enrage": ("💀 ENRAGE", "Letzte Schadensprüfung – der Boss muss fallen."),
}

def _party_lobby_key(guild_id: int, channel_id: int):
    return (guild_id, channel_id)

def _living_players(players: dict):
    return [p for p in players.values() if p.get("hp", 0) > 0]

def _sync_party_hp(state: dict):
    players = state.get("players", {})
    if players:
        state["party_hp"] = sum(max(0, p["hp"]) for p in players.values())
        state["party_max_hp"] = sum(p["max_hp"] for p in players.values())

def _party_members_text(players: dict) -> str:
    if not players:
        return "Noch keine Abenteurer angemeldet."
    lines = []
    for uid, data in players.items():
        icon = ROLE_ICONS.get(data["role"], "•")
        status = "💀 K.O." if data["hp"] <= 0 else f"❤️ {data['hp']}/{data['max_hp']}"
        lines.append(f"{icon} **{data['name']}** — {data['role']} — {status}")
    return "\n".join(lines)

def party_lobby_embed(lobby: dict) -> discord.Embed:
    embed = discord.Embed(
        title="⚔️ KI-Catnip — Kampfgruppe",
        description="Meldet euch für den nächsten Bosskampf an. Catnip zählt schon mal die Pflaster. 🐾",
    )
    embed.add_field(name="👥 Gruppe", value=_party_members_text(lobby["players"]), inline=False)
    embed.add_field(
        name="📋 Rollen",
        value="🛡️ **Tank** • 💚 **Heiler** • ⚔️ **DPS**\nJeder Spieler kann genau eine Rolle wählen.",
        inline=False,
    )
    embed.set_footer(text=f"Angemeldet: {len(lobby['players'])}/8")
    return embed

class BossPartyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=1800)

    async def _join(self, interaction: discord.Interaction, role: str):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return
        key = _party_lobby_key(interaction.guild.id, interaction.channel_id)
        lobby = boss_party_lobbies.get(key)
        if not lobby:
            await interaction.response.send_message("Diese Anmeldung ist nicht mehr aktiv.", ephemeral=True)
            return
        if interaction.user.id not in lobby["players"] and len(lobby["players"]) >= 8:
            await interaction.response.send_message("Die Kampfgruppe ist bereits voll.", ephemeral=True)
            return

        lobby["players"][interaction.user.id] = {
            "name": interaction.user.display_name,
            "role": role,
            "hp": 100,
            "max_hp": 100,
        }
        await interaction.response.edit_message(embed=party_lobby_embed(lobby), view=self)

    @discord.ui.button(label="Tank", emoji="🛡️", style=discord.ButtonStyle.primary)
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "Tank")

    @discord.ui.button(label="Heiler", emoji="💚", style=discord.ButtonStyle.success)
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "Heiler")

    @discord.ui.button(label="DPS", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction, "DPS")

    @discord.ui.button(label="Abmelden", emoji="🚪", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return
        key = _party_lobby_key(interaction.guild.id, interaction.channel_id)
        lobby = boss_party_lobbies.get(key)
        if not lobby:
            await interaction.response.send_message("Diese Anmeldung ist nicht mehr aktiv.", ephemeral=True)
            return
        lobby["players"].pop(interaction.user.id, None)
        await interaction.response.edit_message(embed=party_lobby_embed(lobby), view=self)

@client.tree.command(name="bossgruppe", description="Öffnet die Anmeldung für eine Bosskampf-Gruppe.")
async def bossgruppe(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur KI-Catnip-Administratoren dürfen eine Kampfgruppe eröffnen.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return
    key = _party_lobby_key(interaction.guild.id, interaction.channel_id)
    lobby = {"players": {}}
    boss_party_lobbies[key] = lobby
    await interaction.response.send_message(embed=party_lobby_embed(lobby), view=BossPartyView())



def _boss_key(guild_id: int, channel_id: int):
    return (guild_id, channel_id)


def _hp_bar(current: int, maximum: int, bars: int = 10) -> str:
    if maximum <= 0:
        return "░" * bars
    filled = round(max(0, min(current, maximum)) / maximum * bars)
    return "█" * filled + "░" * (bars - filled)


def boss_embed(state: dict, *, message: str | None = None) -> discord.Embed:
    phase = state["phases"][state["phase"]]
    boss_pct = round(state["hp"] / state["max_hp"] * 100) if state["max_hp"] else 0
    party_pct = round(state["party_hp"] / state["party_max_hp"] * 100) if state["party_max_hp"] else 0

    embed = discord.Embed(
        title=f"⚔️ Bosskampf — {state['name']}",
        description=message or "Die Arena bebt. Catnip übernimmt die Spielleitung. 🐾",
    )
    embed.add_field(
        name="❤️ Boss-HP",
        value=(
            f"`{state['hp']}/{state['max_hp']}`  **{boss_pct}%**\n"
            f"`{_hp_bar(state['hp'], state['max_hp'])}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="💚 Party-HP",
        value=(
            f"`{state['party_hp']}/{state['party_max_hp']}`  **{party_pct}%**\n"
            f"`{_hp_bar(state['party_hp'], state['party_max_hp'])}`"
        ),
        inline=False,
    )
    embed.add_field(name="🔥 Aktuelle Phase", value=phase["name"], inline=False)
    embed.add_field(name="⚡ Attacke", value=phase["attack"], inline=False)
    mech_name, mech_hint = MECHANIC_INFO.get(phase.get("mechanic_type", "aoe"), ("⚙️ Mechanik", ""))
    embed.add_field(name="⚙️ Mechanik-Typ", value=f"**{mech_name}** — {mech_hint}", inline=False)
    if state.get("players"):
        embed.add_field(name="👥 Party", value=_party_members_text(state["players"]), inline=False)
    embed.add_field(name="❓ Mechanik", value=phase["question"], inline=False)

    options = "\n".join(f"**{key})** {label}" for key, label in phase["choices"])
    embed.add_field(name="🎯 Auswahl", value=options, inline=False)
    embed.set_footer(
        text=(
            f"Richtige Antworten: {state['correct']} • Fehler: {state['wrong']} "
            f"• Schaden richtet sich nach Mechanik und Ziel"
        )
    )
    return embed


async def finish_boss_victory(interaction: discord.Interaction, key, state):
    active_boss_battles.pop(key, None)

    reward_lines = []
    players = state.get("players", {})

    if players:
        for uid, player in players.items():
            result = _award_profile_points(
                int(uid),
                player["name"],
                REWARD_BOSS_WIN,
                stat="boss_wins",
            )
            reward_lines.append(
                _reward_line(player["name"], result, REWARD_BOSS_WIN)
            )
    else:
        # Fallback für Tests/Kämpfe ohne /bossgruppe:
        result = _award_profile_points(
            interaction.user.id,
            interaction.user.display_name,
            REWARD_BOSS_WIN,
            stat="boss_wins",
        )
        reward_lines.append(
            _reward_line(interaction.user.display_name, result, REWARD_BOSS_WIN)
        )

    await _persist_rewards(interaction.guild)

    embed = discord.Embed(
        title=f"🏆 VICTORY — {state['name']} besiegt!",
        description=(
            f"Die Gruppe hat **{state['correct']}** Mechaniken erfolgreich gelöst "
            f"und **{state['wrong']}** Fehler gemacht.\n\n"
            f"💚 Verbleibende Party-HP: **{state['party_hp']}/{state['party_max_hp']}**\n\n"
            "KI-Catnip schnurrt zufrieden. Mögen eure nächsten AoEs genauso höflich ausweichen. 🐱"
            + ("\n\n**🏆 Profil-Belohnungen**\n" + "\n".join(reward_lines) if reward_lines else "")
        ),
    )
    await interaction.response.edit_message(embed=embed, view=None)


async def finish_boss_defeat(interaction: discord.Interaction, key, state):
    active_boss_battles.pop(key, None)
    embed = discord.Embed(
        title=f"💀 DEFEAT — {state['name']}",
        description=(
            "Die Party-HP sind auf **0** gefallen.\n\n"
            f"Richtige Antworten: **{state['correct']}** • Fehler: **{state['wrong']}**\n\n"
            "Die Arena verstummt. Catnip notiert: Vielleicht beim nächsten Pull etwas weniger Boden tanken. 🐾"
        ),
    )
    await interaction.response.edit_message(embed=embed, view=None)


async def process_boss_choice(
    interaction: discord.Interaction,
    choice_key: str,
):
    if interaction.guild is None:
        await interaction.response.send_message("Bosskämpfe funktionieren nur auf einem Server.", ephemeral=True)
        return

    key = _boss_key(interaction.guild.id, interaction.channel_id)
    state = active_boss_battles.get(key)
    if not state:
        await interaction.response.send_message(
            "🐾 Dieser Bosskampf ist bereits beendet oder wurde abgebrochen.",
            ephemeral=True,
        )
        return

    players = state.get("players", {})
    player = players.get(interaction.user.id)
    if player and player.get("hp", 0) <= 0:
        await interaction.response.send_message(
            "💀 Du bist K.O. und kannst keine Mechanik beantworten. Ein Heiler muss dich zuerst wiederbeleben.",
            ephemeral=True,
        )
        return

    if interaction.user.id in state["answered_users"]:
        await interaction.response.send_message(
            "🐱 Du hast diese Mechanik bereits beantwortet. Jetzt sind die anderen Abenteurer dran.",
            ephemeral=True,
        )
        return

    phase = state["phases"][state["phase"]]
    state["answered_users"].add(interaction.user.id)

    if choice_key == phase["correct"]:
        state["correct"] += 1
        damage = max(1, state["max_hp"] // len(state["phases"]))
        state["hp"] = max(0, state["hp"] - damage)

        final_phase = state["phase"] >= len(state["phases"]) - 1
        if final_phase or state["hp"] <= 0:
            state["hp"] = 0
            await finish_boss_victory(interaction, key, state)
            return

        state["phase"] += 1
        state["answered_users"].clear()
        await interaction.response.edit_message(
            embed=boss_embed(
                state,
                message=(
                    f"✅ **{interaction.user.display_name} löst die Mechanik!** "
                    f"{state['name']} erleidet **{damage} Schaden**. "
                    "Die nächste Phase beginnt!"
                ),
            ),
            view=BossCombatView(),
        )
        return

    state["wrong"] += 1
    mechanic_type = phase.get("mechanic_type", "aoe")
    players = state.get("players", {})

    if players:
        damage_total = 0
        if mechanic_type == "tankbuster":
            living = _living_players(players)
            targets = [p for p in living if p["role"] == "Tank"] or living[:1]
            per_target = 55
        elif mechanic_type == "raidwide":
            targets = _living_players(players)
            per_target = 25
        elif mechanic_type == "stack":
            targets = _living_players(players)
            per_target = 30
        elif mechanic_type == "spread":
            targets = _living_players(players)
            per_target = 35
        elif mechanic_type == "enrage":
            targets = _living_players(players)
            per_target = 100
        else:
            living = _living_players(players)
            targets = [players.get(interaction.user.id)] if players.get(interaction.user.id) and players[interaction.user.id]["hp"] > 0 else living[:1]
            per_target = 40

        for target in [t for t in targets if t]:
            before = target["hp"]
            target["hp"] = max(0, target["hp"] - per_target)
            damage_total += before - target["hp"]

        state["party_hp"] = sum(p["hp"] for p in players.values())
    else:
        damage_total = state["wrong_damage"]
        state["party_hp"] = max(0, state["party_hp"] - state["wrong_damage"])

    if state["party_hp"] <= 0 or (players and not _living_players(players)):
        await finish_boss_defeat(interaction, key, state)
        return

    await interaction.response.edit_message(
        embed=boss_embed(
            state,
            message=(
                f"💥 **{interaction.user.display_name} liegt daneben!** "
                f"Die Party verliert **{damage_total} HP**. "
                "Die Mechanik bleibt aktiv – ein anderer Spieler kann sie noch lösen."
            ),
        ),
        view=BossCombatView(),
    )



async def _survival_action(interaction: discord.Interaction, action: str):
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    key = _boss_key(interaction.guild.id, interaction.channel_id)
    state = active_boss_battles.get(key)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft kein Bosskampf.", ephemeral=True)
        return

    players = state.get("players", {})
    actor = players.get(interaction.user.id)
    if not actor:
        await interaction.response.send_message(
            "Du bist nicht in der aktuellen Kampfgruppe angemeldet.",
            ephemeral=True,
        )
        return

    if actor["hp"] <= 0:
        await interaction.response.send_message(
            "💀 Du bist K.O. und kannst gerade keine Kampfaktion ausführen.",
            ephemeral=True,
        )
        return

    if action in {"heal", "revive"} and actor["role"] != "Heiler":
        await interaction.response.send_message(
            "💚 Diese Aktion ist nur für angemeldete Heiler verfügbar.",
            ephemeral=True,
        )
        return

    if action == "heal":
        wounded = [p for p in players.values() if 0 < p["hp"] < p["max_hp"]]
        if not wounded:
            await interaction.response.send_message("✨ Niemand benötigt gerade Heilung.", ephemeral=True)
            return
        # Heilt automatisch das am stärksten verletzte lebende Gruppenmitglied.
        target = min(wounded, key=lambda p: p["hp"] / p["max_hp"])
        amount = min(35, target["max_hp"] - target["hp"])
        target["hp"] += amount
        _sync_party_hp(state)
        await interaction.response.send_message(
            embed=boss_embed(
                state,
                message=f"💚 **{actor['name']}** heilt **{target['name']}** um **{amount} HP**.",
            ),
            view=BossCombatView(),
        )
        return

    if action == "revive":
        ko = [p for p in players.values() if p["hp"] <= 0]
        if not ko:
            await interaction.response.send_message("✨ Niemand ist K.O.", ephemeral=True)
            return
        target = ko[0]
        target["hp"] = 25
        _sync_party_hp(state)
        await interaction.response.send_message(
            embed=boss_embed(
                state,
                message=f"✨ **{actor['name']}** belebt **{target['name']}** mit **25 HP** wieder!",
            ),
            view=BossCombatView(),
        )
        return


class BossCombatView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="A", emoji="🅰️", style=discord.ButtonStyle.primary, row=0)
    async def answer_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "A")

    @discord.ui.button(label="B", emoji="🅱️", style=discord.ButtonStyle.primary, row=0)
    async def answer_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "B")

    @discord.ui.button(label="C", emoji="🇨", style=discord.ButtonStyle.secondary, row=0)
    async def answer_c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "C")

    @discord.ui.button(label="D", emoji="🇩", style=discord.ButtonStyle.secondary, row=0)
    async def answer_d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "D")

    @discord.ui.button(label="Heilen", emoji="💚", style=discord.ButtonStyle.success, row=1)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _survival_action(interaction, "heal")

    @discord.ui.button(label="Wiederbeleben", emoji="✨", style=discord.ButtonStyle.success, row=1)
    async def revive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _survival_action(interaction, "revive")


class BossAnswerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="A", emoji="🅰️", style=discord.ButtonStyle.primary)
    async def answer_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "A")

    @discord.ui.button(label="B", emoji="🅱️", style=discord.ButtonStyle.primary)
    async def answer_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "B")

    @discord.ui.button(label="C", emoji="🇨", style=discord.ButtonStyle.secondary)
    async def answer_c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "C")

    @discord.ui.button(label="D", emoji="🇩", style=discord.ButtonStyle.secondary)
    async def answer_d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await process_boss_choice(interaction, "D")


@client.tree.command(name="bossstart", description="Startet einen interaktiven KI-Catnip-Bosskampf.")
@app_commands.describe(boss="Boss auswählen")
@app_commands.choices(
    boss=[
        app_commands.Choice(name="Ifrit", value="Ifrit"),
        app_commands.Choice(name="Titan", value="Titan"),
        app_commands.Choice(name="Jupiter", value="Jupiter"),
    ]
)
async def bossstart(interaction: discord.Interaction, boss: app_commands.Choice[str]):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur freigeschaltete KI-Catnip-Administratoren dürfen Bosskämpfe starten.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message("Bosskämpfe funktionieren nur auf einem Server.", ephemeral=True)
        return

    key = _boss_key(interaction.guild.id, interaction.channel_id)
    if key in active_boss_battles:
        await interaction.response.send_message(
            "⚠️ In diesem Channel läuft bereits ein Bosskampf. Nutze zuerst `/bossstop`.",
            ephemeral=True,
        )
        return

    template = BOSS_TEMPLATES[boss.value]
    state = {
        "name": boss.value,
        "hp": template["max_hp"],
        "max_hp": template["max_hp"],
        "party_hp": template["party_hp"],
        "party_max_hp": template["party_hp"],
        "wrong_damage": template["wrong_damage"],
        "phase": 0,
        "phases": template["phases"],
        "correct": 0,
        "wrong": 0,
        "answered_users": set(),
        "players": {},
    }

    lobby = boss_party_lobbies.get(key)
    if lobby and lobby["players"]:
        state["players"] = {
            uid: dict(data) for uid, data in lobby["players"].items()
        }
        state["party_max_hp"] = sum(p["max_hp"] for p in state["players"].values())
        state["party_hp"] = sum(p["hp"] for p in state["players"].values())

    active_boss_battles[key] = state

    await interaction.response.send_message(
        embed=boss_embed(
            state,
            message=f"⚔️ **{boss.value}** betritt die Arena! Die Antwortbuttons sind bereit.",
        ),
        view=BossCombatView(),
    )


@client.tree.command(name="bossstatus", description="Zeigt den aktuellen Bosskampf in diesem Channel.")
async def bossstatus(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bosskämpfe funktionieren nur auf einem Server.", ephemeral=True)
        return

    state = active_boss_battles.get(_boss_key(interaction.guild.id, interaction.channel_id))
    if not state:
        await interaction.response.send_message("🐾 In diesem Channel läuft gerade kein Bosskampf.", ephemeral=True)
        return

    await interaction.response.send_message(embed=boss_embed(state), ephemeral=True)


@client.tree.command(name="bossantwort", description="Fallback: beantwortet die aktuelle Bossmechanik per A, B, C oder D.")
@app_commands.describe(antwort="A, B, C oder D")
async def bossantwort(interaction: discord.Interaction, antwort: str):
    choice = (antwort or "").strip().upper()
    if choice not in {"A", "B", "C", "D"}:
        await interaction.response.send_message(
            "Bitte antworte mit **A**, **B**, **C** oder **D**.",
            ephemeral=True,
        )
        return

    # Für Slash-Fallback wird die öffentliche Bossnachricht nicht editiert,
    # sondern der aktuelle Stand als neue Nachricht ausgegeben.
    if interaction.guild is None:
        await interaction.response.send_message("Bosskämpfe funktionieren nur auf einem Server.", ephemeral=True)
        return

    key = _boss_key(interaction.guild.id, interaction.channel_id)
    state = active_boss_battles.get(key)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft aktuell kein Bosskampf.", ephemeral=True)
        return

    players = state.get("players", {})
    player = players.get(interaction.user.id)
    if player and player.get("hp", 0) <= 0:
        await interaction.response.send_message(
            "💀 Du bist K.O. und kannst keine Mechanik beantworten. Ein Heiler muss dich zuerst wiederbeleben.",
            ephemeral=True,
        )
        return

    if interaction.user.id in state["answered_users"]:
        await interaction.response.send_message(
            "🐱 Du hast diese Mechanik bereits beantwortet.",
            ephemeral=True,
        )
        return

    phase = state["phases"][state["phase"]]
    state["answered_users"].add(interaction.user.id)

    if choice == phase["correct"]:
        state["correct"] += 1
        damage = max(1, state["max_hp"] // len(state["phases"]))
        state["hp"] = max(0, state["hp"] - damage)

        if state["phase"] >= len(state["phases"]) - 1 or state["hp"] <= 0:
            state["hp"] = 0
            active_boss_battles.pop(key, None)

            reward_lines = []
            players = state.get("players", {})
            if players:
                for uid, player in players.items():
                    result = _award_profile_points(
                        int(uid),
                        player["name"],
                        REWARD_BOSS_WIN,
                        stat="boss_wins",
                    )
                    reward_lines.append(
                        _reward_line(player["name"], result, REWARD_BOSS_WIN)
                    )
            else:
                result = _award_profile_points(
                    interaction.user.id,
                    interaction.user.display_name,
                    REWARD_BOSS_WIN,
                    stat="boss_wins",
                )
                reward_lines.append(
                    _reward_line(interaction.user.display_name, result, REWARD_BOSS_WIN)
                )

            await _persist_rewards(interaction.guild)

            await interaction.response.send_message(
                f"🏆 **VICTORY! {state['name']} wurde besiegt.** "
                f"Party-HP: {state['party_hp']}/{state['party_max_hp']}\n\n"
                + "**🏆 Profil-Belohnungen**\n"
                + "\n".join(reward_lines)
            )
            return

        state["phase"] += 1
        state["answered_users"].clear()
        await interaction.response.send_message(
            embed=boss_embed(state, message=f"✅ Richtig! {state['name']} erleidet **{damage} Schaden**."),
            view=BossCombatView(),
        )
        return

    state["wrong"] += 1
    mechanic_type = phase.get("mechanic_type", "aoe")
    players = state.get("players", {})

    if players:
        damage_total = 0
        if mechanic_type == "tankbuster":
            living = _living_players(players)
            targets = [p for p in living if p["role"] == "Tank"] or living[:1]
            per_target = 55
        elif mechanic_type == "raidwide":
            targets = _living_players(players)
            per_target = 25
        elif mechanic_type == "stack":
            targets = _living_players(players)
            per_target = 30
        elif mechanic_type == "spread":
            targets = _living_players(players)
            per_target = 35
        elif mechanic_type == "enrage":
            targets = _living_players(players)
            per_target = 100
        else:
            living = _living_players(players)
            targets = [players.get(interaction.user.id)] if players.get(interaction.user.id) and players[interaction.user.id]["hp"] > 0 else living[:1]
            per_target = 40

        for target in [t for t in targets if t]:
            before = target["hp"]
            target["hp"] = max(0, target["hp"] - per_target)
            damage_total += before - target["hp"]

        state["party_hp"] = sum(p["hp"] for p in players.values())
    else:
        damage_total = state["wrong_damage"]
        state["party_hp"] = max(0, state["party_hp"] - state["wrong_damage"])

    if state["party_hp"] <= 0 or (players and not _living_players(players)):
        active_boss_battles.pop(key, None)
        await interaction.response.send_message(
            f"💀 **DEFEAT!** Die Party wurde von **{state['name']}** besiegt."
        )
        return

    await interaction.response.send_message(
        embed=boss_embed(
            state,
            message=f"💥 Falsch! Die Party verliert **{damage_total} HP**.",
        ),
        view=BossCombatView(),
    )


@client.tree.command(name="bossstop", description="Beendet den Bosskampf im aktuellen Channel.")
async def bossstop(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Nur freigeschaltete KI-Catnip-Administratoren dürfen Bosskämpfe beenden.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message("Bosskämpfe funktionieren nur auf einem Server.", ephemeral=True)
        return

    key = _boss_key(interaction.guild.id, interaction.channel_id)
    state = active_boss_battles.pop(key, None)
    if not state:
        await interaction.response.send_message("🐾 Hier läuft kein Bosskampf.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"🛑 Der Bosskampf gegen **{state['name']}** wurde beendet."
    )


@client.tree.command(
    name="catnip",
    description="Zeigt, wer KI-Catnip ist und wie sein Charakter tickt."
)
async def catnip_info(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"🐱 {BOT_NAME} — Charakterprofil",
        description=(
            "Euer digitaler Eorzea-Begleiter der **Schattenflauscher**: "
            "hilfsbereit, neugierig, ein kleines bisschen frech und mit genug "
            "Catnip im Äther für lange Abenteuer."
        ),
    )
    embed.add_field(
        name="💜 Persönlichkeit",
        value="Freundlich • neugierig • leicht verspielt • FFXIV-begeistert",
        inline=False,
    )
    embed.add_field(
        name="📚 Was ich ernst nehme",
        value=(
            "Zuverlässige FFXIV-Antworten, euren persönlichen Spoilerstand "
            "und die Trennung zwischen Fakten und erfundener Event-Lore."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐾 Kleine Eigenheiten",
        value=(
            "Situationsabhängige Reaktionen, gelegentliche Katzen- und "
            "Eorzea-Anspielungen — Wissen kommt aber immer vor Katzenchaos."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Spoilerschutz",
        value=(
            "Der mit `/fortschritt` gesetzte Story-Stand bleibt maßgeblich. "
            "`/spoiler an` gibt spätere Storydetails nur auf Wunsch frei."
        ),
        inline=False,
    )
    embed.set_footer(text="KI-Catnip • Schattenflauscher • Stufe 3")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# EVENT-ADMIN-DASHBOARD
# ============================================================

def event_admin_overview_embed(interaction: discord.Interaction) -> discord.Embed:
    guild_id = interaction.guild.id if interaction.guild else 0
    channel_id = interaction.channel_id
    key = (guild_id, channel_id)

    signup = active_event_signups.get(key)
    riddle = active_riddle_events.get(key)
    boss = active_boss_battles.get(key)
    lobby = boss_party_lobbies.get(key)

    embed = discord.Embed(
        title="🎛️ KI-Catnip — Event-Admin-Zentrale",
        description=(
            "Hier findest du die wichtigsten Event-Werkzeuge an einem Ort. "
            "Die Buttons starten oder verwalten die Systeme direkt im aktuellen Channel."
        ),
    )

    embed.add_field(
        name="📅 Event-Anmeldung",
        value=(
            "`/eventerstellen` · `/eventstatus` · `/eventliste` · `/eventbeenden`\n"
            + ("🟢 Anmeldung läuft" if signup else "⚪ Keine aktive Anmeldung")
        ),
        inline=False,
    )
    embed.add_field(
        name="🧩 Rätsel-Events",
        value=(
            "`/raetselevent` · `/raetselantwort` · `/raetselhinweis` · "
            "`/raetselloesung` · `/raetselstatus` · `/raetselstop`\n"
            + ("🟢 Rätsel-Event läuft" if riddle else "⚪ Kein aktives Rätsel-Event")
        ),
        inline=False,
    )
    embed.add_field(
        name="⚔️ Bosskämpfe",
        value=(
            "`/bossgruppe` · `/bossstart` · `/bossstatus` · `/bossstop`\n"
            + ("🟢 Bosskampf läuft" if boss else "⚪ Kein aktiver Bosskampf")
            + (" · 👥 Gruppe vorhanden" if lobby and lobby.get("players") else "")
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆 Event-Profile",
        value=(
            "`/profil` · `/rangliste` · `/belohnungen` · `/punkte`\n"
            "Rätsel- und Bossbelohnungen werden weiterhin automatisch gespeichert."
        ),
        inline=False,
    )
    embed.add_field(
        name="🐾 Direktsteuerung",
        value=(
            "Über die Buttons unten kannst du **Anmeldungen, Rätsel und Bosskämpfe "
            "direkt erstellen** oder den aktuellen Eventstatus prüfen."
        ),
        inline=False,
    )
    embed.set_footer(text="Nur für freigeschaltete KI-Catnip-Administratoren")
    return embed


class EventCreateModal(discord.ui.Modal, title="📅 Event-Anmeldung erstellen"):
    event_title = discord.ui.TextInput(
        label="Eventtitel",
        placeholder="z. B. Schattenflauscher Prüfungsabend",
        max_length=100,
    )
    when = discord.ui.TextInput(
        label="Termin",
        placeholder="z. B. Samstag 20:00 Uhr",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Beschreibung",
        placeholder="Kurze Beschreibung des Events",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=700,
    )
    max_players = discord.ui.TextInput(
        label="Max. aktive Spieler",
        placeholder="8",
        default="8",
        max_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return

        try:
            max_count = int(str(self.max_players).strip())
            if max_count < 0 or max_count > 50:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Max. Spieler muss eine Zahl zwischen 0 und 50 sein.",
                ephemeral=True,
            )
            return

        key = _event_key(interaction.guild.id, interaction.channel_id)
        if key in active_event_signups:
            await interaction.response.send_message(
                "⚠️ In diesem Channel läuft bereits eine Event-Anmeldung.",
                ephemeral=True,
            )
            return

        state = {
            "title": str(self.event_title).strip(),
            "when": str(self.when).strip(),
            "description": str(self.description).strip(),
            "max_players": max_count,
            "creator_id": interaction.user.id,
            "creator_name": interaction.user.display_name,
            "signups": {},
        }
        active_event_signups[key] = state

        await interaction.response.send_message(
            embed=event_signup_embed(state),
            view=EventSignupView(),
        )


class RiddleCreateModal(discord.ui.Modal, title="🧩 Rätsel-Event starten"):
    theme = discord.ui.TextInput(
        label="Thema",
        placeholder="z. B. Verfluchte Ruinen unter Gridania",
        max_length=120,
    )
    stations = discord.ui.TextInput(
        label="Stationen (1–5)",
        placeholder="3",
        default="3",
        max_length=1,
    )
    difficulty = discord.ui.TextInput(
        label="Schwierigkeit",
        placeholder="Leicht / Mittel / Schwer / Extrem",
        default="Mittel",
        max_length=10,
    )
    endboss = discord.ui.TextInput(
        label="Endboss",
        placeholder="Keiner / Ifrit / Titan / Jupiter",
        default="Keiner",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return

        try:
            count = int(str(self.stations).strip())
            if count < 1 or count > 5:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Stationen muss zwischen 1 und 5 liegen.",
                ephemeral=True,
            )
            return

        difficulty = str(self.difficulty).strip().capitalize()
        if difficulty not in {"Leicht", "Mittel", "Schwer", "Extrem"}:
            await interaction.response.send_message(
                "⚠️ Schwierigkeit: Leicht, Mittel, Schwer oder Extrem.",
                ephemeral=True,
            )
            return

        boss_raw = str(self.endboss).strip().lower()
        boss_map = {
            "keiner": "none",
            "kein": "none",
            "none": "none",
            "ifrit": "Ifrit",
            "titan": "Titan",
            "jupiter": "Jupiter",
        }
        boss_name = boss_map.get(boss_raw)
        if boss_name is None:
            await interaction.response.send_message(
                "⚠️ Endboss muss Keiner, Ifrit, Titan oder Jupiter sein.",
                ephemeral=True,
            )
            return

        key = _riddle_key(interaction.guild.id, interaction.channel_id)
        if key in active_riddle_events:
            await interaction.response.send_message(
                "⚠️ In diesem Channel läuft bereits ein Rätsel-Event.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        theme = str(self.theme).strip()
        stations = await generate_ai_riddle_stations(
            interaction.channel_id,
            interaction.user.display_name,
            theme,
            count,
            difficulty,
        )
        used_source = "KI-generiert"

        if not stations:
            stations = _fallback_riddle_stations(theme, min(count, 3))
            used_source = "Preset-Fallback"

        state = {
            "theme": theme,
            "stations": stations,
            "index": 0,
            "score": 0,
            "wrong": 0,
            "hints_used_current": 0,
            "solved_by": [],
            "solver_ids": {},
            "source": used_source,
            "difficulty": difficulty,
            "endboss": boss_name,
        }
        active_riddle_events[key] = state

        boss_text = boss_name if boss_name != "none" else "Keiner"
        await interaction.followup.send(
            embed=riddle_event_embed(
                state,
                message=(
                    f"🧩 **{theme}** beginnt!\n"
                    f"Quelle: **{used_source}** • Schwierigkeit: **{difficulty}** "
                    f"• Endboss: **{boss_text}**"
                ),
            )
        )


class BossStartModal(discord.ui.Modal, title="⚔️ Bosskampf starten"):
    boss = discord.ui.TextInput(
        label="Boss",
        placeholder="Ifrit / Titan / Jupiter",
        default="Jupiter",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return

        boss_map = {
            "ifrit": "Ifrit",
            "titan": "Titan",
            "jupiter": "Jupiter",
        }
        boss_name = boss_map.get(str(self.boss).strip().lower())

        if not boss_name:
            await interaction.response.send_message(
                "⚠️ Verfügbare Bosse: Ifrit, Titan oder Jupiter.",
                ephemeral=True,
            )
            return

        key = _boss_key(interaction.guild.id, interaction.channel_id)
        if key in active_boss_battles:
            await interaction.response.send_message(
                "⚠️ In diesem Channel läuft bereits ein Bosskampf.",
                ephemeral=True,
            )
            return

        template = BOSS_TEMPLATES[boss_name]
        state = {
            "name": boss_name,
            "hp": template["max_hp"],
            "max_hp": template["max_hp"],
            "party_hp": template["party_hp"],
            "party_max_hp": template["party_hp"],
            "wrong_damage": template["wrong_damage"],
            "phase": 0,
            "phases": template["phases"],
            "correct": 0,
            "wrong": 0,
            "answered_users": set(),
            "players": {},
        }

        lobby = boss_party_lobbies.get(key)
        if lobby and lobby["players"]:
            state["players"] = {
                uid: dict(data) for uid, data in lobby["players"].items()
            }
            state["party_max_hp"] = sum(p["max_hp"] for p in state["players"].values())
            state["party_hp"] = sum(p["hp"] for p in state["players"].values())

        active_boss_battles[key] = state

        await interaction.response.send_message(
            embed=boss_embed(
                state,
                message=f"⚔️ **{boss_name}** betritt die Arena! Die Antwortbuttons sind bereit.",
            ),
            view=BossCombatView(),
        )


class EventManageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message(
                "🔒 Dieses Menü ist nur für freigeschaltete Event-Admins.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Anmeldung schließen", emoji="📅", style=discord.ButtonStyle.secondary)
    async def close_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        state = active_event_signups.pop(
            _event_key(interaction.guild.id, interaction.channel_id), None
        )
        await interaction.response.send_message(
            "✅ Event-Anmeldung geschlossen." if state else "ℹ️ Keine aktive Event-Anmeldung.",
            ephemeral=True,
        )

    @discord.ui.button(label="Rätsel stoppen", emoji="🧩", style=discord.ButtonStyle.secondary)
    async def stop_riddle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        state = active_riddle_events.pop(
            _riddle_key(interaction.guild.id, interaction.channel_id), None
        )
        await interaction.response.send_message(
            "✅ Rätsel-Event beendet." if state else "ℹ️ Kein aktives Rätsel-Event.",
            ephemeral=True,
        )

    @discord.ui.button(label="Boss stoppen", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def stop_boss(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        state = active_boss_battles.pop(
            _boss_key(interaction.guild.id, interaction.channel_id), None
        )
        await interaction.response.send_message(
            "✅ Bosskampf beendet." if state else "ℹ️ Kein aktiver Bosskampf.",
            ephemeral=True,
        )


class EventAdminDashboard(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message(
                "🔒 Dieses Dashboard ist nur für freigeschaltete Event-Admins.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Event erstellen", emoji="📅", style=discord.ButtonStyle.primary, row=0)
    async def create_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EventCreateModal())

    @discord.ui.button(label="Rätsel starten", emoji="🧩", style=discord.ButtonStyle.primary, row=0)
    async def create_riddle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RiddleCreateModal())

    @discord.ui.button(label="Bossgruppe", emoji="👥", style=discord.ButtonStyle.secondary, row=0)
    async def create_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        key = _party_lobby_key(interaction.guild.id, interaction.channel_id)
        lobby = {"players": {}}
        boss_party_lobbies[key] = lobby
        await interaction.response.send_message(
            embed=party_lobby_embed(lobby),
            view=BossPartyView(),
        )

    @discord.ui.button(label="Boss starten", emoji="⚔️", style=discord.ButtonStyle.danger, row=0)
    async def create_boss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BossStartModal())

    @discord.ui.button(label="Status aktualisieren", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=event_admin_overview_embed(interaction),
            view=self,
        )

    @discord.ui.button(label="Teilnehmerliste", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def participant_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return

        state = active_event_signups.get(
            _event_key(interaction.guild.id, interaction.channel_id)
        )
        if not state:
            await interaction.response.send_message(
                "📅 In diesem Channel läuft keine Event-Anmeldung.",
                ephemeral=True,
            )
            return

        lines = []
        for role in ("Tank", "Heiler", "DPS", "Dabei", "Ersatzbank"):
            members = [
                f"<@{uid}>"
                for uid, data in state["signups"].items()
                if data.get("role") == role
            ]
            lines.append(
                f"{EVENT_ROLE_ICONS[role]} **{role}:** "
                + (", ".join(members) if members else "—")
            )

        await interaction.response.send_message(
            f"📋 **Teilnehmerliste — {state['title']}**\n\n" + "\n".join(lines),
            ephemeral=True,
        )

    @discord.ui.button(label="Verwalten / Stoppen", emoji="🛠️", style=discord.ButtonStyle.secondary, row=1)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🛠️ **Aktive Eventsysteme verwalten**",
            view=EventManageView(),
            ephemeral=True,
        )


@client.tree.command(
    name="eventadmin",
    description="Öffnet die Event-Admin-Zentrale von KI-Catnip."
)
async def eventadmin(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Diese Event-Zentrale ist nur für freigeschaltete KI-Catnip-Administratoren verfügbar.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=event_admin_overview_embed(interaction),
        view=EventAdminDashboard(),
        ephemeral=True,
    )



# ============================================================
# STUFE 9 — SCHATTENPFOTEN-WISSENSDATENBANK
# Eigene FC-/Event-Lore, strikt getrennt von offizieller FFXIV-Lore
# ============================================================

SHADOWPAW_DB_MARKER = "KI_CATNIP_SHADOWPAW_DB_V1"
shadowpaw_knowledge = {}

SHADOWPAW_CATEGORIES = {
    "charakter": ("👤", "Charakter"),
    "ort": ("🗺️", "Ort"),
    "boss": ("⚔️", "Boss"),
    "gegenstand": ("💎", "Gegenstand"),
    "fraktion": ("🛡️", "Fraktion"),
    "ereignis": ("📜", "Ereignis"),
    "sonstiges": ("🐾", "Sonstiges"),
}


def _shadowpaw_guild_store(guild_id: int) -> dict:
    return shadowpaw_knowledge.setdefault(str(guild_id), {})


def _shadowpaw_normalize_category(value: str) -> str:
    value = (value or "").strip().lower()
    aliases = {
        "char": "charakter",
        "character": "charakter",
        "location": "ort",
        "place": "ort",
        "item": "gegenstand",
        "faction": "fraktion",
        "event": "ereignis",
        "other": "sonstiges",
    }
    value = aliases.get(value, value)
    return value if value in SHADOWPAW_CATEGORIES else "sonstiges"


def _shadowpaw_entry_key(name: str) -> str:
    return " ".join((name or "").lower().split())


def _shadowpaw_entry_embed(entry: dict) -> discord.Embed:
    category = entry.get("category", "sonstiges")
    emoji, category_name = SHADOWPAW_CATEGORIES.get(
        category, SHADOWPAW_CATEGORIES["sonstiges"]
    )
    embed = discord.Embed(
        title=f"{emoji} {entry.get('name', 'Unbenannter Eintrag')}",
        description=entry.get("content") or "Keine Beschreibung vorhanden.",
    )
    embed.add_field(
        name="🐾 Wissensbereich",
        value=f"Schattenpfoten-Lore • {category_name}",
        inline=True,
    )
    if entry.get("tags"):
        embed.add_field(
            name="🏷️ Tags",
            value=entry["tags"],
            inline=True,
        )
    embed.add_field(
        name="✍️ Eingetragen von",
        value=entry.get("author_name", "Unbekannt"),
        inline=True,
    )
    embed.add_field(
        name="⚠️ Lore-Trennung",
        value=(
            "Dieser Eintrag gehört zur **Schattenpfoten-Wissensdatenbank** "
            "und ist nicht automatisch offizielle Final-Fantasy-XIV-Lore."
        ),
        inline=False,
    )
    embed.set_footer(text="KI-Catnip • Schattenpfoten-Wissensdatenbank")
    return embed


def _shadowpaw_serialize_guild(guild_id: int) -> str:
    payload = {
        "marker": SHADOWPAW_DB_MARKER,
        "guild_id": guild_id,
        "entries": _shadowpaw_guild_store(guild_id),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _get_or_create_shadowpaw_data_channel(guild: discord.Guild):
    # Nutzt bevorzugt denselben versteckten Datenbereich wie die Profile.
    channel = discord.utils.get(guild.text_channels, name="ki-catnip-data")
    if channel:
        return channel

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
    }
    return await guild.create_text_channel(
        "ki-catnip-data",
        overwrites=overwrites,
        reason="KI-Catnip Datenspeicher",
    )


async def save_shadowpaw_knowledge(guild: discord.Guild):
    channel = await _get_or_create_shadowpaw_data_channel(guild)
    content = _shadowpaw_serialize_guild(guild.id)

    # Discord-Nachrichtenlimit berücksichtigen.
    if len(content) > 1800:
        raw = content.encode("utf-8")
        file = discord.File(
            io.BytesIO(raw),
            filename=f"shadowpaw_knowledge_{guild.id}.json",
        )
        await channel.send(
            f"{SHADOWPAW_DB_MARKER} FILE guild={guild.id}",
            file=file,
        )
    else:
        await channel.send(
            f"{SHADOWPAW_DB_MARKER}\n```json\n{content}\n```"
        )


async def load_shadowpaw_knowledge_for_guild(guild: discord.Guild):
    channel = discord.utils.get(guild.text_channels, name="ki-catnip-data")
    if not channel:
        _shadowpaw_guild_store(guild.id)
        return

    try:
        async for msg in channel.history(limit=200):
            if not msg.content.startswith(SHADOWPAW_DB_MARKER):
                continue

            if msg.attachments:
                attachment = msg.attachments[0]
                raw = await attachment.read()
                payload = json.loads(raw.decode("utf-8"))
            else:
                raw = msg.content.split("\n", 1)[1]
                raw = raw.replace("```json", "", 1)
                if raw.endswith("```"):
                    raw = raw[:-3]
                payload = json.loads(raw.strip())

            if payload.get("marker") == SHADOWPAW_DB_MARKER:
                shadowpaw_knowledge[str(guild.id)] = payload.get("entries", {})
                return
    except Exception as exc:
        print(f"Schattenpfoten-Wissen konnte nicht geladen werden: {exc}")

    _shadowpaw_guild_store(guild.id)


def shadowpaw_context(guild_id: int, query: str, limit: int = 6) -> str:
    entries = _shadowpaw_guild_store(guild_id)
    if not entries:
        return ""

    words = {
        word.lower()
        for word in re.findall(r"[\wÄÖÜäöüß'-]+", query or "")
        if len(word) >= 3
    }

    scored = []
    for entry in entries.values():
        haystack = " ".join([
            entry.get("name", ""),
            entry.get("category", ""),
            entry.get("tags", ""),
            entry.get("content", ""),
        ]).lower()
        score = sum(1 for word in words if word in haystack)
        if score:
            scored.append((score, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [entry for _, entry in scored[:limit]]

    if not selected:
        return ""

    blocks = []
    for entry in selected:
        blocks.append(
            f"[Schattenpfoten-Lore | {entry.get('category', 'sonstiges')}]\n"
            f"Name: {entry.get('name')}\n"
            f"Inhalt: {entry.get('content')}\n"
            f"Tags: {entry.get('tags') or '-'}"
        )

    return "\n\n".join(blocks)


class ShadowpawKnowledgeModal(discord.ui.Modal, title="🐾 Wissen hinzufügen"):
    entry_name = discord.ui.TextInput(
        label="Name / Titel",
        placeholder="z. B. Festung Nachtwacht",
        max_length=100,
    )
    category = discord.ui.TextInput(
        label="Kategorie",
        placeholder="Charakter / Ort / Boss / Gegenstand / Fraktion / Ereignis",
        default="Sonstiges",
        max_length=30,
    )
    content = discord.ui.TextInput(
        label="Beschreibung / Lore",
        placeholder="Was soll KI-Catnip darüber wissen?",
        style=discord.TextStyle.paragraph,
        max_length=1800,
    )
    tags = discord.ui.TextInput(
        label="Tags (optional)",
        placeholder="z. B. Ishgard, Blutmond, Saga",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return

        name = str(self.entry_name).strip()
        key = _shadowpaw_entry_key(name)
        store = _shadowpaw_guild_store(interaction.guild.id)

        if key in store:
            await interaction.response.send_message(
                "⚠️ Ein Eintrag mit diesem Namen existiert bereits. "
                "Nutze `/wissenbearbeiten` zum Ändern.",
                ephemeral=True,
            )
            return

        entry = {
            "name": name,
            "category": _shadowpaw_normalize_category(str(self.category)),
            "content": str(self.content).strip(),
            "tags": str(self.tags).strip(),
            "author_id": interaction.user.id,
            "author_name": interaction.user.display_name,
        }
        store[key] = entry

        try:
            await save_shadowpaw_knowledge(interaction.guild)
        except Exception as exc:
            store.pop(key, None)
            await interaction.response.send_message(
                f"❌ Speichern fehlgeschlagen: `{type(exc).__name__}`",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content="✅ In der **Schattenpfoten-Wissensdatenbank** gespeichert.",
            embed=_shadowpaw_entry_embed(entry),
            ephemeral=True,
        )


class ShadowpawKnowledgeEditModal(discord.ui.Modal, title="✏️ Wissen bearbeiten"):
    content = discord.ui.TextInput(
        label="Neue Beschreibung / Lore",
        style=discord.TextStyle.paragraph,
        max_length=1800,
    )
    tags = discord.ui.TextInput(
        label="Neue Tags (optional)",
        required=False,
        max_length=200,
    )

    def __init__(self, entry_key: str, entry: dict):
        super().__init__()
        self.entry_key = entry_key
        self.content.default = entry.get("content", "")
        self.tags.default = entry.get("tags", "")

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
            return

        store = _shadowpaw_guild_store(interaction.guild.id)
        entry = store.get(self.entry_key)
        if not entry:
            await interaction.response.send_message(
                "❌ Der Eintrag wurde nicht mehr gefunden.",
                ephemeral=True,
            )
            return

        old_content = entry.get("content", "")
        old_tags = entry.get("tags", "")
        entry["content"] = str(self.content).strip()
        entry["tags"] = str(self.tags).strip()
        entry["author_id"] = interaction.user.id
        entry["author_name"] = interaction.user.display_name

        try:
            await save_shadowpaw_knowledge(interaction.guild)
        except Exception as exc:
            entry["content"] = old_content
            entry["tags"] = old_tags
            await interaction.response.send_message(
                f"❌ Speichern fehlgeschlagen: `{type(exc).__name__}`",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content="✅ Schattenpfoten-Wissen aktualisiert.",
            embed=_shadowpaw_entry_embed(entry),
            ephemeral=True,
        )


@client.tree.command(
    name="wissen",
    description="Sucht in der Schattenpfoten-Wissensdatenbank."
)
@app_commands.describe(suche="Name, Begriff oder Tag")
async def wissen(interaction: discord.Interaction, suche: str):
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    store = _shadowpaw_guild_store(interaction.guild.id)
    query = suche.strip().lower()

    exact = store.get(_shadowpaw_entry_key(suche))
    if exact:
        await interaction.response.send_message(
            embed=_shadowpaw_entry_embed(exact),
            ephemeral=True,
        )
        return

    matches = []
    for entry in store.values():
        haystack = " ".join([
            entry.get("name", ""),
            entry.get("category", ""),
            entry.get("tags", ""),
            entry.get("content", ""),
        ]).lower()
        if query in haystack:
            matches.append(entry)

    if not matches:
        await interaction.response.send_message(
            f"🐾 Zu **{suche}** habe ich in der Schattenpfoten-Wissensdatenbank nichts gefunden.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🐾 Schattenpfoten-Wissensdatenbank",
        description=f"Treffer für **{suche}**",
    )
    for entry in matches[:10]:
        emoji, cat_name = SHADOWPAW_CATEGORIES.get(
            entry.get("category", "sonstiges"),
            SHADOWPAW_CATEGORIES["sonstiges"],
        )
        excerpt = entry.get("content", "")
        if len(excerpt) > 250:
            excerpt = excerpt[:247] + "..."
        embed.add_field(
            name=f"{emoji} {entry.get('name')} — {cat_name}",
            value=excerpt or "Keine Beschreibung",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(
    name="wissenhinzufuegen",
    description="Admin: fügt der Schattenpfoten-Wissensdatenbank einen Eintrag hinzu."
)
async def wissenhinzufuegen(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
        return
    await interaction.response.send_modal(ShadowpawKnowledgeModal())


@client.tree.command(
    name="wissenbearbeiten",
    description="Admin: bearbeitet einen Eintrag der Schattenpfoten-Wissensdatenbank."
)
@app_commands.describe(name="Exakter Name des Eintrags")
async def wissenbearbeiten(interaction: discord.Interaction, name: str):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    key = _shadowpaw_entry_key(name)
    entry = _shadowpaw_guild_store(interaction.guild.id).get(key)
    if not entry:
        await interaction.response.send_message(
            f"❌ Kein Eintrag namens **{name}** gefunden.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        ShadowpawKnowledgeEditModal(key, entry)
    )


@client.tree.command(
    name="wissenloeschen",
    description="Admin: löscht einen Eintrag aus der Schattenpfoten-Wissensdatenbank."
)
@app_commands.describe(name="Exakter Name des Eintrags", bestaetigung="Zum Löschen: LOESCHEN")
async def wissenloeschen(
    interaction: discord.Interaction,
    name: str,
    bestaetigung: str,
):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return
    if bestaetigung.strip().upper() not in {"LOESCHEN", "LÖSCHEN"}:
        await interaction.response.send_message(
            "⚠️ Abgebrochen. Gib bei `bestaetigung` **LOESCHEN** ein.",
            ephemeral=True,
        )
        return

    store = _shadowpaw_guild_store(interaction.guild.id)
    key = _shadowpaw_entry_key(name)
    entry = store.pop(key, None)
    if not entry:
        await interaction.response.send_message(
            f"❌ Kein Eintrag namens **{name}** gefunden.",
            ephemeral=True,
        )
        return

    try:
        await save_shadowpaw_knowledge(interaction.guild)
    except Exception as exc:
        store[key] = entry
        await interaction.response.send_message(
            f"❌ Löschen konnte nicht gespeichert werden: `{type(exc).__name__}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🗑️ **{entry['name']}** wurde aus der Schattenpfoten-Wissensdatenbank gelöscht.",
        ephemeral=True,
    )


@client.tree.command(
    name="wissensliste",
    description="Zeigt die Einträge der Schattenpfoten-Wissensdatenbank."
)
@app_commands.describe(kategorie="Optional: Charakter, Ort, Boss, Gegenstand, Fraktion, Ereignis")
async def wissensliste(interaction: discord.Interaction, kategorie: str = ""):
    if interaction.guild is None:
        await interaction.response.send_message("Nur auf einem Server verfügbar.", ephemeral=True)
        return

    store = _shadowpaw_guild_store(interaction.guild.id)
    category_filter = _shadowpaw_normalize_category(kategorie) if kategorie.strip() else None

    entries = [
        entry for entry in store.values()
        if category_filter is None or entry.get("category") == category_filter
    ]
    entries.sort(key=lambda e: e.get("name", "").lower())

    if not entries:
        await interaction.response.send_message(
            "🐾 Die Schattenpfoten-Wissensdatenbank enthält dafür noch keine Einträge.",
            ephemeral=True,
        )
        return

    lines = []
    for entry in entries[:40]:
        emoji, cat_name = SHADOWPAW_CATEGORIES.get(
            entry.get("category", "sonstiges"),
            SHADOWPAW_CATEGORIES["sonstiges"],
        )
        lines.append(f"{emoji} **{entry.get('name')}** — {cat_name}")

    if len(entries) > 40:
        lines.append(f"\n… und {len(entries) - 40} weitere Einträge.")

    embed = discord.Embed(
        title="🐾 Schattenpfoten-Wissensdatenbank",
        description="\n".join(lines),
    )
    embed.set_footer(text=f"{len(entries)} Einträge gefunden")
    await interaction.response.send_message(embed=embed, ephemeral=True)


class ShadowpawAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Wissen hinzufügen", emoji="➕", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ShadowpawKnowledgeModal())

    @discord.ui.button(label="Übersicht", emoji="📚", style=discord.ButtonStyle.primary)
    async def overview(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("Nur auf einem Server.", ephemeral=True)
            return
        store = _shadowpaw_guild_store(interaction.guild.id)
        counts = {key: 0 for key in SHADOWPAW_CATEGORIES}
        for entry in store.values():
            counts[entry.get("category", "sonstiges")] += 1
        lines = []
        for key, (emoji, label) in SHADOWPAW_CATEGORIES.items():
            lines.append(f"{emoji} **{label}:** {counts[key]}")
        await interaction.response.send_message(
            "🐾 **Schattenpfoten-Wissensdatenbank**\n\n"
            + "\n".join(lines)
            + f"\n\n📚 **Gesamt:** {len(store)}",
            ephemeral=True,
        )


@client.tree.command(
    name="wissensadmin",
    description="Admin-Menü der Schattenpfoten-Wissensdatenbank."
)
async def wissensadmin(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message("🔒 Keine Berechtigung.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🐾 Schattenpfoten-Wissensdatenbank",
        description=(
            "Eigene Charaktere, Orte, Bosse, Gegenstände, Fraktionen und "
            "Ereignisse verwalten.\n\n"
            "Dieses Wissen wird **getrennt von offizieller FFXIV-Lore** gespeichert."
        ),
    )
    embed.add_field(
        name="📚 Lesen",
        value="`/wissen` · `/wissensliste`",
        inline=False,
    )
    embed.add_field(
        name="🛠️ Admin",
        value="`/wissenhinzufuegen` · `/wissenbearbeiten` · `/wissenloeschen`",
        inline=False,
    )
    await interaction.response.send_message(
        embed=embed,
        view=ShadowpawAdminView(),
        ephemeral=True,
    )



# ============================================================
# STUFE 10 — KI-CATNIP SYSTEMDIAGNOSE
# ============================================================

def _diag_icon(ok: bool, warn: bool = False) -> str:
    if ok:
        return "✅"
    if warn:
        return "⚠️"
    return "❌"


def _diag_line(label: str, status: str, detail: str = "") -> str:
    return f"{status} **{label}**" + (f" — {detail}" if detail else "")


def _diagnose_permissions(guild: discord.Guild):
    me = guild.me
    if me is None:
        return [
            _diag_line("Bot-Mitglied", "❌", "Bot-Mitglied konnte nicht aufgelöst werden.")
        ]

    perms = me.guild_permissions

    checks = [
        ("Kanäle ansehen", perms.view_channel),
        ("Nachrichten senden", perms.send_messages),
        ("Nachrichtenverlauf lesen", perms.read_message_history),
        ("Kanäle verwalten", perms.manage_channels),
        ("Nachrichten verwalten", perms.manage_messages),
        ("Rollen verwalten", perms.manage_roles),
        ("Dateien anhängen", perms.attach_files),
        ("Links einbetten", perms.embed_links),
    ]

    lines = []
    for label, ok in checks:
        lines.append(
            _diag_line(
                label,
                "✅" if ok else "❌",
                "vorhanden" if ok else "fehlt",
            )
        )

    if perms.administrator:
        lines.append(
            _diag_line(
                "Administrator",
                "✅",
                "aktiv — dadurch sind praktisch alle Serverrechte abgedeckt",
            )
        )
    else:
        lines.append(
            _diag_line(
                "Administrator",
                "⚠️",
                "nicht aktiv — okay, wenn die Einzelrechte korrekt gesetzt sind",
            )
        )

    return lines


def _diagnose_config():
    lines = []

    lines.append(
        _diag_line(
            "Discord Token",
            "✅" if bool(DISCORD_TOKEN) else "❌",
            "gesetzt" if DISCORD_TOKEN else "fehlt",
        )
    )
    lines.append(
        _diag_line(
            "Gemini API Key",
            "✅" if bool(GEMINI_API_KEY) else "❌",
            "gesetzt" if GEMINI_API_KEY else "fehlt",
        )
    )
    lines.append(
        _diag_line(
            "Gemini Modell",
            "✅" if bool(GEMINI_MODEL) else "❌",
            GEMINI_MODEL or "nicht gesetzt",
        )
    )
    lines.append(
        _diag_line(
            "Gemini Free Tier",
            "✅" if GEMINI_FREE_TIER else "⚠️",
            "aktiv" if GEMINI_FREE_TIER else "nicht aktiv",
        )
    )
    lines.append(
        _diag_line(
            "Websuche",
            "✅" if WEB_SEARCH else "⚠️",
            "aktiv" if WEB_SEARCH else "deaktiviert",
        )
    )

    return lines


def _diagnose_intents():
    intents = client.intents
    return [
        _diag_line(
            "Message Content Intent",
            "✅" if intents.message_content else "❌",
            "aktiv" if intents.message_content else "deaktiviert",
        ),
        _diag_line(
            "Server Members Intent",
            "✅" if intents.members else "❌",
            "aktiv" if intents.members else "deaktiviert",
        ),
    ]


def _diagnose_data(guild: discord.Guild):
    lines = []

    data_channel = discord.utils.get(
        guild.text_channels,
        name=PROFILE_DATA_CHANNEL_NAME,
    )

    lines.append(
        _diag_line(
            "Datenchannel",
            "✅" if data_channel else "❌",
            f"#{PROFILE_DATA_CHANNEL_NAME} vorhanden"
            if data_channel
            else f"#{PROFILE_DATA_CHANNEL_NAME} fehlt",
        )
    )

    lines.append(
        _diag_line(
            "Spielerprofile",
            "✅",
            f"{len(player_profiles)} Profil(e) im Speicher",
        )
    )

    shadowpaw_store = _shadowpaw_guild_store(guild.id)
    lines.append(
        _diag_line(
            "Schattenpfoten-Wissensdatenbank",
            "✅",
            f"{len(shadowpaw_store)} Eintrag/Einträge geladen",
        )
    )

    return lines


def _diagnose_features(guild: discord.Guild, channel_id: int):
    key = (guild.id, channel_id)

    lines = [
        _diag_line(
            "Private Channels",
            "✅" if PRIVATE_CHANNELS_ENABLED else "⚠️",
            "aktiv" if PRIVATE_CHANNELS_ENABLED else "deaktiviert",
        ),
        _diag_line(
            "Rückkehr-Begrüßung",
            "✅" if RETURN_GREETING_ENABLED else "⚠️",
            "aktiv" if RETURN_GREETING_ENABLED else "deaktiviert",
        ),
        _diag_line(
            "Persönlicher Spoilerschutz",
            "✅",
            "verfügbar über /fortschritt und /spoiler",
        ),
        _diag_line(
            "Event-Anmeldung",
            "✅",
            "aktiv" if key in active_event_signups else "bereit",
        ),
        _diag_line(
            "Rätsel-System",
            "✅",
            "aktiv" if key in active_riddle_events else "bereit",
        ),
        _diag_line(
            "Boss-System",
            "✅",
            "aktiv" if key in active_boss_battles else "bereit",
        ),
        _diag_line(
            "Bossgruppe",
            "✅",
            "vorhanden"
            if key in boss_party_lobbies
            and boss_party_lobbies[key].get("players")
            else "bereit",
        ),
        _diag_line(
            "RP-System",
            "✅",
            "bereit",
        ),
        _diag_line(
            "Event-Admin-Dashboard",
            "✅",
            "bereit über /eventadmin",
        ),
    ]

    return lines


def _diagnose_commands():
    try:
        commands = client.tree.get_commands()
        return [
            _diag_line(
                "Slash-Commands",
                "✅" if commands else "❌",
                f"{len(commands)} lokal registriert",
            )
        ]
    except Exception as exc:
        return [
            _diag_line(
                "Slash-Commands",
                "❌",
                f"{type(exc).__name__}",
            )
        ]


def diagnose_embed(interaction: discord.Interaction) -> discord.Embed:
    guild = interaction.guild

    embed = discord.Embed(
        title="🔧 KI-Catnip — Systemdiagnose",
        description=(
            "Technischer Schnellcheck der wichtigsten KI-Catnip-Systeme.\n"
            "✅ OK · ⚠️ Hinweis · ❌ Problem"
        ),
    )

    if guild is None:
        embed.add_field(
            name="❌ Server",
            value="Die Diagnose muss auf einem Discord-Server ausgeführt werden.",
            inline=False,
        )
        return embed

    sections = [
        ("🤖 Konfiguration", _diagnose_config()),
        ("📡 Discord Intents", _diagnose_intents()),
        ("🛡️ Discord-Berechtigungen", _diagnose_permissions(guild)),
        ("💾 Daten & Wissen", _diagnose_data(guild)),
        ("⚙️ Systeme", _diagnose_features(guild, interaction.channel_id)),
        ("⌨️ Commands", _diagnose_commands()),
    ]

    for title, lines in sections:
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1021] + "..."
        embed.add_field(
            name=title,
            value=value,
            inline=False,
        )

    embed.set_footer(
        text="Stufe 10 • Keine geheimen Tokens oder API-Keys werden angezeigt"
    )
    return embed


async def _run_real_gemini_diagnostic():
    """
    Führt eine minimale echte API-Anfrage aus.
    Kein FFXIV-Prompt, keine Websuche, extrem kleine Antwort.
    """
    response = await ai.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text="Antworte ausschließlich mit dem Wort OK."
                    )
                ],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=10,
        ),
    )

    answer = (response.text or "").strip()
    return answer


class DiagnoseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_bot_admin(interaction.user.id):
            await interaction.response.send_message(
                "🔒 Die Systemdiagnose ist nur für freigeschaltete KI-Catnip-Administratoren verfügbar.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Neu prüfen",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            embed=diagnose_embed(interaction),
            view=self,
        )

    @discord.ui.button(
        label="Gemini live testen",
        emoji="🧠",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def gemini_test(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            answer = await _run_real_gemini_diagnostic()
            if answer:
                await interaction.followup.send(
                    f"✅ **Gemini-Livetest erfolgreich.**\n"
                    f"Modell: `{GEMINI_MODEL}`\n"
                    f"Antwort erhalten: `{answer[:50]}`",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "⚠️ Gemini hat geantwortet, aber keinen Text geliefert.",
                    ephemeral=True,
                )
        except Exception as exc:
            await interaction.followup.send(
                f"❌ **Gemini-Livetest fehlgeschlagen.**\n"
                f"`{type(exc).__name__}: {str(exc)[:500]}`",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Daten prüfen",
        emoji="💾",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def data_test(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.",
                ephemeral=True,
            )
            return

        data_channel = discord.utils.get(
            interaction.guild.text_channels,
            name=PROFILE_DATA_CHANNEL_NAME,
        )

        if data_channel is None:
            await interaction.response.send_message(
                f"❌ Datenchannel `#{PROFILE_DATA_CHANNEL_NAME}` wurde nicht gefunden.",
                ephemeral=True,
            )
            return

        perms = data_channel.permissions_for(interaction.guild.me)

        lines = [
            _diag_line(
                "Channel sichtbar",
                "✅" if perms.view_channel else "❌",
            ),
            _diag_line(
                "Nachrichten senden",
                "✅" if perms.send_messages else "❌",
            ),
            _diag_line(
                "Verlauf lesen",
                "✅" if perms.read_message_history else "❌",
            ),
            _diag_line(
                "Nachrichten verwalten",
                "✅" if perms.manage_messages else "⚠️",
            ),
            _diag_line(
                "Spielerprofile geladen",
                "✅",
                str(len(player_profiles)),
            ),
            _diag_line(
                "Schattenpfoten-Einträge",
                "✅",
                str(len(_shadowpaw_guild_store(interaction.guild.id))),
            ),
        ]

        await interaction.response.send_message(
            "💾 **KI-Catnip Datenprüfung**\n\n" + "\n".join(lines),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Admin-IDs prüfen",
        emoji="👑",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def admins_test(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Nur auf einem Server verfügbar.",
                ephemeral=True,
            )
            return

        lines = []
        for uid in sorted(EVENT_ADMIN_USER_IDS):
            member = interaction.guild.get_member(uid)
            if member:
                lines.append(
                    f"✅ **{member.display_name}** (`{uid}`)"
                )
            else:
                lines.append(
                    f"⚠️ `{uid}` — aktuell nicht im Servercache"
                )

        await interaction.response.send_message(
            "👑 **Freigeschaltete KI-Catnip-Admins**\n\n"
            + "\n".join(lines),
            ephemeral=True,
        )


@client.tree.command(
    name="diagnose",
    description="Admin: prüft die wichtigsten KI-Catnip-Systeme."
)
async def diagnose(interaction: discord.Interaction):
    if not is_bot_admin(interaction.user.id):
        await interaction.response.send_message(
            "🔒 Die Systemdiagnose ist nur für freigeschaltete KI-Catnip-Administratoren verfügbar.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Die Diagnose muss auf einem Discord-Server ausgeführt werden.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        embed=diagnose_embed(interaction),
        view=DiagnoseView(),
        ephemeral=True,
    )



@client.event
async def on_ready():
    for _guild in client.guilds:
        await load_shadowpaw_knowledge_for_guild(_guild)
    for guild in client.guilds:
        await load_profiles_from_discord(guild)
    for guild in client.guilds:
        await sync_event_admin_role(guild)

    print("=" * 56)
    print(f"✓ {client.user} ist online.")
    print(f"✓ Modell: {GEMINI_MODEL}")
    print(f"✓ @Mention-Fragen: aktiv")
    print(f"✓ Catnip-Persönlichkeit: Stufe 3 aktiv")
    print(f"✓ Bosskämpfe: Stufe 4.3 aktiv (Heilung + K.O. + Wiederbelebung)")
    print(f"✓ Rätsel-Events: Stufe 5.2 aktiv (KI-Rätsel + Preset-Fallback + Endboss)")
    print(f"✓ Spielerprofile: Stufe 6.2 aktiv (Auto-Rewards + Titel + Rangliste)")
    print(f"✓ Charakterprofile: Stufe 7.1 aktiv (/charaktererstellen, /charakterprofil, /charakterbearbeiten)")
    print(f"✓ RP-Spielleiter: Stufe 7.2 aktiv (/rp, /rpquest, /rpgruppe)")
    print(f"✓ Event-Anmeldung: Stufe 8.1 aktiv (/eventerstellen, Rollen-Buttons, /eventliste)")
    print(f"✓ Event-Admin-Dashboard aktiv (/eventadmin)")
    print(f"✓ Schattenpfoten-Wissensdatenbank aktiv (/wissen, /wissensadmin)")
    print(f"✓ Systemdiagnose: Stufe 10 aktiv (/diagnose)")
    print(f"✓ Spielersuche: Stufe 10.1 aktiv (/spielersuche)")
    print(f"✓ Private FFXIV-Channels: {'aktiv' if PRIVATE_CHANNELS_ENABLED else 'deaktiviert'}")
    print(f"✓ Websuche: {'aktiv' if WEB_SEARCH else 'deaktiviert'}")
    print(f"✓ Monatsbudget: {MONTHLY_BUDGET_EUR:.2f} EUR")
    print(f"✓ Modell: {GEMINI_MODEL} (sparsame Voreinstellung)")
    print(f"✓ News: nicht enthalten")
    print(f"✓ Serverstatus: nicht enthalten")
    print("=" * 56)


client.run(DISCORD_TOKEN)
