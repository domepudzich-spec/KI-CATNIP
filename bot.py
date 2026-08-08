import os
import re
import json
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
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


# Letzte Aktivität pro privatem User-Channel.
# Wird nur im Arbeitsspeicher gehalten; nach einem Bot-Neustart beginnt die
# Rückkehr-Erkennung neu.
private_user_last_activity = {}


SYSTEM_PROMPT = f"""
Du heißt {BOT_NAME}. Wenn Nutzer dich mit "{BOT_NAME}", "Catnip" oder "KI-Catnip" ansprechen, erkennst du dies als deinen Namen. Du bist ein spezialisierter deutschsprachiger Wissensassistent
für FINAL FANTASY XIV Online (FFXIV) und die Eorzea-Enzyklopädie
der Discord-Gemeinschaft „Schattenflauscher“.

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
- Spoiler sind standardmäßig VERBOTEN.
- Ohne ausdrückliche Spoilerfreigabe darfst du KEINE wichtigen Storyenthüllungen nennen.
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
    if channel_id in spoiler_enabled_channels:
        return (
            "🔓 **Spoiler sind in diesem Channel derzeit ERLAUBT.**\n"
            "Mit `/spoiler aus` aktivierst du den vollständigen Spoilerschutz wieder."
        )
    return (
        "🔒 **Spoilerschutz ist AKTIV.**\n"
        "KI-Catnip vermeidet wichtige Storyenthüllungen. "
        "Mit `/spoiler an` kannst du Spoiler für diesen Channel ausdrücklich erlauben."
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

    spoiler_instruction = (
        "SPOILERFREIGABE FÜR DIESE ANFRAGE: JA. Größere Enthüllungen weiterhin deutlich markieren."
        if allow_spoilers
        else
        "SPOILERFREIGABE FÜR DIESE ANFRAGE: NEIN. Strikter Spoilerschutz. "
        "Keine Tode, Verrate, Identitätsenthüllungen, Bossidentitäten, Schicksale, "
        "Storywendungen oder spätere Ereignisse verraten."
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
    return f"FFXIV_PRIVAT_USER_ID={member.id}"


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

    embed = discord.Embed(
        title=f"🐱 Willkommen bei KI-Catnip, {member.display_name}!",
        description=(
            f"Schön, dass du da bist! **Wie kann ich dir bei FINAL FANTASY XIV helfen?**\n\n"
            f"Frag mich einfach mit `@{BOT_NAME}` oder nutze einen meiner Slash-Commands. "
            f"Ich kann dir unter anderem bei Lore, Jobs, Kämpfen, Quests, Mounts, Minions und Guides helfen."
        ),
    )
    embed.add_field(
        name="💬 Einfach fragen",
        value=(
            f"`@{BOT_NAME} Wie funktioniert Viper?`\n"
            "`/ffxiv` – allgemeine FFXIV-Frage"
        ),
        inline=False,
    )
    embed.add_field(
        name="📚 Wissen & Gameplay",
        value=(
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
        name="🔎 Nachschlagen",
        value=(
            "`/charakter` – Spielercharakter suchen\n"
            "`/mount` – Mount-Herkunft und Freischaltung\n"
            "`/minion` – Minion-Herkunft und Freischaltung\n"
            "`/markt` – Marktbrettdaten über Universalis"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎭 Events & Spielleiter",
        value=(
            "`/quiz` – FFXIV-Quiz für Events\n"
            "`/event` – komplettes Gildenevent planen\n"
            "`/boss` – Event-Bosskampf erstellen\n"
            "`/raetsel` – einzelnes FFXIV-Rätsel erstellen"
        ),
        inline=False,
    )
    embed.add_field(
        name="📄 Guides & PDFs",
        value="`/pdf` – erstellt einen FFXIV-Guide als PDF-Datei",
        inline=False,
    )
    embed.add_field(
        name="🔒 Privater Chat",
        value=(
            "`/privatchat` – privaten FFXIV-Channel nachträglich erstellen\n"
            "`/reset` – Gesprächskontext dieses Channels löschen\n"
            "`/botinfo` – Funktionsübersicht anzeigen"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Spoilerschutz",
        value=(
            "Standardmäßig sind **alle wichtigen Storyspoiler gesperrt**.\n"
            "`/spoiler an` – Spoiler für diesen Channel erlauben\n"
            "`/spoiler aus` – Spoilerschutz wieder aktivieren\n"
            "`/spoiler status` – aktuellen Zustand prüfen"
        ),
        inline=False,
    )
    embed.set_footer(
        text="Der Channel ist nur für dich, den Bot und freigeschaltete Administratoren sichtbar."
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
            "🔓 **Spoiler wurden für diesen Channel ausdrücklich freigegeben.**\n"
            "KI-Catnip darf jetzt auch wichtige Storyenthüllungen nennen. "
            "Mit `/spoiler aus` kannst du den Schutz jederzeit wieder aktivieren.",
            ephemeral=True,
        )
        return

    if modus.value == "aus":
        spoiler_enabled_channels.discard(channel_id)
        await interaction.response.send_message(
            "🔒 **Spoilerschutz aktiviert.**\n"
            "KI-Catnip verrät ab jetzt keine wichtigen Storyenthüllungen mehr.",
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
        "🧹 **Gesprächsverlauf gelöscht.**\n🔒 Spoilerschutz wurde ebenfalls wieder aktiviert.",
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
    embed.add_field(
        name="🔒 Spoilerschutz (dieser Channel)",
        value="🔓 Spoiler erlaubt" if channel_id in spoiler_enabled_channels else "🔒 Aktiv",
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

    @discord.ui.button(label="Spoiler umschalten", emoji="🔒", style=discord.ButtonStyle.primary)
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


@client.event
async def on_ready():
    for guild in client.guilds:
        await sync_event_admin_role(guild)

    print("=" * 56)
    print(f"✓ {client.user} ist online.")
    print(f"✓ Modell: {GEMINI_MODEL}")
    print(f"✓ @Mention-Fragen: aktiv")
    print(f"✓ Private FFXIV-Channels: {'aktiv' if PRIVATE_CHANNELS_ENABLED else 'deaktiviert'}")
    print(f"✓ Websuche: {'aktiv' if WEB_SEARCH else 'deaktiviert'}")
    print(f"✓ Monatsbudget: {MONTHLY_BUDGET_EUR:.2f} EUR")
    print(f"✓ Modell: {GEMINI_MODEL} (sparsame Voreinstellung)")
    print(f"✓ News: nicht enthalten")
    print(f"✓ Serverstatus: nicht enthalten")
    print("=" * 56)


client.run(DISCORD_TOKEN)
