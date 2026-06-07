from __future__ import annotations

import os
import re
import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from functools import partial
from typing import List, Optional, Tuple

import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
IG_USER     = os.environ.get("IG_USER", "")
IG_PASS     = os.environ.get("IG_PASS", "")
_raw_group  = os.environ.get("GROUP_ID", "")
GROUP_ID    = int(_raw_group) if _raw_group else None

DOWNLOAD_DIR   = Path("/tmp/ig_downloads")
MAX_FILE_BYTES = 50 * 1024 * 1024   # 50 Mo


# ── Helpers ───────────────────────────────────────────────────────────────────
def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_username(text: str) -> Optional[str]:
    text = text.strip()
    # Nettoyer les parametres d'URL (?hl=fr, etc.)
    text = text.split("?")[0].rstrip("/")
    for pattern in (
        r"instagram\.com/([A-Za-z0-9._]+)",
        r"^@?([A-Za-z0-9._]{1,30})$",
    ):
        m = re.search(pattern, text)
        if m:
            username = m.group(1)
            if username not in ("p", "reel", "reels", "stories", "explore", "accounts", "tv", "direct"):
                return username
    return None


def human_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return "{:.0f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} Go".format(n)


def collect_media_files(directory: Path) -> Tuple[List[Path], List[str]]:
    """Collecte tous les fichiers media valides dans un dossier."""
    files: List[Path] = []
    skipped: List[str] = []
    if not directory.exists():
        return files, skipped
    for ext in ("*.mp4", "*.jpg", "*.jpeg", "*.png", "*.webp"):
        for f in directory.rglob(ext):
            try:
                size = f.stat().st_size
                if size == 0:
                    continue
                if size <= MAX_FILE_BYTES:
                    files.append(f)
                else:
                    skipped.append("{} ({} > 50 Mo)".format(f.name, human_size(size)))
            except OSError:
                pass
    files.sort()
    return files, skipped


# ── METHODE 1 : yt-dlp (posts & reels, sans login) ───────────────────────────
def _ytdlp_download(username: str, profile_dir: Path) -> List[str]:
    """
    Telecharge tous les posts et reels via yt-dlp.
    Retourne la liste des erreurs/avertissements.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--quiet",
        "--no-playlist",
        "--ignore-errors",
        "-o", str(profile_dir / "%(upload_date)s_%(id)s.%(ext)s"),
        "--merge-output-format", "mp4",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "https://www.instagram.com/{}/".format(username),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode not in (0, 1):
            errors.append("yt-dlp code {}: {}".format(result.returncode, result.stderr[:200]))
        if result.stderr:
            for line in result.stderr.splitlines():
                if "ERROR" in line.upper() and "login" not in line.lower():
                    errors.append(line[:150])
    except subprocess.TimeoutExpired:
        errors.append("yt-dlp timeout apres 5 minutes.")
    except FileNotFoundError:
        errors.append("yt-dlp non trouve. Verifier requirements.txt.")
    except Exception as exc:
        errors.append("yt-dlp exception : {}".format(exc))

    return errors


# ── METHODE 2 : instaloader avec login (stories & highlights) ─────────────────
def _build_loader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        dirname_pattern=str(DOWNLOAD_DIR / "{target}"),
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        quiet=True,
        request_timeout=30,
    )
    return L


def _instaloader_login(L: instaloader.Instaloader) -> bool:
    """Retourne True si login reussi."""
    if not (IG_USER and IG_PASS):
        return False
    try:
        L.login(IG_USER, IG_PASS)
        logger.info("Login Instagram OK : %s", IG_USER)
        return True
    except instaloader.exceptions.BadCredentialsException:
        logger.error("Login Instagram : mauvais identifiants")
        return False
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        logger.error("Login Instagram : 2FA active, impossible de se connecter")
        return False
    except Exception as exc:
        logger.warning("Login Instagram echoue : %s", exc)
        return False


def _instaloader_posts(username: str, profile_dir: Path) -> List[str]:
    """
    Fallback : telecharge les posts via instaloader avec login.
    Utilise uniquement si yt-dlp n'a rien recupere.
    """
    skipped: List[str] = []
    L = _build_loader()
    logged_in = _instaloader_login(L)
    if not logged_in:
        skipped.append("instaloader fallback : login requis pour posts.")
        return skipped
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        for post in profile.get_posts():
            try:
                L.download_post(post, target=profile_dir)
            except Exception as exc:
                skipped.append("post {}: {}".format(post.shortcode, exc))
    except instaloader.exceptions.ProfileNotExistsException:
        skipped.append("Profil @{} introuvable via instaloader.".format(username))
    except instaloader.exceptions.LoginRequiredException:
        skipped.append("Profil prive : login requis.")
    except Exception as exc:
        skipped.append("instaloader posts : {}".format(exc))
    return skipped


def _instaloader_stories(username: str, profile_dir: Path) -> List[str]:
    """Telecharge stories + highlights via instaloader (login obligatoire)."""
    skipped: List[str] = []
    if not (IG_USER and IG_PASS):
        skipped.append("Stories/Highlights : IG_USER et IG_PASS requis dans Railway.")
        return skipped

    L = _build_loader()
    if not _instaloader_login(L):
        skipped.append("Stories : echec du login Instagram.")
        return skipped

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except Exception as exc:
        skipped.append("Stories : profil inaccessible : {}".format(exc))
        return skipped

    # Stories actives
    try:
        L.download_stories(userids=[profile.userid], filename_target=profile_dir)
    except TypeError:
        try:
            # Ancienne signature instaloader
            L.download_storyitem  # test existence
            for item in L.get_stories(userids=[profile.userid]):
                for story_item in item.get_items():
                    try:
                        L.download_storyitem(story_item, target=profile_dir)
                    except Exception as exc:
                        skipped.append("story item: {}".format(exc))
        except Exception as exc:
            skipped.append("Stories : {}".format(exc))
    except Exception as exc:
        skipped.append("Stories : {}".format(exc))

    # Highlights
    try:
        for highlight in instaloader.Instaloader.get_highlights(L, profile):
            try:
                for item in highlight.get_items():
                    L.download_storyitem(item, target=profile_dir)
            except Exception as exc:
                skipped.append("Highlight {}: {}".format(highlight.title, exc))
    except Exception as exc:
        skipped.append("Highlights : {}".format(exc))

    return skipped


# ── Orchestrateur principal ───────────────────────────────────────────────────
def _sync_download(username: str, content_types: List[str]) -> Tuple[List[Path], List[str]]:
    """
    Strategie :
    - Posts/Reels  -> yt-dlp (sans login, plus fiable)
                   -> fallback instaloader avec login si yt-dlp vide
    - Stories      -> instaloader avec login
    - Highlights   -> instaloader avec login
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = DOWNLOAD_DIR / username
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    all_skipped: List[str] = []
    want_posts    = "posts"      in content_types or "reels" in content_types
    want_stories  = "stories"    in content_types
    want_highlights = "highlights" in content_types

    # ── Posts & Reels via yt-dlp ──────────────────────────────────────────────
    if want_posts:
        logger.info("yt-dlp : telechargement posts/reels @%s", username)
        errs = _ytdlp_download(username, profile_dir)
        all_skipped.extend(errs)

        # Verifier combien de fichiers recuperes
        files_after_ytdlp, _ = collect_media_files(profile_dir)

        # Fallback instaloader si yt-dlp n'a rien donne
        if not files_after_ytdlp:
            logger.info("yt-dlp : aucun fichier, fallback instaloader")
            errs2 = _instaloader_posts(username, profile_dir)
            all_skipped.extend(errs2)

    # ── Stories & Highlights via instaloader ──────────────────────────────────
    if want_stories or want_highlights:
        logger.info("instaloader : stories/highlights @%s", username)
        errs = _instaloader_stories(username, profile_dir)
        all_skipped.extend(errs)

    # ── Collecter tous les fichiers ───────────────────────────────────────────
    files, size_skipped = collect_media_files(profile_dir)
    all_skipped.extend(size_skipped)

    if not files and not all_skipped:
        all_skipped.append(
            "Aucun media trouve. Le compte est peut-etre prive ou vide."
        )

    logger.info("@%s : %d fichier(s) recuperes, %d ignores", username, len(files), len(all_skipped))
    return files, all_skipped


# ── Topic supergroupe ─────────────────────────────────────────────────────────
async def get_or_create_topic(bot, chat_id: int, username: str) -> Optional[int]:
    try:
        forum_topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name="@{}".format(username),
        )
        return forum_topic.message_thread_id
    except TelegramError as exc:
        err = str(exc).lower()
        if any(kw in err for kw in ("not a supergroup", "forum", "not supported", "chat not found", "need administrator")):
            return None
        logger.warning("create_forum_topic : %s", exc)
        return None


# ── Envoi fichier ─────────────────────────────────────────────────────────────
async def send_file(bot, chat_id: int, thread_id: Optional[int], f: Path, caption: str):
    kwargs = dict(chat_id=chat_id, caption=caption)
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    with open(f, "rb") as fh:
        if f.suffix.lower() == ".mp4":
            await bot.send_video(video=fh, **kwargs)
        else:
            await bot.send_photo(photo=fh, **kwargs)


# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Instagram Downloader Bot</b>\n\n"
        "Envoie un lien de profil Instagram ou un <code>@username</code>.\n\n"
        "Exemples :\n"
        "• <code>https://www.instagram.com/natgeo</code>\n"
        "• <code>@natgeo</code>\n\n"
        "💡 En supergroupe avec Topics actives, chaque profil cree son propre fil.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    has_creds = bool(IG_USER and IG_PASS)
    creds = "✅ configures" if has_creds else "❌ manquants (IG_USER / IG_PASS)"
    group = "✅ {}".format(GROUP_ID) if GROUP_ID else "❌ non defini (envoi dans le chat courant)"
    await update.message.reply_text(
        "📖 <b>Aide</b>\n\n"
        "<b>Utilisation :</b> envoie un lien ou @username Instagram\n\n"
        "<b>Contenu :</b> Posts · Reels · Stories · A la une\n\n"
        "<b>Identifiants Instagram :</b> {}\n"
        "<b>Groupe cible :</b> {}\n\n"
        "<i>Posts/Reels : telecharges via yt-dlp (sans login)\n"
        "Stories/Highlights : necessitent IG_USER + IG_PASS</i>".format(creds, group),
        parse_mode=ParseMode.HTML,
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    username = extract_username(text)

    if not username:
        await update.message.reply_text(
            "❌ Lien Instagram non reconnu.\n"
            "Format attendu : <code>https://www.instagram.com/username</code> ou <code>@username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["ig_username"] = username

    has_creds = bool(IG_USER and IG_PASS)
    note = "" if has_creds else "\n\n⚠️ <i>Stories/Highlights : IG_USER et IG_PASS non configures.</i>"

    keyboard = [
        [
            InlineKeyboardButton("📸 Posts",    callback_data="type_posts"),
            InlineKeyboardButton("🎬 Reels",    callback_data="type_reels"),
        ],
        [
            InlineKeyboardButton("📖 Stories",  callback_data="type_stories"),
            InlineKeyboardButton("⭐ A la une", callback_data="type_highlights"),
        ],
        [InlineKeyboardButton("✅ Tout telecharger", callback_data="type_all")],
    ]

    await update.message.reply_text(
        "📲 Profil : <b>@{}</b>\n\nQue veux-tu telecharger ?{}".format(esc(username), note),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = context.user_data.get("ig_username")
    if not username:
        await query.edit_message_text("❌ Session expiree. Renvoie le lien Instagram.")
        return

    type_map = {
        "type_posts":      ["posts"],
        "type_reels":      ["reels"],
        "type_stories":    ["stories"],
        "type_highlights": ["highlights"],
        "type_all":        ["posts", "reels", "stories", "highlights"],
    }
    content_types = type_map.get(query.data, ["posts"])
    chat_id = GROUP_ID if GROUP_ID else update.effective_chat.id
    bot = context.bot

    await query.edit_message_text(
        "⏳ <b>@{}</b> — telechargement en cours…".format(esc(username)),
        parse_mode=ParseMode.HTML,
    )

    loop = asyncio.get_event_loop()
    try:
        files, skipped = await loop.run_in_executor(
            None,
            partial(_sync_download, username, content_types),
        )
    except Exception as exc:
        logger.exception("Erreur inattendue")
        await query.edit_message_text(
            "❌ Erreur inattendue : {}".format(esc(str(exc))),
            parse_mode=ParseMode.HTML,
        )
        return

    if not files:
        reasons = "\n".join("• {}".format(esc(s)) for s in skipped) if skipped else "Aucun media trouve."
        await query.edit_message_text(
            "😕 Aucun fichier pour <b>@{}</b>.\n\n{}".format(esc(username), reasons),
            parse_mode=ParseMode.HTML,
        )
        return

    thread_id = await get_or_create_topic(bot, chat_id, username)

    await query.edit_message_text(
        "📤 Envoi de <b>{}</b> fichier(s) pour <b>@{}</b>…".format(len(files), esc(username)),
        parse_mode=ParseMode.HTML,
    )

    sent = 0
    for f in files:
        try:
            await send_file(bot, chat_id, thread_id, f, caption="@{}".format(username))
            sent += 1
            await asyncio.sleep(0.5)
        except TelegramError as exc:
            skipped.append("{} : {}".format(f.name, exc))

    summary = "✅ <b>{}/{}</b> fichier(s) envoye(s) pour <b>@{}</b>.".format(
        sent, len(files), esc(username)
    )
    if skipped:
        items = "\n".join("• {}".format(esc(s)) for s in skipped[:10])
        summary += "\n\n⚠️ <b>Ignores :</b>\n{}".format(items)

    await query.edit_message_text(summary, parse_mode=ParseMode.HTML)

    profile_dir = DOWNLOAD_DIR / username
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_type_choice, pattern=r"^type_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("Bot demarre.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
