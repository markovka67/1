#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK Community Faction Bot (single-file edition)

Запуск:
  VK_GROUP_TOKEN=... VK_GROUP_ID=... python3 bot.py

Опционально:
  BOT_DB_PATH=bot.db
  SENIOR_ADMIN_ID=576521317
"""

import os
import re
import signal
import sqlite3
import sys
import threading
import time
import json
import random
import base64
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import logging
import hashlib
import hmac
from logging.handlers import RotatingFileHandler

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

# ---------------------------- Константы ----------------------------

FACTIONS = [
    "Армия",
    "МВД",
    "СМИ",
    "Политика",
    "ФСБ",
    "Больница",
    "Народная Воля",
    "Красная Мафия",
    "Розовая Мафия",
    "Aperture",
]
SECRET_FACTION = "Админ"
HIDDEN_FACTIONS = ["Другие"]
ALL_FACTIONS = FACTIONS + HIDDEN_FACTIONS + [SECRET_FACTION]
STATE_FACTIONS = {"Армия", "МВД", "СМИ", "Политика", "ФСБ", "Больница"}
MAFIA_FACTIONS = {"Красная Мафия", "Розовая Мафия"}

DEFAULT_ROLE_NAME = "Одобренный пользователь"
DEFAULT_LIMIT_PER_MIN = 100

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONTROL_PIPE_PATH = os.getenv("CONTROL_PIPE_PATH", os.path.join(BASE_DIR, "vk_bot_control.pipe"))
CONTROL_PIPE_ENABLED = os.getenv("CONTROL_PIPE_ENABLED", "0").strip() == "1"
MSK_TZ = timezone(timedelta(hours=3), name="MSK")
TG_SENIOR_ADMIN_USERNAME = os.getenv("TG_SENIOR_ADMIN_USERNAME", "").strip().lstrip("@").lower()
TG_SENIOR_ADMIN_ID = int(os.getenv("TG_SENIOR_ADMIN_ID", "0") or "0")

def _make_logger() -> logging.Logger:
    log = logging.getLogger("bot")
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    # Файл с ротацией: 10 MB × 5 файлов
    fh = RotatingFileHandler(
        os.path.join(BASE_DIR, "bot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S"
    ))
    # Консоль — только WARNING+
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("[BOT] %(levelname)s: %(message)s"))
    log.addHandler(fh)
    log.addHandler(ch)
    return log

logger = _make_logger()
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "bot.db")
ISTORIA_DB_PATH = os.path.join(BASE_DIR, "istoria.db")
LISTS_DB_PATH = os.path.join(BASE_DIR, "lists.db")
SUBSCRIPTIONS_DB_PATH = os.path.join(BASE_DIR, "subscriptions.db")
# ENCRYPTION_KEY_A/B are set below with validation

def _derive_secure_key(key_a: str, key_b: str) -> bytes:
    return hashlib.sha256(f"{key_a}:{key_b}".encode("utf-8")).digest()

def _secure_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(out[:length])

def _secure_encrypt_text(text: str, key_a: str, key_b: str) -> str:
    raw = text.encode("utf-8")
    key = _derive_secure_key(key_a, key_b)
    nonce = os.urandom(16)
    stream = _secure_keystream(key, nonce, len(raw))
    cipher = bytes(x ^ y for x, y in zip(raw, stream))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    token = base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")
    return "v2:" + token

def _secure_decrypt_text(token: str, key_a: str, key_b: str) -> Optional[str]:
    if not token.startswith("v2:"):
        return None
    try:
        blob = base64.urlsafe_b64decode(token[3:].encode("ascii"))
        if len(blob) < 32:
            return ""
        nonce = blob[:16]
        tag = blob[-16:]
        cipher = blob[16:-16]
        key = _derive_secure_key(key_a, key_b)
        expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            return ""
        stream = _secure_keystream(key, nonce, len(cipher))
        raw = bytes(x ^ y for x, y in zip(cipher, stream))
        return raw.decode("utf-8")
    except Exception:
        return ""

TG_PEER_SHIFT = 10_000_000_000_000
PRIVACY_POLICY_URL = os.getenv("PRIVACY_POLICY_URL", "https://vk.ru/@pulse_rwpe-politika-konfedencialnosti").strip()

# ---------------------------- Быстрые настройки версии ----------------------------
# Для тестового стенда достаточно поменять эти 2 строки:
BOT_VERSION = "20.06.2026 16:04(МСК)"
BOT_DB_PATH_CONFIG = os.path.join(BASE_DIR, "bot.db")
# Backward-compat alias for legacy typo in some deployments/scripts.
BOT_DB_PATH_CONIG = BOT_DB_PATH_CONFIG

# Загружаем переменные из .env файла
from dotenv import load_dotenv
load_dotenv()  

# Все секреты берутся из переменных окружения
HARDCODED_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN", "")
HARDCODED_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))
WALL_READ_TOKEN = (
    os.getenv("VK_WALL_TOKEN", "").strip()
    or os.getenv("VK_USER_TOKEN", "").strip()
    or os.getenv("VK_SERVICE_TOKEN", "").strip()
)
_key_a = os.getenv("BOT_KEY_A", "").strip()
_key_b = os.getenv("BOT_KEY_B", "").strip()
if not _key_a or not _key_b:
    sys.exit(
        "FATAL: BOT_KEY_A и BOT_KEY_B должны быть заданы в .env\n"
        "Генерация: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )
if len(_key_a) < 16 or len(_key_b) < 16:
    sys.exit("FATAL: BOT_KEY_A и BOT_KEY_B должны быть не менее 16 символов.")
ENCRYPTION_KEY_A: str = _key_a
ENCRYPTION_KEY_B: str = _key_b
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
WIPE_PASSWORD = os.getenv("WIPE_PASSWORD", "")
SENIOR_ADMIN_ID_CONFIG = int(os.getenv("SENIOR_ADMIN_ID", "576521317"))

FACTION_COMMUNITY_TOKENS = {
    "Армия": os.getenv("ARMY_TOKEN", ""),
    "СМИ": os.getenv("MEDIA_TOKEN", ""),
    "Больница": os.getenv("HOSPITAL_TOKEN", ""),
    "МВД": os.getenv("POLICE_TOKEN", ""),
    "ФСБ": os.getenv("FSB_TOKEN", ""),
}


# Политики доступа по умолчанию:
# Для каждой команды строго две строки:
# 1) Уровень пользователя по умолчанию
# 2) Админ права по умолчанию
COMMAND_ACCESS: dict[str, dict[str, int]] = {
    "!я": {"user_default": 0, "admin_default": 0},
    "!банан": {"user_default": 70, "admin_default": 10},
    "!версия": {"user_default": 0, "admin_default": 0},
    "!бан": {"user_default": 70, "admin_default": 0},
    "!кик": {"user_default": 70, "admin_default": 0},
    "!разбан": {"user_default": 70, "admin_default": 0},
    "!мут": {"user_default": 70, "admin_default": 0},
    "!размут": {"user_default": 70, "admin_default": 0},
    "!пред": {"user_default": 70, "admin_default": 0},
    "!снятьпред": {"user_default": 70, "admin_default": 0},
    "!чс": {"user_default": 70, "admin_default": 10},
    "!снятьчс": {"user_default": 70, "admin_default": 10},
    "!списокчс": {"user_default": 0, "admin_default": 10},
    "!выговор": {"user_default": 0, "admin_default": 0},
    "!снять": {"user_default": 70, "admin_default": 0},
    "!чат": {"user_default": 70, "admin_default": 40},
    "!убрать": {"user_default": 70, "admin_default": 40},
    "!пуш": {"user_default": 70, "admin_default": 0},
    "!пуш-": {"user_default": 0, "admin_default": 15},
    "!пуш+": {"user_default": 0, "admin_default": 15},
    "!узнать": {"user_default": 0, "admin_default": 30},
    "!супербан": {"user_default": 0, "admin_default": 30},
    "!чаты": {"user_default": 0, "admin_default": 30},
    "!все": {"user_default": 0, "admin_default": 80},
    "!блок": {"user_default": 0, "admin_default": 80},
    "!разблок": {"user_default": 0, "admin_default": 80},
    "!лидер": {"user_default": 0, "admin_default": 100},
    "!лидеры": {"user_default": 0, "admin_default": 0},
    "!уволиться": {"user_default": 0, "admin_default": 0},
    "!снятьлидера": {"user_default": 0, "admin_default": 100},
    "!админ": {"user_default": 0, "admin_default": 100},
    "!стереть": {"user_default": 0, "admin_default": 100},
    "!ботбан": {"user_default": 0, "admin_default": 100},
    "!ботразбан": {"user_default": 0, "admin_default": 100},
    "!лимиткоманд": {"user_default": 0, "admin_default": 100},
    "!логчат": {"user_default": 0, "admin_default": 100},
    "!логвкл": {"user_default": 0, "admin_default": 100},
    "!логвыкл": {"user_default": 0, "admin_default": 100},
    "!отключить": {"user_default": 0, "admin_default": 40},
    "!включить": {"user_default": 0, "admin_default": 40},
    "!чистка": {"user_default": 70, "admin_default": 10},
    "!поиск": {"user_default": 0, "admin_default": 10},
    "!скрыть": {"user_default": 0, "admin_default": 10},
    "!раскрыть": {"user_default": 80, "admin_default": 20},
    "!уволить": {"user_default": 70, "admin_default": 10},
    "!нанять": {"user_default": 70, "admin_default": 10},
    "!рейтинг": {"user_default": 0, "admin_default": 50},
    "!админсчет": {"user_default": 0, "admin_default": 40},
    "!право": {"user_default": 0, "admin_default": 30},
    "!команда": {"user_default": 0, "admin_default": 30},
    "!админправо": {"user_default": 0, "admin_default": 100},
    "!переименовать": {"user_default": 70, "admin_default": 10},
    "!новый": {"user_default": 0, "admin_default": 90},
    "!дж": {"user_default": 0, "admin_default": 0},
    "!синхроль": {"user_default": 0, "admin_default": 70},
    "!банфракция": {"user_default": 70, "admin_default": 10},
    "!изменить": {"user_default": 0, "admin_default": 50},
    "!новая": {"user_default": 70, "admin_default": 0},
    "!команды": {"user_default": 0, "admin_default": 0},
    "!логи": {"user_default": 0, "admin_default": 0},
    "!жалоба": {"user_default": 0, "admin_default": 0},
    "!жалобы": {"user_default": 0, "admin_default": 0},
    "!рассмотрение": {"user_default": 0, "admin_default": 0},
    "!одобрить": {"user_default": 0, "admin_default": 0},
    "!отказать": {"user_default": 0, "admin_default": 0},
    "!принять": {"user_default": 0, "admin_default": 0},
    "!отклонить": {"user_default": 0, "admin_default": 0},
    "!оффадминувед": {"user_default": 0, "admin_default": 0},
    "!админувед": {"user_default": 0, "admin_default": 0},
    "!дубль": {"user_default": 0, "admin_default": 0},
    "!одобритьдубль": {"user_default": 0, "admin_default": 0},
    "!новая подписка": {"user_default": 70, "admin_default": 40},
    "!подписка": {"user_default": 70, "admin_default": 40},
    "!подписки": {"user_default": 0, "admin_default": 0},
    "!отписаться": {"user_default": 70, "admin_default": 40},
    "!права": {"user_default": 0, "admin_default": 0},
    "!роли": {"user_default": 0, "admin_default": 0},
    "!создать": {"user_default": 70, "admin_default": 0},
    "!удалить": {"user_default": 60, "admin_default": 10},
    "!роль": {"user_default": 0, "admin_default": 0},
    "!снять": {"user_default": 70, "admin_default": 0},
    "!повысить": {"user_default": 70, "admin_default": 0},
    "!понизить": {"user_default": 70, "admin_default": 0},
    "!заметка": {"user_default": 0, "admin_default": 0},
    "!заметки": {"user_default": 0, "admin_default": 0},
    "!закладка": {"user_default": 0, "admin_default": 0},
    "!закладки": {"user_default": 0, "admin_default": 0},
    "!банлист": {"user_default": 0, "admin_default": 0},
    "!мутлист": {"user_default": 0, "admin_default": 0},
    "!списокпредов": {"user_default": 0, "admin_default": 0},
    "!иммунитет": {"user_default": 70, "admin_default": 10},
    "!снятьиммунитет": {"user_default": 70, "admin_default": 10},
    "!иммунитеты": {"user_default": 0, "admin_default": 10},
    "!админы": {"user_default": 0, "admin_default": 0},
    "!суперразбан": {"user_default": 0, "admin_default": 30},
    "!чатжалоб": {"user_default": 0, "admin_default": 100},
    "!самобан": {"user_default": 0, "admin_default": 0},
    "!помощь": {"user_default": 0, "admin_default": 0},
    "!снятьрольвезде": {"user_default": 0, "admin_default": 50},
    "!тишина": {"user_default": 0, "admin_default": 50},
    "!снятьтишину": {"user_default": 0, "admin_default": 50},
    "!новый список": {"user_default": 0, "admin_default": 90},
    "!список": {"user_default": 0, "admin_default": 0},
    "!все списки": {"user_default": 0, "admin_default": 0},
    "!удалить список": {"user_default": 0, "admin_default": 90},
    "!голос": {"user_default": 0, "admin_default": 50},
    "!допрассм": {"user_default": 0, "admin_default": 100},
    "!допинфа": {"user_default": 0, "admin_default": 0},
    "!инфа": {"user_default": 0, "admin_default": 0},
    "!удалитьинфу": {"user_default": 0, "admin_default": 0},
    "!облик": {"user_default": 0, "admin_default": 100},
    "!проверкачата": {"user_default": 0, "admin_default": 80},
    "!удаленный": {"user_default": 0, "admin_default": 80},
    "!тишина": {"user_default": 0, "admin_default": 50},
    "!дж заголовок": {"user_default": 0, "admin_default": 70},
    "!дж новая": {"user_default": 0, "admin_default": 70},
    "!дж удалить": {"user_default": 0, "admin_default": 70},
    "!дж переименовать": {"user_default": 0, "admin_default": 70},
    "!снять выговор": {"user_default": 0, "admin_default": 5},
    "!снять роль": {"user_default": 70, "admin_default": 0},
    "!создать роль": {"user_default": 70, "admin_default": 0},
    "!список чс": {"user_default": 0, "admin_default": 10},
    "!список выговоров": {"user_default": 0, "admin_default": 0},
    "!выговоры": {"user_default": 0, "admin_default": 0},
    "!список допрассм": {"user_default": 0, "admin_default": 100},
    "!право админ": {"user_default": 0, "admin_default": 100},
    "!синхроль": {"user_default": 0, "admin_default": 70},
    "!сотрудники": {"user_default": 0, "admin_default": 0},
    "!разрешить": {"user_default": 0, "admin_default": 30},
    "!запретить": {"user_default": 0, "admin_default": 30},
    "!проверка команд": {"user_default": 0, "admin_default": 30},
    "!приветствие": {"user_default": 0, "admin_default": 0},
    "!новое приветствие": {"user_default": 0, "admin_default": 40},
    "!удалить приветствие": {"user_default": 0, "admin_default": 40},
    "!отключить чат": {"user_default": 0, "admin_default": 40},
    "!включить чат": {"user_default": 0, "admin_default": 40},
    "!список доступ": {"user_default": 0, "admin_default": 30},
    "!списки": {"user_default": 0, "admin_default": 0},
    "!приглос": {"user_default": 0, "admin_default": 40},
    "!аудит файлы": {"user_default": 0, "admin_default": 100},
    "!аватарка": {"user_default": 0, "admin_default": 0},
}

DEFAULT_COMMAND_RIGHTS = {k: v["user_default"] for k, v in COMMAND_ACCESS.items()}
ADMIN_MIN = {k: v["admin_default"] for k, v in COMMAND_ACCESS.items()}
COMMAND_ALIASES: dict[str, list[str]] = {
    "!команды": ["!комнады"],
    "!пуш": ["!рассылка"],
    "!чистка": ["!очистка"],
    "!узнать": ["!инфо"],
    "!размут": ["!разум"],
    "!супербан": ["!супермбан"],
    "!суперразбан": ["!суперазбан"],
    "!рейтинг": ["!рейтиг"],
    "!право": ["!прово"],
    "!уволиться": ["!уволится"],
    "!дж": ["!должности", "!дж_список", "!дж_лист"],
    "!неприниматьдж": ["!непринматьдж", "!нпдж", "!неприниматьработу"],
    "!нанять": ["!нанят", "!принять на работу"],
    "!сотрудники": ["!сотрудники", "!состав"],
    "!синхроль": ["!синхрол", "!синхронрол"],
    "!разрешить": ["!разрешать", "!разрешит"],
    "!запретить": ["!запрет", "!запретить"],
    "!приветствие": ["!привет чат", "!привет"],
    "!новое приветствие": ["!новоеприветствие", "!установить приветствие"],
    "!удалить приветствие": ["!удалитьприветствие", "!убрать приветствие"],
    "!отключить чат": ["!отключитьчат"],
    "!включить чат": ["!включитьчат"],
    "!списки": ["!списки"],
    "!приглос": ["!приглашение", "!пригласить"],
    "!все списки": ["!все списки", "!всесписки"],
    "!список доступ": ["!списокдоступ"],
    "!дж": ["!должности", "!дж_список", "!дж_лист", "!дж в роли"],
}

# ─── Нечёткое сопоставление команд ─────────────────────────────────────────


def _levenshtein(a: str, b: str) -> int:
    """Расстояние Левенштейна между двумя строками (Unicode-safe)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    # Оптимизация: одна строка dp
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,      # удаление
                curr[j - 1] + 1,  # вставка
                prev[j - 1] + (0 if ca == cb else 1),  # замена
            )
        prev = curr
    return prev[lb]


def _fuzzy_resolve_command(raw_cmd: str) -> str:
    """Пытается сопоставить ошибочно введённую команду с ближайшей известной.

    Правила:
    • Команда должна начинаться с '!'
    • Расстояние Левенштейна <= max_dist (зависит от длины команды)
    • Если несколько кандидатов на одном расстоянии — берём самый короткий
      (предпочитаем более конкретное совпадение)
    • Никогда не подбираем если неоднозначность (два разных кандидата с равным расстоянием)
    """
    if not raw_cmd.startswith("!"):
        return raw_cmd

    # Строим плоский список всех известных команд
    known: list[str] = list(COMMAND_ACCESS.keys())
    # Добавляем алиасы как допустимые цели
    for canonical, aliases in COMMAND_ALIASES.items():
        for al in aliases:
            if al not in known:
                known.append(al)

    cmd_len = len(raw_cmd)

    # Порог: короткие команды (<=5 символов с "!") — только 1 ошибка
    # средние (6..9) — до 2 ошибок; длинные (10+) — до 3 ошибок
    if cmd_len <= 5:
        max_dist = 1
    elif cmd_len <= 9:
        max_dist = 2
    else:
        max_dist = 3

    best_dist = max_dist + 1
    best_candidates: list[str] = []

    for known_cmd in known:
        # Быстрая проверка: разница длин уже больше порога → пропускаем
        if abs(len(known_cmd) - cmd_len) > max_dist:
            continue
        dist = _levenshtein(raw_cmd, known_cmd)
        if dist < best_dist:
            best_dist = dist
            best_candidates = [known_cmd]
        elif dist == best_dist:
            best_candidates.append(known_cmd)

    if best_dist == 0:
        # Точное совпадение (или алиас) — вернём canonical через _resolve_alias
        return raw_cmd

    if not best_candidates or best_dist > max_dist:
        return raw_cmd  # Не нашли ничего подходящего

    # Если один однозначный кандидат — возвращаем его canonical форму
    if len(best_candidates) == 1:
        canonical = best_candidates[0]
        # Если это алиас — нормализуем до canonical
        for can, aliases in COMMAND_ALIASES.items():
            if canonical in aliases:
                return can
        return canonical

    # Несколько кандидатов на одном расстоянии:
    # Предпочитаем тот, у которого совпадает длина с введённой командой
    same_len = [c for c in best_candidates if len(c) == cmd_len]
    if len(same_len) == 1:
        canonical = same_len[0]
        for can, aliases in COMMAND_ALIASES.items():
            if canonical in aliases:
                return can
        return canonical

    # Неоднозначность — не угадываем
    return raw_cmd


COMMAND_USAGE: dict[str, str] = {
    "!я": "!я",
    "!версия": "!версия",
    "!команды": "!команды",
    "!логи": "!логи",
    "!бан": "!бан (пользователь) [причина]",
    "!кик": "!кик (пользователь) [причина]",
    "!разбан": "!разбан (пользователь)",
    "!мут": "!мут (пользователь) [время] [причина]",
    "!размут": "!размут (пользователь)",
    "!пред": "!пред (пользователь) (причина)",
    "!выговор": "!выговор (пользователь) (причина)",
    "!снятьвыговор": "!снять выговор (пользователь)",
    "!списоквыговоров": "!список выговоров [фракция]",
    "!снятьпред": "!снятьпред (пользователь)",
    "!банлист": "!банлист",
    "!мутлист": "!мутлист",
    "!банан": "!банан (пользователь) (причина) или ответом на сообщение",
    "!списокпредов": "!списокпредов",
    "!чистка": "!чистка (количество|время)",
    "!пуш": "!пуш (ответ на сообщение) | для Админ: !пуш (фракция) (сервер) [@all]",
    "!пуш-": "!пуш- — отключить пуши в этом чате",
    "!пуш+": "!пуш+ — включить пуши в этом чате",
    "!роли": "!роли",
    "!удалить": "!удалить роль (уровень)",
    "!создать": "!создать роль (название) (уровень)",
    "!переименовать": "!переименовать (уровень роли) (новое название)",
    "!роль": "!роль [пользователь]",
    "!снять": "!снять роль (пользователь) / !снять префикс ...",
    "!повысить": "!повысить (пользователь)",
    "!понизить": "!понизить (пользователь)",
    "!заметка": "!заметка ...",
    "!заметки": "!заметки",
    "!закладка": "!закладка (название) (ответом на сообщение)",
    "!закладки": "!закладки",
    "!новая": "!новая заметка (название) (текст) или ответом на сообщение",
    "!узнать": "!узнать (пользователь|ссылка vk/tg) / ответом на сообщение",
    "!дж": "!дж [фракция]",
    "!банфракция": "!банфракция (пользователь) (причина) / для Админ: !банфракция (фракция) (пользователь) (причина)",
    "!новый": "!новый префикс (пользователь) (название) (смайлик)",
    "!изменить": "!изменить (фракцию|фио|ник|должность) (пользователь) (новое значение)",
    "!поиск": "!поиск (никнейм)",
    "!скрыть": "!скрыть (пользователь)",
    "!раскрыть": "!раскрыть (пользователь)",
    "!уволить": "!уволить (пользователь) | !снять (пользователь)",
    "!дж": "!дж | !дж новая (название) (уровень) | !дж удалить (уровень/название) | !дж заголовок (название) (мин-макс) | !дж заголовок удалить (название) | !дж переименовать (название) (новое) | !дж в роли",
    "!сотрудники": "!сотрудники [фракция] [сервер]",
    "!неприниматьдж": "!неприниматьдж — не получать предложения о найме",
    "!нанять": "!нанять (пользователь) (должность)",
    "!рейтинг": "!рейтинг (+N|-N) (пользователь) (причина)",
    "!админсчет": "!админсчет",
    "!ботразбан": "!ботразбан (пользователь)",
    "!жалоба": "!жалоба (reply на сообщение)",
    "!жалобы": "!жалобы",
    "!рассмотрение": "!рассмотрение (номер жалобы) или ответом на сообщение жалобы",
    "!одобрить": "!одобрить (номер заявки)",
    "!отказать": "!отказать (номер заявки)",
    "!принять": "!принять (номер жалобы) или ответом на сообщение жалобы",
    "!отклонить": "!отклонить (номер жалобы) или ответом на сообщение жалобы",
    "!оффадминувед": "!оффадминувед (пользователь)",
    "!админувед": "!админувед (пользователь)",
    "!дубль": "!дубль (ник)",
    "!одобритьдубль": "!одобритьдубль (номер)",
    "!подписка": "!подписка пинг вкл / !подписка пинг выкл",
    "!право": "!право (команда) (уровень роли)",
    "!команда": "!команда - !команда (уровень) / !команда + !команда",
    "!админправо": "!админправо (команда) (уровень админ прав)",
    "!админы": "!админы",
    "!суперразбан": "!суперразбан (пользователь)",
    "!чатжалоб": "!чатжалоб + / !чатжалоб - (в чате)",
    "!самобан": "!самобан",
    "!помощь": "!помощь",
    "!снятьрольвезде": "!снятьрольвезде (пользователь)",
    "!тишина": "!тишина (минуты) (сообщений)",
    "!снятьтишину": "!снятьтишину",
    "!голос": "!голос (пользователь)",
    "!допрассм": "!допрассм + (пользователь) (фракция) (сервер) / !допрассм - (пользователь)",
    "!списокдопрассм": "!список допрассм",
    "!допинфа": "!допинфа (пользователь) (текст) / ответом на сообщение",
    "!инфа": "!инфа (пользователь)",
    "!удалитьинфу": "!удалить инфу (пользователь) (номер)",
    "!облик": "!облик 0 (пользователь)",
    "!новая подписка": "!новая подписка (ссылка на сообщество)",
    "!подписки": "!подписки",
    "!отписаться": "!отписаться (ссылка на сообщество)",
    "!проверкачата": "!проверкачата (номер чата)",
    "!удаленный": "!удаленный доступ (номер чата) / !удаленный доступ стоп",
    "!чат": "!чат фракции (фракция) (сервер 1..3)",
    "!убрать": "!убрать чат фракции",
    "!чаты": "!чаты фракций (фракция) (сервер 1..3)",
    "!лидер": "!лидер (фракция) (сервер 1..3) (пользователь)",
    "!лидеры": "!лидеры [фракция]",
    "!уволиться": "!уволиться (причина)",
    "!стереть": "!стереть (пользователь)",
    "!все": "!все чаты",
    "!блок": "!блок чат (номер)",
    "!разблок": "!разблок чат (номер)",
    "!права": "!права [уровень роли]",
    "!тишина": "!тишина [минуты] [сообщений]",
    "!новый список": "!новый список (название) (уровень_адм_прав)",
    "!список": "!список (название)",
    "!все списки": "!все списки",
    "!удалить список": "!удалить список (название)",
    "!тишина_иммунитет": "!тишина иммунитет [+|-] [пользователь] [время]",
    "!тишина_админпорог": "!тишина админпорог <уровень>",

    "!приглос": "!приглос [уровень роли] — настроить минимальную роль для приглашения в чат",
    "!аудит": "!аудит файлы — проверить хеши bot1.py, .env, bot.db",
    "!аватарка": "!аватарка [@username|ссылка|id] — показать аватарку пользователя; можно ответом на сообщение",
}

COMMAND_REQUIRED_ARGS: dict[str, list[str]] = {
    "!бан": ["пользователь", "причина (опционально)"],
    "!кик": ["пользователь", "причина (опционально)"],
    "!мут": ["пользователь", "время (опционально)", "причина (опционально)"],
    "!пред": ["пользователь", "причина"],
    "!выговор": ["пользователь", "причина"],
    "!разбан": ["пользователь"],
    "!размут": ["пользователь"],
    "!роль": ["пользователь (опционально)", "уровень (для выдачи роли)"],
    "!снять": ["роль", "пользователь"],
    "!право": ["команда", "уровень роли"],
    "!дубль": ["ник"],
    "!одобритьдубль": ["номер запроса"],
    "!стереть": ["пользователь"],
    "!допинфа": ["пользователь", "текст (или reply)"],
    "!инфа": ["пользователь"],
    "!удалитьинфу": ["пользователь", "номер"],
    "!облик": ["0", "пользователь"],
}

COMMAND_COOLDOWNS: dict[str, int] = {k: 1 for k in COMMAND_ACCESS.keys()}
COMMAND_COOLDOWNS["!я"] = 180
COMMAND_COOLDOWNS["!дубль"] = 86400

CHAT_ONLY_COMMANDS = {
    "!бан", "!кик", "!разбан", "!мут", "!размут", "!роли", "!создать", "!создать роль",
    "!роль", "!снять", "!снять роль", "!переименовать", "!пуш", "!банлист", "!мутлист",
    "!новоеприветствие", "!новое приветствие", "!удалитьприветствие", "!удалить приветствие",
    "!заметка", "!заметки", "!иммунитет", "!снятьиммунитет", "!иммунитеты", "!право",
    "!пред", "!повысить", "!понизить", "!админы", "!снятьпред", "!списокпредов",
    "!выговор", "!список", "!список выговоров", "!выговоры", "!тишина", "!снятьтишину",
    "!голос", "!приглос", "!проверкачата", "!удаленный", "!новая подписка", "!подписки", "!отписаться",
}

WIPE_SESSION_TTL_SEC = 300
REMOTE_ACCESS_TTL_SEC = 1800




# ---------------------------- БД ----------------------------


class DB:
    def __init__(self, path: str, senior_admin_id: int):
        self.path = path
        self.senior_admin_id = senior_admin_id
        self._init()

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        return c

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    vk_id INTEGER PRIMARY KEY,
                    nickname TEXT,
                    rp_name TEXT,
                    position TEXT,
                    faction TEXT,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 0,
                    admin_level INTEGER NOT NULL DEFAULT 0,
                    bot_ban INTEGER NOT NULL DEFAULT 0,
                    consent_accepted INTEGER NOT NULL DEFAULT 0,
                    consent_accepted_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS leaders (
                    faction TEXT PRIMARY KEY,
                    vk_id INTEGER NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS registration_sessions (
                    vk_id INTEGER PRIMARY KEY,
                    stage TEXT NOT NULL,
                    server_id INTEGER,
                    nickname TEXT,
                    faction TEXT,
                    rp_name TEXT
                );

                CREATE TABLE IF NOT EXISTS registration_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vk_id INTEGER NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    platform TEXT NOT NULL DEFAULT 'vk',
                    nickname TEXT NOT NULL,
                    faction TEXT NOT NULL,
                    rp_name TEXT NOT NULL,
                    position TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT,
                    faction TEXT,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    welcome_text TEXT
                );

                CREATE TABLE IF NOT EXISTS chat_controls (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS chat_invite_level (
                    chat_id INTEGER PRIMARY KEY,
                    min_role INTEGER NOT NULL DEFAULT 40,
                    min_admin INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_push_disabled (
                    chat_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS chat_greetings (
                    chat_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    attachment TEXT NOT NULL DEFAULT '',
                    sticker_id INTEGER,
                    created_by INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_roles (
                    chat_id INTEGER NOT NULL,
                    level INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY(chat_id, level)
                );

                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    role_level INTEGER NOT NULL DEFAULT 0,
                    immunity_level INTEGER NOT NULL DEFAULT 0,
                    muted_until INTEGER,
                    banned INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, vk_id)
                );

                CREATE TABLE IF NOT EXISTS faction_strikes (
                    faction TEXT NOT NULL,
                    vk_id INTEGER NOT NULL,
                    strikes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(faction, vk_id)
                );

                CREATE TABLE IF NOT EXISTS command_rights (
                    command TEXT PRIMARY KEY,
                    min_role INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_command_rights (
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    min_role INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, command)
                );

                CREATE TABLE IF NOT EXISTS command_admin_rights (
                    command TEXT PRIMARY KEY,
                    min_admin INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_greetings (
                    chat_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    attachment TEXT NOT NULL DEFAULT '',
                    sticker_id INTEGER,
                    source_message_id INTEGER,
                    created_by INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_bookmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    message_link TEXT NOT NULL,
                    creator_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(chat_id, name)
                );

                CREATE TABLE IF NOT EXISTS account_sync_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    source_platform TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    nickname TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_links (
                    user_id INTEGER PRIMARY KEY,
                    master_id INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS blocked_chats (
                    chat_id INTEGER PRIMARY KEY,
                    blocked_by INTEGER NOT NULL,
                    blocked_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tg_peer_routes (
                    peer_id INTEGER PRIMARY KEY,
                    tg_chat_id INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_bans (
                    vk_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS warning_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    issuer_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ban_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    issuer_id INTEGER NOT NULL,
                    reason TEXT,
                    created_at INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS mute_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    issuer_id INTEGER NOT NULL,
                    reason TEXT,
                    created_at INTEGER NOT NULL,
                    until_ts INTEGER,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS faction_blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faction TEXT NOT NULL,
                    vk_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    issuer_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_templates (
                    code TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_console_sessions (
                    vk_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    target_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS user_prefixes (
                    vk_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    PRIMARY KEY(vk_id, name)
                );

                CREATE TABLE IF NOT EXISTS preapproved_profiles (
                    vk_id INTEGER PRIMARY KEY,
                    approved INTEGER NOT NULL DEFAULT 1,
                    nickname TEXT,
                    rp_name TEXT,
                    position TEXT,
                    faction TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    reporter_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    attachments TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    review_by INTEGER,
                    review_at INTEGER,
                    closed_by INTEGER,
                    closed_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS admin_notify_settings (
                    vk_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS admin_complaint_stats (
                    vk_id INTEGER PRIMARY KEY,
                    accepted_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS extra_reviewers (
                    vk_id INTEGER NOT NULL,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL,
                    added_at INTEGER NOT NULL,
                    PRIMARY KEY(vk_id, faction, server_id)
                );

                CREATE TABLE IF NOT EXISTS registration_review_recipients (
                    request_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    PRIMARY KEY(request_id, vk_id)
                );

                CREATE TABLE IF NOT EXISTS user_extra_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    info_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tg_usernames (
                    username TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oblik_users (
                    vk_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wipe_sessions (
                    actor_id INTEGER PRIMARY KEY,
                    target_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dj_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    level INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE(faction, server_id, level),
                    UNIQUE(faction, server_id, name COLLATE NOCASE)
                );

                CREATE TABLE IF NOT EXISTS dj_headers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    title TEXT NOT NULL,
                    level_min INTEGER NOT NULL,
                    level_max INTEGER NOT NULL,
                    UNIQUE(faction, server_id, title COLLATE NOCASE)
                );

                CREATE TABLE IF NOT EXISTS hire_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_user_id INTEGER NOT NULL,
                    actor_user_id INTEGER NOT NULL,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    position_name TEXT NOT NULL,
                    position_level INTEGER NOT NULL DEFAULT 0,
                    old_faction TEXT NOT NULL DEFAULT '',
                    old_server_id INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    step TEXT NOT NULL DEFAULT 'user_confirm'
                );

                CREATE TABLE IF NOT EXISTS hire_opt_out (
                    vk_id INTEGER PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS resign_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    reason TEXT NOT NULL DEFAULT '',
                    faction TEXT NOT NULL DEFAULT '',
                    server_id INTEGER NOT NULL DEFAULT 1,
                    leader_id INTEGER,
                    created_at INTEGER NOT NULL,
                    step TEXT NOT NULL DEFAULT 'user_confirm'
                );

                CREATE TABLE IF NOT EXISTS chat_disabled_commands (
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    PRIMARY KEY(chat_id, command)
                );

                CREATE TABLE IF NOT EXISTS remote_access_sessions (
                    actor_id INTEGER PRIMARY KEY,
                    source_chat_id INTEGER NOT NULL,
                    target_chat_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_silence (
                    chat_id INTEGER PRIMARY KEY,
                    window_min INTEGER NOT NULL,
                    msg_limit INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS silence_exceptions (
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    until_ts INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, vk_id)
                );
                

                CREATE TABLE IF NOT EXISTS chat_member_roles (
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    role_name TEXT NOT NULL,
                    assigned_at INTEGER DEFAULT 0,
                    PRIMARY KEY(chat_id, vk_id, role_name)
                );

                CREATE TABLE IF NOT EXISTS chat_command_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    vk_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_complaints_status_id ON complaints(status, id);
                CREATE INDEX IF NOT EXISTS idx_chat_command_logs_chat_id ON chat_command_logs(chat_id, id);
                CREATE INDEX IF NOT EXISTS idx_chat_members_chat_role ON chat_members(chat_id, role_level DESC);
                CREATE INDEX IF NOT EXISTS idx_users_faction_server ON users(faction, server_id);


                """
            )
            try:
                c.execute("ALTER TABLE users ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN rating INTEGER NOT NULL DEFAULT 100")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN approved_by INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN approved_at INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN consent_accepted INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN consent_accepted_at INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN server_id INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE registration_sessions ADD COLUMN server_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE registration_requests ADD COLUMN server_id INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE registration_requests ADD COLUMN platform TEXT NOT NULL DEFAULT 'vk'")
            except sqlite3.OperationalError:
                pass
            # ... существующие ALTER TABLE ...
            try:
                c.execute("ALTER TABLE users ADD COLUMN platform TEXT DEFAULT 'vk'")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE users ADD COLUMN nick2 TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("""CREATE TABLE IF NOT EXISTS chat_invite_level (
                    chat_id INTEGER PRIMARY KEY,
                    min_role INTEGER NOT NULL DEFAULT 40,
                    min_admin INTEGER NOT NULL DEFAULT 0
                )""")
            except Exception:
                pass
            try:
                c.execute("CREATE TABLE IF NOT EXISTS chat_push_disabled (chat_id INTEGER PRIMARY KEY)")
            except Exception:
                pass
            try:
                c.execute("ALTER TABLE chats ADD COLUMN server_id INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE faction_strikes ADD COLUMN server_id INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            c.execute("UPDATE users SET server_id=1 WHERE server_id IS NULL OR server_id<1 OR server_id>3")
            c.execute("UPDATE chats SET server_id=1 WHERE server_id IS NULL OR server_id<1 OR server_id>3")
            c.execute("UPDATE registration_requests SET server_id=1 WHERE server_id IS NULL OR server_id<1 OR server_id>3")
            c.execute("UPDATE faction_strikes SET server_id=1 WHERE server_id IS NULL OR server_id<1 OR server_id>3")
            try:
                c.execute("ALTER TABLE notes ADD COLUMN attachments TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE notes ADD COLUMN source_message_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE chat_greetings ADD COLUMN source_message_id INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE complaints ADD COLUMN review_by INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE complaints ADD COLUMN review_at INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("ALTER TABLE resign_requests ADD COLUMN server_id INTEGER NOT NULL DEFAULT 1")
            except sqlite3.OperationalError:
                pass
            try:
                c.execute("""CREATE TABLE IF NOT EXISTS chat_greetings (
                    chat_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    attachment TEXT NOT NULL DEFAULT '',
                    sticker_id INTEGER,
                    created_by INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )""")
            except Exception:
                pass
            # Таблицы персональных разрешений/запретов команд
            for _ddl2 in [
                """CREATE TABLE IF NOT EXISTS user_cmd_allow (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_vk_id INTEGER NOT NULL,
                    command TEXT NOT NULL COLLATE NOCASE,
                    granted_by INTEGER NOT NULL,
                    granted_at INTEGER NOT NULL,
                    UNIQUE(target_vk_id, command COLLATE NOCASE)
                )""",
                """CREATE TABLE IF NOT EXISTS user_cmd_deny (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_vk_id INTEGER NOT NULL,
                    command TEXT NOT NULL COLLATE NOCASE,
                    denied_by INTEGER NOT NULL,
                    denied_at INTEGER NOT NULL,
                    UNIQUE(target_vk_id, command COLLATE NOCASE)
                )""",
            ]:
                try:
                    c.execute(_ddl2)
                except Exception:
                    pass
            # dj_positions: добавляем UNIQUE по level (одно название на уровень)
            try:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS dj_positions_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        faction TEXT NOT NULL,
                        server_id INTEGER NOT NULL DEFAULT 1,
                        level INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        UNIQUE(faction, server_id, level),
                        UNIQUE(faction, server_id, name COLLATE NOCASE)
                    );
                    INSERT OR IGNORE INTO dj_positions_new(id,faction,server_id,level,name)
                        SELECT id,faction,server_id,level,name FROM dj_positions;
                    DROP TABLE dj_positions;
                    ALTER TABLE dj_positions_new RENAME TO dj_positions;
                """)
            except Exception:
                pass
            # Создаём новые таблицы если БД уже существует
            for _ddl in [
                """CREATE TABLE IF NOT EXISTS dj_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    level INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE(faction, server_id, name COLLATE NOCASE)
                )""",
                """CREATE TABLE IF NOT EXISTS dj_headers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    title TEXT NOT NULL,
                    level_min INTEGER NOT NULL,
                    level_max INTEGER NOT NULL,
                    UNIQUE(faction, server_id, title COLLATE NOCASE)
                )""",
                """CREATE TABLE IF NOT EXISTS hire_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_user_id INTEGER NOT NULL,
                    actor_user_id INTEGER NOT NULL,
                    faction TEXT NOT NULL,
                    server_id INTEGER NOT NULL DEFAULT 1,
                    position_name TEXT NOT NULL,
                    position_level INTEGER NOT NULL DEFAULT 0,
                    old_faction TEXT NOT NULL DEFAULT '',
                    old_server_id INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    step TEXT NOT NULL DEFAULT 'user_confirm'
                )""",
                "CREATE TABLE IF NOT EXISTS hire_opt_out (vk_id INTEGER PRIMARY KEY)",
            ]:
                try:
                    c.execute(_ddl)
                except Exception:
                    pass
            try:
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS leaders_v2 (
                        faction TEXT NOT NULL,
                        server_id INTEGER NOT NULL DEFAULT 1,
                        vk_id INTEGER NOT NULL,
                        PRIMARY KEY(faction, server_id)
                    )
                    """
                )
                c.execute("INSERT OR IGNORE INTO leaders_v2(faction,server_id,vk_id) SELECT faction,1,vk_id FROM leaders")
                c.execute("DROP TABLE leaders")
                c.execute("ALTER TABLE leaders_v2 RENAME TO leaders")
            except sqlite3.OperationalError:
                pass
            c.execute(
                "INSERT OR IGNORE INTO users(vk_id, approved, admin_level) VALUES(?,1,100)",
                (self.senior_admin_id,),
            )
            c.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES('command_limit_per_minute',?)",
                (str(DEFAULT_LIMIT_PER_MIN),),
            )
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('logging_enabled','0')")
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('log_chat_id','0')")

        with sqlite3.connect(ISTORIA_DB_PATH) as h:
            h.execute(
                """
                CREATE TABLE IF NOT EXISTS job_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nickname TEXT NOT NULL,
                    target_vk_id INTEGER NOT NULL,
                    old_faction TEXT,
                    old_position TEXT,
                    new_faction TEXT,
                    new_position TEXT,
                    actor_vk_id INTEGER,
                    event_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    secure_blob TEXT
                )
                """
            )
            try:
                h.execute("ALTER TABLE job_history ADD COLUMN secure_blob TEXT")
            except sqlite3.OperationalError:
                pass


# ---------------------------- Бот ----------------------------


@dataclass
class Ctx:
    user_id: int
    peer_id: int
    text: str
    reply_user_id: Optional[int] = None
    reply_text: Optional[str] = None
    message_cmid: Optional[int] = None
    message_id: Optional[int] = None
    reply_cmid: Optional[int] = None
    reply_message_id: Optional[int] = None
    reply_attachments: Optional[list[str]] = None
    reply_attachment_ids: Optional[list[str]] = None
    reply_link_urls: Optional[list[str]] = None
    platform: str = "vk"
    tg_is_chat: bool = False
    tg_username: Optional[str] = None

    @property
    def is_chat(self) -> bool:
        if self.platform == "tg":
            return self.tg_is_chat
        return self.peer_id >= 2_000_000_000

    @property
    def chat_id(self) -> Optional[int]:
        return self.peer_id - 2_000_000_000 if self.is_chat else None


# ---------------------------- БД Списков (с шифрованием) ----------------------------


class ListsDB:
    """
    Отдельная зашифрованная БД для глобальных списков.
    Содержимое каждого списка хранится в зашифрованном виде (XOR + base64).
    Ключ шифрования = ENCRYPTION_KEY_A + ENCRYPTION_KEY_B.
    Доступ контролируется полем min_admin_level.
    """

    def __init__(self, path: str, key_a: str, key_b: str) -> None:
        self.path = path
        self._key_a = key_a
        self._key_b = key_b
        self._init()

    @staticmethod
    def _xor_bytes(data: bytes, key: str) -> bytes:
        k = key.encode("utf-8")
        if not k:
            return data
        return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))

    def _encrypt(self, text: str) -> str:
        """Версионированное шифрование содержимого; старый XOR оставлен только для чтения."""
        return _secure_encrypt_text(text, self._key_a, self._key_b)

    def _decrypt(self, token: str) -> str:
        """Обратное декодирование."""
        secure = _secure_decrypt_text(token, self._key_a, self._key_b)
        if secure is not None:
            return secure
        try:
            b = base64.urlsafe_b64decode(token.encode("ascii"))
            step1 = self._xor_bytes(b, self._key_b)
            step2 = self._xor_bytes(step1, self._key_a)
            return step2.decode("utf-8")
        except Exception:
            return ""

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        # Дополнительная защита: запретить внешний доступ через SQLite ATTACH
        c.execute("PRAGMA trusted_schema=OFF")
        return c

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS global_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    min_admin_level INTEGER NOT NULL DEFAULT 0,
                    creator_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    content_enc TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_global_lists_admin ON global_lists(min_admin_level);

                CREATE TABLE IF NOT EXISTS list_access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name TEXT NOT NULL COLLATE NOCASE,
                    vk_id INTEGER NOT NULL,
                    granted_by INTEGER NOT NULL,
                    granted_at INTEGER NOT NULL,
                    UNIQUE(list_name, vk_id)
                );
                CREATE INDEX IF NOT EXISTS idx_list_access_name ON list_access(LOWER(list_name));
                CREATE INDEX IF NOT EXISTS idx_list_access_user ON list_access(vk_id);
                """
            )

    def create_list(self, name: str, min_admin: int, creator_id: int, ts: int) -> bool:
        """Создать новый список. Возвращает False если имя уже занято."""
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT INTO global_lists(name,min_admin_level,creator_id,created_at,updated_at,content_enc) VALUES(?,?,?,?,?,?)",
                    (name.strip(), int(min_admin), int(creator_id), ts, ts, self._encrypt("")),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_list(self, name: str) -> bool:
        with self.conn() as c:
            cur = c.execute("DELETE FROM global_lists WHERE name=?", (name.strip(),))
            return cur.rowcount > 0

    def get_list(self, name: str, user_admin_level: int, vk_id: Optional[int] = None) -> Optional[dict]:
        """Получить список, если у пользователя есть права доступа.
        Поиск без учёта регистра. Уровень 0 = доступно всем.
        Если vk_id указан — проверяется также персональный доступ.
        """
        with self.conn() as c:
            row = c.execute(
                "SELECT id,name,min_admin_level,creator_id,created_at,updated_at,content_enc FROM global_lists WHERE LOWER(name)=LOWER(?)",
                (name.strip(),),
            ).fetchone()
        if not row:
            return None
        min_adm = int(row["min_admin_level"])
        # Проверяем доступ по уровню
        has_level_access = (min_adm == 0 or user_admin_level >= min_adm)
        if not has_level_access:
            # Проверяем персональный доступ
            if vk_id is None:
                return None
            with self.conn() as c:
                personal = c.execute(
                    "SELECT 1 FROM list_access WHERE LOWER(list_name)=LOWER(?) AND vk_id=?",
                    (name.strip(), int(vk_id)),
                ).fetchone()
            if not personal:
                return None
        content = self._decrypt(str(row["content_enc"]))
        return {
            "id": row["id"],
            "name": row["name"],
            "min_admin_level": row["min_admin_level"],
            "creator_id": row["creator_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "content": content,
        }

    def update_list(self, name: str, new_content: str, ts: int) -> bool:
        enc = self._encrypt(new_content)
        with self.conn() as c:
            cur = c.execute(
                "UPDATE global_lists SET content_enc=?, updated_at=? WHERE name=? COLLATE NOCASE",
                (enc, ts, name.strip()),
            )
            return cur.rowcount > 0

    # ── Персональный доступ ────────────────────────────────────────────────

    def grant_access(self, list_name: str, vk_id: int, granted_by: int, ts: int) -> tuple[bool, str]:
        """Выдать персональный доступ к списку. Возвращает (ok, сообщение)."""
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM global_lists WHERE LOWER(name)=LOWER(?)", (list_name.strip(),)
            ).fetchone()
        if not row:
            return False, f"Список «{list_name}» не найден."
        try:
            with self.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO list_access(list_name,vk_id,granted_by,granted_at) VALUES(?,?,?,?)",
                    (list_name.strip(), int(vk_id), int(granted_by), ts),
                )
            return True, ""
        except Exception as e:
            return False, str(e)

    def revoke_access(self, list_name: str, vk_id: int) -> bool:
        """Отозвать персональный доступ."""
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM list_access WHERE LOWER(list_name)=LOWER(?) AND vk_id=?",
                (list_name.strip(), int(vk_id)),
            )
        return cur.rowcount > 0

    def get_access_entries(self, list_name: str) -> list[dict]:
        """Все персональные доступы к конкретному списку."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT vk_id, granted_by, granted_at FROM list_access "
                "WHERE LOWER(list_name)=LOWER(?) ORDER BY granted_at",
                (list_name.strip(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_access(self, list_name: str, user_admin_level: int, vk_id: int) -> bool:
        """Проверяет доступ: по уровню admin ИЛИ персональный."""
        with self.conn() as c:
            row = c.execute(
                "SELECT min_admin_level FROM global_lists WHERE LOWER(name)=LOWER(?)",
                (list_name.strip(),),
            ).fetchone()
        if not row:
            return False
        min_adm = int(row["min_admin_level"])
        if min_adm == 0 or user_admin_level >= min_adm:
            return True
        # Проверяем персональный доступ
        with self.conn() as c:
            personal = c.execute(
                "SELECT 1 FROM list_access WHERE LOWER(list_name)=LOWER(?) AND vk_id=?",
                (list_name.strip(), int(vk_id)),
            ).fetchone()
        return bool(personal)

    def all_lists(self, user_admin_level: int, vk_id: Optional[int] = None) -> list[dict]:
        """Вернуть все списки, доступные данному пользователю.
        Учитывает: уровень admin + персональный доступ.
        """
        with self.conn() as c:
            if vk_id is not None:
                rows = c.execute(
                    "SELECT DISTINCT g.id,g.name,g.min_admin_level,g.creator_id,g.created_at,g.updated_at "
                    "FROM global_lists g "
                    "LEFT JOIN list_access la ON LOWER(la.list_name)=LOWER(g.name) AND la.vk_id=? "
                    "WHERE g.min_admin_level=0 OR g.min_admin_level<=? OR la.vk_id IS NOT NULL "
                    "ORDER BY g.name COLLATE NOCASE",
                    (int(vk_id), int(user_admin_level)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id,name,min_admin_level,creator_id,created_at,updated_at FROM global_lists "
                    "WHERE min_admin_level=0 OR min_admin_level<=? ORDER BY name COLLATE NOCASE",
                    (int(user_admin_level),),
                ).fetchall()
        return [dict(r) for r in rows]


class CommunitySubscriptionsDB:
    def __init__(self, path: str, key_a: str, key_b: str) -> None:
        self.path = path
        self._key_a = key_a
        self._key_b = key_b
        self._init()

    def _encrypt(self, text: str) -> str:
        return _secure_encrypt_text(text, self._key_a, self._key_b)

    def _decrypt(self, token: str) -> str:
        secure = _secure_decrypt_text(token or "", self._key_a, self._key_b)
        return secure if secure is not None else str(token or "")

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA trusted_schema=OFF")
        return c

    def _init(self) -> None:
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    domain_enc TEXT NOT NULL,
                    title_enc TEXT NOT NULL,
                    url_enc TEXT NOT NULL,
                    last_post_id INTEGER NOT NULL DEFAULT 0,
                    ping_all INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(chat_id, owner_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_subscriptions_owner ON chat_subscriptions(owner_id);
                CREATE INDEX IF NOT EXISTS idx_chat_subscriptions_chat ON chat_subscriptions(chat_id);
                """
            )
            try:
                c.execute("ALTER TABLE chat_subscriptions ADD COLUMN ping_all INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    def add_subscription(self, chat_id: int, owner_id: int, domain: str, title: str, url: str, created_by: int, created_at: int, last_post_id: int) -> bool:
        with self.conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO chat_subscriptions
                (chat_id,owner_id,domain_enc,title_enc,url_enc,last_post_id,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    int(chat_id),
                    int(owner_id),
                    self._encrypt(domain),
                    self._encrypt(title),
                    self._encrypt(url),
                    int(last_post_id),
                    int(created_by),
                    int(created_at),
                ),
            )
            return cur.rowcount > 0

    def remove_subscription(self, chat_id: int, owner_id: int) -> bool:
        with self.conn() as c:
            cur = c.execute("DELETE FROM chat_subscriptions WHERE chat_id=? AND owner_id=?", (int(chat_id), int(owner_id)))
            return cur.rowcount > 0

    def list_chat(self, chat_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM chat_subscriptions WHERE chat_id=? ORDER BY id", (int(chat_id),)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM chat_subscriptions ORDER BY owner_id, chat_id").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_last_post(self, sub_id: int, last_post_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE chat_subscriptions SET last_post_id=? WHERE id=?", (int(last_post_id), int(sub_id)))

    def set_chat_ping_all(self, chat_id: int, enabled: bool) -> int:
        with self.conn() as c:
            cur = c.execute(
                "UPDATE chat_subscriptions SET ping_all=? WHERE chat_id=?",
                (1 if enabled else 0, int(chat_id)),
            )
            return int(cur.rowcount or 0)

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "chat_id": int(row["chat_id"]),
            "owner_id": int(row["owner_id"]),
            "domain": self._decrypt(row["domain_enc"]),
            "title": self._decrypt(row["title_enc"]),
            "url": self._decrypt(row["url_enc"]),
            "last_post_id": int(row["last_post_id"] or 0),
            "ping_all": int(row["ping_all"] or 0),
            "created_by": int(row["created_by"]),
            "created_at": int(row["created_at"]),
        }


class FactionBot:
    def __init__(self, token: str, group_id: int, db: DB):
        self.db = db
        self.group_id = group_id
        self.vk_session = vk_api.VkApi(token=token)
        self.api = self.vk_session.get_api()
        self.wall_api = vk_api.VkApi(token=WALL_READ_TOKEN).get_api() if WALL_READ_TOKEN else None
        self._wall_token_missing_logged = False
        self.longpoll = VkBotLongPoll(self.vk_session, group_id)
        self.running = True
        self.rate: dict[int, deque[float]] = defaultdict(deque)
        self.unapproved_notice_ts: dict[int, int] = {}
        self.user_name_cache: dict[int, tuple[str, int]] = {}
        self._active_command: tuple[int, str, int] | None = None
        self._logging_in_progress = False
        self.failed_access_window: dict[int, deque[int]] = defaultdict(deque)
        self.tg_token = TELEGRAM_BOT_TOKEN
        self.tg_offset = 0
        self.peer_routes: dict[int, tuple[str, int]] = {}
        self.community_group_ids: dict[str, int] = {}
        self.admin_console_selected_faction: dict[int, str] = {}
        self.http = requests.Session()
        self.command_last_run: dict[tuple[int, str], int] = {}
        self._int_setting_cache: dict[str, tuple[int, int]] = {}
        self.silence_window: dict[tuple[int, int], deque[int]] = defaultdict(deque)
        self.silence_spam_window: dict[tuple[int, int], deque[int]] = defaultdict(deque)
        self.chat_command_history: dict[int, deque[tuple[int, str, int]]] = defaultdict(lambda: deque(maxlen=200))
        self._current_user_for_parse: Optional[int] = None
        self.tg_senior_admin_runtime_id: Optional[int] = None
        # Пул потоков для параллельного выполнения команд
        self._cmd_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cmd")
        self._cmd_queue_slots = threading.BoundedSemaphore(128)
        # Инициализация БД списков
        self.lists_db = ListsDB(
            path=LISTS_DB_PATH,
            key_a=ENCRYPTION_KEY_A,
            key_b=ENCRYPTION_KEY_B,
        )
        self.subscriptions_db = CommunitySubscriptionsDB(
            path=SUBSCRIPTIONS_DB_PATH,
            key_a=ENCRYPTION_KEY_A,
            key_b=ENCRYPTION_KEY_B,
        )

    # ---------- infra ----------
    def _get_silence_admin_threshold(self) -> int:
        #Возвращает админ-уровень, дающий иммунитет к тишине"""
        return int(self._get_setting("silence_admin_threshold", "40"))

    def _resolve_route(self, peer_id: int) -> tuple[str, int]:
        route = self.peer_routes.get(peer_id)
        if route:
            return route
        with self.db.conn() as c:
            row = c.execute("SELECT tg_chat_id FROM tg_peer_routes WHERE peer_id=?", (peer_id,)).fetchone()
        if row:
            tg_chat_id = int(row["tg_chat_id"])
            self.peer_routes[peer_id] = ("tg", tg_chat_id)
            return ("tg", tg_chat_id)
        return ("vk", peer_id)

    def send(self, peer_id: int, text: str, disable_mentions: int = 1) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
        else:
            self.api.messages.send(peer_id=peer_id, random_id=0, message=text, disable_mentions=disable_mentions)
        self._log_command_response(peer_id, text)

    def send_with_keyboard(self, peer_id: int, text: str, buttons: list[list[str]]) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
            self._log_command_response(peer_id, text)
            return
        self.api.messages.send(
            peer_id=peer_id,
            random_id=0,
            message=text,
            keyboard=self._keyboard(buttons),
            disable_mentions=1,
        )
        self._log_command_response(peer_id, text)

    def send_with_attachments(self, peer_id: int, text: str, attachments: list[str], disable_mentions: int = 1) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
            return
        clean_attachments = [str(a) for a in attachments or [] if str(a).strip()]
        chunks = [clean_attachments[i:i + 10] for i in range(0, len(clean_attachments), 10)] or [[]]
        for idx, chunk in enumerate(chunks):
            params = {
                "peer_id": peer_id,
                "random_id": random.randint(1, 2_147_483_647),
                "message": text if idx == 0 else "📎 Вложения к сообщению выше",
                "disable_mentions": disable_mentions,
            }
            if chunk:
                params["attachment"] = ",".join(chunk)
            try:
                self.api.messages.send(**params)
            except Exception as e:
                logger.error(f"send_with_attachments chunk failed: {e}; attachments={chunk}")
                if idx == 0:
                    self.api.messages.send(
                        peer_id=peer_id,
                        random_id=random.randint(1, 2_147_483_647),
                        message=text,
                        disable_mentions=disable_mentions,
                    )
                for one in chunk:
                    try:
                        self.api.messages.send(
                            peer_id=peer_id,
                            random_id=random.randint(1, 2_147_483_647),
                            message="📎 Вложение к сообщению выше",
                            attachment=one,
                            disable_mentions=disable_mentions,
                        )
                    except Exception as one_e:
                        logger.error(f"send single attachment failed: {one_e}; attachment={one}")
        self._log_command_response(peer_id, text)

    def send_with_forwarded_message(
        self,
        peer_id: int,
        text: str,
        forward_message_ids: list[int],
        attachments: Optional[list[str]] = None,
    ) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
            return
        sent_forward = False
        ids = [str(int(x)) for x in forward_message_ids or [] if x]
        clean_attachments = [str(a) for a in attachments or [] if str(a).strip()]
        if ids:
            try:
                params = {
                    "peer_id": peer_id,
                    "random_id": random.randint(1, 2_147_483_647),
                    "message": text,
                    "forward_messages": ",".join(ids),
                    "disable_mentions": 1,
                }
                if clean_attachments and len(clean_attachments) <= 10:
                    params["attachment"] = ",".join(clean_attachments)
                self.api.messages.send(**params)
                sent_forward = True
            except Exception as e:
                logger.error(f"forward complaint message failed: {e}; ids={ids}")
        if clean_attachments and not sent_forward:
            self.send_with_attachments(peer_id, text, clean_attachments)
        elif not sent_forward:
            self.send(peer_id, text)
        elif clean_attachments and len(clean_attachments) > 10:
            self.send_with_attachments(peer_id, "📎 Дополнительные вложения к жалобе", clean_attachments[10:])
        self._log_command_response(peer_id, text)

    def send_ephemeral(self, peer_id: int, text: str, ttl_sec: int = 5) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
            self._log_command_response(peer_id, text)
            return
        try:
            msg_id = int(
                self.api.messages.send(
                    peer_id=peer_id,
                    random_id=0,
                    message=text,
                    disable_mentions=1,
                )
            )
        except Exception:
            self.send(peer_id, text)
            return
        self._log_command_response(peer_id, text)

        def _cleanup() -> None:
            time.sleep(max(1, int(ttl_sec)))
            self._api_method("messages.delete", {"message_ids": str(msg_id), "delete_for_all": 1})
            self._api_method("messages.delete", {"message_ids": str(msg_id)})

        threading.Thread(target=_cleanup, daemon=True).start()

    def _send_tg(self, chat_id: int, text: str) -> None:
        if not self.tg_token:
            return
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        try:
            self.http.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        except Exception:
            pass

    def _api_method(self, name: str, params: dict) -> bool:
        try:
            self.vk_session.method(name, params)
            return True
        except Exception:
            return False

    def _remember_chat_command(self, ctx: Ctx, text: str) -> None:
        if not ctx.is_chat or not text.startswith("!"):
            return
        if self._is_oblik_user(ctx.user_id):
            return
        cmd_name = text.split()[0].strip().lower()
        if cmd_name == "!я":
            return
        now = self.now_ts()
        with self.db.conn() as c:
            c.execute(
                "INSERT INTO chat_command_logs(chat_id,vk_id,command,created_at) VALUES(?,?,?,?)",
                (int(ctx.chat_id), int(ctx.user_id), cmd_name, now),
            )
            c.execute(
                """
                DELETE FROM chat_command_logs
                WHERE chat_id=? AND id NOT IN (
                    SELECT id FROM chat_command_logs
                    WHERE chat_id=?
                    ORDER BY id DESC
                    LIMIT 200
                )
                """,
                (int(ctx.chat_id), int(ctx.chat_id)),
            )

    def _last_chat_command_logs(self, chat_id: int, limit: int = 20) -> list[dict]:
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT vk_id,command,created_at
                FROM chat_command_logs
                WHERE chat_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(chat_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _keyboard(buttons: list[list[str]]) -> str:
        payload = {"one_time": True, "buttons": []}
        for row in buttons:
            payload["buttons"].append(
                [
                    {"action": {"type": "text", "label": label}, "color": "secondary"}
                    for label in row
                ]
            )
        return json.dumps(payload, ensure_ascii=False)

    def _vk_group_method(self, faction: str, method: str, params: dict) -> tuple[bool, dict]:
        token = FACTION_COMMUNITY_TOKENS.get(faction)
        if not token:
            return False, {"error": "token_missing"}
        url = f"https://api.vk.com/method/{method}"
        try:
            resp = self.http.post(url, data={**params, "access_token": token, "v": "5.199"}, timeout=12).json()
        except Exception:
            return False, {"error": "network"}
        if "error" in resp:
            return False, resp
        return True, resp.get("response", {})

    def send_dm(self, user_id: int, text: str) -> None:
        self.send(user_id, text)

    def send_user_notice(self, user_id: int, text: str) -> bool:
        tg_peer = TG_PEER_SHIFT + abs(int(user_id))
        if tg_peer in self.peer_routes:
            try:
                self.send(tg_peer, text)
                return True
            except Exception:
                return False
        try:
            self.send_dm(int(user_id), text)
            return True
        except Exception:
            return False

    def send_dm_vk_with_inline(self, user_id: int, text: str, request_id: int) -> bool:
        keyboard = {
            "inline": True,
            "buttons": [
                [
                    {"action": {"type": "callback", "label": f"Одобрить #{request_id}", "payload": json.dumps({"cmd": "approve", "id": request_id})}, "color": "positive"},
                    {"action": {"type": "callback", "label": f"Отказать #{request_id}", "payload": json.dumps({"cmd": "reject", "id": request_id})}, "color": "negative"},
                ]
            ],
        }
        try:
            self.api.messages.send(peer_id=user_id, random_id=0, message=text, keyboard=json.dumps(keyboard), disable_mentions=1)
            return True
        except Exception:
            return False

    def send_dm_vk_with_sync_approve(self, user_id: int, text: str, request_id: int) -> bool:
        keyboard = {
            "inline": True,
            "buttons": [
                [
                    {"action": {"type": "callback", "label": "Одобрить", "payload": json.dumps({"cmd": "sync_approve", "id": request_id})}, "color": "positive"},
                ]
            ],
        }
        try:
            self.api.messages.send(peer_id=user_id, random_id=0, message=text, keyboard=json.dumps(keyboard), disable_mentions=1)
            return True
        except Exception:
            return False

    def send_chat(self, chat_id: int, text: str) -> None:
        self.send(2_000_000_000 + chat_id, text)

    def now_ts(self) -> int:
        return int(time.time())

    def _fmt_msk_dt(self, ts: int) -> str:
        return datetime.fromtimestamp(int(ts), tz=MSK_TZ).strftime("%d.%m.%Y %H:%M (МСК)")

    def _is_senior_admin_ctx(self, ctx: Ctx) -> bool:
        if ctx.platform == "tg":
            uname = (ctx.tg_username or "").strip().lstrip("@").lower()
            if TG_SENIOR_ADMIN_ID > 0 and int(ctx.user_id) == TG_SENIOR_ADMIN_ID:
                return True
            if TG_SENIOR_ADMIN_USERNAME and uname and uname == TG_SENIOR_ADMIN_USERNAME:
                return True
            return self.tg_senior_admin_runtime_id is not None and int(ctx.user_id) == int(self.tg_senior_admin_runtime_id)
        return int(ctx.user_id) == int(self.db.senior_admin_id)

    def _check_rate(self, user_id: int) -> bool:
        lim = self._get_int_setting_cached("command_limit_per_minute", DEFAULT_LIMIT_PER_MIN, ttl_sec=30)
        dq = self.rate[user_id]
        cur = time.time()
        while dq and cur - dq[0] > 60:
            dq.popleft()
        if len(dq) >= lim:
            return False
        dq.append(cur)
        return True

    def _get_int_setting_cached(self, key: str, default: int, ttl_sec: int = 30) -> int:
        now = self.now_ts()
        cached = self._int_setting_cache.get(key)
        if cached and now - cached[1] <= ttl_sec:
            return cached[0]
        try:
            value = int(self._get_setting(key, str(default)))
        except Exception:
            value = default
        self._int_setting_cache[key] = (value, now)
        return value

    def _check_command_cooldown(self, user_id: int, cmd: str) -> tuple[bool, int]:
        cooldown = int(COMMAND_COOLDOWNS.get(cmd, 1))
        if cooldown <= 0:
            return True, 0
        key = (user_id, cmd)
        now = self.now_ts()
        last = self.command_last_run.get(key, 0)
        if now - last < cooldown:
            return False, cooldown - (now - last)
        self.command_last_run[key] = now
        return True, 0

    def _user(self, user_id: int) -> sqlite3.Row | None:
        user_id = self._canonical_user_id(user_id)
        with self.db.conn() as c:
            return c.execute("SELECT * FROM users WHERE vk_id=?", (user_id,)).fetchone()

    def _ensure_user(self, user_id: int) -> None:
        user_id = self._canonical_user_id(user_id)
        with self.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (user_id,))

    def _canonical_user_id(self, user_id: int) -> int:
        current = int(user_id)
        with self.db.conn() as c:
            for _ in range(3):
                row = c.execute("SELECT master_id FROM account_links WHERE user_id=?", (current,)).fetchone()
                if not row or int(row["master_id"]) == current:
                    break
                current = int(row["master_id"])
        return current

    def _has_personal_data_consent(self, user_id: int) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT consent_accepted FROM users WHERE vk_id=?", (user_id,)).fetchone()
        return bool(row and int(row["consent_accepted"] or 0) == 1)

    def _accept_personal_data_consent(self, user_id: int) -> None:
        with self.db.conn() as c:
            c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (user_id,))
            c.execute(
                "UPDATE users SET consent_accepted=1, consent_accepted_at=? WHERE vk_id=?",
                (self.now_ts(), user_id),
            )

    @staticmethod
    def _consent_message_text() -> str:
        return (
            "🔐 Перед началом работы с ботом\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Нажимая «Согласиться», вы подтверждаете:\n"
            "• согласие на обработку персональных данных\n"
            "• согласие с Политикой конфиденциальности\n\n"
            f"📄 Политика: {PRIVACY_POLICY_URL}"
        )

    def _send_consent_prompt(self, peer_id: int) -> None:
        route = self.peer_routes.get(peer_id, ("vk", peer_id))
        text = self._consent_message_text()
        if route[0] == "tg":
            self._send_tg(route[1], text + "\n\nОтправьте: СОГЛАСИТЬСЯ")
            self._log_command_response(peer_id, text)
            return
        self.api.messages.send(
            peer_id=peer_id,
            random_id=0,
            message=text,
            keyboard=self._keyboard([["СОГЛАСИТЬСЯ"]]),
            disable_mentions=1,
        )
        self._log_command_response(peer_id, text)

    def _handle_consent_gate(self, ctx: Ctx, low: str) -> bool:
        if ctx.is_chat:
            return False
        self._ensure_user(ctx.user_id)
        if self._has_personal_data_consent(ctx.user_id):
            return False
        normalized = low.strip()
        if normalized in {"согласен", "согласиться", "принять", "✅ принять", "✅ согласиться"}:
            self._accept_personal_data_consent(ctx.user_id)
            u = self._user(ctx.user_id)
            with self.db.conn() as c:
                sess = c.execute("SELECT 1 FROM registration_sessions WHERE vk_id=?", (ctx.user_id,)).fetchone()
            if not u or int(u["approved"] or 0) == 0:
                if not sess:
                    self.handle_start(ctx)
                    return True
            self.send(ctx.peer_id, "✅ Согласие сохранено.")
            return True
        if normalized in {"/start", "начать"}:
            self._send_consent_prompt(ctx.peer_id)
            return True
        return False

    def _member(self, chat_id: int, user_id: int) -> sqlite3.Row | None:
        with self.db.conn() as c:
            return c.execute(
                "SELECT * FROM chat_members WHERE chat_id=? AND vk_id=?", (chat_id, user_id)
            ).fetchone()

    def _ensure_member(self, chat_id: int, user_id: int) -> None:
        with self.db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO chat_members(chat_id,vk_id,role_level,immunity_level,banned) VALUES(?,?,0,0,0)",
                (chat_id, user_id),
            )
            c.execute(
                "INSERT OR IGNORE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)",
                (chat_id, 0, DEFAULT_ROLE_NAME),
            )

    def _parse_user_fast(self, token: str) -> Optional[int]:
        """Быстрый парсинг пользователя без VK API запросов.
        Работает только с числовыми id, [idXXX|...] упоминаниями и @цифры.
        Не делает resolveScreenName — используйте для горячих команд (!админ и т.п.)
        """
        token = token.strip()
        if token.lower() in {"я", "i"} and self._current_user_for_parse is not None:
            return int(self._current_user_for_parse)
        # Числовой id
        if token.isdigit():
            return int(token)
        # [idXXX|имя]
        m = re.search(r"\[(?:id)?(\d+)\|", token)
        if m:
            return int(m.group(1))
        # @цифры
        if token.startswith("@") and token[1:].isdigit():
            return int(token[1:])
        # id123 без скобок
        m = re.match(r"^id(\d+)$", token, re.I)
        if m:
            return int(m.group(1))
        # Из БД по нику (без VK API)
        with self.db.conn() as c:
            row = c.execute(
                "SELECT vk_id FROM users WHERE LOWER(COALESCE(nickname,''))=LOWER(?) LIMIT 1",
                (token,),
            ).fetchone()
            if row:
                return int(row["vk_id"])
        return None

    def _parse_user(self, token: str) -> Optional[int]:
        token = token.strip()
        if token.lower() in {"я", "i"} and self._current_user_for_parse is not None:
            return int(self._current_user_for_parse)
        token = (
            token.replace("m.vk.com/", "vk.com/")
            .replace("m.vk.ru/", "vk.ru/")
            .replace("vk.ru/", "vk.com/")
            .replace("https://vk.cc/", "https://vk.com/")
        )
        token = token.replace("telegram.me/", "t.me/")
        if token.startswith("https://t.me/") or token.startswith("http://t.me/"):
            token = token.split("t.me/", 1)[-1].split("?", 1)[0].strip("/")
            if token.isdigit():
                return int(token)
            return None
        if token.startswith("tg://user?id="):
            tg_id = token.split("tg://user?id=", 1)[-1].split("&", 1)[0]
            if tg_id.isdigit():
                return int(tg_id)
            return None
        if token.startswith("https://") or token.startswith("http://"):
            token = token.split("vk.com/", 1)[-1].split("?", 1)[0].strip("/")
        if token.startswith("@"):
            mention = token[1:]
            if mention.isdigit():
                return int(mention)
            with self.db.conn() as c:
                tg_row = c.execute("SELECT user_id FROM tg_usernames WHERE LOWER(username)=LOWER(?)", (mention,)).fetchone()
                if tg_row:
                    return int(tg_row["user_id"])
            token = mention
        m = re.search(r"id(\d+)", token)
        if m:
            return int(m.group(1))
        m = re.search(r"\[(?:id)?(\d+)\|", token)
        if m:
            return int(m.group(1))
        m = re.search(r"vk\\.(?:com|ru)/(id\\d+|[a-zA-Z0-9_.]+)", token)
        if m:
            token = m.group(1)
        if token.isdigit():
            return int(token)
        try:
            data = self.api.utils.resolveScreenName(screen_name=token)
            if data and data.get("type") == "user":
                return int(data["object_id"])
        except Exception:
            pass
        with self.db.conn() as c:
            row = c.execute("SELECT vk_id FROM users WHERE LOWER(COALESCE(nickname,''))=LOWER(?) LIMIT 1", (token,)).fetchone()
            if row:
                return int(row["vk_id"])
        return None


    def _safe_download_avatar(self, url: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[Optional[bytes], str]:
        """Скачивает только HTTPS-аватарку, полученную от VK API, с лимитом размера."""
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme != "https" or not parsed.netloc:
            return None, "VK вернул некорректную ссылку на аватарку."

        try:
            with self.http.get(url, stream=True, timeout=(5, 15), allow_redirects=False) as resp:
                if resp.status_code != 200:
                    return None, "Не удалось скачать аватарку пользователя."
                content_type = str(resp.headers.get("Content-Type") or "").lower()
                if not content_type.startswith("image/"):
                    return None, "VK вернул не изображение вместо аватарки."
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    return None, "Аватарка слишком большая для безопасной отправки."

                data = bytearray()
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        return None, "Аватарка слишком большая для безопасной отправки."
                if not data:
                    return None, "VK вернул пустое изображение аватарки."
                return bytes(data), ""
        except Exception as e:
            logger.error(f"avatar download failed for host={parsed.netloc}: {e}")
            return None, "Не удалось безопасно скачать аватарку."

    def _upload_message_photo_bytes(self, image: bytes, peer_id: int) -> Optional[str]:
        """Загружает байты изображения в сообщения VK и возвращает attachment photo..."""
        try:
            server = self.api.photos.getMessagesUploadServer(peer_id=int(peer_id))
            upload_url = str(server.get("upload_url") or "")
            parsed = urlparse(upload_url)
            if parsed.scheme != "https" or not parsed.netloc:
                logger.error("VK returned unsafe messages upload URL")
                return None

            files = {"photo": ("avatar.jpg", image, "image/jpeg")}
            upload = self.http.post(upload_url, files=files, timeout=(5, 20)).json()
            saved = self.api.photos.saveMessagesPhoto(
                photo=upload.get("photo"),
                server=upload.get("server"),
                hash=upload.get("hash"),
            )
            if not saved:
                return None
            photo = saved[0]
            owner_id = int(photo.get("owner_id"))
            photo_id = int(photo.get("id"))
            access_key = str(photo.get("access_key") or "")
            attachment = f"photo{owner_id}_{photo_id}"
            if access_key:
                attachment += f"_{access_key}"
            return attachment
        except Exception as e:
            logger.error(f"avatar upload failed: {e}")
            return None

    def cmd_avatar(self, ctx: Ctx, parts: list[str]) -> None:
        target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
        if target is None:
            self.send(ctx.peer_id, "🖼 Формат: !аватарка (@username|ссылка|id) или ответом на сообщение пользователя.")
            return
        if int(target) <= 0:
            self.send(ctx.peer_id, "❌ Можно показать аватарку только пользователя VK.")
            return

        try:
            users = self.api.users.get(
                user_ids=str(int(target)),
                fields="photo_id,photo_max,photo_max_orig,photo_400_orig,photo_200_orig,photo_100,photo_50",
            )
        except Exception as e:
            logger.error(f"users.get for avatar failed target={target}: {e}")
            self.send(ctx.peer_id, "❌ Не удалось получить профиль пользователя VK.")
            return

        if not users:
            self.send(ctx.peer_id, "❌ Пользователь не найден.")
            return

        user = users[0]

        photo_url = (
            user.get("photo_max_orig")
            or user.get("photo_max")
            or user.get("photo_400_orig")
            or user.get("photo_200_orig")
            or user.get("photo_100")
            or user.get("photo_50")
            or ""
        )
        photo_url = str(photo_url).strip()

        logger.info(
            f"[avatar] target={target} fields={ {k: user.get(k) for k in ['photo_id','photo_max_orig','photo_max','photo_400_orig','photo_200_orig','photo_100','photo_50']} }"
        )

        if not photo_url:
            self.send(ctx.peer_id, f"🖼 У пользователя {self._fmt_user(int(target))} VK не вернул ссылку на аватарку.")
            return

        if "camera_" in photo_url or "deactivated_" in photo_url:
            self.send(ctx.peer_id, f"🖼 У пользователя {self._fmt_user(int(target))} стоит стандартная аватарка VK.")
            return

        image, err = self._safe_download_avatar(photo_url)
        if image is None:
            self.send(ctx.peer_id, f"❌ {err}")
            return

        attachment = self._upload_message_photo_bytes(image, ctx.peer_id)
        if not attachment:
            self.send(ctx.peer_id, "❌ Не удалось загрузить аватарку в сообщение VK.")
            return

        self.send_with_attachments(
            ctx.peer_id,
            f"🖼 Аватарка пользователя {self._fmt_user(int(target))}:",
            [attachment],
        )




    def _fmt_user_ref(self, user_id: int, name: Optional[str] = None) -> str:
        if user_id <= 0:
            return "неизвестно"
        safe_name = (name or f"id{user_id}").strip() or f"id{user_id}"
        safe_name = safe_name.replace("[", "(").replace("]", ")").replace("|", "¦")
        return f"[id{user_id}|{safe_name}]"

    def _fmt_user(self, user_id: int) -> str:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return "неизвестно"
        return self._fmt_users_bulk([uid]).get(uid, "неизвестно")

    def _fmt_users_bulk(self, user_ids: list[int] | tuple[int, ...] | set[int]) -> dict[int, str]:
        """Форматирует пользователей пачкой, чтобы списковые команды не делали VK API запрос на каждую строку."""
        now = self.now_ts()
        ids: list[int] = []
        seen: set[int] = set()
        for raw_uid in user_ids:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid in seen:
                continue
            seen.add(uid)
            ids.append(uid)

        formatted: dict[int, str] = {}
        missing: list[int] = []
        for uid in ids:
            cached = self.user_name_cache.get(uid)
            if cached and now - cached[1] < 3600:
                formatted[uid] = self._fmt_user_ref(uid, cached[0])
            else:
                missing.append(uid)

        # VK users.get принимает список id, поэтому забираем имена пачками вместо десятков
        # последовательных сетевых запросов. Это особенно ускоряет !жалобы и !админы.
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            try:
                infos = self.api.users.get(user_ids=",".join(str(uid) for uid in chunk))
            except Exception:
                infos = []
            for info in infos or []:
                try:
                    uid = int(info.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if uid <= 0:
                    continue
                full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or f"id{uid}"
                self.user_name_cache[uid] = (full_name, now)
                formatted[uid] = self._fmt_user_ref(uid, full_name)

        for uid in ids:
            formatted.setdefault(uid, self._fmt_user_ref(uid))
        return formatted

    def _send_long_text(self, peer_id: int, text: str, max_len: int = 3500) -> None:
        """Безопасно отправляет длинный ответ несколькими сообщениями, не теряя строки."""
        text = str(text or "")
        if len(text) <= max_len:
            self.send(peer_id, text)
            return
        part_lines: list[str] = []
        part_len = 0
        for line in text.splitlines():
            extra = len(line) + (1 if part_lines else 0)
            if part_lines and part_len + extra > max_len:
                self.send(peer_id, "\n".join(part_lines))
                part_lines = []
                part_len = 0
            if len(line) > max_len:
                while len(line) > max_len:
                    if part_lines:
                        self.send(peer_id, "\n".join(part_lines))
                        part_lines = []
                        part_len = 0
                    self.send(peer_id, line[:max_len])
                    line = line[max_len:]
            part_lines.append(line)
            part_len += len(line) + (1 if part_len else 0)
        if part_lines:
            self.send(peer_id, "\n".join(part_lines))

    def _get_setting(self, key: str, default: str = "") -> str:
        with self.db.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _normalize_command_name(self, raw_cmd: str) -> str:
        cmd = raw_cmd.strip().lower()
        if not cmd.startswith("!"):
            cmd = f"!{cmd}"
        return self._resolve_alias(cmd)

    def _extract_command_token(self, raw_tokens: list[str]) -> Optional[str]:
        """Извлекает каноническое имя команды из списка токенов.
        Поддерживает многословные команды: '!дж заголовок', '!новый список', и т.д.
        Алгоритм: пробуем самую длинную подстроку токенов, которая есть в COMMAND_ACCESS.
        """
        if not raw_tokens:
            return None
        tokens = [t.strip().lower() for t in raw_tokens if t and t.strip()]
        if not tokens:
            return None
        # Нормализуем каждый токен
        normalized_tokens = []
        for t in tokens:
            n = t if t.startswith("!") else f"!{t}"
            normalized_tokens.append(n)

        # Пробуем от самого длинного совпадения к самому короткому
        for length in range(len(normalized_tokens), 0, -1):
            candidate = " ".join(normalized_tokens[:length])
            # Убираем ! со второго слова и далее для многословных команд
            # (команды хранятся как "!дж заголовок", "!новый список" и т.п.)
            if length > 1:
                parts_cand = normalized_tokens[:length]
                # Первое слово с !, остальные без
                candidate_clean = parts_cand[0] + " " + " ".join(p.lstrip("!") for p in parts_cand[1:])
            else:
                candidate_clean = normalized_tokens[0]
            if candidate_clean in COMMAND_ACCESS:
                return candidate_clean
            # Также пробуем через _resolve_alias
            resolved = self._resolve_alias(candidate_clean)
            if resolved in COMMAND_ACCESS:
                return resolved

        # Fallback: первый токен через normalize
        return self._normalize_command_name(tokens[0])

    def _get_admin_min(self, cmd: str) -> int:
        """Возвращает минимальный admin_level для команды.
        Если переопределено через !админправо — возвращает это значение (может быть 0).
        Если не переопределено — возвращает дефолт из ADMIN_MIN/COMMAND_ACCESS.
        """
        normalized = self._normalize_command_name(cmd)
        with self.db.conn() as c:
            row = c.execute("SELECT min_admin FROM command_admin_rights WHERE command=?", (normalized,)).fetchone()
            if not row and normalized.startswith("!"):
                legacy = normalized[1:]
                row = c.execute("SELECT min_admin FROM command_admin_rights WHERE command=?", (legacy,)).fetchone()
        if row:
            return int(row[0])
        return ADMIN_MIN.get(normalized, 0)

    def _get_effective_admin_min(self, cmd: str) -> int:
        """Возвращает итоговый минимальный admin_level с учётом всех источников.
        Порядок приоритетов:
          1. Если !админправо переопределило команду — берём это значение (даже 0).
          2. Иначе берём COMMAND_ACCESS[cmd]["admin_default"].
          3. Иначе ADMIN_MIN.get(cmd, 0).
        """
        normalized = self._normalize_command_name(cmd)
        with self.db.conn() as c:
            row = c.execute("SELECT min_admin FROM command_admin_rights WHERE command=?", (normalized,)).fetchone()
            if not row and normalized.startswith("!"):
                row = c.execute(
                    "SELECT min_admin FROM command_admin_rights WHERE command=?", (normalized[1:],)
                ).fetchone()
        if row is not None:
            # Явное переопределение через !админправо — оно главное
            return int(row[0])
        # Нет переопределения — берём из кода
        code_val = COMMAND_ACCESS.get(cmd, {}).get("admin_default", 0)
        if code_val > 0:
            return code_val
        return ADMIN_MIN.get(normalized, 0)

    def _record_failed_access(self, user_id: int, reason: str) -> None:
        """Записывает неудачную попытку доступа.
        При 5+ попытках за 5 минут — уведомляет лог-чат.
        При 15+ попытках за 5 минут — автоматический ботбан (защита от брутфорса).
        """
        if int(user_id) == int(self.db.senior_admin_id):
            return  # Старшего админа никогда не баним
        # Не баним пользователей фракции Админ и с высоким admin_level
        if self._is_admin_faction_user(user_id) or self._get_admin_level(user_id) >= 50:
            return
        now = self.now_ts()
        dq = self.failed_access_window[user_id]
        dq.append(now)
        while dq and now - dq[0] > 300:
            dq.popleft()
        attempts = len(dq)
        log_chat_id = int(self._get_setting("log_chat_id", "0") or 0)
        if attempts >= 5:
            if log_chat_id > 0:
                self._logging_in_progress = True
                try:
                    self.send_chat(
                        log_chat_id,
                        f"⚠️ Аномалия доступа: {self._fmt_user(user_id)} ({reason}), попыток за 5 мин: {attempts}",
                    )
                except Exception:
                    pass
                finally:
                    self._logging_in_progress = False
        if attempts >= 15:
            # Автоматический ботбан за агрессивные попытки
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO bot_bans(vk_id) VALUES(?)", (user_id,))
            autoban_msg = f"🚫 АВТОБАН: id{user_id} — {attempts} несанкционированных попыток за 5 мин. Причина: {reason}"
            logger.warning(f"AUTOBAN: user={user_id} attempts={attempts} reason={reason!r}")
            # Уведомляем старшего админа в ЛС
            self._alert_senior_admin(autoban_msg)
            if log_chat_id > 0:
                self._logging_in_progress = True
                try:
                    self.send_chat(log_chat_id, autoban_msg)
                except Exception:
                    pass
                finally:
                    self._logging_in_progress = False

    @staticmethod
    def _contains_prompt_injection(text: str) -> bool:
        """Проверяет наличие паттернов инъекций/попыток взлома в тексте команды."""
        t = text.lower()
        patterns = [
            "ignore previous instructions",
            "system prompt",
            "developer message",
            "jailbreak",
            "bypass",
            "отключи проверки",
            "игнорируй инструкции",
            "forget your instructions",
            "you are now",
            "act as",
            "new persona",
            "override admin",
            "дай права",
            "установи уровень",
            "__import__",
            "exec(",
            "eval(",
            "os.system",
            "subprocess",
            # SQL-injection стоп-слова (т.к. команды парсятся вручную, но для заметок/причин)
            "'; drop",
            "1=1--",
            "union select",
        ]
        return any(p in t for p in patterns)

    @staticmethod
    def _sanitize_input(text: str, max_len: int = 500) -> str:
        """Обрезает слишком длинный ввод и убирает control-символы."""
        # Удаляем control chars (кроме newline/tab)
        cleaned = "".join(c for c in text if c >= " " or c in "\n\t")
        return cleaned[:max_len]

    def _is_immune_to_silence(self, user_id: int, chat_id: int) -> bool:
        """Проверяет иммунитет к режиму тишины.
        Иммунитет имеют:
          1. Пользователи с admin_level >= silence_admin_threshold (настраивается через !тишина админпорог)
          2. Пользователи, добавленные в silence_exceptions для этого чата (временный голос)
        """
        # Динамический порог из настроек (по умолчанию 40)
        threshold = self._get_silence_admin_threshold()
        if threshold > 0 and self._get_admin_level(user_id) >= threshold:
            return True
        return False

    def _enforce_chat_silence(self, ctx: Ctx, conversation_message_id: Optional[int]) -> bool:
        if not ctx.is_chat:
            return False

        # Постоянный иммунитет по admin_level >= порог
        if self._is_immune_to_silence(ctx.user_id, ctx.chat_id):
            return False

        now = self.now_ts()
        with self.db.conn() as c:
            rule = c.execute(
                "SELECT window_min,msg_limit,enabled FROM chat_silence WHERE chat_id=?",
                (ctx.chat_id,),
            ).fetchone()
            if not rule or int(rule["enabled"] or 0) == 0:
                return False
            # Временный голос (silence_exceptions)
            ex = c.execute(
                "SELECT until_ts FROM silence_exceptions WHERE chat_id=? AND vk_id=?",
                (ctx.chat_id, ctx.user_id),
            ).fetchone()
            if ex:
                if int(ex["until_ts"] or 0) > now:
                    return False  # Временный голос активен
                else:
                    c.execute(
                        "DELETE FROM silence_exceptions WHERE chat_id=? AND vk_id=?",
                        (ctx.chat_id, ctx.user_id),
                    )
        window_sec = int(rule["window_min"]) * 60
        limit = int(rule["msg_limit"])
        key = (ctx.chat_id, ctx.user_id)
        spam_dq = self.silence_spam_window[key]
        while spam_dq and now - spam_dq[0] > 60:
            spam_dq.popleft()
        spam_dq.append(now)
        dq = self.silence_window[key]
        while dq and now - dq[0] > window_sec:
            dq.popleft()
        if len(dq) >= limit:
            if conversation_message_id is not None:
                self._api_method(
                    "messages.delete",
                    {"peer_id": ctx.peer_id, "cmids": str(conversation_message_id), "delete_for_all": 1},
                )
            if len(spam_dq) > 10:
                until = now + 3600
                already_muted = False
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO chat_members(chat_id,vk_id) VALUES(?,?)",
                        (ctx.chat_id, ctx.user_id),
                    )
                    muted = c.execute(
                        "SELECT muted_until FROM chat_members WHERE chat_id=? AND vk_id=?",
                        (ctx.chat_id, ctx.user_id),
                    ).fetchone()
                    already_muted = bool(muted and int(muted["muted_until"] or 0) > now)
                    if not already_muted:
                        c.execute(
                            "UPDATE chat_members SET muted_until=? WHERE chat_id=? AND vk_id=?",
                            (until, ctx.chat_id, ctx.user_id),
                        )
                        c.execute(
                            "INSERT INTO mute_logs(chat_id,vk_id,issuer_id,reason,created_at,until_ts,active) "
                            "VALUES(?,?,?,?,?,?,1)",
                            (ctx.chat_id, ctx.user_id, 0, "Спам в режиме тишины", now, until),
                        )
                if not already_muted:
                    try:
                        self._apply_vk_mute(ctx.peer_id, ctx.user_id, 3600)
                    except Exception:
                        pass
                    self.send(
                        ctx.peer_id,
                        f"Пользователю {self._fmt_user(ctx.user_id)} выдан мут, причина: Спам в режиме тишины",
                    )
            return True
        dq.append(now)
        return False

    def _log_command_response(self, peer_id: int, response_text: str) -> None:
        if self._logging_in_progress or not self._active_command:
            return
        actor_id, cmd_text, origin_peer_id = self._active_command
        cmd_name = cmd_text.split()[0].strip().lower() if cmd_text.split() else ""
        if cmd_name == "!я":
            self._active_command = None
            return
        if self._is_oblik_user(actor_id):
            self._active_command = None
            return
        if peer_id != origin_peer_id:
            return
        if self._get_setting("logging_enabled", "0") != "1":
            self._active_command = None
            return
        log_chat_id = int(self._get_setting("log_chat_id", "0") or 0)
        if log_chat_id <= 0:
            self._active_command = None
            return
        self._logging_in_progress = True
        try:
            self.send_chat(
                log_chat_id,
                f"🧾 ЛОГ\nКоманда: {cmd_text}\nКем: {self._fmt_user(actor_id)}\nОтвет: {response_text}",
            )
        except Exception:
            pass
        finally:
            self._logging_in_progress = False
            self._active_command = None

    @staticmethod
    def _faction_group(faction: Optional[str]) -> str:
        if faction in STATE_FACTIONS:
            return "state"
        if faction in MAFIA_FACTIONS:
            return "mafia"
        if faction == SECRET_FACTION:
            return "admin"
        return "other"

    @staticmethod
    def _xor_bytes(data: bytes, key: str) -> bytes:
        k = key.encode("utf-8")
        if not k:
            return data  # пустой ключ → без шифрования
        return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))

    def _double_encrypt(self, text: str) -> str:
        return _secure_encrypt_text(text, ENCRYPTION_KEY_A, ENCRYPTION_KEY_B)

    def _double_decrypt(self, token: str) -> str:
        secure = _secure_decrypt_text(token, ENCRYPTION_KEY_A, ENCRYPTION_KEY_B)
        if secure is not None:
            return secure
        try:
            b = base64.urlsafe_b64decode(token.encode("ascii"))
            step1 = self._xor_bytes(b, ENCRYPTION_KEY_B)
            step2 = self._xor_bytes(step1, ENCRYPTION_KEY_A)
            return step2.decode("utf-8")
        except Exception:
            return ""

    def _add_history(
        self,
        nickname: str,
        target_vk_id: int,
        old_faction: Optional[str],
        old_position: Optional[str],
        new_faction: Optional[str],
        new_position: Optional[str],
        actor_vk_id: Optional[int],
        event_type: str,
    ) -> None:
        with sqlite3.connect(ISTORIA_DB_PATH) as h:
            secure = self._double_encrypt(
                json.dumps(
                    {
                        "old_faction": old_faction,
                        "old_position": old_position,
                        "new_faction": new_faction,
                        "new_position": new_position,
                        "actor_vk_id": actor_vk_id,
                    },
                    ensure_ascii=False,
                )
            )
            h.execute(
                """
                INSERT INTO job_history(
                    nickname,target_vk_id,old_faction,old_position,new_faction,new_position,actor_vk_id,event_type,created_at,secure_blob
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    nickname or "не указан",
                    target_vk_id,
                    old_faction,
                    old_position,
                    new_faction,
                    new_position,
                    actor_vk_id,
                    event_type,
                    self.now_ts(),
                    secure,
                ),
            )

    def _missing_data(self, cmd: str, parts: list[str], ctx: Ctx) -> bool:
        min_tokens = {
            "!бан": 2,
            "!кик": 2,
            "!разбан": 2,
            "!мут": 2,
            "!размут": 2,
            "!пред": 2,
            "!снятьпред": 2,
            "!снятьчс": 3,
            "!чс": 4,
            "!пастечат": 2,
            "!узнать": 2,
            "!фракция": 3,
            "!скрыть": 2,
            "!раскрыть": 2,
            "!уволить": 2,
            "!нанять": 3,
            "!создать": 4,
            "!поиск": 2,
            "!новый": 5,
            "!снять": 3,
            "!лидер": 3,
            "!право": 3,
            "!админ": 4,
            "!лимиткоманд": 2,
            "!банфракция": 3,
            "!переименовать": 3,
            "!изменить": 4,
            "!выговор": 3,
            "!супербан": 3,
            "!оффадминувед": 2,
            "!админувед": 2,
            "!рейтинг": 4,
            "!админправо": 3,
            "!закладка": 2,
            "!дубль": 2,
            "!одобритьдубль": 2,
            "!стереть": 2,
            "!допинфа": 3,
            "!инфа": 2,
            "!удалитьинфу": 4,
            "!облик": 3,
        }
        if cmd in {"!бан", "!кик", "!разбан", "!мут", "!размут", "!пред", "!снятьпред", "!узнать", "!скрыть", "!раскрыть", "!уволить", "!нанять", "!банфракция", "!выговор", "!супербан"}:
            need = min_tokens.get(cmd, 2)
            # Если команда вызывается ответом на сообщение, допускаем отсутствие аргумента (пользователь)
            if ctx.reply_user_id is not None and len(parts) >= need - 1:
                return False
        if cmd in min_tokens and len(parts) < min_tokens[cmd]:
            return True
        return False

    def _missing_args_message(self, cmd: str) -> str:
        usage = COMMAND_USAGE.get(cmd, cmd)
        args = COMMAND_REQUIRED_ARGS.get(cmd)
        if not args:
            return f"❗ Недостаточно аргументов.\nФормат: {usage}"
        return (
            "❗ Недостаточно аргументов.\n"
            f"Формат: {usage}\n"
            f"Требуются: {', '.join(args)}"
        )

    @staticmethod
    def _normalize_bang_command(text: str) -> str:
        return re.sub(r"^!\s+", "!", text.strip(), flags=re.IGNORECASE)

    @staticmethod
    def _resolve_alias(cmd: str) -> str:
        """Нормализует команду: сначала точный поиск по алиасам,
        затем нечёткое сопоставление через расстояние Левенштейна."""
        # 1. Точное совпадение
        for canonical, aliases in COMMAND_ALIASES.items():
            if cmd == canonical or cmd in aliases:
                return canonical
        # 2. Точное совпадение с известными командами
        if cmd in COMMAND_ACCESS:
            return cmd
        # 3. Нечёткое сопоставление
        fuzzy = _fuzzy_resolve_command(cmd)
        if fuzzy != cmd:
            # Ещё раз прогоняем через алиасы на случай если fuzzy вернул алиас
            for canonical, aliases in COMMAND_ALIASES.items():
                if fuzzy == canonical or fuzzy in aliases:
                    return canonical
            return fuzzy
        return cmd

    def _get_role_level(self, chat_id: int, user_id: int) -> int:
        self._ensure_member(chat_id, user_id)
        row = self._member(chat_id, user_id)
        return int(row["role_level"]) if row else 0

    def _is_oblik_user(self, user_id: int) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT enabled FROM oblik_users WHERE vk_id=?", (int(user_id),)).fetchone()
        return bool(row and int(row["enabled"] or 0) == 1)

    def _get_admin_level_visible(self, user_id: int) -> int:
        self._ensure_user(user_id)
        u = self._user(user_id)
        return int(u["admin_level"]) if u else 0

    def _get_admin_level(self, user_id: int) -> int:
        if self._is_oblik_user(user_id):
            return 100
        return self._get_admin_level_visible(user_id)

    def _is_bot_banned(self, user_id: int) -> bool:
        with self.db.conn() as c:
            return c.execute("SELECT 1 FROM bot_bans WHERE vk_id=?", (user_id,)).fetchone() is not None

    def _is_bot_closed(self) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key='bot_closed'").fetchone()
        return bool(row and row[0] == "1")

    def _can_affect(self, actor_id: int, target_id: int, chat_id: Optional[int]) -> bool:
        actor_admin = self._get_admin_level(actor_id)
        target_admin = self._get_admin_level(target_id)
        if chat_id is not None:
            is_chat_leader = self._is_leader_for_chat_faction(actor_id, chat_id)
            a_role = self._get_role_level(chat_id, actor_id)
            t_role = self._get_role_level(chat_id, target_id)
            with self.db.conn() as c:
                t = c.execute(
                    "SELECT immunity_level FROM chat_members WHERE chat_id=? AND vk_id=?",
                    (chat_id, target_id),
                ).fetchone()
                imm = int(t[0]) if t else 0
            if is_chat_leader and actor_admin >= target_admin:
                return True
            if actor_admin < 100 and actor_admin <= target_admin and a_role <= t_role:
                return False
            if a_role <= imm and actor_admin < 100:
                return False
        else:
            if actor_admin <= target_admin:
                return False
        return True

    def _has_custom_role_right(self, chat_id: Optional[int], cmd: str) -> bool:
        normalized = self._normalize_command_name(cmd)
        with self.db.conn() as c:
            if chat_id is not None:
                row = c.execute(
                    "SELECT 1 FROM chat_command_rights WHERE chat_id=? AND command=?",
                    (chat_id, normalized),
                ).fetchone()
                if not row and normalized.startswith("!"):
                    row = c.execute(
                        "SELECT 1 FROM chat_command_rights WHERE chat_id=? AND command=?",
                        (chat_id, normalized[1:]),
                    ).fetchone()
                if row:
                    return True
            row = c.execute("SELECT 1 FROM command_rights WHERE command=?", (normalized,)).fetchone()
            if row:
                return True
            if normalized.startswith("!"):
                legacy = normalized[1:]
                row = c.execute("SELECT 1 FROM command_rights WHERE command=?", (legacy,)).fetchone()
                return row is not None
        return False

    def _required_role(self, cmd: str, chat_id: Optional[int] = None) -> int:
        normalized = self._normalize_command_name(cmd)
        with self.db.conn() as c:
            if chat_id is not None:
                row = c.execute(
                    "SELECT min_role FROM chat_command_rights WHERE chat_id=? AND command=?",
                    (chat_id, normalized),
                ).fetchone()
                if not row and normalized.startswith("!"):
                    legacy = normalized[1:]
                    row = c.execute(
                        "SELECT min_role FROM chat_command_rights WHERE chat_id=? AND command=?",
                        (chat_id, legacy),
                    ).fetchone()
                if row:
                    return int(row[0])
            row = c.execute("SELECT min_role FROM command_rights WHERE command=?", (normalized,)).fetchone()
            if not row and normalized.startswith("!"):
                legacy = normalized[1:]
                row = c.execute("SELECT min_role FROM command_rights WHERE command=?", (legacy,)).fetchone()
        return int(row[0]) if row else DEFAULT_COMMAND_RIGHTS.get(normalized, 0)

    def _has_access(self, ctx: Ctx, cmd: str) -> bool:
        """Проверяет доступ пользователя к команде.

        Приоритет правил:
        1. Старший админ → всегда True.
        2. Ботбан → всегда False.
        3. Персональный запрет (!запретить) → False.
        4. Персональное разрешение (!разрешить) → True.
        5. Команда отключена в этом чате → False.
        6. Спецкейсы (!изменить, !чат для лидера).
        7. effective_admin_min → check admin_level и role.
        """
        if self._is_senior_admin_ctx(ctx):
            return True
        if self._is_bot_banned(ctx.user_id):
            return False
        cmd_norm = self._normalize_command_name(cmd)
        if not ctx.is_chat and cmd_norm in CHAT_ONLY_COMMANDS:
            return False
        # Персональные запреты/разрешения — один запрос
        with self.db.conn() as c:
            denied = c.execute(
                "SELECT 1 FROM user_cmd_deny WHERE target_vk_id=? AND LOWER(command)=LOWER(?)",
                (ctx.user_id, cmd_norm),
            ).fetchone()
            allowed = c.execute(
                "SELECT granted_by FROM user_cmd_allow WHERE target_vk_id=? AND LOWER(command)=LOWER(?)",
                (ctx.user_id, cmd_norm),
            ).fetchone()
        if denied:
            return False
        if ctx.is_chat:
            with self.db.conn() as c:
                off = c.execute(
                    "SELECT 1 FROM chat_disabled_commands WHERE chat_id=? AND command=?",
                    (ctx.chat_id, self._normalize_command_name(cmd)),
                ).fetchone()
            if off:
                return False

        admin_lvl = self._get_admin_level(ctx.user_id)

        # ── Спецкейсы ─────────────────────────────────────────────────────────
        if cmd == "!изменить":
            return admin_lvl >= 50 or (5 <= admin_lvl <= 10)
        if cmd == "!чат" and ctx.is_chat and self._is_leader_user(ctx.user_id):
            return True
        # Лидер фракции имеет полный доступ к управлению ролями (выдача/снятие/создание/
        # удаление/переименование роли, список ролей) в чате своей фракции, независимо
        # от его текущей роли в этом чате.
        if ctx.is_chat and self._is_role_management_command(cmd, ctx.text):
            if self._is_leader_for_chat_faction(ctx.user_id, ctx.chat_id):
                return True

        # ── Вычисляем effective_admin_min ─────────────────────────────────────
        # Использует _get_effective_admin_min: учитывает !админправо + код + ADMIN_MIN
        # !админправо имеет наивысший приоритет и может менять порог в любую сторону.
        effective_admin_min = self._get_effective_admin_min(cmd)
        if allowed:
            if effective_admin_min <= 0:
                return True
            granter_id = int(allowed["granted_by"] or 0)
            if granter_id == int(self.db.senior_admin_id) or self._get_admin_level(granter_id) >= effective_admin_min:
                return True

        # ── Команды с требованием admin_level ────────────────────────────────
        if effective_admin_min > 0:
            if admin_lvl >= effective_admin_min:
                return True
            # admin_level недостаточен. Роль помогает ТОЛЬКО при явном кастомном праве.
            if ctx.is_chat and self._has_custom_role_right(ctx.chat_id, cmd):
                role_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
                return role_lvl >= self._required_role(cmd, ctx.chat_id)
            # Нет кастомного права → доступ закрыт, даже с высокой ролью
            return False

        # ── Публичные команды (effective_admin_min == 0) ──────────────────────
        # Чат-only команды недоступны в ЛС
        if not ctx.is_chat and cmd_norm in CHAT_ONLY_COMMANDS:
            return False
        if ctx.is_chat:
            role_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
            return role_lvl >= self._required_role(cmd, ctx.chat_id)
        return True

    def _target_from_args_or_reply(self, ctx: Ctx, token: Optional[str]) -> Optional[int]:
        if token:
            parsed = self._parse_user(token)
            if parsed:
                return parsed
        return ctx.reply_user_id

    def _extract_faction_and_rest(self, parts: list[str], start_idx: int) -> tuple[Optional[str], list[str]]:
        joined = " ".join(parts[start_idx:]).strip()
        joined_low = joined.lower()
        for faction in sorted(ALL_FACTIONS, key=len, reverse=True):
            if joined_low.startswith(faction.lower()):
                rest = joined[len(faction):].strip()
                return faction, rest.split() if rest else []
        return None, []

    def _faction_positions(self, faction: str) -> list[str]:
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT DISTINCT position
                FROM users
                WHERE faction=? AND position IS NOT NULL AND TRIM(position) != '' AND LOWER(position) != 'не указана'
                ORDER BY position COLLATE NOCASE
                """,
                (faction,),
            ).fetchall()
        return [r["position"] for r in rows]

    def _is_admin_faction_user(self, user_id: int) -> bool:
        u = self._user(user_id)
        return bool(u and u["faction"] == SECRET_FACTION)

    def _is_leader_user(self, user_id: int) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT 1 FROM leaders WHERE vk_id=? LIMIT 1", (user_id,)).fetchone()
        return row is not None

    def _complaint_admin_mentions(self, chat_id: int, server_id: int) -> list[int]:
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT u.vk_id
                FROM users u
                LEFT JOIN admin_notify_settings s ON s.vk_id=u.vk_id
                WHERE u.faction=? AND COALESCE(s.enabled,1)=1 AND COALESCE(u.server_id,1)=?
                ORDER BY u.vk_id
                """,
                (SECRET_FACTION, int(server_id or 1)),
            ).fetchall()
        return [int(r["vk_id"]) for r in rows]

    def _chat_peer_id(self, chat_id: int) -> int:
        return 2_000_000_000 + int(chat_id)

    def _broadcast_to_chat_rows(self, rows: list[sqlite3.Row], text: str) -> int:
        sent = 0
        for row in rows:
            chat_id = int(row["chat_id"])
            peer_id = self._chat_peer_id(chat_id)
            try:
                self.send(peer_id, text)
                sent += 1
                if sent % 8 == 0:
                    time.sleep(5)
            except Exception:
                continue
        return sent

    def _platform_profile_ref(self, platform: str, user_id: int) -> str:
        if platform == "tg":
            return f"tg://user?id={int(user_id)}"
        return f"https://vk.com/id{int(user_id)}"

    @staticmethod
    def _normalize_note_key(name: str) -> str:
        return re.sub(r"\s+", " ", (name or "").strip()).casefold()

    def _find_note_by_name(self, chat_id: int, raw_name: str, with_content: bool = False) -> Optional[sqlite3.Row]:
        wanted = self._normalize_note_key(raw_name)
        if not wanted:
            return None
        columns = "name,min_role,attachments,content,source_message_id" if with_content else "name,min_role,attachments,source_message_id"
        with self.db.conn() as c:
            rows = c.execute(f"SELECT {columns} FROM notes WHERE chat_id=?", (chat_id,)).fetchall()
        for row in rows:
            if self._normalize_note_key(str(row["name"] or "")) == wanted:
                return row
        return None

    def _format_user_extra_info(self, target_id: int) -> str:
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT author_id,info_text,created_at FROM user_extra_info WHERE target_id=? ORDER BY id ASC",
                (int(target_id),),
            ).fetchall()
        if not rows:
            return f"ℹ️ Доп. информация для {self._fmt_user(int(target_id))} отсутствует."
        lines = [f"🗂 Доп. информация для {self._fmt_user(int(target_id))}:"]
        for idx, row in enumerate(rows, start=1):
            dt = datetime.fromtimestamp(int(row["created_at"])).strftime("%d:%m:%Y")
            lines.append(f"{idx}. {self._fmt_user(int(row['author_id']))} — {row['info_text']} — дата {dt}")
        return "\n".join(lines)

    def _extract_complaint_server(self, text: str, fallback: int) -> int:
        t = (text or "").lower()
        patterns = [
            r"\brw\s*([123])\b",
            r"\bсервер\D*([123])\b",
            r"^\s*3[\).:-]?\s*(?:rw\s*)?([123])\b",
        ]
        for pattern in patterns:
            m = re.search(pattern, t, flags=re.I | re.M)
            if m:
                return int(m.group(1))
        return int(fallback or 1)

    @staticmethod
    def _nickname_match_distance(a: str, b: str) -> int:
        return _levenshtein(a.lower(), b.lower())

    def _complaint_nickname_links(self, text: str, reporter_id: int) -> list[str]:
        raw_candidates = re.findall(r"\b[A-Za-z][A-Za-z0-9_]{3,31}\b", text or "")
        skip = {"rw", "vk", "id", "http", "https", "com", "club", "public"}
        candidates: list[str] = []
        for cand in raw_candidates:
            low = cand.lower()
            if low in skip or low.startswith("id") and low[2:].isdigit():
                continue
            if cand not in candidates:
                candidates.append(cand)
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT vk_id,nickname FROM users WHERE approved=1 AND TRIM(COALESCE(nickname,''))!=''"
            ).fetchall()
        known = [(int(r["vk_id"]), str(r["nickname"])) for r in rows]
        matched: dict[str, tuple[int, str, int]] = {}
        for cand in candidates:
            best: Optional[tuple[int, str, int]] = None
            for uid, nick in known:
                dist = self._nickname_match_distance(cand, nick)
                limit = 1 if len(cand) <= 6 else 2
                if cand.lower() == nick.lower():
                    dist = 0
                if dist <= limit and (best is None or dist < best[2] or (dist == best[2] and len(nick) > len(best[1]))):
                    best = (uid, nick, dist)
            if best:
                matched[best[1]] = best
        reporter = self._user(reporter_id)
        if reporter and reporter["nickname"]:
            nick = str(reporter["nickname"])
            matched.setdefault(nick, (int(reporter_id), nick, 0))
        lines = []
        for nick, (uid, real_nick, dist) in sorted(matched.items(), key=lambda x: x[0].lower()):
            shown = real_nick if dist == 0 else f"{nick} → {real_nick}"
            lines.append(f"{shown} = https://vk.com/id{uid}")
        return lines

    def _wipe_user_data(self, target: int) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM users WHERE vk_id=?", (target,))
            c.execute("DELETE FROM registration_sessions WHERE vk_id=?", (target,))
            c.execute("DELETE FROM registration_requests WHERE vk_id=?", (target,))
            c.execute("DELETE FROM preapproved_profiles WHERE vk_id=?", (target,))
            c.execute("DELETE FROM user_prefixes WHERE vk_id=?", (target,))
            c.execute("DELETE FROM bot_bans WHERE vk_id=?", (target,))
            c.execute("DELETE FROM chat_members WHERE vk_id=?", (target,))
            c.execute("DELETE FROM leaders WHERE vk_id=?", (target,))
            try:
                c.execute("DELETE FROM leaders_v2 WHERE vk_id=?", (target,))
            except sqlite3.OperationalError:
                pass
            c.execute("DELETE FROM warning_logs WHERE vk_id=? OR issuer_id=?", (target, target))
            c.execute("DELETE FROM ban_logs WHERE vk_id=? OR issuer_id=?", (target, target))
            c.execute("DELETE FROM mute_logs WHERE vk_id=? OR issuer_id=?", (target, target))
            c.execute("DELETE FROM complaints WHERE reporter_id=? OR target_id=?", (target, target))
            c.execute("DELETE FROM faction_strikes WHERE vk_id=?", (target,))
            c.execute("DELETE FROM faction_blacklist WHERE vk_id=? OR issuer_id=?", (target, target))
            c.execute("DELETE FROM silence_exceptions WHERE vk_id=?", (target,))
            c.execute("DELETE FROM account_sync_requests WHERE source_id=? OR target_id=?", (target, target))
            c.execute("DELETE FROM account_links WHERE user_id=? OR master_id=?", (target, target))
            c.execute("DELETE FROM admin_notify_settings WHERE vk_id=?", (target,))
            c.execute("DELETE FROM admin_complaint_stats WHERE vk_id=?", (target,))
            c.execute("DELETE FROM extra_reviewers WHERE vk_id=?", (target,))
            c.execute("DELETE FROM registration_review_recipients WHERE vk_id=?", (target,))
            c.execute("DELETE FROM user_extra_info WHERE target_id=? OR author_id=?", (target, target))
            c.execute("DELETE FROM tg_usernames WHERE user_id=?", (target,))
            c.execute("DELETE FROM oblik_users WHERE vk_id=?", (target,))
            c.execute("DELETE FROM wipe_sessions WHERE target_id=? OR actor_id=?", (target, target))
        with sqlite3.connect(ISTORIA_DB_PATH) as h:
            h.execute("DELETE FROM job_history WHERE target_vk_id=? OR actor_vk_id=?", (target, target))
            h.commit()

    def _snapshot_user_before_wipe(self, target: int) -> str:
        with self.db.conn() as c:
            user = c.execute(
                "SELECT vk_id,nickname,rp_name,position,faction,server_id,approved,admin_level,hidden FROM users WHERE vk_id=?",
                (int(target),),
            ).fetchone()
            reqs = c.execute(
                "SELECT id,nickname,faction,server_id,position,status FROM registration_requests WHERE vk_id=? ORDER BY id DESC LIMIT 5",
                (int(target),),
            ).fetchall()
            info_rows = c.execute(
                "SELECT author_id,info_text,created_at FROM user_extra_info WHERE target_id=? ORDER BY id DESC LIMIT 5",
                (int(target),),
            ).fetchall()
            prefixes = c.execute("SELECT name,emoji FROM user_prefixes WHERE vk_id=? ORDER BY name", (int(target),)).fetchall()
            leaders = c.execute("SELECT faction,server_id FROM leaders WHERE vk_id=?", (int(target),)).fetchall()
        lines = [f"🧾 Последняя копия данных {self._fmt_user(int(target))}:"]
        if user:
            lines.append(
                f"Профиль: nick={user['nickname'] or '-'} | rp={user['rp_name'] or '-'} | "
                f"должность={user['position'] or '-'} | фракция={user['faction'] or '-'} | "
                f"сервер={int(user['server_id'] or 1)} | approved={int(user['approved'] or 0)} | "
                f"admin={int(user['admin_level'] or 0)} | hidden={int(user['hidden'] or 0)}"
            )
        else:
            lines.append("Профиль: отсутствовал в users.")
        if leaders:
            lines.append("Лидерство: " + "; ".join([f"{r['faction']} (сервер {int(r['server_id'] or 1)})" for r in leaders]))
        if prefixes:
            lines.append("Префиксы: " + ", ".join([f"{r['emoji']} {r['name']}" for r in prefixes]))
        if reqs:
            lines.append("Заявки (до 5):")
            for r in reqs:
                lines.append(
                    f"• #{int(r['id'])} | {r['nickname']} | {r['faction']} | srv {int(r['server_id'] or 1)} | {r['position']} | {r['status']}"
                )
        if info_rows:
            lines.append("Доп. инфа (до 5):")
            for r in info_rows:
                dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d:%m:%Y")
                lines.append(f"• {self._fmt_user(int(r['author_id']))} — {r['info_text']} — {dt}")
        return "\n".join(lines[:80])

    def _compose_reply_note_content(self, ctx: Ctx) -> Optional[str]:
        parts: list[str] = []
        if ctx.reply_text:
            parts.append(ctx.reply_text.strip())
        for url in (ctx.reply_link_urls or []):
            if url not in (ctx.reply_text or ""):
                parts.append(url)
        text = "\n\n".join(p for p in parts if p).strip()
        return text or None

    def _attachment_id(self, att: dict) -> Optional[str]:
        at = att.get("type")
        item = att.get(at, {}) if at else {}
        owner = item.get("owner_id")
        aid = item.get("id")
        if at and owner is not None and aid is not None:
            access_key = item.get("access_key")
            if not access_key and at in ("video", "doc", "audio_message") and self.wall_api is not None:
                # У группы (бота) часто нет прав напрямую видеть видео/файл обычного юзера —
                # пробуем получить access_key через юзер-токен с более широкими правами.
                try:
                    if at == "video":
                        info = self.wall_api.video.get(owner_id=owner, videos=[f"{owner}_{aid}"])
                    elif at == "doc":
                        info = self.wall_api.docs.getById(docs=[f"{owner}_{aid}"])
                    else:
                        info = None
                    items = (info or {}).get("items", [])
                    if items:
                        access_key = items[0].get("access_key")
                except Exception:
                    pass
            if access_key:
                return f"{at}{owner}_{aid}_{access_key}"
            return f"{at}{owner}_{aid}"
        if at == "doc":
            doc = att.get("doc", {})
            if "owner_id" in doc and "id" in doc:
                return f"doc{doc['owner_id']}_{doc['id']}"
        return None

    @staticmethod
    def _extract_link_urls(attachments: list[dict]) -> list[str]:
        urls: list[str] = []
        for a in attachments or []:
            if a.get("type") == "link":
                url = (a.get("link") or {}).get("url")
                if url:
                    urls.append(url)
        return urls

    def _collect_attachment_ids(self, attachments: list[dict]) -> tuple[list[str], list[str]]:
        types: list[str] = []
        ids: list[str] = []
        for a in attachments or []:
            at = a.get("type")
            if at:
                types.append(str(at))
            aid = self._attachment_id(a)
            if aid:
                ids.append(aid)
        return types, ids

    def _complaint_id_from_notification_reply(self, ctx: Ctx) -> Optional[int]:
        text = ctx.reply_text or ""
        if not text:
            return None
        complaint_chat_id = int(self._get_setting("complaint_chat_id", "0") or 0)
        if complaint_chat_id > 0 and (not ctx.is_chat or int(ctx.chat_id) != complaint_chat_id):
            return None
        if ctx.platform == "vk":
            if int(ctx.reply_user_id or 0) not in {-int(self.group_id), int(self.group_id)}:
                return None
        if "#жалоба" not in text.lower():
            return None
        m = re.search(r"^\s*⚠️\s*Поступила новая жалоба\s*#(\d+)\b", text, flags=re.M)
        return int(m.group(1)) if m else None

    def _community_group_id(self, faction: str) -> Optional[int]:
        if faction in self.community_group_ids:
            return self.community_group_ids[faction]
        ok, data = self._vk_group_method(faction, "groups.getById", {"extended": 0})
        if not ok:
            return None
        if isinstance(data, dict) and "groups" in data:
            info = (data.get("groups") or [{}])[0]
        else:
            info = data[0] if isinstance(data, list) and data else {}
        gid = int(info.get("id", 0))
        if gid <= 0:
            return None
        self.community_group_ids[faction] = gid
        return gid

    def _extract_community_slug(self, raw: str) -> Optional[str]:
        text = (raw or "").strip()
        if not text:
            return None
        text = text.split()[0].strip().strip("()[]<>")
        text = text.replace("https://", "").replace("http://", "")
        text = text.replace("m.vk.com/", "vk.com/").replace("vk.ru/", "vk.com/")
        if text.startswith("vk.com/"):
            text = text.split("vk.com/", 1)[1]
        text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
        if text.startswith("club") and text[4:].isdigit():
            return text
        if text.startswith("public") and text[6:].isdigit():
            return text
        if re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", text):
            return text
        return None

    def _resolve_community(self, raw: str) -> Optional[dict]:
        slug = self._extract_community_slug(raw)
        if not slug:
            return None
        try:
            data = self.api.utils.resolveScreenName(screen_name=slug)
            if not data or data.get("type") not in {"group", "page"}:
                return None
            owner_id = -int(data["object_id"])
            group_data = self.api.groups.getById(group_ids=str(abs(owner_id)))
            if isinstance(group_data, dict) and "groups" in group_data:
                group = (group_data.get("groups") or [{}])[0]
            else:
                group = group_data[0] if isinstance(group_data, list) and group_data else {}
            domain = str(group.get("screen_name") or slug)
            title = str(group.get("name") or domain)
            return {"owner_id": owner_id, "domain": domain, "title": title, "url": f"https://vk.com/{domain}"}
        except Exception:
            return None

    def _latest_wall_post_id(self, owner_id: int) -> int:
        data = self._wall_get(int(owner_id), count=1)
        items = data.get("items", []) if isinstance(data, dict) else []
        return int(items[0].get("id") or 0) if items else 0

    def _wall_get(self, owner_id: int, count: int = 5) -> dict:
        if not WALL_READ_TOKEN:
            if not self._wall_token_missing_logged:
                logger.error(
                    "VK wall subscriptions need VK_WALL_TOKEN, VK_USER_TOKEN, or VK_SERVICE_TOKEN. "
                    "VK_GROUP_TOKEN cannot call wall.get."
                )
                self._wall_token_missing_logged = True
            return {}
        if self.wall_api is not None:
            try:
                data = self.wall_api.wall.get(owner_id=int(owner_id), count=int(count), filter="owner")
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.error(f"wall.get via wall token failed for owner_id={owner_id}: {e}")
        try:
            resp = self.http.get(
                "https://api.vk.com/method/wall.get",
                params={
                    "owner_id": int(owner_id),
                    "count": int(count),
                    "filter": "owner",
                    "access_token": WALL_READ_TOKEN,
                    "v": "5.199",
                },
                timeout=12,
            ).json()
            if "error" in resp:
                logger.error(f"wall.get API error for owner_id={owner_id}: {resp['error']}")
                return {}
            return resp.get("response", {}) if isinstance(resp, dict) else {}
        except Exception as e:
            logger.error(f"wall.get HTTP fallback failed for owner_id={owner_id}: {e}")
            return {}

    def _format_wall_post(self, sub: dict, post: dict) -> tuple[str, list[str]]:
        post_id = int(post.get("id") or 0)
        owner_id = int(post.get("owner_id") or sub["owner_id"])
        text = str(post.get("text") or "").strip()
        date_ts = int(post.get("date") or self.now_ts())
        link = f"https://vk.com/wall{owner_id}_{post_id}"
        attachments: list[str] = []
        for att in post.get("attachments", []) or []:
            aid = self._attachment_id(att)
            if aid:
                attachments.append(aid)
        if len(text) > 2600:
            text = text[:2600].rstrip() + "\n…"
        return (
            f"📰 Новый пост: {sub['title']}\n"
            f"🕒 {self._fmt_msk_dt(date_ts)}\n\n"
            f"{text or '(без текста)'}\n\n"
            f"{link}",
            attachments,
        )

    def _subscription_poll_loop(self) -> None:
        time.sleep(20)
        while self.running:
            try:
                for sub in self.subscriptions_db.list_all():
                    try:
                        data = self._wall_get(int(sub["owner_id"]), count=5)
                        items = data.get("items", []) if isinstance(data, dict) else []
                        fresh = [
                            p for p in items
                            if int(p.get("id") or 0) > int(sub["last_post_id"] or 0)
                            and not p.get("is_pinned")
                            and str(p.get("post_type") or "post") == "post"
                        ]
                        fresh.sort(key=lambda p: int(p.get("id") or 0))
                        for post in fresh:
                            text, attachments = self._format_wall_post(sub, post)
                            ping_all = int(sub.get("ping_all") or 0) == 1
                            if ping_all:
                                text = "@all\n" + text
                            peer_id = self._chat_peer_id(int(sub["chat_id"]))
                            if attachments:
                                self.send_with_attachments(peer_id, text, attachments, disable_mentions=0 if ping_all else 1)
                            else:
                                self.send(peer_id, text, disable_mentions=0 if ping_all else 1)
                            self.subscriptions_db.update_last_post(int(sub["id"]), int(post.get("id") or sub["last_post_id"]))
                    except Exception as e:
                        logger.error(f"subscription poll error for {sub.get('owner_id')}: {e}")
            except Exception as e:
                logger.error(f"_subscription_poll_loop error: {e}")
            time.sleep(90)

    def _community_action(self, faction: str, action: str, target: int) -> bool:
        gid = self._community_group_id(faction)
        if not gid:
            return False
        if action == "add_admin":
            ok, _ = self._vk_group_method(faction, "groups.editManager", {"group_id": gid, "user_id": target, "role": "moderator"})
            return ok
        if action == "remove_admin":
            ok, _ = self._vk_group_method(faction, "groups.editManager", {"group_id": gid, "user_id": target, "role": "none"})
            if not ok:
                ok, _ = self._vk_group_method(faction, "groups.editManager", {"group_id": gid, "user_id": target, "role": "editor", "is_contact": 0})
            return ok
        if action == "add_blacklist":
            ok, _ = self._vk_group_method(faction, "groups.ban", {"group_id": gid, "owner_id": target})
            return ok
        if action == "remove_blacklist":
            ok, _ = self._vk_group_method(faction, "groups.unban", {"group_id": gid, "owner_id": target})
            return ok
        return False

    def _is_leader_for_chat_faction(self, user_id: int, chat_id: int) -> bool:
        with self.db.conn() as c:
            ch = c.execute("SELECT faction,server_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if not ch or not ch["faction"]:
                return False
            leader = c.execute(
                "SELECT vk_id FROM leaders WHERE faction=? AND server_id=?",
                (ch["faction"], int(ch["server_id"] or 1)),
            ).fetchone()
        return bool(leader and int(leader["vk_id"]) == user_id)

    @staticmethod
    def _is_role_management_command(cmd: str, text: str) -> bool:
        """Команды управления ролями чата: выдача/снятие/создание/удаление/переименование роли,
        список ролей. Используется чтобы дать лидеру фракции полный доступ к ролям в его чате."""
        if cmd == "!роль" or cmd == "!роли" or cmd == "!переименовать":
            return True
        if cmd in {"!снять", "!создать", "!удалить"}:
            second = (text.split()[1].lower() if len(text.split()) >= 2 else "")
            return second == "роль"
        return False

    def _is_leader_for(self, user_id: int, faction: str, server_id: int) -> bool:
        with self.db.conn() as c:
            row = c.execute("SELECT vk_id FROM leaders WHERE faction=? AND server_id=?", (faction, server_id)).fetchone()
        return bool(row and int(row["vk_id"]) == int(user_id))

    def _all_known_chats(self) -> list[sqlite3.Row]:
        with self.db.conn() as c:
            rows = c.execute(
                """
                SELECT chat_id, MAX(title) AS title, MAX(faction) AS faction
                FROM (
                    SELECT chat_id, title, faction FROM chats
                    UNION ALL
                    SELECT chat_id, NULL AS title, NULL AS faction FROM chat_controls
                    UNION ALL
                    SELECT chat_id, NULL AS title, NULL AS faction FROM chat_members
                )
                GROUP BY chat_id
                ORDER BY chat_id
                """
            ).fetchall()
        return rows

    def _can_manage_role_level(self, ctx: Ctx, role_level: int) -> bool:
        if self._get_admin_level(ctx.user_id) >= 100:
            return True
        if ctx.is_chat and self._is_leader_for_chat_faction(ctx.user_id, ctx.chat_id):
            return True
        actor_lvl = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
        return actor_lvl >= role_level

    def _apply_vk_mute(self, peer_id: int, target_id: int, duration_sec: int) -> bool:
        calls = [
            {"peer_id": peer_id, "member_ids": str(target_id), "action": "ro", "for": duration_sec},
            {"peer_id": peer_id, "member_id": target_id, "action": "ro", "for": duration_sec},
        ]
        for payload in calls:
            if self._api_method("messages.changeConversationMemberRestrictions", payload):
                return True
        return False

    def _apply_vk_unmute(self, peer_id: int, target_id: int) -> bool:
        calls = [
            {"peer_id": peer_id, "member_ids": str(target_id), "action": "rw", "for": 0},
            {"peer_id": peer_id, "member_id": target_id, "action": "rw", "for": 0},
        ]
        for payload in calls:
            if self._api_method("messages.changeConversationMemberRestrictions", payload):
                return True
        return False

    # ---------- registration ----------

    @staticmethod
    def _registration_faction_buttons() -> list[list[str]]:
        return [
            ["Армия", "МВД", "СМИ"],
            ["Политика", "ФСБ", "Больница"],
            ["Красная Мафия", "Розовая Мафия"],
            ["Aperture"],
        ]

    def handle_start(self, ctx: Ctx) -> None:
        self._ensure_user(ctx.user_id)
        with self.db.conn() as c:
            pre = c.execute("SELECT * FROM preapproved_profiles WHERE vk_id=?", (ctx.user_id,)).fetchone()
            if pre:
                c.execute(
                    """
                    UPDATE users
                    SET approved=1,
                        nickname=COALESCE(?, nickname),
                        rp_name=COALESCE(?, rp_name),
                        position=COALESCE(?, position),
                        faction=COALESCE(?, faction)
                    WHERE vk_id=?
                    """,
                    (pre["nickname"], pre["rp_name"], pre["position"], pre["faction"], ctx.user_id),
                )
        u = self._user(ctx.user_id)
        if u and int(u["approved"]) == 1:
            self.send(ctx.peer_id, "✅ Вы уже зарегистрированы.")
            return
        with self.db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO registration_sessions(vk_id,stage,server_id,nickname,faction,rp_name) VALUES(?,?,?,?,?,?)",
                (ctx.user_id, "server", None, None, None, None),
            )
        self.send_with_keyboard(ctx.peer_id, "👋 Регистрация\nВыберите номер сервера: 1, 2 или 3.", [["1", "2", "3"]])

    def handle_registration_session(self, ctx: Ctx) -> bool:
        with self.db.conn() as c:
            sess = c.execute("SELECT * FROM registration_sessions WHERE vk_id=?", (ctx.user_id,)).fetchone()
            if not sess:
                return False
            if ctx.text.strip().lower() == "/cancel":
                c.execute("DELETE FROM registration_sessions WHERE vk_id=?", (ctx.user_id,))
                self.send(ctx.peer_id, "✅ Регистрация отменена. Чтобы начать заново, используйте /start.")
                return True
            stage = sess["stage"]

            if stage == "server":
                server_text = ctx.text.strip()
                if server_text not in {"1", "2", "3"}:
                    self.send_with_keyboard(ctx.peer_id, "❌ Укажите сервер: 1, 2 или 3.", [["1", "2", "3"]])
                    return True
                c.execute(
                    "UPDATE registration_sessions SET stage='nickname', server_id=? WHERE vk_id=?",
                    (int(server_text), ctx.user_id),
                )
                self.send(ctx.peer_id, "Введите ваш NickName на сервере:")
                return True

            if stage == "nickname":
                nick_input = ctx.text.strip()
                if not nick_input:
                    self.send(ctx.peer_id, "❌ NickName не может быть пустым. Введите ваш NickName:")
                    return True
                c.execute(
                    "UPDATE registration_sessions SET stage='faction', nickname=? WHERE vk_id=?",
                    (nick_input, ctx.user_id),
                )
                self.send_with_keyboard(
                    ctx.peer_id,
                    "🏛 Выберите фракцию:\n" + "\n".join(f"• {x}" for x in FACTIONS),
                    self._registration_faction_buttons(),
                )
                return True

            if stage == "faction":
                faction = ctx.text.strip()
                if faction not in FACTIONS:
                    self.send_with_keyboard(
                        ctx.peer_id,
                        "❌ Неверная фракция. Выберите из списка кнопками.",
                        self._registration_faction_buttons(),
                    )
                    return True
                c.execute(
                    "UPDATE registration_sessions SET stage='rp_name', faction=? WHERE vk_id=?",
                    (faction, ctx.user_id),
                )
                self.send(ctx.peer_id, "🧾 Введите РП ФИО (самостоятельно изменить потом нельзя):")
                return True

            if stage == "rp_name":
                rp_input = ctx.text.strip()
                if not rp_input:
                    self.send(ctx.peer_id, "❌ РП ФИО не может быть пустым. Введите РП ФИО:")
                    return True
                c.execute(
                    "UPDATE registration_sessions SET stage='position', rp_name=? WHERE vk_id=?",
                    (rp_input, ctx.user_id),
                )
                self.send(ctx.peer_id, "💼 Введите должность:")
                return True

            if stage == "position":
                position = ctx.text.strip()
                if not position:
                    self.send(ctx.peer_id, "❌ Должность не может быть пустой. Введите должность:")
                    return True

                nickname = sess["nickname"]
                faction = sess["faction"]
                rp_name = sess["rp_name"]
                server_id = int(sess["server_id"] or 1)

                # Защита от дублирования: проверяем нет ли уже pending заявки от этого пользователя
                existing = c.execute(
                    "SELECT id FROM registration_requests WHERE vk_id=? AND status='pending'",
                    (ctx.user_id,),
                ).fetchone()
                if existing:
                    # Сессию чистим, но не создаём новую заявку
                    c.execute("DELETE FROM registration_sessions WHERE vk_id=?", (ctx.user_id,))
                    self.send(
                        ctx.peer_id,
                        f"⚠️ У вас уже есть заявка #{existing['id']} на рассмотрении. Ждите решения Лидера фракции.",
                    )
                    return True

                # Вставляем заявку — ТОЛЬКО в стадии position
                cur = c.execute(
                    "INSERT INTO registration_requests(vk_id,server_id,platform,nickname,faction,rp_name,position,status)"
                    " VALUES(?,?,?,?,?,?,?,'pending')",
                    (ctx.user_id, server_id, ctx.platform, nickname, faction, rp_name, position),
                )
                req_id = cur.lastrowid
                if not req_id:
                    req_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Создаём/обновляем запись пользователя (не одобрен)
                c.execute(
                    "INSERT OR REPLACE INTO users"
                    "(vk_id,nickname,rp_name,position,faction,server_id,approved,admin_level,bot_ban,consent_accepted,consent_accepted_at)"
                    " VALUES(?,?,?,?,?,?,0,0,0,1,?)",
                    (ctx.user_id, nickname, rp_name, position, faction, server_id, self.now_ts()),
                )
                # Удаляем сессию ПОСЛЕ успешной вставки
                c.execute("DELETE FROM registration_sessions WHERE vk_id=?", (ctx.user_id,))

                # Подтверждение пользователю (без "Ожидайте ответа" — лишнее)
                self.send(
                    ctx.peer_id,
                    f"✅ Заявка #{req_id} отправлена. Ожилайте ответа!\n"
                    f"Ник: {nickname} | Фракция: {faction} | Сервер: {server_id}\n"
                    f"РП ФИО: {rp_name} | Должность: {position}",
                )

                # Текст для рассматривающих
                review_text = (
                    f"📥 Новая заявка #{req_id}\n"
                    f"Пользователь: {self._fmt_user(ctx.user_id)}\n"
                    f"Ник: {nickname} | Фракция: {faction} | Сервер: {server_id}\n"
                    f"РП ФИО: {rp_name} | Должность: {position}\n"
                    f"Платформа: {'Telegram' if ctx.platform == 'tg' else 'VK'}\n\n"
                    "Действие: !одобрить (номер) или !отказать (номер)"
                )

        # Уведомляем рассматривающих ВНЕ транзакции (не блокируем БД)
        delivered = self._notify_registration_reviewers(req_id, faction, server_id, review_text)
        if not delivered:
            logger.info(f"Заявка #{req_id}: не удалось отправить рассматривающим.",
                file=sys.stderr,)

        self._add_history(
            nickname=nickname,
            target_vk_id=ctx.user_id,
            old_faction=None,
            old_position=None,
            new_faction=faction,
            new_position=position,
            actor_vk_id=None,
            event_type="registration",
        )
        return True

        return False

    # ---------- commands ----------

    def cmd_profile(self, ctx: Ctx) -> None:
        def _load_prefixes() -> list[sqlite3.Row]:
            canonical_id = self._canonical_user_id(ctx.user_id)
            with self.db.conn() as c:
                return c.execute("SELECT name,emoji FROM user_prefixes WHERE vk_id=? ORDER BY name", (canonical_id,)).fetchall()

        with ThreadPoolExecutor(max_workers=3) as pool:
            fut_user = pool.submit(self._user, ctx.user_id)
            fut_admin = pool.submit(self._get_admin_level_visible, ctx.user_id)
            fut_pref = pool.submit(_load_prefixes)
            u = fut_user.result()
            admin_lvl = fut_admin.result()
            pref = fut_pref.result()
        role_lvl = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
        hidden_in_chat = bool(u and int(u["hidden"] or 0) == 1 and ctx.is_chat)
        nick = u["nickname"] if u and u["nickname"] else "не указан"
        nick2 = (u["nick2"] if "nick2" in u.keys() else None) if u else None
        position = u["position"] if u and u["position"] else "не указана"
        faction = u["faction"] if u and u["faction"] else "не указана"
        if hidden_in_chat:
            nick = "(скрыто)"
            nick2 = None
            position = "(скрыто)"
            faction = "(скрыто)"
        pref_line = "\n🧷 Префиксы: нет"
        if pref:
            pref_line = "\n🧷 Префиксы: " + ", ".join([f"{p['name']} {p['emoji']}" for p in pref])
        nick_str = nick
        if nick2:
            nick_str += f" ({nick2})"
        self.send(
            ctx.peer_id,
            (
                "👤 Ваш профиль\n"
                f"• Ник: {nick_str}\n"
                f"• Должность: {position}\n"
                f"• Фракция: {faction}\n"
                f"• Сервер: {u['server_id'] if u and u['server_id'] else 1}\n"
                f"• Уровень в чате: {role_lvl}\n"
                f"• Админ уровень: {admin_lvl}"
                f"{pref_line}"
            ),
        )

    def _registration_reviewer_ids(self, faction: str, server_id: int) -> list[int]:
        """Возвращает список VK id всех получателей заявки данной фракции/сервера.
        Порядок: лидер → доп. рассматривающие → старший admin.
        """
        with self.db.conn() as c:
            # Ищем лидера конкретного сервера
            leader = c.execute(
                "SELECT vk_id FROM leaders WHERE faction=? AND server_id=?",
                (faction, int(server_id)),
            ).fetchone()
            # Доп. рассматривающие для фракции+сервера
            extra_rows = c.execute(
                "SELECT vk_id FROM extra_reviewers WHERE faction=? AND server_id=? ORDER BY vk_id",
                (faction, int(server_id)),
            ).fetchall()
            # Также доп. рассматривающие без привязки к серверу (server_id=0 как "все серверы")
            extra_any = c.execute(
                "SELECT vk_id FROM extra_reviewers WHERE faction=? AND server_id=0 ORDER BY vk_id",
                (faction,),
            ).fetchall()
        out: list[int] = []
        if leader:
            lid = int(leader["vk_id"] if isinstance(leader, sqlite3.Row) else leader[0])
            out.append(lid)
        for r in list(extra_rows) + list(extra_any):
            uid = int(r["vk_id"])
            if uid not in out:
                out.append(uid)
        # Старший админ получает заявку только если нет других получателей
        if not out:
            senior = int(self.db.senior_admin_id)
            out.append(senior)
        return out

    def _notify_registration_reviewers(self, request_id: int, faction: str, server_id: int, text: str) -> bool:
        """Отправляет заявку всем рассматривающим и записывает их в БД.
        Сначала все записи в БД, потом отправка сообщений — чтобы не держать
        транзакцию открытой во время сетевых запросов к VK API.
        """
        recipients = self._registration_reviewer_ids(faction, server_id)
        # Сначала записываем получателей в БД (быстро, без API)
        with self.db.conn() as c:
            for to_user in recipients:
                c.execute(
                    "INSERT OR IGNORE INTO registration_review_recipients(request_id,vk_id) VALUES(?,?)",
                    (request_id, to_user),
                )
        # Потом отправляем сообщения (медленно, сеть) — БД уже не заблокирована
        delivered_any = False
        for to_user in recipients:
            try:
                ok = self.send_dm_vk_with_inline(to_user, text, request_id)
                if ok:
                    delivered_any = True
            except Exception as e:
                logger.error(f"Ошибка отправки заявки #{request_id} пользователю {to_user}: {e}")
        return delivered_any

    def _approve_reject_by_id(self, actor_id: int, peer_id: int, req_id: int, approve: bool, actor_platform: str = "vk", actor_username: Optional[str] = None) -> str:
        with self.db.conn() as c:
            req = c.execute("SELECT * FROM registration_requests WHERE id=?", (req_id,)).fetchone()
            if not req:
                return "❌ Заявка не найдена."
            if req["status"] != "pending":
                return "❌ Заявка уже обработана."
            faction = req["faction"]
            server_id = int(req["server_id"] or 1) if "server_id" in req.keys() else 1
            leader = c.execute("SELECT vk_id FROM leaders WHERE faction=? AND server_id=?", (faction, server_id)).fetchone()
            extra = c.execute(
                "SELECT 1 FROM extra_reviewers WHERE vk_id=? AND faction=? AND server_id=?",
                (actor_id, faction, server_id),
            ).fetchone()
            actor = actor_id
            is_tg_senior = actor_platform == "tg" and (actor_username or "").strip().lstrip("@").lower() == TG_SENIOR_ADMIN_USERNAME
            if actor != self.db.senior_admin_id and not is_tg_senior and (not leader or int(leader[0]) != actor) and not extra:
                return "⛔ Только лидер, доп. рассматривающий или старший админ может обработать эту заявку."
            recipients = c.execute(
                "SELECT vk_id FROM registration_review_recipients WHERE request_id=?",
                (req_id,),
            ).fetchall()
            if approve:
                c.execute(
                    "UPDATE users SET approved=1, approved_by=?, approved_at=?, server_id=? WHERE vk_id=?",
                    (actor_id, self.now_ts(), server_id, req["vk_id"]),
                )
                c.execute("UPDATE registration_requests SET status='approved' WHERE id=?", (req_id,))
                self.send_user_notice(int(req["vk_id"]), "✅ Ваша заявка одобрена. Доступ к боту открыт.")
                result = "одобрена"
            else:
                c.execute("UPDATE registration_requests SET status='rejected' WHERE id=?", (req_id,))
                self.send_user_notice(
                    int(req["vk_id"]),
                    "❌ Ваша заявка отклонена.\n"
                    "Пожалуйста, пройдите регистрацию заново более ответственно: /start или «Начать».",
                )
                result = "отклонена"
            c.execute("DELETE FROM registration_review_recipients WHERE request_id=?", (req_id,))
        actor_ref = self._platform_profile_ref(actor_platform, actor_id)
        for row in recipients:
            uid = int(row["vk_id"])
            if uid == int(actor_id):
                continue
            self.send_user_notice(uid, f"ℹ️ Заявка #{req_id} {result} ({actor_ref}).")
        return f"✅ Заявка #{req_id} {result}."

    def _approve_reject(self, ctx: Ctx, approve: bool) -> None:
        m = re.match(r"^!(одобрить|отказать)\s+(\d+)$", ctx.text.strip(), flags=re.I)
        if not m:
            self.send(ctx.peer_id, "Формат: !одобрить (номер) / !отказать (номер)")
            return
        req_id = int(m.group(2))
        self.send(ctx.peer_id, self._approve_reject_by_id(ctx.user_id, ctx.peer_id, req_id, approve, ctx.platform, ctx.tg_username))

    def _approve_sync_request(self, approver_id: int, request_id: int) -> str:
        now = self.now_ts()
        with self.db.conn() as c:
            req = c.execute("SELECT * FROM account_sync_requests WHERE id=?", (request_id,)).fetchone()
            if not req or req["status"] != "pending":
                return "❌ Запрос синхронизации не найден или уже обработан."
            if int(req["target_id"]) != int(approver_id):
                return "⛔ Этот запрос предназначен другому пользователю."
            if int(req["expires_at"]) < now:
                c.execute("UPDATE account_sync_requests SET status='expired' WHERE id=?", (request_id,))
                return "⌛ Запрос синхронизации истёк."
            src = c.execute("SELECT * FROM users WHERE vk_id=?", (int(req["source_id"]),)).fetchone()
            tgt = c.execute("SELECT * FROM users WHERE vk_id=?", (int(req["target_id"]),)).fetchone()
            if not src or not tgt:
                return "❌ Не удалось найти оба профиля для синхронизации."
            c.execute(
                """
                UPDATE users
                SET nickname=?, rp_name=?, position=?, faction=?, server_id=?, approved=?, admin_level=?, hidden=?
                WHERE vk_id=?
                """,
                (
                    tgt["nickname"], tgt["rp_name"], tgt["position"], tgt["faction"],
                    int(tgt["server_id"] or 1), int(tgt["approved"] or 0), int(tgt["admin_level"] or 0), int(tgt["hidden"] or 0),
                    int(req["source_id"]),
                ),
            )
            master_id = self._canonical_user_id(int(req["target_id"]))
            c.execute("INSERT OR REPLACE INTO account_links(user_id,master_id) VALUES(?,?)", (int(req["source_id"]), master_id))
            c.execute("INSERT OR REPLACE INTO account_links(user_id,master_id) VALUES(?,?)", (int(req["target_id"]), master_id))
            c.execute("UPDATE account_sync_requests SET status='approved' WHERE id=?", (request_id,))
        self.send_user_notice(int(req["source_id"]), f"✅ Синхронизация аккаунта по нику «{req['nickname']}» подтверждена.")
        return "✅ Синхронизация успешно выполнена."

    def _admin_console_text_profile(self, target_id: int) -> str:
        with self.db.conn() as c:
            u = c.execute("SELECT * FROM users WHERE vk_id=?", (target_id,)).fetchone()
            pref = c.execute("SELECT name,emoji FROM user_prefixes WHERE vk_id=? ORDER BY name", (target_id,)).fetchall()
        if not u:
            return f"Пользователь {self._fmt_user(target_id)} не найден в БД."
        nick2_val = u["nick2"] if "nick2" in u.keys() and u["nick2"] else None
        nick_display = (u["nickname"] or "не указан")
        if nick2_val:
            nick_display += f" ({nick2_val})"
        lines = [
            f"👤 Профиль {self._fmt_user(target_id)}",
            f"• Ник: {nick_display}",
            f"• ФИО: {u['rp_name'] or 'не указано'}",
            f"• Должность: {u['position'] or 'не указана'}",
            f"• Фракция: {u['faction'] or 'не указана'}",
            f"• Сервер: {u['server_id'] or 1}",
            f"• Админ уровень: {u['admin_level']}",
            f"• Рейтинг: {u['rating'] if u['rating'] is not None else 100}/100",
            f"• Префиксы: {', '.join([p['name'] + ' ' + p['emoji'] for p in pref]) if pref else 'нет'}",
        ]
        return "\n".join(lines)

    def _handle_admin_console(self, ctx: Ctx) -> bool:
        if ctx.is_chat:
            return False

        text = ctx.text.strip()
        lower = text.lower()
        with self.db.conn() as c:
            session = c.execute("SELECT state,target_id FROM admin_console_sessions WHERE vk_id=?", (ctx.user_id,)).fetchone()

        if lower == "!админконсоль":
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Доступ только у старшего админа.")
                return True
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (ctx.user_id,))
                c.execute("UPDATE users SET admin_level=100 WHERE vk_id=?", (ctx.user_id,))
                c.execute(
                    "INSERT OR REPLACE INTO admin_console_sessions(vk_id,state,target_id) VALUES(?, 'menu', NULL)",
                    (ctx.user_id,),
                )
            self.send(
                ctx.peer_id,
                "🛠 Админ консоль\nВыберите раздел:",
            )
            if ctx.platform == "vk":
                self.api.messages.send(
                    peer_id=ctx.peer_id,
                    random_id=0,
                    message="Кнопки админ-консоли активированы.",
                    keyboard=self._keyboard([["Настройки", "Фракции"], ["Чужой профиль", "Управление сообществами"], ["Выйти"]]),
                    disable_mentions=1,
                )
            return True

        if not session:
            return False

        if not self._is_senior_admin_ctx(ctx):
            with self.db.conn() as c:
                c.execute("DELETE FROM admin_console_sessions WHERE vk_id=?", (ctx.user_id,))
            self.admin_console_selected_faction.pop(ctx.user_id, None)
            self.send(ctx.peer_id, "⛔ Админ-консоль закрыта: доступ только у старшего админа.")
            return True

        state = session["state"]
        target_id = session["target_id"]

        if lower == "выйти":
            with self.db.conn() as c:
                c.execute("DELETE FROM admin_console_sessions WHERE vk_id=?", (ctx.user_id,))
            self.admin_console_selected_faction.pop(ctx.user_id, None)
            self.send(ctx.peer_id, "✅ Вы вышли из админ консоли.")
            return True

        if state == "menu":
            if lower == "настройки":
                self.send(ctx.peer_id, "⚙️ Раздел Настройки пока в текстовом режиме.")
                return True
            if lower == "фракции":
                self.send(ctx.peer_id, "🏛 Фракции:\n" + "\n".join([f"• {x}" for x in FACTIONS]))
                return True
            if lower == "чужой профиль":
                with self.db.conn() as c:
                    c.execute(
                        "UPDATE admin_console_sessions SET state='wait_profile_user', target_id=NULL WHERE vk_id=?",
                        (ctx.user_id,),
                    )
                self.send(ctx.peer_id, "Введите пользователя (ссылка/@username/id), чей профиль хотите изменить.")
                return True
            if lower == "управление сообществами":
                with self.db.conn() as c:
                    c.execute(
                        "UPDATE admin_console_sessions SET state='community_pick', target_id=NULL WHERE vk_id=?",
                        (ctx.user_id,),
                    )
                if ctx.platform == "vk":
                    self.api.messages.send(
                        peer_id=ctx.peer_id,
                        random_id=0,
                        message="Выберите сообщество:",
                        keyboard=self._keyboard([["Армия", "МВД", "СМИ"], ["Больница", "ФСБ"], ["Выйти"]]),
                        disable_mentions=1,
                    )
                return True
            self.send(ctx.peer_id, "Выберите: Настройки / Фракции / Чужой профиль / Выйти")
            return True

        if state == "community_pick":
            faction = next((f for f in FACTION_COMMUNITY_TOKENS if f.lower() == lower), None)
            if not faction:
                self.send(ctx.peer_id, "Выберите сообщество кнопкой.")
                return True
            with self.db.conn() as c:
                c.execute("UPDATE admin_console_sessions SET state='community_action' WHERE vk_id=?", (ctx.user_id,))
            self.admin_console_selected_faction[ctx.user_id] = faction
            if ctx.platform == "vk":
                self.api.messages.send(
                    peer_id=ctx.peer_id,
                    random_id=0,
                    message=f"Сообщество: {faction}\nВыберите действие:",
                    keyboard=self._keyboard([["Добавить админа", "Снять админа"], ["Добавить в чс", "Снять с чс"], ["Выйти"]]),
                    disable_mentions=1,
                )
            return True

        if state == "community_action":
            action_map = {
                "добавить админа": "add_admin",
                "снять админа": "remove_admin",
                "добавить в чс": "add_blacklist",
                "снять с чс": "remove_blacklist",
            }
            action = action_map.get(lower)
            if not action:
                self.send(ctx.peer_id, "Выберите действие кнопкой.")
                return True
            with self.db.conn() as c:
                c.execute("UPDATE admin_console_sessions SET state=? WHERE vk_id=?", (f"community_user_{action}", ctx.user_id))
            self.send(ctx.peer_id, "Введите пользователя (ссылка/@username/id).")
            return True

        if state.startswith("community_user_"):
            faction = self.admin_console_selected_faction.get(ctx.user_id)
            action = state.replace("community_user_", "", 1)
            uid = self._parse_user(text)
            if not uid or not faction:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return True
            ok = self._community_action(str(faction), action, uid)
            if not ok:
                self.send(ctx.peer_id, "Ошибка доступа")
            else:
                labels = {
                    "add_admin": "добавлен в админы",
                    "remove_admin": "снят с админов",
                    "add_blacklist": "добавлен в чс",
                    "remove_blacklist": "снят с чс",
                }
                self.send(ctx.peer_id, f"✅ {self._fmt_user(uid)} {labels.get(action, 'обработан')} в сообществе {faction}.")
            with self.db.conn() as c:
                c.execute("UPDATE admin_console_sessions SET state='menu' WHERE vk_id=?", (ctx.user_id,))
            return True

        if state == "wait_profile_user":
            uid = self._parse_user(text)
            if uid is None:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return True
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (uid,))
                c.execute(
                    "UPDATE admin_console_sessions SET state='profile_field_select', target_id=? WHERE vk_id=?",
                    (uid, ctx.user_id),
                )
            self.send(
                ctx.peer_id,
                self._admin_console_text_profile(uid)
                + "\n\nЧто изменить? Напишите: Ник / ФИО / Должность / Фракция / Выйти",
            )
            return True

        if state == "profile_field_select":
            if lower in {"ник", "фио", "должность", "фракция"}:
                next_state = {
                    "ник": "edit_nick",
                    "фио": "edit_rp_name",
                    "должность": "edit_position",
                    "фракция": "edit_faction",
                }[lower]
                with self.db.conn() as c:
                    c.execute("UPDATE admin_console_sessions SET state=? WHERE vk_id=?", (next_state, ctx.user_id))
                if lower == "фракция":
                    self.send(ctx.peer_id, "Введите новую фракцию из списка:\n" + "\n".join([f"• {x}" for x in ALL_FACTIONS]))
                else:
                    self.send(ctx.peer_id, f"Введите новое значение поля «{lower}».")
                return True
            self.send(ctx.peer_id, "Напишите: Ник / ФИО / Должность / Фракция / Выйти")
            return True

        if state in {"edit_nick", "edit_rp_name", "edit_position", "edit_faction"} and target_id:
            value = text.strip()
            if state == "edit_faction" and value not in ALL_FACTIONS:
                self.send(ctx.peer_id, "❌ Неверная фракция. Используйте одно из значений списка.")
                return True
            field = {"edit_nick": "nickname", "edit_rp_name": "rp_name", "edit_position": "position", "edit_faction": "faction"}[state]
            with self.db.conn() as c:
                _SAFE2 = frozenset({"faction","rp_name","nickname","position","server_id","nick2"})
                if field not in _SAFE2:
                    logger.critical(f"SQLi attempt (interactive) field={field!r} actor={ctx.user_id}")
                    self._alert_senior_admin(f"🚨 SQLi попытка (interactive !изменить): поле={field!r}, id{ctx.user_id}")
                    self.send(ctx.peer_id, "⛔ Недопустимое поле.")
                    return
                c.execute(f"UPDATE users SET {field}=? WHERE vk_id=?", (value, target_id))
                c.execute(
                    f"INSERT INTO preapproved_profiles(vk_id,approved,{field},updated_at) VALUES(?,1,?,?) "
                    f"ON CONFLICT(vk_id) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at",
                    (target_id, value, self.now_ts()),
                )
                c.execute("UPDATE admin_console_sessions SET state='profile_field_select' WHERE vk_id=?", (ctx.user_id,))
            self.send(
                ctx.peer_id,
                "✅ Изменено.\n" + self._admin_console_text_profile(int(target_id)) + "\n\nЧто ещё изменить? Ник / ФИО / Должность / Фракция / Выйти",
            )
            return True

        return False

    # ---------- main dispatcher ----------


    # ══════════════════════════════════════════════════════════════════════════
    # ДЖ / Найм — вспомогательные методы
    # ══════════════════════════════════════════════════════════════════════════

    def _dj_get_positions(self, faction: str, server_id: int) -> list[dict]:
        """Возвращает список должностей ДЖ (level, name) отсортированных по убыванию уровня."""
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT id, level, name FROM dj_positions WHERE faction=? AND server_id=? ORDER BY level DESC, name COLLATE NOCASE",
                (faction, int(server_id)),
            ).fetchall()
        return [dict(r) for r in rows]

    def _dj_get_headers(self, faction: str, server_id: int) -> list[dict]:
        """Возвращает заголовки разделов ДЖ (title, level_min, level_max)."""
        with self.db.conn() as c:
            rows = c.execute(
                "SELECT id, title, level_min, level_max FROM dj_headers WHERE faction=? AND server_id=? ORDER BY level_max DESC",
                (faction, int(server_id)),
            ).fetchall()
        return [dict(r) for r in rows]

    def _dj_find_position(self, faction: str, server_id: int, query: str) -> Optional[dict]:
        """Ищет должность по уровню или названию (нечёткое NOCASE)."""
        query = query.strip()
        with self.db.conn() as c:
            if query.lstrip("-").isdigit():
                row = c.execute(
                    "SELECT id, level, name FROM dj_positions WHERE faction=? AND server_id=? AND level=? LIMIT 1",
                    (faction, int(server_id), int(query)),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id, level, name FROM dj_positions WHERE faction=? AND server_id=? AND LOWER(name)=LOWER(?) LIMIT 1",
                    (faction, int(server_id), query),
                ).fetchone()
        return dict(row) if row else None

    def _dj_format_member(self, uid: int, platform: str = "vk") -> str:
        """Форматирует строку для сотрудника в списке ДЖ:
        Ник – [Имя ВК](ссылка) – или (TG) если телеграмм.
        """
        # Получаем имя из кэша или через API
        cached = self.user_name_cache.get(uid)
        if cached:
            vk_name = cached[0]
        else:
            try:
                info = self.api.users.get(user_ids=str(uid))[0]
                vk_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
                self.user_name_cache[uid] = (vk_name, self.now_ts())
            except Exception:
                vk_name = f"id{uid}"
        
        with self.db.conn() as c:
            u = c.execute("SELECT nickname, platform FROM users WHERE vk_id=?", (uid,)).fetchone()
            nick_str = (u["nickname"] if u else None) or ""
            user_platform = (u["platform"] if u else "vk") or "vk"
        
        if user_platform == "tg":
            return f"{nick_str} – {vk_name} (TG)"
        else:
            return f"{nick_str} – [id{uid}|{vk_name}]"
            
    def _dj_render(self, faction: str, server_id: int) -> str:
        """Рендерит красивый список ДЖ с заголовками, счётчиком сотрудников."""
        positions = self._dj_get_positions(faction, server_id)
        headers = self._dj_get_headers(faction, server_id)

        # Получаем реальных сотрудников на каждой должности (+ nickname + platform)
        with self.db.conn() as c:
            staff_rows = c.execute(
                "SELECT vk_id, position, nickname, platform FROM users "
                "WHERE faction=? AND server_id=? AND approved=1 "
                "AND TRIM(COALESCE(position,''))!='' AND LOWER(COALESCE(position,''))!='не указана'",
                (faction, int(server_id)),
            ).fetchall()

        # Группируем сотрудников по должности (case-insensitive): {pos_key: [(uid, nick, platform)]}
        staff_by_pos: dict[str, list[tuple]] = {}
        for s in staff_rows:
            key = str(s["position"] or "").strip().lower()
            staff_by_pos.setdefault(key, []).append((
                int(s["vk_id"]),
                str(s["nickname"] or ""),
                str(s["platform"] or "vk"),
            ))

        total_staff = sum(len(v) for v in staff_by_pos.values())
        total_positions = len(positions)

        lines = [f"📋 СПИСОК ДЖ ФРАКЦИИ {faction.upper()} RW#{server_id}", ""]

        sorted_headers = sorted(headers, key=lambda h: -h["level_max"])

        def _render_header(h: dict) -> str:
            title = h["title"].upper()
            interval = f"({h['level_min']}-{h['level_max']})"
            
            header_line = f"=== {title} ==="
            title_len = len(title)
            interval_len = len(interval)
            
            if interval_len >= title_len:
                pad_left = 2
            else:
                pad_left = 2 + (title_len - interval_len) // 2
            
            interval_line = " " * pad_left + interval
            return f"{header_line}\n{interval_line}"

        def _format_staff_line(level: int, pos_name: str, uid: int, nick: str, platform: str) -> str:
            nick_str = nick.strip() if nick.strip() else "—"
            
            if platform == "tg":
                cached = self.user_name_cache.get(uid)
                vk_name = cached[0] if cached else None
                ref = f"{vk_name or ('tg_id_' + str(uid))} (TG)"
            else:
                # Используем _fmt_user для красивой ссылки
                ref = self._fmt_user(uid)
            
            return f"{level}) {nick_str} — {ref} — {pos_name}."

        used_positions: set[int] = set()  # <-- ЭТА СТРОКА ДОЛЖНА БЫТЬ ЗДЕСЬ

        for h in sorted_headers:
            section_positions = [p for p in positions if h["level_min"] <= p["level"] <= h["level_max"] and p["id"] not in used_positions]
            if not section_positions:
                continue
            lines.append(_render_header(h))
            for p in section_positions:
                used_positions.add(p["id"])
                staff = staff_by_pos.get(p["name"].strip().lower(), [])
                if staff:
                    for uid, nick, platform in staff:
                        lines.append(_format_staff_line(p["level"], p["name"], uid, nick, platform))
                else:
                    lines.append(f"{p['level']}) (вакансия) — {p['name']}.")
            lines.append("")

        unassigned = [p for p in positions if p["id"] not in used_positions]
        if unassigned:
            lines.append(" === БЕЗ РАЗДЕЛА ===")
            for p in unassigned:
                staff = staff_by_pos.get(p["name"].strip().lower(), [])
                if staff:
                    for uid, nick, platform in staff:
                        lines.append(_format_staff_line(p["level"], p["name"], uid, nick, platform))
                else:
                    lines.append(f"{p['level']}) (вакансия) — {p['name']}.")
            lines.append("")

        lines.append(f"📊 Итого: {total_positions} должностей | 👥 {total_staff} сотрудников")
        return "\n".join(lines)

    def _parse_level_range(self, s: str) -> Optional[tuple[int, int]]:
        """Парсит строку вида '80-90' или '80 - 90' или '90-80' → (min, max)."""
        s = s.strip()
        m = re.match(r"^(\d+)\s*[-–—]\s*(\d+)$", s)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))

    def _kick_from_faction_chats(self, user_id: int, faction: str, server_id: int) -> int:
        """Кикает пользователя из всех чатов фракции на сервере. Возвращает кол-во чатов."""
        with self.db.conn() as c:
            chats = c.execute(
                "SELECT chat_id FROM chats WHERE faction=? AND server_id=?",
                (faction, int(server_id)),
            ).fetchall()
        kicked = 0
        for row in chats:
            try:
                self.api.messages.removeChatUser(chat_id=int(row["chat_id"]), member_id=user_id)
                kicked += 1
            except Exception:
                pass
        return kicked

    def _send_hire_offer_buttons(self, target_id: int, offer_id: int, actor_faction: str, pos_name: str) -> bool:
        """Отправляет предложение о найме целевому пользователю с кнопками."""
        keyboard = json.dumps({
            "inline": True,
            "buttons": [[
                {"action": {"type": "callback", "label": "✅ Принять", "payload": json.dumps({"cmd": "hire_accept", "id": offer_id})}, "color": "positive"},
                {"action": {"type": "callback", "label": "❌ Отказать", "payload": json.dumps({"cmd": "hire_reject", "id": offer_id})}, "color": "negative"},
            ]],
        })
        msg = (
            f"💼 Вам поступило предложение о работе!\n"
            f"Фракция: {actor_faction}\n"
            f"Должность: {pos_name}\n\n"
            "Принять предложение?"
        )
        try:
            self.api.messages.send(peer_id=target_id, random_id=0, message=msg, keyboard=keyboard, disable_mentions=1)
            return True
        except Exception as e:
            logger.error(f"[hire] Ошибка отправки оффера: {e}")
            return False

    def _send_hire_old_chats_question(self, target_id: int, offer_id: int, old_faction: str, old_server: int) -> None:
        """После принятия оффера — спрашиваем про выход из старых чатов."""
        keyboard = json.dumps({
            "inline": True,
            "buttons": [[
                {"action": {"type": "callback", "label": "✅ Да, выйти", "payload": json.dumps({"cmd": "hire_kick_old", "id": offer_id})}, "color": "negative"},
                {"action": {"type": "callback", "label": "❌ Нет", "payload": json.dumps({"cmd": "hire_keep_old", "id": offer_id})}, "color": "secondary"},
            ]],
        })
        msg = f"Выйти из чатов фракции {old_faction} (сервер {old_server}) — предыдущего места работы?"
        try:
            self.api.messages.send(peer_id=target_id, random_id=0, message=msg, keyboard=keyboard, disable_mentions=1)
        except Exception:
            pass

    def _send_leader_kick_question(self, leader_id: int, fired_user_id: int, offer_id: int, fired_faction: str, fired_server: int) -> None:
        """Уведомляем лидера старой фракции об уходе сотрудника."""
        cached = self.user_name_cache.get(fired_user_id)
        name = cached[0] if cached else f"id{fired_user_id}"
        keyboard = json.dumps({
            "inline": True,
            "buttons": [[
                {"action": {"type": "callback", "label": "✅ Кикнуть", "payload": json.dumps({"cmd": "leader_kick_yes", "id": offer_id, "uid": fired_user_id, "srv": fired_server, "fac": fired_faction})}, "color": "negative"},
                {"action": {"type": "callback", "label": "❌ Нет", "payload": json.dumps({"cmd": "leader_kick_no", "id": offer_id})}, "color": "secondary"},
            ]],
        })
        msg = (
            f"ℹ️ Сотрудник [id{fired_user_id}|{name}] принял предложение о работе и покидает вашу фракцию {fired_faction} (сервер {fired_server}).\n"
            "Кикнуть его из всех чатов вашей фракции?"
        )
        try:
            self.api.messages.send(peer_id=leader_id, random_id=0, message=msg, keyboard=keyboard, disable_mentions=1)
        except Exception:
            pass

    # ── resign helpers ─────────────────────────────────────────────────────────

    def _send_resign_user_confirm(self, ctx: Ctx, reason: str) -> None:
        """Отправляет пользователю кнопки подтверждения увольнения."""
        keyboard = {
            "inline": True,
            "buttons": [[
                {"action": {"type": "callback", "label": "✅ Да, уволиться", "payload": json.dumps({"cmd": "resign_confirm", "uid": ctx.user_id})}, "color": "negative"},
                {"action": {"type": "callback", "label": "❌ Нет, отмена", "payload": json.dumps({"cmd": "resign_cancel", "uid": ctx.user_id})}, "color": "secondary"},
            ]],
        }
        msg = (
            f"⚠️ Вы уверены, что хотите уволиться?\n"
            f"Причина: {reason}\n\n"
            "После подтверждения запрос уйдёт лидеру вашей фракции."
        )
        try:
            self.api.messages.send(
                peer_id=ctx.user_id,  # ВСЕГДА в ЛС, не в чат
                random_id=0,
                message=msg,
                keyboard=json.dumps(keyboard),
                disable_mentions=1,
            )
        except Exception as e:
            logger.error(f"[resign] Ошибка отправки подтверждения: {e}")

    def _send_resign_leader_confirm(self, leader_id: int, user_id: int, reason: str, req_id: int) -> bool:
        """Отправляет лидеру фракции запрос на подтверждение увольнения."""
        keyboard = {
            "inline": True,
            "buttons": [[
                {"action": {"type": "callback", "label": "✅ Подтвердить", "payload": json.dumps({"cmd": "resign_leader_yes", "id": req_id})}, "color": "positive"},
                {"action": {"type": "callback", "label": "❌ Отказать", "payload": json.dumps({"cmd": "resign_leader_no", "id": req_id})}, "color": "negative"},
            ]],
        }
        # Пытаемся получить имя из кэша, иначе форматируем через _fmt_user (без блокировки)
        cached = self.user_name_cache.get(user_id)
        user_display = cached[0] if cached else f"id{user_id}"
        with self.db.conn() as c:
            u_info = c.execute(
                "SELECT nickname, faction, server_id FROM users WHERE vk_id=?", (user_id,)
            ).fetchone()
        faction_str = u_info["faction"] if u_info else "не указана"
        server_str = str(int(u_info["server_id"] or 1)) if u_info else "?"
        nick_str = u_info["nickname"] if u_info and u_info["nickname"] else "не указан"
        msg = (
            f"📋 Запрос на увольнение\n"
            f"Сотрудник: [id{user_id}|{user_display}]\n"
            f"Ник: {nick_str} | Фракция: {faction_str} | Сервер: {server_str}\n"
            f"Причина: {reason}\n\n"
            "Подтвердить увольнение?"
        )
        try:
            self.api.messages.send(
                peer_id=leader_id,
                random_id=0,
                message=msg,
                keyboard=json.dumps(keyboard),
                disable_mentions=1,
            )
            return True
        except Exception as e:
            logger.error(f"[resign] Ошибка отправки лидеру: {e}")
            return False

    def _do_resign(self, user_id: int) -> str:
        """Выполняет увольнение: сбрасывает фракцию и должность пользователя."""
        with self.db.conn() as c:
            u = c.execute("SELECT faction, position, server_id FROM users WHERE vk_id=?", (user_id,)).fetchone()
            if not u:
                return "❌ Пользователь не найден."
            old_faction = u["faction"]
            old_position = u["position"]
            c.execute(
                "UPDATE users SET faction='', position='', admin_level=0 WHERE vk_id=?",
                (user_id,),
            )
            c.execute("DELETE FROM resign_requests WHERE user_id=?", (user_id,))
        cached = self.user_name_cache.get(user_id)
        name_str = cached[0] if cached else f"id{user_id}"
        try:
            self.send_dm(user_id, "✅ Ваше увольнение подтверждено. Фракция и должность обнулены.")
        except Exception:
            pass
        try:
            self._add_history(
                nickname=name_str,
                target_vk_id=user_id,
                old_faction=old_faction,
                old_position=old_position,
                new_faction=None,
                new_position=None,
                actor_vk_id=None,
                event_type="resign",
            )
        except Exception as e:
            logger.error(f"_add_history error in _do_resign: {e}")
        return f"✅ Пользователь {name_str} (id{user_id}) уволен."

    def _send_greeting_payload(self, peer_id: int, text: str, attachment: str, sticker_id, on_partial_fail=None) -> None:
        """Отправляет приветствие (текст + вложение/стикер), устойчиво к нерабочим вложениям.
        Если комбинированная отправка не удалась — пробуем текст отдельно и каждое вложение по одному,
        чтобы один битый/недоступный файл (например, чужое приватное видео без access_key) не убивал
        всё приветствие целиком."""
        if sticker_id:
            try:
                self.api.messages.send(peer_id=peer_id, random_id=0, sticker_id=int(sticker_id), disable_mentions=1)
                return
            except Exception as e:
                logger.error(f"Ошибка отправки стикера приветствия: {e}")
        attachments = [a for a in str(attachment or "").split(",") if a.strip()][:10]
        kwargs = {"peer_id": peer_id, "random_id": 0, "message": text or "👋 Приветствие чата:", "disable_mentions": 1}
        if attachments:
            kwargs["attachment"] = ",".join(attachments)
        try:
            self.api.messages.send(**kwargs)
            return
        except Exception as e:
            logger.error(f"Ошибка комбинированной отправки приветствия: {e}; attachments={attachments}")
        # Фолбэк: текст отдельно
        try:
            self.api.messages.send(peer_id=peer_id, random_id=0, message=text or "👋 Приветствие чата:", disable_mentions=1)
        except Exception as e:
            logger.error(f"Ошибка отправки текста приветствия: {e}")
        # Фолбэк: вложения по одному, пропускаем те что не отправляются
        failed = []
        for one in attachments:
            try:
                self.api.messages.send(
                    peer_id=peer_id, random_id=random.randint(1, 2_147_483_647),
                    message="📎 Вложение приветствия", attachment=one, disable_mentions=1,
                )
            except Exception as e:
                logger.error(f"Вложение приветствия недоступно: {e}; attachment={one}")
                failed.append(one)
        if failed and on_partial_fail:
            on_partial_fail(failed)

    def handle_command(self, ctx: Ctx) -> None:
        # Защита: обрезаем слишком длинный ввод (DoS защита)
        raw_text = (ctx.text or "")
        if len(raw_text) > 2000:
            return  # Игнорируем аномально длинные сообщения без ответа
        text = self._normalize_bang_command(self._sanitize_input(raw_text, 1000))
        low = text.lower()
        is_senior = self._is_senior_admin_ctx(ctx)

        if self._contains_prompt_injection(text):
            self._record_failed_access(ctx.user_id, "prompt-injection pattern")
            return

        if self._handle_consent_gate(ctx, low):
            return

        if low in {"/start", "начать"}:
            if ctx.is_chat:
                return
            self.handle_start(ctx)
            return

        if (not ctx.is_chat) and self.handle_registration_session(ctx):
            return

        if self._is_bot_banned(ctx.user_id) and not is_senior:
            return

        if self._handle_admin_console(ctx):
            return

        if not low.startswith("!"):
            with self.db.conn() as c:
                ws = c.execute("SELECT target_id,created_at FROM wipe_sessions WHERE actor_id=?", (ctx.user_id,)).fetchone()
            if ws:
                if self.now_ts() - int(ws["created_at"] or 0) > WIPE_SESSION_TTL_SEC:
                    with self.db.conn() as c:
                        c.execute("DELETE FROM wipe_sessions WHERE actor_id=?", (ctx.user_id,))
                    self.send(ctx.peer_id, "⏳ Сессия стирания данных истекла. Запустите команду заново.")
                    return
                if ctx.text.strip() == WIPE_PASSWORD:
                    target = int(ws["target_id"])
                    snapshot = self._snapshot_user_before_wipe(target)
                    self._wipe_user_data(target)
                    with self.db.conn() as c:
                        c.execute("DELETE FROM wipe_sessions WHERE actor_id=?", (ctx.user_id,))
                    self.send(ctx.peer_id, f"✅ Данные пользователя {self._fmt_user(target)} полностью удалены.")
                    self.send_dm(
                        int(self.db.senior_admin_id),
                        f"⚠️ Стерка данных выполнена.\n"
                        f"Кто выполнил: {self._fmt_user(int(ctx.user_id))}\n"
                        f"Кого стерли: {self._fmt_user(int(target))}\n\n"
                        f"{snapshot}",
                    )
                else:
                    self.send(ctx.peer_id, "❌ Неверный пароль для стирания данных.")
                    with self.db.conn() as c:
                        c.execute("DELETE FROM wipe_sessions WHERE actor_id=?", (ctx.user_id,))
                return

        if not low.startswith("!"):
            return

        # Игнорируем одиночный "!" без команды
        _first_token = low.split()[0] if low.split() else ""
        if _first_token == "!":
            return
        cmd = self._resolve_alias(_first_token)
        self._current_user_for_parse = ctx.user_id
        if cmd != low.split()[0]:
            tokens = text.split()
            if tokens:
                tokens[0] = cmd
                text = " ".join(tokens)
                low = text.lower()
        pre_parts = text.split()
        # Многословные команды — разрешение по первым двум токенам
        if cmd == "!удалить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "инфу":
            cmd = "!удалитьинфу"
        if cmd == "!удалить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "список":
            cmd = "!удалить список"
        if cmd == "!новый" and len(pre_parts) >= 2 and pre_parts[1].lower() == "список":
            cmd = "!новый список"
        if cmd == "!новая" and len(pre_parts) >= 2 and pre_parts[1].lower() == "подписка":
            cmd = "!новая подписка"
        if cmd == "!все" and len(pre_parts) >= 2 and pre_parts[1].lower() == "списки":
            cmd = "!все списки"
        if cmd == "!дж" and len(pre_parts) >= 3 and pre_parts[1].lower() == "в" and pre_parts[2].lower() in {"роли", "роль"}:
            cmd = "!дж"
            # оставляем parts[1]="в" чтобы субкоманда "в роли" распозналась
        if cmd == "!проверка" and len(pre_parts) >= 2 and pre_parts[1].lower() in {"команд", "команды"}:
            cmd = "!проверка команд"
        if cmd == "!новое" and len(pre_parts) >= 2 and pre_parts[1].lower() == "приветствие":
            cmd = "!новое приветствие"
        if cmd == "!список" and len(pre_parts) >= 2 and pre_parts[1].lower() == "доступ":
            cmd = "!список доступ"
        if cmd == "!список" and len(pre_parts) >= 2 and pre_parts[1].lower() == "выговоров":
            cmd = "!список выговоров"
        if cmd == "!все" and len(pre_parts) >= 2 and pre_parts[1].lower() == "списки":
            cmd = "!все списки"
        if cmd == "!удалить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "приветствие":
            cmd = "!удалить приветствие"
        if cmd == "!отключить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "чат":
            cmd = "!отключить чат"
        if cmd == "!включить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "чат":
            cmd = "!включить чат"
        self._active_command = (ctx.user_id, text, ctx.peer_id)
        if ctx.is_chat:
            with self.db.conn() as c:
                blocked = c.execute("SELECT 1 FROM blocked_chats WHERE chat_id=?", (ctx.chat_id,)).fetchone()
            if blocked and not is_senior:
                return
        if (not is_senior) and (not self._check_rate(ctx.user_id)):
            self.send(ctx.peer_id, "⏳ Лимит команд в минуту превышен.")
            return

        bot_closed = self._is_bot_closed()
        # ── Проверка: чат отключён? ──────────────────────────────────────────
        # Делаем ДО проверки регистрации — чтобы не слать "вы не зарегистрированы" в отключённом чате
        if ctx.is_chat:
            with self.db.conn() as c:
                _ch = c.execute("SELECT enabled FROM chat_controls WHERE chat_id=?", (ctx.chat_id,)).fetchone()
            _chat_enabled = True if not _ch else bool(int(_ch["enabled"]))
            if not is_senior and not _chat_enabled:
                # В отключённом чате разрешаем только !включить чат (все варианты написания)
                allow_cmds = {"!включить чат", "!включить", "!включитьчат"}
                if cmd not in allow_cmds:
                    return  # молча игнорируем — не пишем ничего, не пишем "не зарегистрирован"

        if bot_closed and self._get_admin_level(ctx.user_id) == 0 and not is_senior:
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Бот временно закрыт. Доступ только для админов.")
            return

        # Для всех команд, кроме /start и заявок, нужен approved аккаунт
        u = self._user(ctx.user_id)
        if (not is_senior) and (not bot_closed) and (not u or int(u["approved"] or 0) == 0) and cmd not in {"!дубль"}:
            now = self.now_ts()
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Аккаунт не одобрен. Пожалуйста зарегистрируйтесь: /start в личные сообщения сообщества бота.")
            else:
                last = self.unapproved_notice_ts.get(ctx.user_id, 0)
                if now - last >= 600:
                    self.send(ctx.peer_id, "⛔ Аккаунт не одобрен. Пожалуйста зарегистрируйтесь: /start в личные сообщения сообщества бота.")
                    self.unapproved_notice_ts[ctx.user_id] = now
            return

        if not self._has_access(ctx, cmd):
            self._record_failed_access(ctx.user_id, f"no-access:{cmd}")
            self.send(ctx.peer_id, "⛔ Недостаточно прав для команды.")
            return

        parts = text.split()
        if cmd == "!список" and len(parts) >= 2 and parts[1].lower() == "чс":
            cmd = "!списокчс"
            parts = ["!списокчс"] + parts[2:]
        if cmd == "!список" and len(parts) >= 2 and parts[1].lower() == "допрассм":
            fake = Ctx(
                user_id=ctx.user_id,
                peer_id=ctx.peer_id,
                text="!списокдопрассм",
                platform=ctx.platform,
                tg_is_chat=ctx.tg_is_chat,
                tg_username=ctx.tg_username,
            )
            if not self._has_access(fake, "!списокдопрассм"):
                self.send(ctx.peer_id, "⛔ Недостаточно прав для команды.")
                return
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT vk_id,faction,server_id FROM extra_reviewers ORDER BY faction,server_id,vk_id"
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "📋 Список доп. рассматривающих пуст.")
                return
            out = ["📋 Список доп. рассматривающих:"]
            for r in rows:
                out.append(f"• {self._fmt_user(int(r['vk_id']))} — {r['faction']} — {int(r['server_id'])}")
            self.send(ctx.peer_id, "\n".join(out))
            return
        if self._missing_data(cmd, parts, ctx):
            self.send(ctx.peer_id, self._missing_args_message(cmd))
            return

        cd_ok, cd_left = self._check_command_cooldown(ctx.user_id, cmd)
        if (not is_senior) and (not cd_ok):
            self.send(ctx.peer_id, f"⏳ Подождите {cd_left} сек. перед повтором команды {cmd}.")
            return

        self._remember_chat_command(ctx, text)

        if cmd in {"!одобрить", "!отказать"}:
            self._approve_reject(ctx, cmd == "!одобрить")
            return

        if cmd == "!я":
            self.cmd_profile(ctx)
            return

        if cmd == "!версия":
            self.send(
                ctx.peer_id,
                f"🧪 Версия бота: {BOT_VERSION}\n💾 База данных: bot official",
            )
            return

        if cmd == "!аватарка":
            self.cmd_avatar(ctx, parts)
            return


        if cmd == "!банан":
            # Получаем целевого пользователя (из ответа или из аргумента)
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            
            # Проверяем, указан ли пользователь
            if target is None:
                self.send(ctx.peer_id, "🍌 Формат: !банан (пользователь) (причина) или ответом на сообщение с указанием причины")
                return
            
            # Получаем причину бана
            if len(parts) > 1 and self._parse_user(parts[1]) is not None:
                # Если пользователь указан через аргумент, то причина - всё после него
                reason = " ".join(parts[2:]).strip() if len(parts) > 2 else "не указана"
            else:
                # Если пользователь получен из ответа, то причина - все аргументы
                reason = " ".join(parts[1:]).strip() if len(parts) > 1 else "не указана"
            
            # Если причина пустая
            if not reason or reason.strip() == "":
                reason = "не указана"
            
            # Проверяем права администратора
            admin_level = self._get_admin_level(ctx.user_id)
            if admin_level < 10:
                self.send(ctx.peer_id, "❌ У вас недостаточно прав для выдачи бана.")
                return
            
            # Проверяем, что нельзя забанить самого себя
            if target == ctx.user_id:
                self.send(ctx.peer_id, "❌ Нельзя забанить самого себя!")
                return
            
            # Получаем имя пользователя (целевого)
            target_name = self._fmt_user(int(target))
            
          
            
            
            # Отправляем сообщение о бане
            ban_message = f"🍌 Пользователь {target_name} был забанен на первом сервере по причине: {reason} на 30 минут."
            self.send(ctx.peer_id, ban_message)
            
            return
        


        if cmd == "!помощь":
            self.send(ctx.peer_id, "Список команд: https://vk.ru/@pulse_rwpe-n-bota")
            return
            
        

        if cmd in {"!команды", "!комнады"}:
            actor = self._user(ctx.user_id)
            is_admin_faction = bool(actor and actor["faction"] == SECRET_FACTION)
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0

            role_probe: Optional[int] = None
            admin_probe: Optional[int] = None

            if len(parts) >= 2:
                if not is_admin_faction and not self._is_senior_admin_ctx(ctx):
                    self.send(ctx.peer_id, "⛔ Указывать уровни роли/админ прав в !команды могут только пользователи фракции Админ.")
                    return
                if not parts[1].isdigit():
                    self.send(ctx.peer_id, "Формат: !команды [уровень роли] [уровень админ прав]")
                    return
                role_probe = int(parts[1])
            if len(parts) >= 3:
                if not parts[2].isdigit():
                    self.send(ctx.peer_id, "Формат: !команды [уровень роли] [уровень админ прав]")
                    return
                admin_probe = int(parts[2])

            eff_role = role_probe if role_probe is not None else actor_role
            eff_admin = admin_probe if admin_probe is not None else actor_admin
            chat_id_ctx = ctx.chat_id if ctx.is_chat else None

            # Персональные разрешения/запреты
            with self.db.conn() as c:
                allow_rows_k = c.execute(
                    "SELECT command, granted_by FROM user_cmd_allow WHERE target_vk_id=?", (ctx.user_id,)
                ).fetchall()
                deny_rows_k = c.execute(
                    "SELECT command FROM user_cmd_deny WHERE target_vk_id=?", (ctx.user_id,)
                ).fetchall()
            allow_map_k = {r["command"]: r["granted_by"] for r in allow_rows_k}
            deny_set_k = {r["command"] for r in deny_rows_k}

            # Строим категории по admin_level для красивого отображения
            CATEGORIES = [
                (0,   "🟢 Публичные (все)"),
                (5,   "🔵 Модераторы (admin 5+)"),
                (10,  "🟡 admin 10+"),
                (30,  "🟠 admin 30+"),
                (40,  "🔴 admin 40+"),
                (50,  "🔴 admin 50+"),
                (70,  "🟣 admin 70+"),
                (90,  "⚫ admin 90+"),
                (100, "💀 Только старший админ (100)"),
            ]

            available: list[str] = []
            for ccmd in sorted(COMMAND_ACCESS.keys()):
                if ccmd == "!облик" and not self._is_senior_admin_ctx(ctx):
                    continue
                cmd_norm_k = self._normalize_command_name(ccmd)

                # Персональный запрет — пропускаем
                if cmd_norm_k in deny_set_k:
                    continue

                # Персональное разрешение — показываем с пометкой
                if cmd_norm_k in allow_map_k:
                    granter_k = self.user_name_cache.get(allow_map_k[cmd_norm_k])
                    gname = granter_k[0] if granter_k else f"id{allow_map_k[cmd_norm_k]}"
                    available.append(f"• {ccmd}  ✅ разрешено: {gname}")
                    continue

                # Вычисляем требования
                eff_min_admin = self._get_effective_admin_min(ccmd)
                need_role = self._required_role(ccmd, chat_id_ctx)
                has_custom_role = ctx.is_chat and self._has_custom_role_right(ctx.chat_id, ccmd)

                # Проверяем доступность по тем же правилам что _has_access
                accessible = False
                tag = ""
                if eff_min_admin > 0:
                    if eff_admin >= eff_min_admin:
                        accessible = True
                        tag = f"[адм.{eff_min_admin}+]"
                    elif has_custom_role and eff_role >= need_role:
                        accessible = True
                        tag = f"[роль {need_role}+ кастом]"
                else:
                    if need_role == 0:
                        accessible = True
                        tag = ""
                    elif eff_role >= need_role:
                        accessible = True
                        tag = f"[роль {need_role}+]"

                if accessible:
                    usage = COMMAND_USAGE.get(ccmd, ccmd)
                    line = f"• {usage}"
                    if tag:
                        line += f"  {tag}"
                    available.append(line)

            if not available:
                self.send(ctx.peer_id, "📘 Нет доступных команд.")
                return

            unique_lines = sorted(set(available), key=lambda x: x.lower())
            header = "📘 Команды бота"
            if role_probe is not None or admin_probe is not None:
                header += f" (просмотр для: роль={eff_role}, адм.={eff_admin})"
            header += f"\nВаш уровень: роль={actor_role}, адм.={actor_admin}"

            # Разбиваем на части по 3800 символов и отправляем в ЛС
            # В чате сообщаем что команды отправлены в ЛС
            VK_LIMIT = 3800
            chunks: list[str] = []
            current = header
            is_first = True
            for line in unique_lines:
                candidate = (current + "\n" + line) if not is_first else (current + "\n\n" + line)
                if len(candidate) > VK_LIMIT:
                    chunks.append(current)
                    current = "📘 Команды (продолжение):\n" + line
                    is_first = False
                else:
                    current = candidate
                    is_first = False
            chunks.append(current)

            # Если всё влезает в одно сообщение — отвечаем прямо в чат/ЛС
            CHAT_LIMIT = 3500
            if len(chunks) == 1 and len(chunks[0]) <= CHAT_LIMIT:
                self.send(ctx.peer_id, chunks[0])
            else:
                # Не влезает — шлём в ЛС
                if ctx.is_chat:
                    self.send(ctx.peer_id, f"📘 Список из {len(unique_lines)} команд отправлен вам в личные сообщения.")
                for chunk in chunks:
                    try:
                        self.send_dm(ctx.user_id, chunk)
                    except Exception:
                        self.send(ctx.peer_id, chunk)
            return

        if cmd == "!логи" and ctx.is_chat:
            items = self._last_chat_command_logs(int(ctx.chat_id), 20)
            if not items:
                self.send(ctx.peer_id, "📜 Логи команд: пусто.")
                return
            lines = ["📜 Последние 20 команд в этом чате:"]
            for idx, item in enumerate(items, start=1):
                uid = int(item["vk_id"])
                used_cmd = str(item["command"])
                dt = self._fmt_msk_dt(int(item["created_at"]))
                lines.append(f"{idx}. {used_cmd} — {self._fmt_user(int(uid))} — {dt}")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd in {"!дж", "!должности"}:
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            actor_admin = self._get_admin_level(ctx.user_id)
            is_admin_faction = actor_faction == SECRET_FACTION or actor_admin >= 90

            # Защита: !дж доступна только в чате своей фракции (или в ЛС)
            # Не применяется к фракции Админ и admin 90+
            if ctx.is_chat and not is_admin_faction:
                with self.db.conn() as c:
                    chat_row = c.execute(
                        "SELECT faction FROM chats WHERE chat_id=?", (ctx.chat_id,)
                    ).fetchone()
                chat_faction = str(chat_row["faction"] or "").strip() if chat_row else ""
                # Если чат не привязан к фракции или привязан к другой фракции — блокируем
                if not chat_faction or chat_faction.lower() != (actor_faction or "").lower():
                    self.send(ctx.peer_id, "⛔ Вам команда доступна только в чате своей фракции.")
                    return

            # ── субкоманда: !дж заголовок ───────────────────────────────────
            if len(parts) >= 2 and parts[1].lower() == "заголовок":
                # Права: лидер своей фракции или admin 70+
                if not self._is_leader_user(ctx.user_id) and actor_admin < 70:
                    self.send(ctx.peer_id, "⛔ Только лидер фракции или admin 70+ может управлять заголовками.")
                    return

                # !дж заголовок удалить (название)
                if len(parts) >= 3 and parts[2].lower() == "удалить":
                    title_del = " ".join(parts[3:]).strip()
                    if not title_del:
                        self.send(ctx.peer_id, "Формат: !дж заголовок удалить (название)")
                        return
                    with self.db.conn() as c:
                        cur = c.execute(
                            "DELETE FROM dj_headers WHERE faction=? AND server_id=? AND LOWER(title)=LOWER(?)",
                            (actor_faction, actor_server, title_del),
                        )
                    if cur.rowcount:
                        self.send(ctx.peer_id, f"✅ Заголовок «{title_del.upper()}» удалён.")
                    else:
                        self.send(ctx.peer_id, f"❌ Заголовок «{title_del}» не найден.")
                    return

                # !дж заголовок (название) (интервал)  — интервал последний аргумент вида NN-NN
                # Интервал может быть "80-90", "80 - 90", "80- 90" и т.д.
                if len(parts) < 4:
                    self.send(ctx.peer_id, "Формат: !дж заголовок (название) (мин-макс)\nПример: !дж заголовок Руководство 80-90")
                    return
                # Парсим интервал — пробуем взять последний или последние два токена
                range_parsed = None
                range_end_idx = len(parts)
                # Пробуем последний один токен как диапазон
                range_parsed = self._parse_level_range(parts[-1])
                if range_parsed:
                    title_parts = parts[2:-1]
                else:
                    # Пробуем последние три токена "NN - NN"
                    if len(parts) >= 5:
                        range_try = " ".join(parts[-3:])
                        range_parsed = self._parse_level_range(range_try)
                        if range_parsed:
                            title_parts = parts[2:-3]
                if not range_parsed or not title_parts:
                    self.send(ctx.peer_id, "❌ Не удалось распознать интервал. Пример: !дж заголовок Руководство 80-90")
                    return
                title = " ".join(title_parts).strip().upper()
                lmin, lmax = range_parsed
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO dj_headers(faction,server_id,title,level_min,level_max) VALUES(?,?,?,?,?)",
                        (actor_faction, actor_server, title, lmin, lmax),
                    )
                self.send(ctx.peer_id, f"✅ Заголовок «{title}» ({lmin}–{lmax}) добавлен/обновлён.")
                return

            # ── субкоманда: !дж новая ───────────────────────────────────────
            if len(parts) >= 2 and parts[1].lower() == "новая":
                if not self._is_leader_user(ctx.user_id) and actor_admin < 70:
                    self.send(ctx.peer_id, "⛔ Только лидер фракции или admin 70+ может добавлять должности.")
                    return
                # Последний токен — уровень
                if len(parts) < 4 or not parts[-1].lstrip("-").isdigit():
                    self.send(ctx.peer_id, "Формат: !дж новая (название должности) (уровень)\nПример: !дж новая Руководитель отдела 60")
                    return
                level = int(parts[-1])
                pos_name = " ".join(parts[2:-1]).strip()
                if not pos_name:
                    self.send(ctx.peer_id, "❌ Укажите название должности.")
                    return
                try:
                    with self.db.conn() as c:
                        c.execute(
                            "INSERT INTO dj_positions(faction,server_id,level,name) VALUES(?,?,?,?)",
                            (actor_faction, actor_server, level, pos_name),
                        )
                    self.send(ctx.peer_id, f"✅ Должность «{pos_name}» (уровень {level}) добавлена в ДЖ.")
                except Exception:
                    self.send(ctx.peer_id, f"⚠️ Должность «{level}» уже существует в ДЖ.")
                return

            # ── субкоманда: !дж переименовать ───────────────────────────────
            if len(parts) >= 2 and parts[1].lower() == "переименовать":
                if not self._is_leader_user(ctx.user_id) and actor_admin < 70:
                    self.send(ctx.peer_id, "⛔ Только лидер фракции или admin 70+ может переименовывать должности.")
                    return
                if len(parts) < 4:
                    self.send(ctx.peer_id, "Формат: !дж переименовать (уровень или название) (новое название)")
                    return
                # Ищем позицию — всё кроме последнего токена (если не число, то последние два слова = новое название?)
                # Формат: !дж переименовать <query...> <новое_название...>
                # query = первый токен (уровень числом) или всё до последнего пробела
                # Если parts[2] — число, то query=parts[2], new_name=parts[3:]
                # Если нет — parts[2] — первое слово query, parts[-1] — последнее слово нового имени (неоднозначно)
                # Упрощаем: query = parts[2], new_name = parts[3:]
                query = parts[2]
                new_name = " ".join(parts[3:]).strip()
                if not new_name:
                    self.send(ctx.peer_id, "❌ Укажите новое название.")
                    return
                found = self._dj_find_position(actor_faction, actor_server, query)
                if not found:
                    self.send(ctx.peer_id, f"❌ Должность «{query}» не найдена в ДЖ.")
                    return
                old_name = found["name"]
                try:
                    with self.db.conn() as c:
                        c.execute("UPDATE dj_positions SET name=? WHERE id=?", (new_name, found["id"]))
                        # Обновляем должность у всех сотрудников с этой должностью
                        updated = c.execute(
                            "UPDATE users SET position=? WHERE LOWER(position)=LOWER(?) AND faction=? AND server_id=?",
                            (new_name, old_name, actor_faction, actor_server),
                        ).rowcount
                    if updated:
                        self.send(ctx.peer_id, f"✅ Должность «{old_name}» переименована в «{new_name}». Обновлено сотрудников: {updated}.")
                    else:
                        self.send(ctx.peer_id, f"✅ Должность «{old_name}» переименована в «{new_name}».")
                except Exception:
                    self.send(ctx.peer_id, "❌ Должность с таким названием уже существует.")
                return

            # субкоманда !дж в роли убрана — теперь отдельная команда !синхроль
            if len(parts) >= 2 and parts[1].lower() in {"в", "в роли", "вроли"}:
                self.send(ctx.peer_id, "ℹ️ Используйте команду !синхроль для синхронизации ролей с ДЖ.")
                return

            # ── субкоманда: !дж удалить ─────────────────────────────────────
            if len(parts) >= 2 and parts[1].lower() == "удалить":
                if not self._is_leader_user(ctx.user_id) and actor_admin < 70:
                    self.send(ctx.peer_id, "⛔ Только лидер фракции или admin 70+ может удалять должности.")
                    return
                if len(parts) < 3:
                    self.send(ctx.peer_id, "Формат: !дж удалить (уровень или название)")
                    return
                query = " ".join(parts[2:]).strip()
                found = self._dj_find_position(actor_faction, actor_server, query)
                if not found:
                    self.send(ctx.peer_id, f"❌ Должность «{query}» не найдена в ДЖ.")
                    return
                with self.db.conn() as c:
                    c.execute("DELETE FROM dj_positions WHERE id=?", (found["id"],))
                self.send(ctx.peer_id, f"✅ Должность «{found['name']}» (уровень {found['level']}) удалена.")
                return

            # ── Просмотр ДЖ ─────────────────────────────────────────────────
            target_faction = actor_faction
            target_server = actor_server
            if is_admin_faction and len(parts) >= 2:
                rest_parts = parts[1:]
                if rest_parts[-1] in {"1", "2", "3"}:
                    target_server = int(rest_parts[-1])
                    rest_parts = rest_parts[:-1]
                if rest_parts:
                    target_faction, _ = self._extract_faction_and_rest(["!дж"] + rest_parts, 1)
            if not target_faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция. Укажите фракцию: !дж (фракция) [сервер]")
                return
            positions = self._dj_get_positions(target_faction, target_server)
            if not positions:
                self.send(ctx.peer_id, f"📋 ДЖ фракции {target_faction} (сервер {target_server}): список пуст.\n"
                          "Добавьте должности: !дж новая (название) (уровень)")
                return
            rendered = self._dj_render(target_faction, target_server)
            self.send(ctx.peer_id, rendered)
            return

        if cmd == "!сотрудники":
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            actor_admin = self._get_admin_level(ctx.user_id)
            is_admin_faction = actor_faction == SECRET_FACTION or actor_admin >= 90

            # Защита: только в чате своей фракции (или ЛС)
            if ctx.is_chat and not is_admin_faction:
                with self.db.conn() as c:
                    chat_row = c.execute("SELECT faction FROM chats WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                chat_faction = str(chat_row["faction"] or "").strip() if chat_row else ""
                if not chat_faction or chat_faction.lower() != (actor_faction or "").lower():
                    self.send(ctx.peer_id, "⛔ Вам команда доступна только в чате своей фракции.")
                    return

            # Определяем фракцию и сервер
            target_faction = actor_faction
            target_server = actor_server
            if is_admin_faction and len(parts) >= 2:
                rest = parts[1:]
                if rest[-1] in {"1", "2", "3"}:
                    target_server = int(rest[-1])
                    rest = rest[:-1]
                if rest:
                    target_faction, _ = self._extract_faction_and_rest(["!сотрудники"] + rest, 1)

            if not target_faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return

            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT vk_id, nickname, position, platform FROM users "
                    "WHERE faction=? AND server_id=? AND approved=1 "
                    "AND TRIM(COALESCE(position,''))!='' AND LOWER(COALESCE(position,''))!='не указана' "
                    "ORDER BY position COLLATE NOCASE, nickname COLLATE NOCASE",
                    (target_faction, int(target_server)),
                ).fetchall()

            if not rows:
                self.send(ctx.peer_id, f"👥 В фракции {target_faction} (сервер {target_server}) нет сотрудников.")
                return

            lines = [f"👥 СОТРУДНИКИ ФРАКЦИИ {target_faction.upper()} RW#{target_server}", ""]
            for r in rows:
                uid = int(r["vk_id"])
                nick = str(r["nickname"] or "").strip() or "—"
                pos = str(r["position"] or "").strip()
                platform = str(r["platform"] or "vk")
                cached = self.user_name_cache.get(uid)
                vk_name = cached[0] if cached else None
                if platform == "tg":
                    ref = f"{vk_name or ('id' + str(uid))} (TG)"
                else:
                    ref = f"[id{uid}|{vk_name or ('id' + str(uid))}]"
                lines.append(f"• {ref} — {pos}")
            lines.append(f"\n📊 Итого: {len(rows)} сотрудников")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd == "!приветствие":
            # Показать текущее приветствие чата
            if not ctx.is_chat:
                self.send(ctx.peer_id, "❌ Команда доступна только в чате.")
                return
            with self.db.conn() as c:
                row = c.execute("SELECT text, attachment, sticker_id, source_message_id FROM chat_greetings WHERE chat_id=?", (ctx.chat_id,)).fetchone()
            if not row or (not str(row["text"]).strip() and not str(row["attachment"] or "").strip() and not row["sticker_id"] and not row["source_message_id"]):
                self.send(ctx.peer_id, "ℹ️ Приветствие для этого чата не установлено.\nЧтобы установить, ответьте на сообщение: !новое приветствие")
                return
            source_message_id = int(row["source_message_id"] or 0)
            if source_message_id:
                try:
                    self.api.messages.send(
                        peer_id=ctx.peer_id,
                        random_id=random.randint(1, 2_147_483_647),
                        forward_messages=str(source_message_id),
                        disable_mentions=1,
                    )
                    return
                except Exception as e:
                    logger.error(f"Не удалось отправить приветствие forward_messages={source_message_id}: {e}")
            # Воспроизводим приветствие
            def _notify_failed(failed_list, _peer=ctx.peer_id):
                self.send(_peer, f"⚠️ {len(failed_list)} вложение(й) приветствия недоступны и не были отправлены.")
            self._send_greeting_payload(
                ctx.peer_id,
                str(row["text"] or ""),
                str(row["attachment"] or ""),
                row["sticker_id"],
                on_partial_fail=_notify_failed,
            )
            return


        if cmd == "!новое приветствие":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "❌ Команда доступна только в чате.")
                return
            if self._get_admin_level(ctx.user_id) < 40 and not self._is_senior_admin_ctx(ctx) and not self._is_leader_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Для установки приветствия нужен admin 40+ или лидер фракции.")
                return
            if not ctx.reply_message_id and not ctx.reply_text and not ctx.reply_user_id and not ctx.reply_attachment_ids:
                self.send(ctx.peer_id, "❌ Используйте команду ОТВЕТОМ на сообщение, которое станет приветствием.")
                return
            # Извлекаем текст и вложения из reply
            greet_text = ctx.reply_text or ""
            for url in (ctx.reply_link_urls or []):
                if url not in greet_text:
                    greet_text = (greet_text + "\n\n" + url).strip()
            attachment_ids = (ctx.reply_attachment_ids or [])[:10]
            if not greet_text.strip() and not attachment_ids:
                self.send(ctx.peer_id, "❌ Сообщение, на которое вы ответили, не содержит ни текста, ни поддерживаемых вложений.")
                return
            # Проверяем каждое вложение пробной отправкой — боту (группе) может не хватать прав на
            # просмотр приватного видео/файла другого пользователя, даже если объект корректно
            # распознан. Лучше предупредить сразу при сохранении, чем молча терять вложение позже,
            # когда зайдёт новый участник. Пробное сообщение сразу удаляем, чтобы не засорять чат.
            working_ids: list[str] = []
            broken_ids: list[str] = []
            for aid in attachment_ids:
                try:
                    probe = self.api.messages.send(
                        peer_id=ctx.peer_id, random_id=random.randint(1, 2_147_483_647),
                        message="🔎 Проверка вложения для приветствия", attachment=aid, disable_mentions=1,
                    )
                    working_ids.append(aid)
                    try:
                        probe_mid = int(probe)
                    except (TypeError, ValueError):
                        probe_mid = None
                    if probe_mid:
                        try:
                            self._api_method("messages.delete", {"message_ids": str(probe_mid), "delete_for_all": 1})
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"Вложение приветствия не прошло проверку при сохранении: {e}; attachment={aid}")
                    broken_ids.append(aid)
            stored_attachment = ",".join(working_ids)
            if not greet_text.strip() and not stored_attachment:
                self.send(ctx.peer_id, "❌ Ни одно вложение не удалось отправить (бот не имеет к ним доступа), а текста нет. Приветствие не сохранено.")
                return
            now = self.now_ts()
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO chat_greetings(chat_id, text, attachment, source_message_id, created_by, updated_at) VALUES(?,?,?,?,?,?)",
                    (ctx.chat_id, greet_text.strip(), stored_attachment, int(ctx.reply_message_id or 0) or None, ctx.user_id, now),
                )
            warn = ""
            if broken_ids:
                warn = (
                    f"\n⚠️ {len(broken_ids)} вложение(й) бот не смог отправить (нет доступа, например приватное "
                    f"видео автора) и они НЕ были сохранены в приветствие."
                )
            self.send(ctx.peer_id, f"✅ Приветствие установлено.\nПросмотр: !приветствие{warn}")
            return

        if cmd == "!удалить приветствие":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "❌ Команда доступна только в чате.")
                return
            if self._get_admin_level(ctx.user_id) < 40 and not self._is_senior_admin_ctx(ctx) and not self._is_leader_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Для удаления приветствия нужен admin 40+ или лидер фракции.")
                return
            with self.db.conn() as c:
                cur = c.execute("DELETE FROM chat_greetings WHERE chat_id=?", (ctx.chat_id,))
            if cur.rowcount:
                self.send(ctx.peer_id, "✅ Приветствие удалено.")
            else:
                self.send(ctx.peer_id, "ℹ️ Приветствие не было установлено.")
            return

        if cmd in {"!приглос", "!приглашение", "!пригласить"} and ctx.is_chat:
            if not self._has_access(ctx, "!приглос"):
                self.send(ctx.peer_id, "⛔ Недостаточно прав для настройки приглашений.")
                return
            if len(parts) < 2:
                # Показать текущий уровень
                with self.db.conn() as c:
                    row = c.execute("SELECT min_role, min_admin FROM chat_invite_level WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                min_role = int(row["min_role"]) if row else 40
                min_adm = int(row["min_admin"]) if row else 0
                self.send(ctx.peer_id,
                    f"👥 Настройка приглашений в этот чат:\n"
                    f"• Минимальная роль: {min_role}\n"
                    f"• Минимальный admin: {min_adm}\n"
                    f"Изменить: !приглос (уровень роли) [admin уровень]")
                return
            if not parts[1].isdigit():
                self.send(ctx.peer_id, "❌ Укажите числовой уровень роли.\nФормат: !приглос (уровень роли) [admin уровень]")
                return
            min_role = int(parts[1])
            min_adm = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            if not (0 <= min_role <= 100):
                self.send(ctx.peer_id, "❌ Уровень роли должен быть от 0 до 100.")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO chat_invite_level(chat_id, min_role, min_admin) VALUES(?,?,?)",
                    (ctx.chat_id, min_role, min_adm),
                )
            self.send(ctx.peer_id,
                f"✅ Установлено: минимальная роль {min_role} для приглашения в чат.\n"
                f"Пользователи с ролью < {min_role} (и admin < {min_adm}) будут кикнуты при попытке добавить кого-то."
            )
            return

        if cmd == "!синхроль":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "❌ Команда доступна только в чате.")
                return
            
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            actor_admin = self._get_admin_level(ctx.user_id)
            
            if not self._is_leader_user(ctx.user_id) and actor_admin < 70:
                self.send(ctx.peer_id, "⛔ Только лидер фракции или admin 70+ может применять !синхроль.")
                return
            
            positions = self._dj_get_positions(actor_faction, actor_server)
            if not positions:
                self.send(ctx.peer_id, "❌ ДЖ список пуст. Добавьте должности через !дж новая.")
                return
            
            # Получаем сотрудников с их должностями
            with self.db.conn() as c:
                staff_rows = c.execute(
                    "SELECT vk_id, position FROM users WHERE faction=? AND server_id=? AND approved=1",
                    (actor_faction, actor_server),
                ).fetchall()
            
            # Группируем сотрудников по должности
            staff_by_pos = {}
            for s in staff_rows:
                pos_key = str(s["position"] or "").strip().lower()
                if pos_key:
                    staff_by_pos.setdefault(pos_key, []).append(int(s["vk_id"]))
            
            # Сначала сбрасываем все текущие роли в БД
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET role_level=0 WHERE chat_id=?", (ctx.chat_id,))
            
            # Создаём карту уровень -> название роли
            level_to_name = {p["level"]: p["name"] for p in positions}
            
            applied = 0
            for p in positions:
                pos_key = p["name"].strip().lower()
                members = staff_by_pos.get(pos_key, [])
                
                # Создаём/обновляем роль в БД чата
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO chat_roles(chat_id, level, name) VALUES(?,?,?)",
                        (ctx.chat_id, p["level"], p["name"]),
                    )
                
                # Назначаем сотрудников на эту роль
                for uid in members:
                    with self.db.conn() as c:
                        c.execute(
                            "UPDATE chat_members SET role_level=? WHERE chat_id=? AND vk_id=?",
                            (p["level"], ctx.chat_id, uid),
                        )
                    applied += 1
            
            result_msg = f"✅ Синхронизация ДЖ с чатом выполнена!\n"
            result_msg += f"• Обновлено в БД: {applied} сотрудников\n"
            result_msg += f"• Ролей в ДЖ: {len(positions)}\n"
            
            # Показываем назначенные роли
            role_lines = []
            for level in sorted(level_to_name.keys(), reverse=True)[:10]:
                role_lines.append(f"  {level} → {level_to_name[level]}")
            if role_lines:
                result_msg += f"\n📋 Роли в чате:\n" + "\n".join(role_lines)
            
            self.send(ctx.peer_id, result_msg)
            return

        if cmd == "!логчат" and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) != 100:
                self.send(ctx.peer_id, "⛔ Только старший админ (100) может назначить лог-чат.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('log_chat_id',?)", (str(ctx.chat_id),))
            self.send(ctx.peer_id, "✅ Этот чат назначен лог-чатом.")
            return

        if cmd == "!чатжалоб":
            if self._get_admin_level(ctx.user_id) != 100:
                self.send(ctx.peer_id, "⛔ Только старший админ (100) может управлять чатом жалоб.")
                return
            if not ctx.is_chat or len(parts) < 2 or parts[1] not in {"+", "-"}:
                self.send(ctx.peer_id, "Формат: !чатжалоб + (в чате) или !чатжалоб - (в чате)")
                return
            if parts[1] == "+":
                with self.db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('complaint_chat_id',?)", (str(ctx.chat_id),))
                self.send(ctx.peer_id, "✅ Этот чат назначен для жалоб.")
                return
            current_chat = int(self._get_setting("complaint_chat_id", "0") or 0)
            if current_chat != int(ctx.chat_id):
                self.send(ctx.peer_id, "ℹ️ Этот чат сейчас не назначен как чат жалоб.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('complaint_chat_id','0')")
            self.send(ctx.peer_id, "✅ Назначение чата жалоб снято.")
            return

        if cmd == "!логвкл":
            if self._get_admin_level(ctx.user_id) != 100:
                self.send(ctx.peer_id, "⛔ Только старший админ (100) может включить логирование.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('logging_enabled','1')")
            self.send(ctx.peer_id, "✅ Логирование команд включено.")
            return

        if cmd == "!логвыкл":
            if self._get_admin_level(ctx.user_id) != 100:
                self.send(ctx.peer_id, "⛔ Только старший админ (100) может выключить логирование.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('logging_enabled','0')")
            self.send(ctx.peer_id, "✅ Логирование команд выключено.")
            return

        if cmd == "!тишина" and ctx.is_chat:
            # Форматы:
            # !тишина - показать текущие настройки
            # !тишина <минуты> <сообщений> - установить/обновить
            # !тишина иммунитет [+|-] [пользователь] - управление исключениями
            # !тишина админпорог <уровень> - установить админ-уровень для иммунитета

            # Получаем минимальный admin_level для этой команды из настроек
            required_admin = self._get_admin_min("!тишина")  # по умолчанию 50 из COMMAND_ACCESS

            if len(parts) >= 2 and parts[1].lower() == "иммунитет":
                # Управление исключениями
                if self._get_admin_level(ctx.user_id) < required_admin and not self._is_senior_admin_ctx(ctx):
                    self.send(ctx.peer_id, f"⛔ Для управления исключениями нужен админ-уровень {required_admin}+.")
                    return
                
                if len(parts) >= 3 and parts[2] == "+":
                    target = (self._parse_user(parts[3]) if len(parts) > 3 else None) or ctx.reply_user_id
                    if target is None:
                        self.send(ctx.peer_id, "Формат: !тишина иммунитет + (пользователь) [время_в_минутах]")
                        return
                    
                    duration_min = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 60
                    until_ts = self.now_ts() + (duration_min * 60)
                    
                    with self.db.conn() as c:
                        c.execute(
                            "INSERT OR REPLACE INTO silence_exceptions(chat_id,vk_id,until_ts) VALUES(?,?,?)",
                            (ctx.chat_id, target, until_ts),
                        )
                    self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} добавлен в исключения тишины на {duration_min} мин.")
                    return
                
                elif len(parts) >= 3 and parts[2] == "-":
                    target = (self._parse_user(parts[3]) if len(parts) > 3 else None) or ctx.reply_user_id
                    if target is None:
                        self.send(ctx.peer_id, "Формат: !тишина иммунитет - (пользователь)")
                        return
                    
                    with self.db.conn() as c:
                        c.execute("DELETE FROM silence_exceptions WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                    self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} удалён из исключений тишины.")
                    return
                
                else:
                    # Показать список исключений
                    with self.db.conn() as c:
                        rows = c.execute(
                            "SELECT vk_id, until_ts FROM silence_exceptions WHERE chat_id=? AND until_ts > ? ORDER BY until_ts",
                            (ctx.chat_id, self.now_ts()),
                        ).fetchall()
                    if not rows:
                        self.send(ctx.peer_id, "📋 Список исключений тишины пуст.")
                        return
                    out = ["📋 Исключения тишины в этом чате:"]
                    for r in rows:
                        until = datetime.fromtimestamp(int(r["until_ts"]), tz=MSK_TZ).strftime("%d.%m.%Y %H:%M")
                        out.append(f"• {self._fmt_user(int(r['vk_id']))} — до {until}")
                    self.send(ctx.peer_id, "\n".join(out))
                    return
            
            elif len(parts) >= 2 and parts[1].lower() == "админпорог" and len(parts) >= 3 and parts[2].isdigit():
                # Установка админ-уровня для иммунитета (глобальная настройка)
                if self._get_admin_level(ctx.user_id) < 100:
                    self.send(ctx.peer_id, "⛔ Только старший админ может менять порог.")
                    return
                
                threshold = int(parts[2])
                if threshold < 0 or threshold > 100:
                    self.send(ctx.peer_id, "❌ Уровень должен быть от 0 до 100.")
                    return
                
                with self.db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('silence_admin_threshold',?)", (str(threshold),))
                self.send(ctx.peer_id, f"✅ Админ-уровень для иммунитета к тишине установлен: {threshold}+")
                return
            
            elif len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                # Установка режима тишины
                if self._get_admin_level(ctx.user_id) < required_admin and not self._is_senior_admin_ctx(ctx):
                    self.send(ctx.peer_id, f"⛔ Для включения тишины нужен админ-уровень {required_admin}+.")
                    return
                
                window_min = max(1, int(parts[1]))
                msg_limit = max(1, int(parts[2]))
                
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO chat_silence(chat_id,window_min,msg_limit,enabled) VALUES(?,?,?,1)",
                        (ctx.chat_id, window_min, msg_limit),
                    )
                
                # Информация об иммунитете
                admin_threshold = int(self._get_setting("silence_admin_threshold", "40"))
                immune_info = f"\n👑 Иммунитет имеют: админы {admin_threshold}+ и пользователи с временным голосом"
                
                self.send(ctx.peer_id, 
                    f"✅ Режим тишины включен: {msg_limit} сообщ. раз в {window_min} мин. на пользователя."
                    f"{immune_info}"
                )
                return
            
            elif len(parts) == 1:
                # Показать текущие настройки
                with self.db.conn() as c:
                    rule = c.execute(
                        "SELECT window_min,msg_limit,enabled FROM chat_silence WHERE chat_id=?",
                        (ctx.chat_id,),
                    ).fetchone()
                
                if not rule or int(rule["enabled"] or 0) == 0:
                    self.send(ctx.peer_id, "ℹ️ Режим тишины в этом чате выключен.")
                    return
                
                admin_threshold = int(self._get_setting("silence_admin_threshold", "40"))
                self.send(ctx.peer_id, 
                    f"📊 Настройки тишины в этом чате:\n"
                    f"• {int(rule['msg_limit'])} сообщ. раз в {int(rule['window_min'])} мин.\n"
                    f"• Статус: {'Включен' if int(rule['enabled']) else 'Выключен'}\n"
                    f"• Иммунитет: админы {admin_threshold}+ и временный голос"
                )
                return
            
            else:
                self.send(ctx.peer_id, 
                    "📖 Форматы команды !тишина:\n"
                    "• !тишина <минуты> <сообщений> — включить\n"
                    "• !тишина — показать настройки\n"
                    "• !тишина иммунитет [+|-] [пользователь] — управление исключениями\n"
                    "• !тишина админпорог <уровень> — установить админ-уровень для иммунитета"
                )
                return

        if cmd == "!снятьтишину" and ctx.is_chat:
            required_silence_admin = self._get_admin_min("!снятьтишину")
            if self._get_admin_level(ctx.user_id) < required_silence_admin and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, f"⛔ Для отключения тишины нужен админ-уровень {required_silence_admin}+.")
                return
            
            with self.db.conn() as c:
                c.execute("DELETE FROM chat_silence WHERE chat_id=?", (ctx.chat_id,))
                # Очищаем только просроченные исключения, активные оставляем
                c.execute("DELETE FROM silence_exceptions WHERE chat_id=? AND until_ts<=?", (ctx.chat_id, self.now_ts()))
            
            self.send(ctx.peer_id, "✅ Режим тишины выключен в этом чате. Активные исключения сохранены.")
            return

        if cmd == "!неприниматьдж":
            with self.db.conn() as c:
                exists = c.execute("SELECT 1 FROM hire_opt_out WHERE vk_id=?", (ctx.user_id,)).fetchone()
                if exists:
                    c.execute("DELETE FROM hire_opt_out WHERE vk_id=?", (ctx.user_id,))
                    self.send(ctx.peer_id, "✅ Вы снова будете получать предложения о найме.")
                else:
                    c.execute("INSERT OR IGNORE INTO hire_opt_out(vk_id) VALUES(?)", (ctx.user_id,))
                    self.send(ctx.peer_id, "✅ Вы больше не будете получать предложения о найме (если у вас есть текущая должность).")
            return

        if cmd == "!уволиться":
            # Работает и в чате и в ЛС
            u = self._user(ctx.user_id)
            if not u or not u["faction"]:
                self.send(ctx.peer_id, "❌ Вы не состоите ни в одной фракции.")
                return
            reason = " ".join(parts[1:]).strip() if len(parts) > 1 else "причина не указана"
            reason = self._sanitize_input(reason, 300)
            faction = u["faction"]
            server_id = int(u["server_id"] or 1)  # Сервер ПОЛЬЗОВАТЕЛЯ — ключевой для поиска лидера
            # Ищем лидера строго по фракции И серверу пользователя
            with self.db.conn() as c:
                leader_row = c.execute(
                    "SELECT vk_id FROM leaders WHERE faction=? AND server_id=? LIMIT 1",
                    (faction, server_id),
                ).fetchone()
                # Только если лидера на этом сервере нет — берём любого лидера фракции
                if not leader_row:
                    leader_row = c.execute(
                        "SELECT vk_id FROM leaders WHERE faction=? ORDER BY server_id LIMIT 1",
                        (faction,),
                    ).fetchone()
                leader_id = int(leader_row["vk_id"]) if leader_row else None
                # Если лидер не найден — увольняем автоматически без сохранения запроса
                if leader_id is None:
                    c.execute(
                        "UPDATE users SET faction='не указана', position='не указана' WHERE vk_id=?",
                        (ctx.user_id,),
                    )
                    auto_msg = (
                        f"✅ Лидер вашей фракции не назначен.\n"
                        "Увольнение выполнено автоматически. Фракция и должность сброшены."
                    )
                    if ctx.is_chat:
                        self.send(ctx.peer_id, auto_msg)
                    self.send_dm(ctx.user_id, auto_msg)
                    return
                # Сохраняем pending запрос с server_id
                c.execute(
                    "INSERT OR REPLACE INTO resign_requests(user_id,reason,faction,server_id,leader_id,created_at,step)"
                    " VALUES(?,?,?,?,?,?,'user_confirm')",
                    (ctx.user_id, reason, faction, server_id, leader_id, self.now_ts()),
                )
            # Подтверждение всегда уходит в ЛС
            self._send_resign_user_confirm(ctx, reason)
            if ctx.is_chat:
                self.send(ctx.peer_id, "📩 Запрос на увольнение отправлен вам в личные сообщения для подтверждения.")
            return

        if cmd == "!голос" and ctx.is_chat:
            target = (self._parse_user(parts[1]) if len(parts) >= 2 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !голос (пользователь)")
                return
            until = self.now_ts() + 1200
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO silence_exceptions(chat_id,vk_id,until_ts) VALUES(?,?,?)",
                    (ctx.chat_id, target, until),
                )
            self.send(ctx.peer_id, f"✅ Пользователю {self._fmt_user(target)} выдан голос на 20 минут.")
            return

        # ─── Глобальные списки ─────────────────────────────────────────────────

        if cmd == "!новый список":
            # Формат: ответом на сообщение — !новый список (название) (уровень)
            # Уровень = минимальный admin_level для просмотра (0 = все)
            # Содержимое берётся из reply_text без изменений
            user_adm = self._get_admin_level(ctx.user_id)
            # Права: любой пользователь (нет ограничений по admin_level)
            # parts: ["!новый", "список", <name_tokens...>, <level>?]
            if len(parts) < 3:
                self.send(ctx.peer_id, "Формат (ответом на сообщение): !новый список (название) [уровень]")
                return
            # Парсим уровень — последний токен если число
            if parts[-1].isdigit():
                list_admin = int(parts[-1])
                list_name = " ".join(parts[2:-1]).strip()
            else:
                list_admin = 0  # По умолчанию — для всех
                list_name = " ".join(parts[2:]).strip()
            if not list_name:
                self.send(ctx.peer_id, "❌ Укажите название списка.")
                return
            if len(list_name) > 80:
                self.send(ctx.peer_id, "❌ Название списка слишком длинное (макс. 80 символов).")
                return
            if list_admin < 0 or list_admin > 100:
                self.send(ctx.peer_id, "❌ Уровень доступа должен быть от 0 до 100.")
                return
            # Содержимое — из reply_text (обязательно)
            if not ctx.reply_text or not ctx.reply_text.strip():
                self.send(ctx.peer_id, "❌ Команда должна быть ответом на сообщение. Содержимое списка берётся из того сообщения.")
                return
            content = ctx.reply_text  # сохраняем как есть, без изменений
            ok = self.lists_db.create_list(list_name, list_admin, ctx.user_id, self.now_ts())
            if ok:
                # Сразу записываем содержимое
                self.lists_db.update_list(list_name, content, self.now_ts())
                self.send(ctx.peer_id, f"✅ Список «{list_name}» создан. Уровень доступа: {list_admin}+ (0 = все).\n!список {list_name}")
            else:
                self.send(ctx.peer_id, f"❌ Список с названием «{list_name}» уже существует.")
            return

        if cmd == "!список доступ":
            # !список доступ (название) — показать кому доступен список
            # !список доступ + (пользователь) (название) — выдать доступ
            user_adm = self._get_admin_level(ctx.user_id)

            if len(parts) >= 2 and parts[1] == "+":
                # !список доступ + (пользователь) (название списка)
                if len(parts) < 4:
                    self.send(ctx.peer_id, "❌ Недостаточно аргументов.\nФормат: !список доступ + (пользователь) (название списка)")
                    return
                uid = self._parse_user_fast(parts[2]) or self._parse_user(parts[2])
                if not uid:
                    self.send(ctx.peer_id, "❌ Не удалось определить пользователя.\nФормат: !список доступ + (пользователь) (название списка)")
                    return
                list_name = " ".join(parts[3:]).strip()
                if not list_name:
                    self.send(ctx.peer_id, "❌ Укажите название списка.\nФормат: !список доступ + (пользователь) (название списка)")
                    return
                ok, err = self.lists_db.grant_access(list_name, uid, ctx.user_id, self.now_ts())
                if ok:
                    self.send(ctx.peer_id, f"✅ {self._fmt_user(uid)} получил персональный доступ к списку «{list_name}».\nТеперь этот список появится у него в !все списки.")
                else:
                    self.send(ctx.peer_id, f"❌ {err}")
                return
            else:
                # !список доступ (название) — показать все доступы
                list_name = " ".join(parts[1:]).strip()
                if not list_name:
                    self.send(ctx.peer_id, "❌ Укажите название списка.\nФормат: !список доступ (название списка)")
                    return
                with self.lists_db.conn() as lc:
                    meta = lc.execute(
                        "SELECT min_admin_level FROM global_lists WHERE LOWER(name)=LOWER(?)", (list_name,)
                    ).fetchone()
                if not meta:
                    self.send(ctx.peer_id, f"❌ Список «{list_name}» не найден.")
                    return
                min_adm = int(meta["min_admin_level"])
                personal_entries = self.lists_db.get_access_entries(list_name)
                lines = [f"🔐 Доступ к списку «{list_name}»:",
                         f"• Минимальный admin-уровень: {min_adm if min_adm > 0 else 'без ограничений (публичный)'}"]
                if min_adm > 0:
                    with self.db.conn() as c:
                        level_users = c.execute(
                            "SELECT vk_id FROM users WHERE admin_level>=? LIMIT 20", (min_adm,)
                        ).fetchall()
                    if level_users:
                        lines.append("• По уровню admin: " + ", ".join(self._fmt_user(r["vk_id"]) for r in level_users))
                    else:
                        lines.append("• По уровню admin: никто")
                if personal_entries:
                    lines.append("• Персональный доступ: " + ", ".join(self._fmt_user(r["vk_id"]) for r in personal_entries))
                else:
                    lines.append("• Персональный доступ: не выдан")
                self.send(ctx.peer_id, "\n".join(lines))
                return

        if cmd == "!список":
            # !список (название) — показать список (поиск без учёта регистра, с учётом персонального доступа)
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !список (название)")
                return
            user_adm = self._get_admin_level(ctx.user_id)
            list_name = " ".join(parts[1:]).strip()
            if not list_name:
                self.send(ctx.peer_id, "❌ Укажите название списка.")
                return
            lst = self.lists_db.get_list(list_name, user_adm, vk_id=ctx.user_id)
            if lst is None:
                with self.lists_db.conn() as lc:
                    row = lc.execute("SELECT min_admin_level FROM global_lists WHERE LOWER(name)=LOWER(?)", (list_name,)).fetchone()
                if row:
                    self.send(ctx.peer_id, f"⛔ Недостаточно прав для просмотра списка «{list_name}» (требуется admin {row['min_admin_level']}+, или запросите персональный доступ).")
                else:
                    self.send(ctx.peer_id, f"❌ Список «{list_name}» не найден.")
                return
            content = lst["content"]
            level_str = f"admin {lst['min_admin_level']}+" if lst["min_admin_level"] > 0 else "публичный"
            header = f"📋 {lst['name']} ({level_str}):"
            self.send(ctx.peer_id, f"{header}\n{content}" if content.strip() else f"📋 Список «{lst['name']}» пуст.")
            return

        if cmd in {"!все списки", "!списки"}:
            user_adm = self._get_admin_level(ctx.user_id)
            all_lists = self.lists_db.all_lists(user_adm, vk_id=ctx.user_id)
            if not all_lists:
                self.send(ctx.peer_id, "📋 Нет доступных списков.")
                return
            out = [f"📋 Доступные вам списки (admin {user_adm}):"]
            for lst in all_lists:
                level_str = f"admin {lst['min_admin_level']}+" if lst["min_admin_level"] > 0 else "публичный"
                # Пометка если доступ персональный (нет нужного admin уровня)
                personal_mark = ""
                if lst["min_admin_level"] > 0 and user_adm < lst["min_admin_level"]:
                    personal_mark = " (персональный доступ)"
                out.append(f"• {lst['name']}  [{level_str}]{personal_mark}  — !список {lst['name']}")
            self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!удалить список":
            need_adm = self._get_admin_min("!удалить список")
            user_adm = self._get_admin_level(ctx.user_id)
            if user_adm < need_adm and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, f"⛔ Для удаления списка нужен админ-уровень {need_adm}+.")
                return
            list_name = " ".join(parts[2:]).strip()
            if not list_name:
                self.send(ctx.peer_id, "Формат: !удалить список (название)")
                return
            ok = self.lists_db.delete_list(list_name)
            if ok:
                self.send(ctx.peer_id, f"✅ Список «{list_name}» удалён.")
            else:
                self.send(ctx.peer_id, f"❌ Список «{list_name}» не найден.")
            return

        # ─────────────────────────────────────────────────────────────────────

        if cmd == "!допрассм":
            if len(parts) >= 5 and parts[1] == "+" and parts[-1] in {"1", "2", "3"}:
                target = self._parse_user(parts[2]) or ctx.reply_user_id
                if target is None:
                    self.send(ctx.peer_id, "Формат: !допрассм + (пользователь) (фракция) (номер сервера)")
                    return
                faction_input = " ".join(parts[3:-1]).strip()
                faction_match = next((f for f in ALL_FACTIONS if f.lower() == faction_input.lower()), None)
                if not faction_match:
                    self.send(ctx.peer_id, "❌ Неизвестная фракция.")
                    return
                server_id = int(parts[-1])
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO extra_reviewers(vk_id,faction,server_id,added_by,added_at) VALUES(?,?,?,?,?)",
                        (target, faction_match, server_id, ctx.user_id, self.now_ts()),
                    )
                self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} добавлен как доп. рассматривающий для {faction_match} (сервер {server_id}).")
                return
            if len(parts) >= 3 and parts[1] == "-":
                target = self._parse_user(parts[2]) or ctx.reply_user_id
                if target is None:
                    self.send(ctx.peer_id, "Формат: !допрассм - (пользователь)")
                    return
                with self.db.conn() as c:
                    c.execute("DELETE FROM extra_reviewers WHERE vk_id=?", (target,))
                self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} удалён из списка доп. рассматривающих.")
                return
            self.send(ctx.peer_id, "Формат: !допрассм + (пользователь) (фракция) (номер сервера) или !допрассм - (пользователь)")
            return

        # !отключить чат — работает с обоими вариантами команды
        _is_disable_cmd = (cmd == "!отключить чат") or (cmd == "!отключить" and len(parts) >= 2 and parts[1].lower() == "чат")
        if _is_disable_cmd and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) < 40 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+.")
                return
            with self.db.conn() as c:
                ch = c.execute("SELECT enabled FROM chat_controls WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                current = True if not ch else bool(int(ch["enabled"]))
                if not current:
                    self.send(ctx.peer_id, "ℹ️ Чат уже отключён. Для включения: !включить чат")
                    return
                c.execute("INSERT OR REPLACE INTO chat_controls(chat_id,enabled) VALUES(?,0)", (ctx.chat_id,))
            self.send(ctx.peer_id,
                "🔕 Чат отключён. Бот игнорирует команды всех пользователей (кроме старшего админа).\n"
                "Для включения: !включить чат"
            )
            return

        # !включить чат — работает с обоими вариантами команды
        _is_enable_cmd = (cmd == "!включить чат") or (cmd == "!включить" and len(parts) >= 2 and parts[1].lower() == "чат")
        if _is_enable_cmd and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) < 40 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+.")
                return
            with self.db.conn() as c:
                ch = c.execute("SELECT enabled FROM chat_controls WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                current = True if not ch else bool(int(ch["enabled"]))
                if current:
                    self.send(ctx.peer_id, "ℹ️ Чат уже включён.")
                    return
                c.execute("INSERT OR REPLACE INTO chat_controls(chat_id,enabled) VALUES(?,1)", (ctx.chat_id,))
            self.send(ctx.peer_id, "🔔 Чат включён. Бот снова принимает команды от всех пользователей.")
            return

        if cmd == "!команда":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Команда доступна только в чате.")
                return
            if len(parts) >= 3 and parts[1] == "-":
                target_cmd = self._normalize_command_name(parts[2])
                if target_cmd not in COMMAND_ACCESS:
                    self.send(ctx.peer_id, f"❌ Неизвестная команда: {parts[2]}")
                    return
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO chat_disabled_commands(chat_id,command) VALUES(?,?)",
                        (ctx.chat_id, target_cmd),
                    )
                self.send(ctx.peer_id, f"✅ Команда {target_cmd} отключена в этом чате.")
                return
            if len(parts) >= 3 and parts[1] == "+":
                target_cmd = self._normalize_command_name(parts[2])
                if target_cmd not in COMMAND_ACCESS:
                    self.send(ctx.peer_id, f"❌ Неизвестная команда: {parts[2]}")
                    return
                with self.db.conn() as c:
                    c.execute("DELETE FROM chat_disabled_commands WHERE chat_id=? AND command=?", (ctx.chat_id, target_cmd))
                self.send(ctx.peer_id, f"✅ Команда {target_cmd} включена в этом чате.")
                return
            if len(parts) >= 4 and parts[1] == "-" and parts[3].isdigit():
                target_cmd = self._normalize_command_name(parts[2])
                lvl = int(parts[3])
                if target_cmd not in COMMAND_ACCESS:
                    self.send(ctx.peer_id, f"❌ Неизвестная команда: {parts[2]}")
                    return
                if lvl < 0 or lvl > 100:
                    self.send(ctx.peer_id, "❌ Уровень прав должен быть в диапазоне 0..100.")
                    return
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO chat_command_rights(chat_id,command,min_role) VALUES(?,?,?)",
                        (ctx.chat_id, target_cmd, lvl),
                    )
                self.send(ctx.peer_id, f"✅ Для {target_cmd} в этом чате установлен порог роли {lvl}.")
                return
            self.send(ctx.peer_id, "Формат: !команда - !команда (уровень) или !команда + !команда")
            return

        if cmd == "!чистка" and ctx.is_chat:
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id)
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для команды нужны права: роль 70+ или админ 10+.")
                return
            if len(parts) < 2 and ctx.reply_cmid is not None:
                deleted = 0
                cmids = [int(ctx.reply_cmid)]
                if ctx.message_cmid is not None:
                    cmids.append(int(ctx.message_cmid))
                cmids = list(dict.fromkeys(cmids))
                for payload in (
                    {"peer_id": ctx.peer_id, "cmids": ",".join(str(x) for x in cmids), "delete_for_all": 1},
                    {"peer_id": ctx.peer_id, "cmids": ",".join(str(x) for x in cmids)},
                ):
                    if self._api_method("messages.delete", payload):
                        deleted = 1
                        break
                if deleted:
                    self._active_command = None
                else:
                    self.send(ctx.peer_id, "❌ Не удалось удалить сообщение.")
                return
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !чистка (количество) или !чистка (время: 10м/2ч/10 мин/20 часов) или reply без аргументов.")
                return
            # Собираем аргумент времени — единица измерения может идти слитно ("10м") или
            # отдельным словом через пробел ("10 мин", "20 часов", "20 ч").
            arg = parts[1].lower()
            rest = parts[2].lower() if len(parts) >= 3 else ""
            ids_to_delete: list[int] = []
            cmids_to_delete: list[int] = []
            try:
                if arg.isdigit() and not rest:
                    limit = min(int(arg), 200)
                    hist = self.api.messages.getHistory(peer_id=ctx.peer_id, count=limit)
                    items = hist.get("items", [])
                    ids_to_delete = [int(m["id"]) for m in items[:limit] if m.get("id") is not None]
                    cmids_to_delete = [int(m["conversation_message_id"]) for m in items[:limit] if m.get("conversation_message_id") is not None]
                else:
                    m = re.match(r"^(\d+)\s*([мmhчh])", arg) if not arg.isdigit() else None
                    if m:
                        val, unit = int(m.group(1)), m.group(2)
                    elif arg.isdigit() and rest:
                        val = int(arg)
                        unit = rest[0]
                    else:
                        self.send(ctx.peer_id, "❌ Неверный формат времени. Примеры: 10м, 2ч, 10 мин, 20 часов.")
                        return
                    sec_per_unit = 60 if unit in {"м", "m"} else 3600
                    sec = val * sec_per_unit

                    # Лимит для обычных пользователей: не больше 24 часов за одну чистку.
                    # Фракция "Админ" (SECRET_FACTION) и пользователи с admin_level > 20 — без лимита.
                    actor = self._user(ctx.user_id)
                    actor_faction = actor["faction"] if actor else None
                    is_unlimited = actor_faction == SECRET_FACTION or actor_admin > 20
                    max_sec = 24 * 3600
                    if not is_unlimited and sec > max_sec:
                        self.send(ctx.peer_id, "⛔ Для вашей роли максимум — 24 часа за одну чистку. Пример: !чистка 24 часа")
                        return

                    cutoff = self.now_ts() - sec
                    items: list[dict] = []
                    offset = 0
                    # Подгружаем историю страницами по 200, пока не выйдем за cutoff или за разумный предел.
                    for _ in range(60):  # до 12000 сообщений вглубь — с запасом на сутки активного чата
                        hist = self.api.messages.getHistory(peer_id=ctx.peer_id, count=200, offset=offset)
                        page = hist.get("items", [])
                        if not page:
                            break
                        items.extend(page)
                        offset += len(page)
                        oldest_date = int(page[-1].get("date", 0))
                        if oldest_date < cutoff or len(page) < 200:
                            break
                    picked = [msg for msg in items if int(msg.get("date", 0)) >= cutoff]
                    ids_to_delete = [int(msg["id"]) for msg in picked if msg.get("id") is not None]
                    cmids_to_delete = [int(msg["conversation_message_id"]) for msg in picked if msg.get("conversation_message_id") is not None]
                deleted = 0
                def _chunks(arr: list[int], n: int = 80) -> list[list[int]]:
                    return [arr[i:i+n] for i in range(0, len(arr), n)]
                if cmids_to_delete:
                    for chunk in _chunks(cmids_to_delete):
                        ok = self._api_method("messages.delete", {"peer_id": ctx.peer_id, "cmids": ",".join(str(x) for x in chunk), "delete_for_all": 1})
                        if ok:
                            deleted += len(chunk)
                        else:
                            ok2 = self._api_method("messages.delete", {"peer_id": ctx.peer_id, "cmids": ",".join(str(x) for x in chunk)})
                            if ok2:
                                deleted += len(chunk)
                if deleted == 0 and ids_to_delete:
                    for chunk in _chunks(ids_to_delete):
                        ok = self._api_method(
                            "messages.delete",
                            {"message_ids": ",".join(str(x) for x in chunk), "delete_for_all": 1},
                        )
                        if ok:
                            deleted += len(chunk)
                if deleted == 0 and ids_to_delete:
                    for chunk in _chunks(ids_to_delete):
                        ok = self._api_method("messages.delete", {"message_ids": ",".join(str(x) for x in chunk)})
                        if ok:
                            deleted += len(chunk)
                if deleted == 0:
                    self.send(ctx.peer_id, "❌ Не удалось удалить сообщения. Проверьте права бота в беседе.")
                else:
                    self._active_command = None
            except Exception:
                self.send(ctx.peer_id, "❌ Не удалось выполнить очистку. Проверьте права бота в беседе.")
            return

        if cmd == "!фракция" and len(parts) >= 3 and parts[1].lower() == SECRET_FACTION.lower():
            if self._get_admin_level(ctx.user_id) < 40:
                self.send(ctx.peer_id, "⛔ Выдать фракцию Админ можно только с админ-уровнем 40+.")
                return
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !фракция Админ (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                c.execute("UPDATE users SET faction=? WHERE vk_id=?", (SECRET_FACTION, target))
            self.send(ctx.peer_id, f"✅ Пользователю {self._fmt_user(target)} выдана фракция {SECRET_FACTION}.")
            return

        if cmd == "!скрыть":
            if self._get_admin_level(ctx.user_id) < 10:
                self.send(ctx.peer_id, "⛔ Команда доступна только админам 10+.")
                return
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !скрыть (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                c.execute("UPDATE users SET hidden=1 WHERE vk_id=?", (target,))
            self.send(ctx.peer_id, f"✅ Данные пользователя {self._fmt_user(target)} скрыты.")
            return

        if cmd == "!раскрыть":
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !раскрыть (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                c.execute("UPDATE users SET hidden=0 WHERE vk_id=?", (target,))
            self.send(ctx.peer_id, f"✅ Данные пользователя {self._fmt_user(target)} раскрыты.")
            return

        if cmd in {"!уволить", "!снять"} and (
            # !снять как синоним уволить — только если следующее слово не "роль/выговор/префикс/рольвезде"
            cmd == "!уволить" or (
                len(parts) < 2 or parts[1].lower() not in {"роль", "выговор", "префикс", "рольвезде"}
            )
        ):
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для команды нужны права: роль 70+ или админ 10+.")
                return
            target = (self._parse_user_fast(parts[1]) or self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "❌ Укажите пользователя.\nФормат: !уволить (пользователь) или ответом на сообщение")
                return
            actor = self._user(ctx.user_id)
            victim = self._user(target)
            if not victim:
                self.send(ctx.peer_id, "❌ Пользователь не найден в системе.")
                return
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            victim_faction = victim["faction"] or "не указана"
            victim_server = int(victim["server_id"] or 1)
            if actor_faction != SECRET_FACTION and actor_faction != victim_faction:
                self.send(ctx.peer_id, "⛔ Можно увольнять только из своей фракции.")
                return
            if actor_faction != SECRET_FACTION and actor_server != victim_server:
                self.send(ctx.peer_id, "⛔ Можно увольнять только пользователей своего сервера.")
                return
            # Проверка уровня: нельзя уволить человека с уровнем должности выше своего
            if not self._is_senior_admin_ctx(ctx) and actor_faction != SECRET_FACTION:
                actor_pos_name = str(actor["position"] if actor and actor["position"] is not None else "")
                actor_dj = self._dj_find_position(actor_faction, actor_server, actor_pos_name) if actor_pos_name else None
                actor_level = actor_dj["level"] if actor_dj else 0
                victim_pos_name = str(victim["position"] if victim["position"] is not None else "")
                if victim_pos_name and victim_pos_name.lower() != "не указана":
                    victim_dj = self._dj_find_position(victim_faction, victim_server, victim_pos_name)
                    victim_level = victim_dj["level"] if victim_dj else 0
                    if victim_level > actor_level:
                        self.send(ctx.peer_id, f"⛔ Вы не можете уволить пользователя с должностью уровня {victim_level} (ваш уровень: {actor_level}).")
                        return
            with self.db.conn() as c:
                c.execute("UPDATE users SET faction='не указана', position='не указана' WHERE vk_id=?", (target,))
                # Также удаляем pending hire offerы для этого пользователя
                c.execute("DELETE FROM hire_offers WHERE target_user_id=?", (target,))
            try:
                self._add_history(
                    nickname=victim["nickname"] or "не указан",
                    target_vk_id=target,
                    old_faction=victim_faction,
                    old_position=victim["position"],
                    new_faction="не указана",
                    new_position="не указана",
                    actor_vk_id=ctx.user_id,
                    event_type="fire",
                )
            except Exception as e:
                logger.error(f"_add_history error in !уволить: {e}")
            actor_name = self.user_name_cache.get(ctx.user_id, (f"id{ctx.user_id}",))[0]
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} уволен из фракции {victim_faction}.")
            try:
                self.send_dm(target, (
                    f"⚠️ Вы уволены из фракции {victim_faction} (сервер {victim_server}).\n"
                    f"Уволил: [id{ctx.user_id}|{actor_name}]"
                ))
            except Exception:
                pass
            return

        if cmd == "!нанять":
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для команды нужны права: роль 70+ или админ 10+.")
                return

            # Парсим: !нанять (цель) (должность_или_уровень) [уровень если должность не в дж]
            target = (self._parse_user_fast(parts[1]) or self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "❌ Не удалось определить пользователя.\nФормат: !нанять (пользователь) (должность или уровень)")
                return

            actor = self._user(ctx.user_id)
            victim = self._user(target)
            if not actor or not victim:
                self.send(ctx.peer_id, "❌ Пользователь не найден в системе.")
                return

            actor_faction = actor["faction"] or ""
            actor_server = int(actor["server_id"] or 1)
            victim_faction = victim["faction"] or ""
            victim_server = int(victim["server_id"] or 1)

            # Проверка уровня: нельзя нанять человека с уровнем должности выше своего
            if not self._is_senior_admin_ctx(ctx):
                actor_pos_name = str(actor["position"] if actor["position"] is not None else "")
                actor_pos = self._dj_find_position(actor_faction, actor_server, actor_pos_name) if actor_pos_name else None
                actor_level = actor_pos["level"] if actor_pos else 0
                victim_pos_name = str(victim["position"] if victim["position"] is not None else "")
                if victim_pos_name and victim_pos_name.lower() != "не указана":
                    victim_pos = self._dj_find_position(victim_faction, victim_server, victim_pos_name)
                    victim_level = victim_pos["level"] if victim_pos else 0
                    if victim_level > actor_level:
                        self.send(ctx.peer_id, f"⛔ Вы не можете нанять пользователя с должностью уровня {victim_level} (ваш уровень: {actor_level}).")
                        return

            # Проверяем opt-out (только если у цели уже есть должность)
            victim_has_position = bool((victim["position"] or "").strip()) and (victim["position"] or "").lower() != "не указана"
            if victim_has_position:
                with self.db.conn() as c:
                    opt = c.execute("SELECT 1 FROM hire_opt_out WHERE vk_id=?", (target,)).fetchone()
                if opt:
                    self.send(ctx.peer_id, f"⛔ Пользователь {self._fmt_user(target)} отключил получение предложений о работе (!неприниматьдж).")
                    return

            # Аргументы после цели — это название должности + опционально уровень
            args_start = 2  # если цель была в parts[1]
            # Если цель взята из reply — части начинаются с parts[1]
            if target == ctx.reply_user_id and ctx.reply_user_id is not None and len(parts) >= 2:
                args_start = 1
            pos_args = parts[args_start:]

            if not pos_args:
                self.send(ctx.peer_id, "❌ Укажите должность или уровень ДЖ.\nФормат: !нанять (пользователь) (должность или уровень)")
                return

            # Определяем должность и уровень
            # Если последний аргумент — число, это уровень
            pos_level: Optional[int] = None
            pos_name_parts = list(pos_args)

            if pos_name_parts and pos_name_parts[-1].lstrip("-").isdigit():
                pos_level = int(pos_name_parts[-1])
                pos_name_parts = pos_name_parts[:-1]

            pos_query = " ".join(pos_name_parts).strip() if pos_name_parts else ""

            # Ищем в ДЖ списке
            dj_pos: Optional[dict] = None
            if pos_query:
                dj_pos = self._dj_find_position(actor_faction, actor_server, pos_query)
            if dj_pos is None and pos_level is not None:
                # Ищем по уровню
                dj_pos = self._dj_find_position(actor_faction, actor_server, str(pos_level))

            if dj_pos:
                new_position = dj_pos["name"]
                new_level = dj_pos["level"]
            elif pos_query:
                # Должности нет в ДЖ — нужен уровень
                if pos_level is None:
                    self.send(ctx.peer_id,
                        f"⚠️ Должность «{pos_query}» не найдена в ДЖ фракции {actor_faction}.\n"
                        f"Добавьте её сначала: !дж новая {pos_query} (уровень)\n"
                        f"Или укажите уровень явно: !нанять (пользователь) {pos_query} (уровень)"
                    )
                    return
                new_position = pos_query
                new_level = pos_level
            else:
                self.send(ctx.peer_id, "❌ Укажите название должности.\nФормат: !нанять (пользователь) (должность)")
                return

            # Сохраняем оффер в БД
            with self.db.conn() as c:
                # Удаляем старые офферы этому пользователю от этого актора
                c.execute("DELETE FROM hire_offers WHERE target_user_id=? AND actor_user_id=?", (target, ctx.user_id))
                cur = c.execute(
                    "INSERT INTO hire_offers(target_user_id,actor_user_id,faction,server_id,position_name,position_level,old_faction,old_server_id,created_at,step)"
                    " VALUES(?,?,?,?,?,?,?,?,?,'user_confirm')",
                    (target, ctx.user_id, actor_faction, actor_server, new_position, new_level,
                     victim_faction, victim_server, self.now_ts()),
                )
                offer_id = cur.lastrowid

            # Отправляем оффер цели
            ok = self._send_hire_offer_buttons(target, offer_id, actor_faction, new_position)
            if ok:
                self.send(ctx.peer_id, f"✅ Предложение о найме отправлено {self._fmt_user(target)} на должность «{new_position}».")
            else:
                # Не смогли отправить в ЛС — нанимаем напрямую (нет ЛС-доступа)
                with self.db.conn() as c:
                    c.execute("UPDATE users SET faction=?, position=?, server_id=? WHERE vk_id=?",
                              (actor_faction, new_position, actor_server, target))
                    c.execute("DELETE FROM hire_offers WHERE id=?", (offer_id,))
                self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} нанят в {actor_faction} на должность «{new_position}» (ЛС недоступны, принято автоматически).")
            return

        if cmd == "!история":
            self.send(ctx.peer_id, "ℹ️ Команда !история удалена.")
            return

        if cmd == "!новый" and len(parts) >= 4 and parts[1].lower() == "префикс":
            if self._get_admin_level(ctx.user_id) < 90:
                self.send(ctx.peer_id, "⛔ Доступно только админам 90+.")
                return

            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !новый префикс (пользователь) (название до 10 слов)")
                return

            prefix_words = parts[3:]
            if not prefix_words:
                self.send(ctx.peer_id, "Формат: !новый префикс (пользователь) (название до 10 слов)")
                return

            if len(prefix_words) > 10:
                self.send(ctx.peer_id, "❌ Префикс слишком длинный. Максимум 10 слов.")
                return

            name = " ".join(prefix_words).strip()
            emoji = ""

            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO user_prefixes(vk_id,name,emoji) VALUES(?,?,?)",
                    (target, name, emoji),
                )

            self.send(ctx.peer_id, f"✅ Префикс «{name}» добавлен пользователю {self._fmt_user(target)}.")
            return

        if cmd == "!снять" and len(parts) >= 2 and parts[1].lower() == "выговор":
            if self._get_admin_level(ctx.user_id) < 5:
                self.send(ctx.peer_id, "⛔ Команда доступна с 5 уровня админ-прав.")
                return
            target = (self._parse_user(parts[2]) if len(parts) > 2 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !снять выговор (пользователь)")
                return
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            victim = self._user(target)
            victim_faction = victim["faction"] if victim else None
            victim_server_id = int(victim["server_id"] or 1) if victim else 1
            if not victim_faction:
                self.send(ctx.peer_id, "❌ У пользователя не указана фракция.")
                return
            if actor_faction != SECRET_FACTION and actor_faction != victim_faction:
                self.send(ctx.peer_id, "⛔ Можно снимать выговор только в своей фракции.")
                return
            faction = victim_faction
            with self.db.conn() as c:
                row = c.execute(
                    "SELECT strikes FROM faction_strikes WHERE faction=? AND vk_id=? AND server_id=?",
                    (faction, target, victim_server_id),
                ).fetchone()
                if not row:
                    self.send(ctx.peer_id, "ℹ️ У пользователя нет выговоров.")
                    return
                strikes = max(0, int(row["strikes"]) - 1)
                if strikes == 0:
                    c.execute("DELETE FROM faction_strikes WHERE faction=? AND vk_id=? AND server_id=?", (faction, target, victim_server_id))
                else:
                    c.execute(
                        "UPDATE faction_strikes SET strikes=? WHERE faction=? AND vk_id=? AND server_id=?",
                        (strikes, faction, target, victim_server_id),
                    )
            self.send(ctx.peer_id, f"✅ Снят один выговор у {self._fmt_user(target)}. Текущий счёт: {strikes}/3.")
            return

        if cmd == "!снять" and len(parts) >= 4 and parts[1].lower() == "префикс":
            if self._get_admin_level(ctx.user_id) < 90:
                self.send(ctx.peer_id, "⛔ Доступно только админам 90+.")
                return
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !снять префикс (пользователь) (название)")
                return
            name = parts[3]
            with self.db.conn() as c:
                c.execute("DELETE FROM user_prefixes WHERE vk_id=? AND name=? COLLATE NOCASE", (target, name))
            self.send(ctx.peer_id, f"✅ Префикс «{name}» удалён у {self._fmt_user(target)}.")
            return

        if cmd == "!закрыть" and len(parts) >= 2 and parts[1].lower() == "бота":
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда доступна только старшему админу.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('bot_closed','1')")
            self.send(ctx.peer_id, "🔒 Бот закрыт. Доступ только для пользователей с админ-правами.")
            return

        if cmd == "!открыть" and len(parts) >= 2 and parts[1].lower() == "бота":
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда доступна только старшему админу.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('bot_closed','0')")
            self.send(ctx.peer_id, "🔓 Бот открыт для всех одобренных пользователей.")
            return

        if cmd == "!право" and ctx.is_chat and len(parts) >= 3 and parts[-1].isdigit() and parts[1].lower() != "админ":
            target_cmd = self._extract_command_token(parts[1:-1])
            if not target_cmd:
                self.send(ctx.peer_id, "Формат: !право (команда) (уровень роли)")
                return
            if target_cmd not in COMMAND_ACCESS:
                self.send(ctx.peer_id, f"❌ Неизвестная команда: {' '.join(parts[1:-1])}")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO chat_command_rights(chat_id,command,min_role) VALUES(?,?,?)",
                    (ctx.chat_id, target_cmd, int(parts[-1])),
                )
            self.send(ctx.peer_id, f"✅ Право для {target_cmd} в этом чате = {parts[-1]}.")
            return

        if cmd == "!право" and len(parts) >= 4 and parts[1].lower() == "админ" and parts[-1].isdigit():
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда !право админ доступна только старшему админу.")
                return
            target_cmd = self._extract_command_token(parts[2:-1])
            if not target_cmd:
                self.send(ctx.peer_id, "Формат: !право админ (команда) (уровень)")
                return
            if target_cmd not in COMMAND_ACCESS:
                self.send(ctx.peer_id, f"❌ Неизвестная команда: {' '.join(parts[2:-1])}")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO command_admin_rights(command,min_admin) VALUES(?,?)",
                    (target_cmd, int(parts[-1])),
                )
            self.send(ctx.peer_id, f"✅ Админ-порог для {target_cmd} = {parts[-1]}.")
            return

        if cmd == "!админправо" and len(parts) >= 3 and parts[-1].isdigit():
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда !админправо доступна только старшему админу.")
                return
            target_cmd = self._extract_command_token(parts[1:-1])
            if not target_cmd:
                self.send(ctx.peer_id, "Формат: !админправо (команда) (уровень админ прав)")
                return
            if target_cmd not in COMMAND_ACCESS:
                self.send(ctx.peer_id, f"❌ Неизвестная команда: {' '.join(parts[1:-1])}")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO command_admin_rights(command,min_admin) VALUES(?,?)",
                    (target_cmd, int(parts[-1])),
                )
            self.send(ctx.peer_id, f"✅ Админ-порог для {target_cmd} = {parts[-1]}.")
            return

        if cmd == "!разрешить":
            # !разрешить (пользователь) (команда)
            actor_admin = self._get_admin_level(ctx.user_id)
            if actor_admin < 30 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Для !разрешить нужен admin 30+.")
                return
            if len(parts) < 3:
                self.send(ctx.peer_id, "Формат: !разрешить (пользователь) (команда)")
                return
            target = (self._parse_user_fast(parts[1]) or self._parse_user(parts[1])) if len(parts) > 1 else None
            target = target or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "❌ Не удалось определить пользователя.")
                return
            # Команда — всё что после пользователя
            cmd_target = " ".join(parts[2:]).strip().lower()
            if not cmd_target.startswith("!"):
                cmd_target = "!" + cmd_target
            cmd_norm = self._normalize_command_name(cmd_target)
            if cmd_norm not in COMMAND_ACCESS:
                self.send(ctx.peer_id, f"❌ Неизвестная команда: {cmd_target}")
                return
            target_admin_min = self._get_effective_admin_min(cmd_norm)
            if target_admin_min > actor_admin and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, f"⛔ Нельзя выдать {cmd_norm}: для неё нужен admin {target_admin_min}+.")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO user_cmd_allow(target_vk_id,command,granted_by,granted_at) VALUES(?,?,?,?)",
                    (target, cmd_norm, ctx.user_id, self.now_ts()),
                )
                # Убираем возможный запрет на эту команду
                c.execute("DELETE FROM user_cmd_deny WHERE target_vk_id=? AND LOWER(command)=LOWER(?)", (target, cmd_norm))
            self.send(ctx.peer_id, f"✅ Пользователю {self._fmt_user(target)} разрешена команда {cmd_norm}.")
            try:
                granter_name = self.user_name_cache.get(ctx.user_id, (f"id{ctx.user_id}",))[0]
                self.send_dm(target, f"ℹ️ Вам разрешена команда {cmd_norm} (выдал: {granter_name}).")
            except Exception:
                pass
            return

        if cmd == "!запретить":
            # !запретить (пользователь) (команда)
            actor_admin = self._get_admin_level(ctx.user_id)
            if actor_admin < 30 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Для !запретить нужен admin 30+.")
                return
            if len(parts) < 3:
                self.send(ctx.peer_id, "Формат: !запретить (пользователь) (команда)")
                return
            target = (self._parse_user_fast(parts[1]) or self._parse_user(parts[1])) if len(parts) > 1 else None
            target = target or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "❌ Не удалось определить пользователя.")
                return
            if int(target) == int(self.db.senior_admin_id):
                self.send(ctx.peer_id, "⛔ Нельзя запретить команду старшему админу.")
                return
            cmd_target = " ".join(parts[2:]).strip().lower()
            if not cmd_target.startswith("!"):
                cmd_target = "!" + cmd_target
            cmd_norm = self._normalize_command_name(cmd_target)
            if cmd_norm not in COMMAND_ACCESS:
                self.send(ctx.peer_id, f"❌ Неизвестная команда: {cmd_target}")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO user_cmd_deny(target_vk_id,command,denied_by,denied_at) VALUES(?,?,?,?)",
                    (target, cmd_norm, ctx.user_id, self.now_ts()),
                )
                c.execute("DELETE FROM user_cmd_allow WHERE target_vk_id=? AND LOWER(command)=LOWER(?)", (target, cmd_norm))
            self.send(ctx.peer_id, f"✅ Пользователю {self._fmt_user(target)} запрещена команда {cmd_norm}.")
            return

        if cmd == "!проверка команд":
            # !проверка команд (пользователь)
            actor_admin = self._get_admin_level(ctx.user_id)
            if actor_admin < 30 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Для !проверка команд нужен admin 30+.")
                return
            target = (self._parse_user_fast(parts[1]) or self._parse_user(parts[1])) if len(parts) > 1 else None
            target = target or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "Формат: !проверка команд (пользователь)")
                return
            target_admin = self._get_admin_level(target)
            target_role = self._get_role_level(ctx.chat_id, target) if ctx.is_chat else 0
            chat_id_ctx = ctx.chat_id if ctx.is_chat else None
            # Загружаем персональные разрешения/запреты
            with self.db.conn() as c:
                allow_rows = c.execute(
                    "SELECT command, granted_by FROM user_cmd_allow WHERE target_vk_id=?", (target,)
                ).fetchall()
                deny_rows = c.execute(
                    "SELECT command FROM user_cmd_deny WHERE target_vk_id=?", (target,)
                ).fetchall()
            allow_map = {r["command"]: r["granted_by"] for r in allow_rows}
            deny_set = {r["command"] for r in deny_rows}
            available = []
            for ccmd in sorted(COMMAND_ACCESS.keys()):
                cmd_norm2 = ccmd
                if cmd_norm2 in deny_set:
                    continue
                if cmd_norm2 in allow_map:
                    granter_cached = self.user_name_cache.get(allow_map[cmd_norm2])
                    granter_name = granter_cached[0] if granter_cached else f"id{allow_map[cmd_norm2]}"
                    available.append(f"• {ccmd} (разрешено: {granter_name})")
                    continue
                eff_admin = self._get_effective_admin_min(cmd_norm2)
                need_role = self._required_role(cmd_norm2, chat_id_ctx)
                if eff_admin > 0:
                    if target_admin >= eff_admin:
                        available.append(f"• {ccmd}  [адм.{eff_admin}+]")
                elif need_role > 0:
                    if target_role >= need_role:
                        available.append(f"• {ccmd}  [роль {need_role}+]")
                else:
                    usage = COMMAND_USAGE.get(ccmd, ccmd)
                    available.append(f"• {usage}")
            header = f"🔍 Команды для {self._fmt_user(target)} (адм.{target_admin}, роль {target_role}):"
            self.send(ctx.peer_id, header + "\n" + "\n".join(available) if available else header + "\nнет доступных команд")
            return

        if cmd == "!узнать":
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !узнать (пользователь) или ответом на сообщение")
                return
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_is_leader = self._is_leader_user(ctx.user_id)

            # Проверка для лидеров: могут смотреть только свою фракцию
            if actor_is_leader and actor_admin < 30 and not self._is_senior_admin_ctx(ctx):
                actor_u = self._user(ctx.user_id)
                target_u = self._user(int(target))
                actor_faction = actor_u["faction"] if actor_u else None
                target_faction = target_u["faction"] if target_u else None
                if not actor_faction or actor_faction != target_faction:
                    target_name = self._fmt_user(int(target))
                    self.send(ctx.peer_id, f"👤 {target_name}\n⛔ Этот пользователь не из вашей фракции. Доступ ограничен.")
                    return

            with self.db.conn() as c:
                tu = c.execute("SELECT hidden FROM users WHERE vk_id=?", (int(target),)).fetchone()
            if tu and int(tu["hidden"] or 0) == 1 and actor_admin < 30:
                self.send(ctx.peer_id, f"👤 Профиль {self._fmt_user(int(target))}\n🔒 Данные скрыты.")
                return
            self.send(ctx.peer_id, self._admin_console_text_profile(int(target)))
            return

        if cmd == "!допинфа":
            target: Optional[int] = None
            info_text = ""
            if ctx.reply_user_id is not None:
                target = int(ctx.reply_user_id)
                info_text = " ".join(parts[1:]).strip()
                if not info_text:
                    info_text = (ctx.reply_text or "").strip()
            else:
                if len(parts) < 3:
                    self.send(ctx.peer_id, "Формат: !допинфа (пользователь) (текст) или ответом на сообщение")
                    return
                target = self._parse_user(parts[1])
                info_text = " ".join(parts[2:]).strip()
            if target is None:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return
            if not info_text:
                self.send(ctx.peer_id, "❌ Укажите текст дополнительной информации.")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT INTO user_extra_info(target_id,author_id,info_text,created_at) VALUES(?,?,?,?)",
                    (int(target), int(ctx.user_id), info_text, self.now_ts()),
                )
            self.send(ctx.peer_id, "✅ Информация сохранена.\n" + self._format_user_extra_info(int(target)))
            return

        if cmd == "!инфа":
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !инфа (пользователь) или ответом на сообщение")
                return
            self.send(ctx.peer_id, self._format_user_extra_info(int(target)))
            return

        if cmd == "!удалитьинфу" and len(parts) >= 4 and parts[1].lower() == "инфу" and parts[-1].isdigit():
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !удалить инфу (пользователь) (номер)")
                return
            num = int(parts[-1])
            with self.db.conn() as c:
                rows = c.execute("SELECT id FROM user_extra_info WHERE target_id=? ORDER BY id ASC", (int(target),)).fetchall()
                if num < 1 or num > len(rows):
                    self.send(ctx.peer_id, "❌ Неверный номер записи.")
                    return
                row_id = int(rows[num - 1]["id"])
                c.execute("DELETE FROM user_extra_info WHERE id=?", (row_id,))
            self.send(ctx.peer_id, "✅ Запись удалена.\n" + self._format_user_extra_info(int(target)))
            return

        if cmd == "!одобритьдубль" and len(parts) >= 2 and parts[1].isdigit():
            self.send(ctx.peer_id, self._approve_sync_request(ctx.user_id, int(parts[1])))
            return

        if cmd == "!дубль":
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !дубль (ник)")
                return
            nickname = " ".join(parts[1:]).strip()
            with self.db.conn() as c:
                target = c.execute(
                    "SELECT vk_id, nickname FROM users WHERE LOWER(COALESCE(nickname,''))=LOWER(?) AND approved=1 LIMIT 1",
                    (nickname,),
                ).fetchone()
            if not target:
                self.send(ctx.peer_id, "❌ Пользователь с таким ником не найден или не одобрен.")
                return
            if int(target["vk_id"]) == int(ctx.user_id):
                self.send(ctx.peer_id, "ℹ️ Это уже ваш аккаунт — синхронизация не требуется.")
                return
            source_label = (
                f"Telegram ([профиль](tg://user?id={ctx.user_id}))"
                if ctx.platform == "tg"
                else f"VK {self._fmt_user(ctx.user_id)}"
            )
            now = self.now_ts()
            expires = now + 24 * 3600
            with self.db.conn() as c:
                cur = c.execute(
                    """
                    INSERT INTO account_sync_requests(source_id,source_platform,target_id,nickname,created_at,expires_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (ctx.user_id, ctx.platform, int(target["vk_id"]), nickname, now, expires),
                )
                req_id = int(cur.lastrowid)
            text = (
                "⚠️ ВНИМАНИЕ: ваш аккаунт пытаются синхронизировать.\n"
                f"Источник: {source_label}\n"
                f"Ник: {nickname}\n\n"
                "Если это вы — нажмите «Одобрить». Если нет — проигнорируйте сообщение."
            )
            delivered = self.send_dm_vk_with_sync_approve(int(target["vk_id"]), text, req_id)
            if not delivered:
                self.send_user_notice(
                    int(target["vk_id"]),
                    f"{text}\n\nЕсли это вы, отправьте команду: !одобритьдубль {req_id}",
                )
            self.send(ctx.peer_id, f"✅ Запрос синхронизации отправлен. Номер: {req_id}. Таймаут: 24 часа.")
            return

        if cmd == "!новая подписка":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Подписки на сообщества настраиваются только в чате.")
                return
            if not WALL_READ_TOKEN:
                self.send(
                    ctx.peer_id,
                    "⛔ Подписки не смогут читать новые посты без токена чтения стены.\n"
                    "Добавьте в .env VK_WALL_TOKEN (или VK_USER_TOKEN/VK_SERVICE_TOKEN) и перезапустите бота.",
                )
                return
            if len(parts) < 3:
                self.send(ctx.peer_id, "Формат: !новая подписка (ссылка на сообщество)")
                return
            raw_link = parts[2]
            community = self._resolve_community(raw_link)
            if not community:
                self.send(ctx.peer_id, "❌ Не удалось распознать сообщество. Укажите ссылку вида https://vk.com/community")
                return
            last_post_id = self._latest_wall_post_id(int(community["owner_id"]))
            ok = self.subscriptions_db.add_subscription(
                chat_id=int(ctx.chat_id),
                owner_id=int(community["owner_id"]),
                domain=str(community["domain"]),
                title=str(community["title"]),
                url=str(community["url"]),
                created_by=int(ctx.user_id),
                created_at=self.now_ts(),
                last_post_id=last_post_id,
            )
            if not ok:
                self.send(ctx.peer_id, f"ℹ️ В этом чате уже есть подписка на {community['title']}.")
                return
            self.send(ctx.peer_id, f"✅ Подписка добавлена: {community['title']}\nНовые посты будут приходить в этот чат.")
            return

        if cmd == "!подписка":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Команда работает только в чате.")
                return
            if len(parts) == 3 and parts[1].lower() == "пинг" and parts[2].lower() in {"вкл", "выкл"}:
                enabled = parts[2].lower() == "вкл"
                changed = self.subscriptions_db.set_chat_ping_all(int(ctx.chat_id), enabled)
                if changed <= 0:
                    self.send(ctx.peer_id, "📭 В этом чате нет подписок на сообщества.")
                    return
                self.send(ctx.peer_id, f"✅ Пинг @all для новых постов {'включен' if enabled else 'выключен'}.")
                return
            self.send(ctx.peer_id, "Формат: !подписка пинг вкл / !подписка пинг выкл")
            return

        if cmd == "!подписки":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Команда работает только в чате.")
                return
            rows = self.subscriptions_db.list_chat(int(ctx.chat_id))
            if not rows:
                self.send(ctx.peer_id, "📭 В этом чате нет подписок на сообщества.")
                return
            lines = ["📬 Подписки этого чата:"]
            for idx, row in enumerate(rows, start=1):
                ping_state = "пинг @all: вкл" if int(row.get("ping_all") or 0) == 1 else "пинг @all: выкл"
                lines.append(f"{idx}. {row['title']} — {row['url']} ({ping_state})")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd == "!отписаться":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Команда работает только в чате.")
                return
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !отписаться (ссылка на сообщество)")
                return
            community = self._resolve_community(parts[1])
            if not community:
                self.send(ctx.peer_id, "❌ Не удалось распознать сообщество.")
                return
            ok = self.subscriptions_db.remove_subscription(int(ctx.chat_id), int(community["owner_id"]))
            self.send(ctx.peer_id, f"✅ Подписка снята: {community['title']}" if ok else "ℹ️ В этом чате такой подписки не было.")
            return

        if cmd == "!жалоба":
            if ctx.reply_user_id is None:
                self.send(
                    ctx.peer_id,
                    "❌ Жалоба должна быть ответом на сообщение нарушителя.\n"
                    "Использование: ответьте на сообщение и напишите !жалоба (доп. информация).",
                )
                return
            extra_info = " ".join(parts[1:]).strip() or "не указана"
            reply_text = (ctx.reply_text or "").strip()
            allowed_attachment_prefixes = ("photo", "video", "doc", "audio", "audio_message")
            reply_attachments = [
                aid for aid in dict.fromkeys(ctx.reply_attachment_ids or [])
                if str(aid).startswith(allowed_attachment_prefixes)
            ][:5]
            reporter = self._user(ctx.user_id)
            target = self._user(int(ctx.reply_user_id))
            reporter_name = (reporter["nickname"] if reporter and reporter["nickname"] else self._fmt_user(ctx.user_id))
            target_name = (target["nickname"] if target and target["nickname"] else "")
            actor_server = self._extract_complaint_server(reply_text, int(reporter["server_id"] or 1) if reporter else 1)
            nick_links = self._complaint_nickname_links(reply_text, int(ctx.user_id))
            if int(ctx.reply_user_id) == int(ctx.user_id):
                reporter_nick = str(reporter["nickname"] or "").lower() if reporter and reporter["nickname"] else ""
                raw_nicks = [
                    n for n in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{3,31}\b", reply_text)
                    if n.lower() not in {"rw", "vk", "http", "https", "com"}
                    and (not reporter_nick or n.lower() != reporter_nick)
                ]
                complaint_candidates = [
                    line for line in nick_links
                    if not reporter_nick or not line.lower().startswith(reporter_nick + " =")
                ]
                if not complaint_candidates and not raw_nicks:
                    self.send(
                        ctx.peer_id,
                        "❌ В тексте жалобы не найден ник нарушителя. Укажите игровое имя нарушителя в сообщении жалобы.",
                    )
                    return
            with self.db.conn() as c:
                cur = c.execute(
                    """
                    INSERT INTO complaints(chat_id,reporter_id,target_id,message_text,attachments,status,created_at)
                    VALUES(?,?,?,?,?,'open',?)
                    """,
                    (
                        int(ctx.chat_id or 0),
                        int(ctx.user_id),
                        int(ctx.reply_user_id),
                        reply_text,
                        ",".join(reply_attachments),
                        self.now_ts(),
                    ),
                )
                complaint_id = int(cur.lastrowid)
            complaint_chat_id = int(self._get_setting("complaint_chat_id", str(ctx.chat_id or 0)) or 0)
            if complaint_chat_id > 0:
                with self.db.conn() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO silence_exceptions(chat_id,vk_id,until_ts) VALUES(?,?,?)",
                        (complaint_chat_id, int(ctx.user_id), self.now_ts() + 600),
                    )
            if ctx.platform == "vk":
                mention_ids = self._complaint_admin_mentions(complaint_chat_id, actor_server)
                mention_map = self._fmt_users_bulk(mention_ids)
                mentions = " ".join(mention_map[uid] for uid in mention_ids if uid in mention_map)
            else:
                mentions = ""
            notify_lines = [
                f"⚠️ Поступила новая жалоба #{complaint_id}.",
                f"1. Заявитель: {reporter_name}",
            ]
            if target and int(ctx.reply_user_id) != int(ctx.user_id) and target_name and target_name != reporter_name:
                notify_lines.append(f"2. На кого жалоба: {target_name}")
            notify_lines.append(f"3. Сервер: {actor_server}")
            notify_lines.append(f"4. Доп. информация: {extra_info}")
            if reply_text:
                notify_lines.append("↩️ Текст сообщения:")
                notify_lines.append(reply_text)
            if "#жалоба" not in reply_text.lower():
                notify_lines.append("#жалоба")
            if nick_links:
                notify_lines.append("🔎 Найденные никнеймы:")
                notify_lines.extend(nick_links)
            notify = "\n".join(notify_lines)
            if mentions:
                notify += "\n" + mentions
            if complaint_chat_id > 0:
                if ctx.platform == "vk":
                    self.send_with_forwarded_message(
                        self._chat_peer_id(complaint_chat_id),
                        notify,
                        [int(ctx.reply_message_id)] if ctx.reply_message_id else [],
                        reply_attachments,
                    )
                else:
                    self.send_chat(complaint_chat_id, notify)
            else:
                if ctx.platform == "vk":
                    self.send_with_forwarded_message(
                        ctx.peer_id,
                        notify,
                        [int(ctx.reply_message_id)] if ctx.reply_message_id else [],
                        reply_attachments,
                    )
                else:
                    self.send(ctx.peer_id, notify)
            self.send(ctx.peer_id, "✅ Жалоба отправлена в чат жалоб.")
            return

        if cmd == "!жалобы":
            if not self._is_admin_faction_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Команда доступна только фракции Админ.")
                return
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT id,reporter_id,review_by,review_at,created_at FROM complaints WHERE status='open' ORDER BY id ASC"
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "📭 Открытых жалоб нет.")
                return
            user_ids = []
            for r in rows:
                user_ids.append(int(r["reporter_id"]))
                if r["review_by"]:
                    user_ids.append(int(r["review_by"]))
            name_map = self._fmt_users_bulk(user_ids)
            out = ["📋 Открытые жалобы:"]
            for r in rows:
                reporter_id = int(r["reporter_id"])
                dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d.%m.%Y %H:%M")
                line = (
                    f"• #{int(r['id'])} | репортёр: {name_map.get(reporter_id, self._fmt_user_ref(reporter_id))}"
                )
                if r["review_by"]:
                    reviewer_id = int(r["review_by"])
                    line += f" | на рассмотрении у {name_map.get(reviewer_id, self._fmt_user_ref(reviewer_id))}"
                line += f" | {dt}"
                out.append(line)
            self._send_long_text(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!рассмотрение":
            if not self._is_admin_faction_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Команда доступна только фракции Админ.")
                return
            complaint_id = None
            if len(parts) >= 2 and parts[1].isdigit():
                complaint_id = int(parts[1])
            elif ctx.reply_text:
                complaint_id = self._complaint_id_from_notification_reply(ctx)
            if complaint_id is None:
                self.send(ctx.peer_id, "Формат: !рассмотрение (номер жалобы) или ответом на сообщение жалобы.")
                return
            now = self.now_ts()
            with self.db.conn() as c:
                row = c.execute(
                    "SELECT id,status,review_by FROM complaints WHERE id=?",
                    (complaint_id,),
                ).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Жалоба не найдена.")
                    return
                if row["status"] != "open":
                    self.send(ctx.peer_id, "❌ Жалоба уже закрыта.")
                    return
                if row["review_by"] and int(row["review_by"]) != int(ctx.user_id):
                    self.send(ctx.peer_id, f"ℹ️ Жалоба #{complaint_id} уже на рассмотрении у {self._fmt_user(int(row['review_by']))}.")
                    return
                c.execute(
                    "UPDATE complaints SET review_by=?, review_at=? WHERE id=?",
                    (int(ctx.user_id), now, complaint_id),
                )
            self.send(ctx.peer_id, f"✅ Жалоба #{complaint_id} взята на рассмотрение: {self._fmt_user(ctx.user_id)}.")
            return

        if cmd in {"!принять", "!отклонить"}:
            if not self._is_admin_faction_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Команда доступна только фракции Админ.")
                return
            complaint_id = None
            if len(parts) >= 3 and parts[1].lower() == "жалобу" and parts[2].isdigit():
                complaint_id = int(parts[2])
            elif len(parts) >= 2 and parts[1].isdigit():
                complaint_id = int(parts[1])
            elif ctx.reply_text:
                complaint_id = self._complaint_id_from_notification_reply(ctx)
            if complaint_id is None:
                self.send(ctx.peer_id, f"Формат: {cmd} (номер жалобы) или ответом на сообщение жалобы.")
                return
            is_accept = cmd == "!принять"
            with self.db.conn() as c:
                row = c.execute("SELECT id,target_id,status,review_by FROM complaints WHERE id=?", (complaint_id,)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Жалоба не найдена.")
                    return
                if row["status"] != "open":
                    self.send(ctx.peer_id, "❌ Жалоба уже закрыта.")
                    return
                if row["review_by"] and int(row["review_by"]) != int(ctx.user_id):
                    self.send(ctx.peer_id, f"⛔ Жалоба #{complaint_id} уже на рассмотрении у {self._fmt_user(int(row['review_by']))}.")
                    return
                target_id = int(row["target_id"])
                c.execute(
                    "UPDATE complaints SET status=?, closed_by=?, closed_at=? WHERE id=?",
                    ("accepted" if is_accept else "rejected", ctx.user_id, self.now_ts(), complaint_id),
                )
                c.execute(
                    "INSERT OR IGNORE INTO admin_complaint_stats(vk_id,accepted_count,rejected_count) VALUES(?,0,0)",
                    (ctx.user_id,),
                )
                if is_accept:
                    c.execute("UPDATE admin_complaint_stats SET accepted_count=accepted_count+1 WHERE vk_id=?", (ctx.user_id,))
                else:
                    c.execute("UPDATE admin_complaint_stats SET rejected_count=rejected_count+1 WHERE vk_id=?", (ctx.user_id,))
                c.execute("INSERT OR IGNORE INTO users(vk_id,rating) VALUES(?,100)", (target_id,))
                rating_row = c.execute("SELECT rating FROM users WHERE vk_id=?", (target_id,)).fetchone()
                current = int(rating_row["rating"] if rating_row and rating_row["rating"] is not None else 100)
                new_rating = min(100, current + 1) if is_accept else max(0, current - 1)
                c.execute("UPDATE users SET rating=? WHERE vk_id=?", (new_rating, target_id))
            self.send(ctx.peer_id, f"✅ Жалоба #{complaint_id} {'принята' if is_accept else 'отклонена'}. Рейтинг: {new_rating}/100.")
            return

        if cmd in {"!оффадминувед", "!админувед"} and len(parts) >= 2:
            if not self._is_admin_faction_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Команда доступна только фракции Админ.")
                return
            target = self._parse_user(parts[1]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, f"Формат: {cmd} (пользователь)")
                return
            enabled = 1 if cmd == "!админувед" else 0
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO admin_notify_settings(vk_id,enabled) VALUES(?,?)", (target, enabled))
            self.send(ctx.peer_id, f"✅ Уведомления по жалобам для {self._fmt_user(target)} {'включены' if enabled else 'выключены'}.")
            return

        if cmd == "!банфракция":
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            if not actor_faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return
            ts = self.now_ts()
            if actor_faction == SECRET_FACTION:
                if len(parts) < 4:
                    self.send(ctx.peer_id, "Формат: !банфракция (фракция) (пользователь) (причина)")
                    return
                faction, rest = self._extract_faction_and_rest(parts, 1)
                if not faction or len(rest) < 2:
                    self.send(ctx.peer_id, "Формат: !банфракция (фракция) (пользователь) (причина)")
                    return
                target = self._parse_user(rest[0]) or ctx.reply_user_id
                reason = " ".join(rest[1:]).strip() or "без причины"
            else:
                faction = actor_faction
                target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
                reason = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
                if target is None or not reason:
                    self.send(ctx.peer_id, "Формат: !банфракция (пользователь) (причина)")
                    return
            if target is None:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT INTO faction_blacklist(faction,vk_id,reason,issuer_id,created_at) VALUES(?,?,?,?,?)",
                    (faction, target, reason, ctx.user_id, ts),
                )
                chats = c.execute("SELECT chat_id FROM chats WHERE faction=?", (faction,)).fetchall()
                for row in chats:
                    chat_id = int(row["chat_id"])
                    c.execute(
                        "INSERT OR IGNORE INTO chat_members(chat_id,vk_id,role_level,immunity_level,banned) VALUES(?,?,0,0,0)",
                        (chat_id, target),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)",
                        (chat_id, 0, DEFAULT_ROLE_NAME),
                    )
                    c.execute("UPDATE chat_members SET banned=1, role_level=0, immunity_level=0 WHERE chat_id=? AND vk_id=?", (chat_id, target))
                    c.execute(
                        "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                        (chat_id, target, ctx.user_id, f"Фракбан {faction}: {reason}", ts),
                    )
                    try:
                        self.api.messages.removeChatUser(chat_id=chat_id, member_id=target)
                    except Exception:
                        pass
                c.execute(
                    "UPDATE users SET faction='не указана', position='не указана' WHERE vk_id=?",
                    (target,),
                )
            self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} забанен в чатах фракции {faction}.")
            return

        if cmd == "!выговор":
            required_admin = self._get_effective_admin_min("!выговор")
            if self._get_admin_level(ctx.user_id) < required_admin:
                self.send(ctx.peer_id, f"⛔ Команда доступна с {required_admin} уровня админ-прав.")
                return
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            if not actor_faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return
            target = None
            reason = ""
            forced_faction = None
            if actor_faction == SECRET_FACTION:
                ff, rest = self._extract_faction_and_rest(parts, 1)
                if not ff:
                    self.send(ctx.peer_id, "Формат для Админ: !выговор (фракция) (пользователь) (причина)")
                    return
                forced_faction = ff
                if rest:
                    target = self._parse_user(rest[0]) or ctx.reply_user_id
                    reason = " ".join(rest[1:]).strip() if len(rest) > 1 else ""
                else:
                    target = ctx.reply_user_id
            else:
                target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
                reason = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
            if target is None or not reason:
                self.send(ctx.peer_id, "Формат: !выговор (пользователь) (причина)")
                return
            if int(target) == int(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Нельзя выдать выговор самому себе.")
                return
            victim = self._user(target)
            if not victim:
                self.send(ctx.peer_id, "❌ Пользователь не найден в системе.")
                return
            victim_faction = victim["faction"] or "не указана"
            victim_server_id = int(victim["server_id"] or 1)
            if actor_faction != SECRET_FACTION and victim_faction != actor_faction:
                self.send(ctx.peer_id, "⛔ Можно выдавать выговор только пользователю своей фракции.")
                return
            faction = forced_faction if actor_faction == SECRET_FACTION else actor_faction
            with self.db.conn() as c:
                c.execute(
                    "INSERT INTO faction_strikes(faction,vk_id,server_id,strikes) VALUES(?,?,?,1) "
                    "ON CONFLICT(faction,vk_id) DO UPDATE SET strikes=strikes+1, server_id=excluded.server_id",
                    (faction, target, victim_server_id),
                )
                strikes = int(
                    c.execute(
                        "SELECT strikes FROM faction_strikes WHERE faction=? AND vk_id=? AND server_id=?",
                        (faction, target, victim_server_id),
                    ).fetchone()["strikes"]
                )
                if strikes >= 3:
                    c.execute("DELETE FROM faction_strikes WHERE faction=? AND vk_id=? AND server_id=?", (faction, target, victim_server_id))
                    self.send(
                        ctx.peer_id,
                        f"⛔ {self._fmt_user(target)} получил 3/3 выговоров.\n"
                        f"Рекомендуется уволить сотрудника: !уволить id{target}\n"
                        "Счетчик выговоров обнулён.",
                    )
                    return
            self.send(
                ctx.peer_id,
                f"✅ Пользователю {self._fmt_user(target)} выдан выговор (варн). Причина: {reason}. Текущий счёт: {strikes}/3.",
            )
            return

        if cmd in {"!список выговоров", "!выговоры"}:
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            if actor_faction == SECRET_FACTION:
                start_idx = 2 if cmd == "!список выговоров" else 1
                ff, rest = self._extract_faction_and_rest(parts, start_idx)
                if not ff or not rest or not rest[0].isdigit():
                    self.send(ctx.peer_id, "Формат для Админ: !список выговоров (фракция) (сервер) или !выговоры (фракция) (сервер)")
                    return
                faction = ff
                server_id = int(rest[0])
                if server_id < 1 or server_id > 3:
                    self.send(ctx.peer_id, "❌ Сервер должен быть 1, 2 или 3.")
                    return
            else:
                faction = actor_faction
                server_id = int(actor["server_id"] or 1) if actor else 1
            if not faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT vk_id,strikes FROM faction_strikes WHERE faction=? AND server_id=? ORDER BY strikes DESC, vk_id ASC",
                    (faction, server_id),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, f"📋 Выговоры фракции {faction} (сервер {server_id}): пусто")
                return
            out = [f"📋 Выговоры фракции {faction} (сервер {server_id}):"]
            for r in rows:
                out.append(f"• {self._fmt_user(int(r['vk_id']))} — {int(r['strikes'])}/3")
            self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!поиск":
            if self._get_admin_level(ctx.user_id) < 10:
                self.send(ctx.peer_id, "⛔ Команда доступна только админам 10+.")
                return
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !поиск (никнейм)")
                return
            nick = " ".join(parts[1:]).strip()
            with self.db.conn() as c:
                row = c.execute(
                    "SELECT vk_id,nickname,faction,position FROM users WHERE nickname=? COLLATE NOCASE",
                    (nick,),
                ).fetchone()
            if not row:
                self.send(ctx.peer_id, "❌ Человек с таким NickName не найден.")
                return
            self.send(
                ctx.peer_id,
                f"🔎 Поиск\nНикнейм: {row['nickname']}\nПользователь: {self._fmt_user(int(row['vk_id']))}\nФракция: {row['faction'] or 'не указана'}\nДолжность: {row['position'] or 'не указана'}",
            )
            return

        if cmd == "!одобрение":
            if self._get_admin_level(ctx.user_id) < 10:
                self.send(ctx.peer_id, "⛔ Команда доступна только админам 10+.")
                return
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !одобрение (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                c.execute("UPDATE users SET approved=1, approved_by=?, approved_at=? WHERE vk_id=?", (ctx.user_id, self.now_ts(), target))
                c.execute(
                    "INSERT OR REPLACE INTO preapproved_profiles(vk_id,approved,nickname,rp_name,position,faction,updated_at) VALUES(?,1,(SELECT nickname FROM users WHERE vk_id=?),(SELECT rp_name FROM users WHERE vk_id=?),(SELECT position FROM users WHERE vk_id=?),(SELECT faction FROM users WHERE vk_id=?),?)",
                    (target, target, target, target, target, self.now_ts()),
                )
            self.send(ctx.peer_id, f"✅ Профиль {self._fmt_user(target)} одобрен.")
            return

        if cmd in {"!пуш-", "!пуш+"} and ctx.is_chat:
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id)
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для управления пушами нужна роль 70+ или admin 10+.")
                return
            with self.db.conn() as c:
                if cmd == "!пуш-":
                    c.execute("INSERT OR IGNORE INTO chat_push_disabled(chat_id) VALUES(?)", (ctx.chat_id,))
                    self.send(ctx.peer_id, "🔕 Пуши отключены в этом чате. Команда !пуш сюда больше не присылает уведомлений.")
                else:
                    c.execute("DELETE FROM chat_push_disabled WHERE chat_id=?", (ctx.chat_id,))
                    self.send(ctx.peer_id, "🔔 Пуши включены в этом чате. Уведомления снова будут приходить.")
            return

        if cmd == "!пуш" and ctx.is_chat:
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id)
            if actor_role < 90 and actor_admin <= 10:
                self.send(ctx.peer_id, "⛔ Для !пуш нужен уровень роли 90+ или админ-уровень > 10.")
                return
            user = self._user(ctx.user_id)
            faction = user["faction"] if user else None
            actor_server = int(user["server_id"] or 1) if user else 1
            if not faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return

            # Пуш работает ТОЛЬКО ответом на сообщение
            push_text = (ctx.reply_text or "").strip()
            push_attachments = ctx.reply_attachment_ids or []
            if not push_text and not push_attachments:
                self.send(ctx.peer_id,
                    "❌ !пуш работает только ответом на сообщение.\n"
                    "Ответьте на нужное сообщение командой !пуш (или !пуш (фракция) (сервер) для Админ).")
                return

            args = parts[1:]

            # Определяем наличие @all в аргументах
            mention_all = any(a.lower() == "@all" for a in args)
            args_clean = [a for a in args if a.lower() != "@all"]

            target_faction = faction
            target_server: Optional[int] = actor_server

            if faction == SECRET_FACTION:
                # Для Админ: !пуш (фракция) (сервер) или !пуш все
                if not args_clean:
                    self.send(ctx.peer_id,
                        "Формат для Админ-фракции (ответом на сообщение):\n"
                        "• !пуш (фракция) (сервер) — пуш во фракцию на сервер\n"
                        "• !пуш все — пуш во все чаты\n"
                        "Добавьте @all для пинга всех: !пуш армия 1 @all")
                    return
                if args_clean[0].lower() in {"все", "all"}:
                    target_faction = "__ALL__"
                    target_server = None
                else:
                    tf, rest_tokens = self._extract_faction_and_rest(["!пуш"] + args_clean, 1)
                    if not tf:
                        self.send(ctx.peer_id, "❌ Не удалось определить фракцию.")
                        return
                    target_faction = tf
                    if rest_tokens and rest_tokens[0].isdigit():
                        target_server = int(rest_tokens[0])
                    else:
                        target_server = None

            # Готовим текст
            all_prefix = "@all\n" if mention_all else ""
            full_text = f"📢 Пуш от {self._fmt_user(ctx.user_id)}\n{all_prefix}{push_text}" if push_text else f"📢 Пуш от {self._fmt_user(ctx.user_id)}\n{all_prefix}[вложение]"

            with self.db.conn() as c:
                if target_faction == "__ALL__":
                    chats = c.execute("SELECT chat_id FROM chats").fetchall()
                elif target_server is not None:
                    chats = c.execute(
                        "SELECT chat_id FROM chats WHERE faction=? AND server_id=?",
                        (target_faction, target_server),
                    ).fetchall()
                else:
                    chats = c.execute("SELECT chat_id FROM chats WHERE faction=?", (target_faction,)).fetchall()

            if not chats:
                srv_lbl = f" сервера {target_server}" if target_server else ""
                self.send(ctx.peer_id, f"⚠️ Нет чатов для {target_faction}{srv_lbl}.")
                return

            # Загружаем чаты у которых пуш отключён
            with self.db.conn() as c:
                disabled_rows = c.execute("SELECT chat_id FROM chat_push_disabled").fetchall()
            disabled_chats = {int(r["chat_id"]) for r in disabled_rows}

            sent = 0
            for row in chats:
                chat_id = int(row["chat_id"])
                # Пропускаем чат где написали !пуш
                if ctx.is_chat and chat_id == ctx.chat_id:
                    continue
                # Пропускаем чаты с отключёнными пушами
                if chat_id in disabled_chats:
                    continue
                try:
                    if push_attachments and ctx.platform == "vk":
                        self.send_with_attachments(self._chat_peer_id(chat_id), full_text, push_attachments)
                    else:
                        self.send(self._chat_peer_id(chat_id), full_text)
                    sent += 1
                except Exception:
                    continue

            target_label = target_faction if target_faction != "__ALL__" else "все фракции"
            srv_label = f" (сервер {target_server})" if target_server else ""
            self.send(ctx.peer_id, f"✅ Пуш отправлен в {sent} чат(ов): {target_label}{srv_label}.")
            return
        if cmd == "!создать" and ctx.is_chat and len(parts) >= 4 and parts[1].lower() == "роль":
            role_name = " ".join(parts[2:-1])
            level = int(parts[-1])
            if level < 0 or level > 100:
                self.send(ctx.peer_id, "❌ Уровень роли: 0..100")
                return
            if not self._can_manage_role_level(ctx, level):
                self.send(ctx.peer_id, "⛔ Нельзя создавать роль выше вашего уровня.")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)",
                    (ctx.chat_id, level, role_name),
                )
            self.send(ctx.peer_id, f"✅ Роль «{role_name}» ({level}) создана.")
            return

        if cmd == "!удалить" and ctx.is_chat and len(parts) >= 3 and parts[1].lower() == "роль":
            if not parts[2].isdigit():
                self.send(ctx.peer_id, "Формат: !удалить роль (уровень)")
                return
            lvl = int(parts[2])
            if lvl == 0:
                self.send(ctx.peer_id, "❌ Роль 0 удалять нельзя.")
                return
            if not self._can_manage_role_level(ctx, lvl):
                self.send(ctx.peer_id, "⛔ Нельзя удалять роль выше вашего уровня.")
                return
            with self.db.conn() as c:
                row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, lvl)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Роль с таким уровнем не найдена.")
                    return
                c.execute("UPDATE chat_members SET role_level=0 WHERE chat_id=? AND role_level=?", (ctx.chat_id, lvl))
                c.execute("DELETE FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, lvl))
            self.send(ctx.peer_id, f"✅ Роль «{row['name']}» ({lvl}) удалена. Всем пользователям с этой ролью выдана роль 0.")
            return

        if cmd == "!переименовать" and ctx.is_chat and len(parts) >= 3:
            if not parts[1].isdigit():
                self.send(ctx.peer_id, "Формат: !переименовать (уровень роли) (новое название)")
                return
            lvl = int(parts[1])
            new_name = " ".join(parts[2:]).strip()
            if not new_name:
                self.send(ctx.peer_id, "❌ Укажите новое название роли.")
                return
            if not self._can_manage_role_level(ctx, lvl):
                self.send(ctx.peer_id, "⛔ Нельзя переименовывать роль выше вашего уровня.")
                return
            with self.db.conn() as c:
                row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, lvl)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Роль с таким уровнем не найдена.")
                    return
                c.execute("UPDATE chat_roles SET name=? WHERE chat_id=? AND level=?", (new_name, ctx.chat_id, lvl))
            self.send(ctx.peer_id, f"✅ Роль {lvl} переименована в «{new_name}».")
            return

        if cmd == "!роли" and ctx.is_chat:
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT level, name FROM chat_roles WHERE chat_id=? ORDER BY level DESC",
                    (ctx.chat_id,),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "ℹ️ Роли не созданы.")
            else:
                out = ["📋 Роли чата:"]
                for r in rows:
                    out.append(f"• {r['name']} — уровень {r['level']}")
                self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!роль" and ctx.is_chat:
            if len(parts) >= 3 and parts[2].isdigit():
                target = self._parse_user(parts[1]) or ctx.reply_user_id
                if target is None:
                    self.send(ctx.peer_id, "Формат: !роль (пользователь) (уровень)")
                    return
                lvl = int(parts[2])
                is_chat_leader = self._is_leader_for_chat_faction(ctx.user_id, ctx.chat_id)
                actor_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
                target_lvl = self._get_role_level(ctx.chat_id, target)
                if not is_chat_leader and actor_lvl < target_lvl and self._get_admin_level(ctx.user_id) < 100:
                    self.send(ctx.peer_id, "⛔ Нельзя менять роль пользователю выше вашего уровня.")
                    return
                if not is_chat_leader and lvl > actor_lvl and self._get_admin_level(ctx.user_id) < 100:
                    self.send(ctx.peer_id, "⛔ Нельзя выдать роль выше своей.")
                    return
                with self.db.conn() as c:
                    exists = c.execute("SELECT 1 FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, lvl)).fetchone()
                    if not exists:
                        self.send(ctx.peer_id, "❌ Такой роли не существует в этом чате.")
                        return
                    self._ensure_member(ctx.chat_id, target)
                    old_member = c.execute("SELECT role_level FROM chat_members WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target)).fetchone()
                    old_lvl = int(old_member["role_level"]) if old_member else 0
                    old_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, old_lvl)).fetchone()
                    new_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, lvl)).fetchone()
                    c.execute("UPDATE chat_members SET role_level=? WHERE chat_id=? AND vk_id=?", (lvl, ctx.chat_id, target))
                old_name = old_row["name"] if old_row else DEFAULT_ROLE_NAME
                new_name = new_row["name"] if new_row else DEFAULT_ROLE_NAME
                self.send(
                    ctx.peer_id,
                    f"✅ Роль пользователя {self._fmt_user(target)} изменена: {old_name} ({old_lvl}) → {new_name} ({lvl}).",
                )
                return
            target = ctx.user_id
            if ctx.reply_user_id is not None and len(parts) == 1:
                target = int(ctx.reply_user_id)
            elif len(parts) >= 2:
                parsed = self._parse_user(parts[1]) or ctx.reply_user_id
                if parsed is None:
                    self.send(ctx.peer_id, "Формат: !роль [пользователь] или ответом на сообщение.")
                    return
                target = parsed
            role_lvl = self._get_role_level(ctx.chat_id, target)
            with self.db.conn() as c:
                row = c.execute(
                    "SELECT name FROM chat_roles WHERE chat_id=? AND level=?",
                    (ctx.chat_id, role_lvl),
                ).fetchone()
            role_name = row["name"] if row else DEFAULT_ROLE_NAME
            self.send(
                ctx.peer_id,
                f"👤 Роль {self._fmt_user(target)} в этом чате: {role_name} ({role_lvl})",
            )
            return

        if cmd == "!снять" and ctx.is_chat and len(parts) >= 3 and parts[1].lower() == "роль":
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "Формат: !снять роль (пользователь)")
                return
            with self.db.conn() as c:
                self._ensure_member(ctx.chat_id, target)
                old_member = c.execute("SELECT role_level FROM chat_members WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target)).fetchone()
                old_lvl = int(old_member["role_level"]) if old_member else 0
                old_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, old_lvl)).fetchone()
                new_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=0", (ctx.chat_id,)).fetchone()
                c.execute("UPDATE chat_members SET role_level=0 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
            old_name = old_row["name"] if old_row else DEFAULT_ROLE_NAME
            new_name = new_row["name"] if new_row else DEFAULT_ROLE_NAME
            self.send(
                ctx.peer_id,
                f"✅ Роль пользователя {self._fmt_user(target)} изменена: {old_name} ({old_lvl}) → {new_name} (0).",
            )
            return

        if cmd == "!повысить" and ctx.is_chat:
            target = (self._parse_user(parts[1]) if len(parts) >= 2 else None) or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "Формат: !повысить (пользователь) или ответом на сообщение.")
                return
            current = self._get_role_level(ctx.chat_id, target)
            actor_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
            if actor_lvl < current and self._get_admin_level(ctx.user_id) < 100:
                self.send(ctx.peer_id, "⛔ Нельзя повышать пользователя с ролью выше вашей.")
                return
            with self.db.conn() as c:
                levels = [int(r[0]) for r in c.execute("SELECT level FROM chat_roles WHERE chat_id=? ORDER BY level", (ctx.chat_id,)).fetchall()]
            nxt = next((lv for lv in levels if lv > current), current)
            if nxt == current:
                self.send(ctx.peer_id, f"ℹ️ {self._fmt_user(target)} уже на максимальной роли. Повышать больше некуда.")
                return
            if nxt > actor_lvl and self._get_admin_level(ctx.user_id) < 100:
                self.send(ctx.peer_id, "⛔ Нельзя выдать роль выше вашей.")
                return
            with self.db.conn() as c:
                old_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, current)).fetchone()
                new_row = c.execute("SELECT name FROM chat_roles WHERE chat_id=? AND level=?", (ctx.chat_id, nxt)).fetchone()
                c.execute("UPDATE chat_members SET role_level=? WHERE chat_id=? AND vk_id=?", (nxt, ctx.chat_id, target))
            old_name = old_row["name"] if old_row else DEFAULT_ROLE_NAME
            new_name = new_row["name"] if new_row else DEFAULT_ROLE_NAME
            self.send(
                ctx.peer_id,
                f"✅ Роль пользователя {self._fmt_user(target)} повышена: {old_name} ({current}) → {new_name} ({nxt}).",
            )
            return

        if cmd == "!понизить" and ctx.is_chat:
            target = (self._parse_user(parts[1]) if len(parts) >= 2 else None) or ctx.reply_user_id
            if not target:
                self.send(ctx.peer_id, "Формат: !понизить (пользователь)")
                return
            current = self._get_role_level(ctx.chat_id, target)
            actor_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
            if actor_lvl < current and self._get_admin_level(ctx.user_id) < 100:
                self.send(ctx.peer_id, "⛔ Нельзя понижать пользователя с ролью выше вашей.")
                return
            with self.db.conn() as c:
                levels = [int(r[0]) for r in c.execute("SELECT level FROM chat_roles WHERE chat_id=? ORDER BY level", (ctx.chat_id,)).fetchall()]
            prev = 0
            for lv in levels:
                if lv < current:
                    prev = lv
            if prev == current:
                self.send(ctx.peer_id, f"ℹ️ {self._fmt_user(target)} уже на минимальной роли. Понижать больше некуда.")
                return
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET role_level=? WHERE chat_id=? AND vk_id=?", (prev, ctx.chat_id, target))
            self.send(ctx.peer_id, f"✅ Роль понижена: {current} -> {prev}")
            return

        if cmd in {"!бан", "!кик", "!разбан", "!мут", "!размут", "!пред", "!снятьпред"} and ctx.is_chat:
            self._mod_commands(ctx, parts, cmd)
            return

        if cmd == "!самобан" and ctx.is_chat:
            with self.db.conn() as c:
                self._ensure_member(ctx.chat_id, ctx.user_id)
                c.execute("UPDATE chat_members SET banned=1 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, ctx.user_id))
                c.execute(
                    "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                    (ctx.chat_id, ctx.user_id, ctx.user_id, "Самобан", self.now_ts()),
                )
            try:
                self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=ctx.user_id)
            except Exception:
                pass
            return

        if cmd in {"!банлист", "!мутлист", "!списокпредов"} and ctx.is_chat:
            self._lists(ctx, cmd)
            return

        if cmd in {"!иммунитет", "!снятьиммунитет", "!иммунитеты"} and ctx.is_chat:
            self._immunity(ctx, parts, cmd)
            return

        if cmd in {"!заметка", "!заметки", "!новая"} and ctx.is_chat:
            self._notes(ctx, parts)
            return

        if cmd in {"!закладка", "!закладки"} and ctx.is_chat:
            self._bookmarks(ctx, parts)
            return

        if cmd == "!админы" and ctx.is_chat:
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT m.vk_id,m.role_level,r.name FROM chat_members m LEFT JOIN chat_roles r ON r.chat_id=m.chat_id AND r.level=m.role_level WHERE m.chat_id=? AND m.role_level>0 ORDER BY m.role_level DESC",
                    (ctx.chat_id,),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "📋 В чате 0 людей с должностью выше одобренного пользователя.")
                return
            name_map = self._fmt_users_bulk([int(r["vk_id"]) for r in rows])
            grouped: dict[str, list[str]] = {}
            for r in rows:
                uid = int(r["vk_id"])
                role_label = f"{r['name'] or 'Роль'} ({int(r['role_level'])})"
                grouped.setdefault(role_label, []).append(name_map.get(uid, self._fmt_user_ref(uid)))
            lines = [f"📋 В чате {len(rows)} людей с должностью выше одобренного пользователя:"]
            for role_label, users in grouped.items():
                lines.append(f"• {role_label}: {', '.join(users)}")
            self._send_long_text(ctx.peer_id, "\n".join(lines))
            return

        # admin-only globals
        if cmd == "!аудит" and len(parts) >= 2 and parts[1].lower() == "файлы":
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Только старший админ.")
                return
            results = ["🔍 Аудит файлов:"]
            check_files = ["bot1.py", ".env", "bot.db", "lists.db", "catalogs.db"]
            for fname in check_files:
                fpath = os.path.join(BASE_DIR, fname)
                if not os.path.exists(fpath):
                    results.append(f"• {fname}: ❌ не найден")
                    continue
                try:
                    h = hashlib.sha256()
                    with open(fpath, "rb") as fh_:
                        for chunk in iter(lambda: fh_.read(65536), b""):
                            h.update(chunk)
                    size_kb = os.path.getsize(fpath) // 1024
                    mtime = int(os.path.getmtime(fpath))
                    dt_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                    results.append(f"• {fname}: sha256={h.hexdigest()[:20]}... | {size_kb}KB | изм: {dt_str}")
                except Exception as ex:
                    results.append(f"• {fname}: ⚠️ ошибка — {ex}")
            self.send(ctx.peer_id, "\n".join(results))
            return

        if cmd == "!стереть" and len(parts) >= 2:
            # Double-check: читаем admin_level из БД (не из кэша) + проверяем сессию
            if not WIPE_PASSWORD or len(WIPE_PASSWORD) < 16 or WIPE_PASSWORD == "2n3Z5opi":
                self.send(ctx.peer_id, "⛔ WIPE_PASSWORD не задан или слишком слабый. Установите пароль длиной от 16 символов в .env.")
                return
            with self.db.conn() as _c:
                _sa_row = _c.execute("SELECT admin_level FROM users WHERE vk_id=?", (ctx.user_id,)).fetchone()
            _db_lvl = int(_sa_row["admin_level"]) if _sa_row else 0
            if not self._is_senior_admin_ctx(ctx) or _db_lvl < 100:
                logger.warning(f"Unauthorized !стереть attempt: user={ctx.user_id} db_level={_db_lvl}")
                self.send(ctx.peer_id, "⛔ Команда !стереть доступна только старшему админу.")
                return
            target = self._parse_user(parts[1]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !стереть (пользователь)")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO wipe_sessions(actor_id,target_id,created_at) VALUES(?,?,?)",
                    (ctx.user_id, int(target), self.now_ts()),
                )
            self.send(
                ctx.peer_id,
                f"⚠️ Подтвердите стирание данных {self._fmt_user(int(target))}.\n"
                "Введите пароль следующим сообщением.",
            )
            return

        if cmd == "!облик" and len(parts) >= 3 and parts[1] == "0":
            with self.db.conn() as _c:
                _sa_row = _c.execute("SELECT admin_level FROM users WHERE vk_id=?", (ctx.user_id,)).fetchone()
            _db_lvl = int(_sa_row["admin_level"]) if _sa_row else 0
            if not self._is_senior_admin_ctx(ctx) or _db_lvl < 100:
                logger.warning(f"Unauthorized !облик attempt: user={ctx.user_id} db_level={_db_lvl}")
                self.send(ctx.peer_id, "⛔ Команда !облик доступна только старшему админу.")
                return
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !облик 0 (пользователь)")
                return
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO oblik_users(vk_id,enabled,created_at) VALUES(?,?,?)",
                    (int(target), 1, self.now_ts()),
                )
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (int(target),))
            self.send(ctx.peer_id, f"✅ Облик 0 активирован для {self._fmt_user(int(target))}.")
            return

        if cmd == "!проверкачата" and len(parts) >= 2 and parts[1].isdigit():
            rows = self._all_known_chats()
            idx = int(parts[1])
            if idx < 1 or idx > len(rows):
                self.send(ctx.peer_id, "❌ Неверный номер чата из списка !все чаты.")
                return
            target_chat_id = int(rows[idx - 1]["chat_id"])
            self.send_chat(target_chat_id, f"🔎 Проверка связи: вызвал {self._fmt_user(ctx.user_id)}.")
            self.send(ctx.peer_id, "✅ Проверка чата отправлена.")
            return

        if cmd == "!удаленный" and len(parts) >= 3 and parts[1].lower() == "доступ":
            if not ctx.is_chat:
                self.send(ctx.peer_id, "⛔ Команда работает только в чате.")
                return
            if parts[2].lower() == "стоп":
                with self.db.conn() as c:
                    c.execute("DELETE FROM remote_access_sessions WHERE actor_id=?", (ctx.user_id,))
                self.send(ctx.peer_id, "✅ Удаленный доступ остановлен.")
                return
            if not parts[2].isdigit():
                self.send(ctx.peer_id, "Формат: !удаленный доступ (номер чата) / !удаленный доступ стоп")
                return
            rows = self._all_known_chats()
            idx = int(parts[2])
            if idx < 1 or idx > len(rows):
                self.send(ctx.peer_id, "❌ Неверный номер чата из списка !все чаты.")
                return
            target_chat_id = int(rows[idx - 1]["chat_id"])
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO remote_access_sessions(actor_id,source_chat_id,target_chat_id,created_at) VALUES(?,?,?,?)",
                    (ctx.user_id, ctx.chat_id, target_chat_id, self.now_ts()),
                )
            self.send(ctx.peer_id, f"✅ Удаленный доступ активирован: чат {ctx.chat_id} -> чат {target_chat_id}.")
            return

        if cmd == "!админ" and len(parts) == 4 and parts[1].lower() in {"права", "роль"}:
            with self.db.conn() as _c:
                _sa_row = _c.execute("SELECT admin_level FROM users WHERE vk_id=?", (ctx.user_id,)).fetchone()
            _db_lvl = int(_sa_row["admin_level"]) if _sa_row else 0
            if not self._is_senior_admin_ctx(ctx) or _db_lvl < 100:
                logger.warning(f"Unauthorized !админ права attempt: user={ctx.user_id} db_level={_db_lvl}")
                self._alert_senior_admin(f"⚠️ Попытка использовать !админ права: id{ctx.user_id} (db_level={_db_lvl})")
                self.send(ctx.peer_id, "⛔ Команда !админ права доступна только старшему админу.")
                return
            if not parts[3].lstrip("-").isdigit():
                self.send(ctx.peer_id, "Формат: !админ права (пользователь) (0..100)")
                return
            lvl = int(parts[3])
            if not (0 <= lvl <= 100):
                self.send(ctx.peer_id, "❌ Уровень должен быть от 0 до 100.")
                return
            
            uid = self._parse_user_fast(parts[2]) or self._parse_user(parts[2])
            if uid is None:
                self.send(ctx.peer_id, "❌ Не удалось найти пользователя.")
                return
            
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (uid,))
                c.execute("UPDATE users SET admin_level=? WHERE vk_id=?", (lvl, uid))
            
            # ИСПРАВЛЕНО: используем _fmt_user для красивого отображения
            self.send(ctx.peer_id, f"✅ Пользователю {self._fmt_user(uid)} установлен админ-уровень {lvl}.")
            return

        if cmd == "!лидер" and len(parts) >= 4:
            faction, rest = self._extract_faction_and_rest(parts, 1)
            if not faction or len(rest) < 2:
                self.send(ctx.peer_id, "Формат: !лидер (фракция) (сервер 1..3) (пользователь)")
                return
            if rest[0] not in {"1", "2", "3"}:
                self.send(ctx.peer_id, "❌ Сервер должен быть 1, 2 или 3.")
                return
            server_id = int(rest[0])
            uid = self._parse_user(rest[1])
            if faction not in FACTIONS or uid is None:
                self.send(ctx.peer_id, "Формат: !лидер (фракция) (сервер 1..3) (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO leaders(faction,server_id,vk_id) VALUES(?,?,?)", (faction, server_id, uid))
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (uid,))
                c.execute("UPDATE users SET admin_level=10 WHERE vk_id=?", (uid,))
            self.send(ctx.peer_id, f"✅ Лидер {faction} (сервер {server_id}): {self._fmt_user(uid)}")
            return

        if cmd == "!лидеры":
            # !лидеры [фракция] — показать всех лидеров или лидеров конкретной фракции
            filter_faction: Optional[str] = None
            if len(parts) >= 2:
                f_input = " ".join(parts[1:]).strip()
                filter_faction = next((f for f in ALL_FACTIONS if f.lower() == f_input.lower()), None)
                if not filter_faction:
                    self.send(ctx.peer_id, f"❌ Фракция не найдена. Доступные: {', '.join(FACTIONS)}")
                    return
            with self.db.conn() as c:
                if filter_faction:
                    rows = c.execute(
                        "SELECT faction, server_id, vk_id FROM leaders WHERE faction=? ORDER BY server_id",
                        (filter_faction,),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT faction, server_id, vk_id FROM leaders ORDER BY faction, server_id"
                    ).fetchall()
            if not rows:
                msg = f"Лидеров фракции {filter_faction} нет." if filter_faction else "Лидеров нет."
                self.send(ctx.peer_id, f"👑 {msg}")
                return
            lines = ["👑 Список лидеров фракций:"]
            current_faction = None
            for row in rows:
                f_name = row["faction"]
                if f_name != current_faction:
                    if current_faction is not None:
                        lines.append("")
                    lines.append(f"🏛 {f_name}:")
                    current_faction = f_name
                uid = int(row["vk_id"])
                cached = self.user_name_cache.get(uid)
                if cached:
                    name_str = cached[0]
                else:
                    name_str = f"id{uid}"
                lines.append(f"  Сервер {row['server_id']}: [id{uid}|{name_str}]")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd == "!снятьлидера" and len(parts) >= 2:
            # Формат: !снятьлидера (фракция) [сервер]
            # Если сервер не указан — снимаем со всех серверов
            faction_parts = parts[1:]
            server_id_filter: Optional[int] = None
            if faction_parts and faction_parts[-1].isdigit() and faction_parts[-1] in {"1", "2", "3"}:
                server_id_filter = int(faction_parts[-1])
                faction_parts = faction_parts[:-1]
            faction = " ".join(faction_parts).strip()
            faction = next((f for f in ALL_FACTIONS if f.lower() == faction.lower()), faction)
            if not faction:
                self.send(ctx.peer_id, "❌ Укажите фракцию.")
                return
            with self.db.conn() as c:
                if server_id_filter is not None:
                    rows = c.execute(
                        "SELECT vk_id, server_id FROM leaders WHERE faction=? AND server_id=?",
                        (faction, server_id_filter),
                    ).fetchall()
                else:
                    rows = c.execute("SELECT vk_id, server_id FROM leaders WHERE faction=?", (faction,)).fetchall()
                if not rows:
                    self.send(ctx.peer_id, "❌ Лидер не найден.")
                    return
                for row in rows:
                    uid = int(row["vk_id"])
                    srv = int(row["server_id"])
                    c.execute("DELETE FROM leaders WHERE faction=? AND server_id=?", (faction, srv))
                    # Сбрасываем admin_level только если у него нет других позиций лидера
                    other = c.execute(
                        "SELECT 1 FROM leaders WHERE vk_id=?", (uid,)
                    ).fetchone()
                    if not other:
                        c.execute("UPDATE users SET admin_level=0 WHERE vk_id=? AND admin_level=10", (uid,))
            removed = ", ".join(f"сервер {int(r['server_id'])}" for r in rows)
            self.send(ctx.peer_id, f"✅ Лидер фракции {faction} снят ({removed}).")
            return

        if cmd == "!изменить" and len(parts) >= 4:
            what = parts[1].lower()
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !изменить (фракцию|фио|ник|должность|сервер) (пользователь) (новое значение)")
                return
            new_value = " ".join(parts[3:]).strip()
            if not new_value:
                self.send(ctx.peer_id, "❌ Укажите новое значение.")
                return
            admin_lvl = self._get_admin_level(ctx.user_id)
            field_map = {
                "фракцию": "faction", "фио": "rp_name", "ник": "nickname",
                "должность": "position", "сервер": "server_id", "ник2": "nick2",
            }
            field = field_map.get(what)
            if not field:
                self.send(ctx.peer_id, "❌ Раздел изменения: фракцию / фио / ник / ник2 / должность / сервер.")
                return
            # ник2 требует admin 30+
            if field == "nick2" and admin_lvl < 30 and not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Для изменения ник2 нужен admin 30+.")
                return
            if admin_lvl < 50 and field != "nick2":
                if not (5 <= admin_lvl <= 10 and field == "position"):
                    self.send(ctx.peer_id, "⛔ Недостаточно прав. Для admin 5-10 доступно только: !изменить должность.")
                    return
                actor_u = self._user(ctx.user_id)
                target_u = self._user(target)
                if not actor_u or not target_u or actor_u["faction"] != target_u["faction"] or int(actor_u["server_id"] or 1) != int(target_u["server_id"] or 1):
                    self.send(ctx.peer_id, "⛔ Изменять должность можно только пользователю своей фракции.")
                    return
            if field == "faction":
                faction_match = next((f for f in ALL_FACTIONS if f.lower() == new_value.lower()), None)
                if not faction_match:
                    self.send(ctx.peer_id, "❌ Укажите корректную фракцию.")
                    return
                new_value = faction_match
            if field == "server_id":
                if new_value not in {"1", "2", "3"}:
                    self.send(ctx.peer_id, "❌ Сервер должен быть 1, 2 или 3.")
                    return
                new_value = int(new_value)
            # SECURITY: runtime whitelist check (field пришёл из field_map выше,
            # но проверяем снова чтобы исключить любые пути обхода)
            _SAFE_FIELDS = frozenset({"faction", "rp_name", "nickname", "position", "server_id", "nick2"})
            if field not in _SAFE_FIELDS:
                logger.critical(f"SQLi attempt in !изменить: field={field!r} actor={ctx.user_id}")
                self._alert_senior_admin(f"🚨 SQLi попытка в !изменить: поле={field!r}, пользователь id{ctx.user_id}")
                self.send(ctx.peer_id, "⛔ Недопустимое поле.")
                return
            max_lengths = {"faction":100,"rp_name":200,"nickname":100,"position":150,"nick2":100,"server_id":1}
            if isinstance(new_value, str) and len(new_value) > max_lengths.get(field, 200):
                self.send(ctx.peer_id, f"❌ Значение слишком длинное (макс. {max_lengths.get(field,200)} симв.).")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                # field гарантированно в whitelist — f-string безопасен
                c.execute(f"UPDATE users SET {field}=? WHERE vk_id=?", (new_value, target))
            logger.info(f"!изменить actor={ctx.user_id} target={target} field={field} value={str(new_value)[:40]!r}")
            self.send(ctx.peer_id, f"✅ Изменено: {self._fmt_user(target)} -> {what} = {new_value}.")
            return

        if cmd == "!рейтинг" and len(parts) >= 4:
            delta_token = parts[1]
            if not re.fullmatch(r"[+-]\d+", delta_token):
                self.send(ctx.peer_id, "Формат: !рейтинг (+N|-N) (пользователь) (причина)")
                return
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !рейтинг (+N|-N) (пользователь) (причина)")
                return
            reason = " ".join(parts[3:]).strip()
            if not reason:
                self.send(ctx.peer_id, "❌ Укажите причину изменения рейтинга.")
                return
            delta = int(delta_token)
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id,rating) VALUES(?,100)", (target,))
                row = c.execute("SELECT rating FROM users WHERE vk_id=?", (target,)).fetchone()
                before = int(row["rating"] if row and row["rating"] is not None else 100)
                after = max(0, min(100, before + delta))
                c.execute("UPDATE users SET rating=? WHERE vk_id=?", (after, target))
            self.send(ctx.peer_id, f"✅ Рейтинг {self._fmt_user(target)} изменен: {before} -> {after}.")
            self.send_dm(target, f"Ваш рейтинг изменили с {before} до {after} по причине: {reason}")
            return

        if cmd == "!админсчет":
            with self.db.conn() as c:
                rows = c.execute(
                    """
                    SELECT s.vk_id, s.accepted_count, s.rejected_count
                    FROM admin_complaint_stats s
                    JOIN users u ON u.vk_id=s.vk_id
                    WHERE u.admin_level>=40 OR u.faction=?
                    ORDER BY (s.accepted_count+s.rejected_count) DESC, s.vk_id ASC
                    """,
                    (SECRET_FACTION,),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "📊 Админ-счетчики жалоб: пусто")
                return
            out = ["📊 Счетчики жалоб админов:"]
            for r in rows:
                out.append(
                    f"• {self._fmt_user(int(r['vk_id']))} — приняты: {int(r['accepted_count'])}, отклонены: {int(r['rejected_count'])}"
                )
            self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!супербан":
            # DB check: берём admin_level прямо из БД, не из кэша
            with self.db.conn() as _sc:
                _superbam_row = _sc.execute("SELECT admin_level FROM users WHERE vk_id=?", (ctx.user_id,)).fetchone()
            _superbam_lvl = int(_superbam_row["admin_level"]) if _superbam_row else 0
            _required_superbam = self._get_effective_admin_min("!супербан")
            if _superbam_lvl < max(_required_superbam, 30) and not self._is_senior_admin_ctx(ctx):
                logger.warning(f"Unauthorized !супербан: user={ctx.user_id} db_level={_superbam_lvl}")
                self.send(ctx.peer_id, "⛔ Недостаточно прав для !супербан.")
                return
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            reason = " ".join(parts[2:]).strip() if len(parts) > 2 else "без причины"
            if target is None:
                self.send(ctx.peer_id, "Формат: !супербан (пользователь) (причина)")
                return
            ts = self.now_ts()
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO bot_bans(vk_id) VALUES(?)", (target,))
                chats = c.execute("SELECT chat_id FROM chats").fetchall()
                for idx, row in enumerate(chats, start=1):
                    chat_id = int(row["chat_id"])
                    c.execute(
                        "INSERT OR IGNORE INTO chat_members(chat_id,vk_id,role_level,immunity_level,banned) VALUES(?,?,0,0,0)",
                        (chat_id, target),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)",
                        (chat_id, 0, DEFAULT_ROLE_NAME),
                    )
                    c.execute("UPDATE chat_members SET banned=1, role_level=0, immunity_level=0 WHERE chat_id=? AND vk_id=?", (chat_id, target))
                    c.execute(
                        "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                        (chat_id, target, ctx.user_id, f"Супербан: {reason}", ts),
                    )
                    try:
                        self.api.messages.removeChatUser(chat_id=chat_id, member_id=target)
                    except Exception:
                        pass
                    if idx % 8 == 0:
                        c.commit()
                        time.sleep(5)
            self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} забанен во всех чатах и в боте.")
            return

        if cmd == "!суперразбан":
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !суперразбан (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("DELETE FROM bot_bans WHERE vk_id=?", (target,))
                chat_rows = c.execute(
                    "SELECT chat_id FROM chat_members WHERE vk_id=? AND banned=1 ORDER BY chat_id",
                    (target,),
                ).fetchall()
                for row in chat_rows:
                    chat_id = int(row["chat_id"])
                    c.execute("UPDATE chat_members SET banned=0 WHERE chat_id=? AND vk_id=?", (chat_id, target))
                    c.execute("UPDATE ban_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1", (chat_id, target))
            self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} разбанен в боте и во всех чатах, где был в бане.")
            return

        if cmd == "!ботбан" and len(parts) >= 2:
            if len(parts) == 2 and parts[1].lower() == "лист":
                with self.db.conn() as c:
                    rows = c.execute("SELECT vk_id FROM bot_bans ORDER BY vk_id").fetchall()
                self.send(ctx.peer_id, "📋 Ботбан:\n" + ("\n".join([f"• {self._fmt_user(int(r[0]))}" for r in rows]) if rows else "пусто"))
                return
            uid = self._parse_user(parts[1])
            if uid is None:
                self.send(ctx.peer_id, "Формат: !ботбан (пользователь) или !ботбан лист")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO bot_bans(vk_id) VALUES(?)", (uid,))
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(uid)} заблокирован в боте.")
            return

        if cmd == "!ботразбан" and len(parts) >= 2:
            uid = self._parse_user(parts[1]) or ctx.reply_user_id
            if uid is None:
                self.send(ctx.peer_id, "Формат: !ботразбан (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("DELETE FROM bot_bans WHERE vk_id=?", (uid,))
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(uid)} разблокирован в боте.")
            return

        if cmd == "!снятьрольвезде" and len(parts) >= 2:
            target = self._parse_user(parts[1]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !снятьрольвезде (пользователь)")
                return
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET role_level=0 WHERE vk_id=?", (target,))
            self.send(ctx.peer_id, f"✅ Роли пользователя {self._fmt_user(target)} сброшены до 0 во всех чатах.")
            return

        if cmd == "!лимиткоманд" and len(parts) == 2 and parts[1].isdigit():
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('command_limit_per_minute',?)", (parts[1],))
            self.send(ctx.peer_id, f"✅ Новый лимит: {parts[1]} команд/мин.")
            return

        if cmd == "!чат" and len(parts) >= 4 and parts[1].lower() == "фракции" and ctx.is_chat:
            actor_admin = self._get_admin_level(ctx.user_id)
            server_raw = parts[-1]
            if server_raw not in {"1", "2", "3"}:
                self.send(ctx.peer_id, "❌ Укажите сервер 1, 2 или 3.")
                return
            server_id = int(server_raw)
            faction = " ".join(parts[2:-1])
            faction_match = next((f for f in ALL_FACTIONS if f.lower() == faction.lower()), None)
            if not faction_match:
                self.send(ctx.peer_id, "❌ Неизвестная фракция.")
                return
            faction = faction_match
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            is_admin_faction = actor_faction == SECRET_FACTION
            is_leader_target = self._is_leader_for(ctx.user_id, faction, server_id)
            if actor_admin < 40 and not is_admin_faction and not is_leader_target:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+ или лидерам своей фракции.")
                return
            if is_leader_target is False and not is_admin_faction and actor_admin < 40 and self._is_leader_user(ctx.user_id):
                self.send(ctx.peer_id, f"⛔ Вы лидер другой фракции.\nВы можете привязывать только чаты своей фракции.")
                return
            if not is_admin_faction and actor_admin >= 40 and faction != actor_faction and not is_leader_target:
                self.send(ctx.peer_id, "⛔ Вы можете привязать чат только к своей фракции.")
                return
            title = f"Чат {ctx.chat_id}"
            try:
                conv = self.api.messages.getConversationsById(peer_ids=ctx.peer_id)
                items = conv.get("items", [])
                if items:
                    title = items[0].get("chat_settings", {}).get("title") or title
            except Exception:
                pass
            with self.db.conn() as c:
                exists = c.execute("SELECT faction FROM chats WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                if exists and exists[0]:
                    self.send(ctx.peer_id, "❌ Этот чат уже привязан к фракции.")
                    return
                c.execute("INSERT OR REPLACE INTO chats(chat_id,title,faction,server_id) VALUES(?,?,?,?)", (ctx.chat_id, title, faction, server_id))
            self.send(ctx.peer_id, f"✅ Чат привязан к фракции: {faction} (сервер {server_id})")
            return

        if cmd == "!убрать" and len(parts) >= 3 and parts[1].lower() == "чат" and parts[2].lower() == "фракции" and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) < 40:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+.")
                return
            with self.db.conn() as c:
                c.execute("UPDATE chats SET faction=NULL WHERE chat_id=?", (ctx.chat_id,))
            self.send(ctx.peer_id, "✅ Привязка чата к фракции снята.")
            return

        if cmd == "!чаты" and len(parts) >= 4 and parts[1].lower() == "фракций":
            if parts[-1] not in {"1", "2", "3"}:
                self.send(ctx.peer_id, "❌ Укажите сервер 1, 2 или 3.")
                return
            server_id = int(parts[-1])
            faction_input = " ".join(parts[2:-1]).strip()
            faction_match = next((f for f in ALL_FACTIONS if f.lower() == faction_input.lower()), faction_input)
            faction = faction_match
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT chat_id,title,server_id FROM chats WHERE LOWER(COALESCE(faction,''))=LOWER(?) AND (server_id=? OR server_id IS NULL) ORDER BY chat_id",
                    (faction, server_id),
                ).fetchall()
                if rows:
                    c.execute(
                        "UPDATE chats SET server_id=? WHERE LOWER(COALESCE(faction,''))=LOWER(?) AND (server_id IS NULL OR server_id<1 OR server_id>3)",
                        (server_id, faction),
                    )
            if not rows:
                self.send(ctx.peer_id, "пусто")
                return
            msg = [f"📋 Чаты фракции {faction} (сервер {server_id}):"]
            for r in rows:
                msg.append(f"• {r['title'] or ('Чат ' + str(r['chat_id']))}")
            self.send(ctx.peer_id, "\n".join(msg))
            return

        if cmd == "!все" and len(parts) >= 2 and parts[1].lower() == "чаты":
            if self._get_admin_level(ctx.user_id) < 80:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 80+.")
                return
            rows = self._all_known_chats()
            if not rows:
                self.send(ctx.peer_id, "📋 Список чатов пуст.")
                return
            with self.db.conn() as c:
                blocked = {int(r["chat_id"]) for r in c.execute("SELECT chat_id FROM blocked_chats").fetchall()}
            lines = ["📋 Все чаты:"]
            for idx, r in enumerate(rows, start=1):
                chat_id = int(r["chat_id"])
                title = r["title"] or f"Чат {chat_id}"
                mark = " ❌" if chat_id in blocked else ""
                faction_note = f" (чат фракции {r['faction']})" if r["faction"] else ""
                lines.append(f"{idx}. {title} [{chat_id}]{faction_note}{mark}")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd in {"!блок", "!разблок"} and len(parts) >= 3 and parts[1].lower() == "чат":
            if self._get_admin_level(ctx.user_id) < 80:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 80+.")
                return
            if not parts[2].isdigit():
                self.send(ctx.peer_id, f"Формат: {cmd} чат (номер)")
                return
            rows = self._all_known_chats()
            idx = int(parts[2])
            if idx < 1 or idx > len(rows):
                self.send(ctx.peer_id, "❌ Неверный номер чата из списка !все чаты.")
                return
            target_chat_id = int(rows[idx - 1]["chat_id"])
            with self.db.conn() as c:
                if cmd == "!блок":
                    c.execute(
                        "INSERT OR REPLACE INTO blocked_chats(chat_id,blocked_by,blocked_at) VALUES(?,?,?)",
                        (target_chat_id, ctx.user_id, self.now_ts()),
                    )
                    self.send(ctx.peer_id, f"✅ Чат [{target_chat_id}] заблокирован для работы бота.")
                else:
                    c.execute("DELETE FROM blocked_chats WHERE chat_id=?", (target_chat_id,))
                    self.send(ctx.peer_id, f"✅ Чат [{target_chat_id}] разблокирован.")
            return

        if cmd == "!права" and ctx.is_chat:
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id)
            actor = self._user(ctx.user_id)
            is_admin_faction = bool(actor and actor["faction"] == SECRET_FACTION)
            target_role = actor_role
            target_admin: Optional[int] = None
            if len(parts) >= 2:
                if not parts[1].isdigit():
                    self.send(ctx.peer_id, "Формат: !права [уровень роли] [уровень админ прав]")
                    return
                target_role = int(parts[1])
                if not is_admin_faction and target_role > actor_role:
                    self.send(ctx.peer_id, "⛔ Нельзя просматривать права для роли выше вашей.")
                    return
            if len(parts) >= 3:
                if not is_admin_faction:
                    self.send(ctx.peer_id, "⛔ Указывать уровень админ прав в !права могут только пользователи фракции Админ.")
                    return
                if not parts[2].isdigit():
                    self.send(ctx.peer_id, "Формат: !права [уровень роли] [уровень админ прав]")
                    return
                target_admin = int(parts[2])
            lines = [f"📘 Права для роли {target_role} в этом чате:"]
            for command_name in sorted(COMMAND_ACCESS.keys()):
                need_role = self._required_role(command_name, ctx.chat_id)
                need_admin = self._get_admin_min(command_name)
                if target_admin is None:
                    if need_admin > 0:
                        continue
                    if need_role <= target_role:
                        lines.append(f"• {command_name} — от роли {need_role}")
                else:
                    if need_role <= target_role or target_admin >= need_admin:
                        lines.append(f"• {command_name} — от роли {need_role}, от админ {need_admin}")
            self.send(ctx.peer_id, "\n".join(lines[:200]))
            return

        if cmd == "!чс":
            if len(parts) < 4:
                self.send(ctx.peer_id, "Формат: !чс (фракция) (пользователь) (причина)")
                return
            faction, rest = self._extract_faction_and_rest(parts, 1)
            if not faction and parts[1].lower() in {"все", "all"}:
                faction = "__ALL__"
                rest = parts[2:]
            if not faction or len(rest) < 2:
                self.send(ctx.peer_id, "❌ Не удалось разобрать фракцию/пользователя. Пример: !чс МВД @user причина")
                return
            target = self._parse_user(rest[0])
            if target is None:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return
            reason = " ".join(rest[1:]).strip() or "без причины"
            ts = self.now_ts()
            with self.db.conn() as c:
                target_factions = ALL_FACTIONS if faction == "__ALL__" else [faction]
                for f in target_factions:
                    c.execute(
                        "INSERT INTO faction_blacklist(faction,vk_id,reason,issuer_id,created_at) VALUES(?,?,?,?,?)",
                        (f, target, reason, ctx.user_id, ts),
                    )
                chats = c.execute(
                    "SELECT chat_id,faction FROM chats" + ("" if faction == "__ALL__" else " WHERE faction=?"),
                    (() if faction == "__ALL__" else (faction,)),
                ).fetchall()
                for row in chats:
                    chat_id = int(row["chat_id"])
                    f_name = row["faction"] if "faction" in row.keys() else faction
                    c.execute(
                        "INSERT OR IGNORE INTO chat_members(chat_id,vk_id,role_level,immunity_level,banned) VALUES(?,?,0,0,0)",
                        (chat_id, target),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)",
                        (chat_id, 0, DEFAULT_ROLE_NAME),
                    )
                    c.execute("UPDATE chat_members SET banned=1 WHERE chat_id=? AND vk_id=?", (chat_id, target))
                    c.execute(
                        "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                        (chat_id, target, ctx.user_id, f"ЧС {f_name}: {reason}", ts),
                    )
                    try:
                        self.api.messages.removeChatUser(chat_id=chat_id, member_id=target)
                    except Exception:
                        pass
            label = "всех фракций" if faction == "__ALL__" else f"фракции {faction}"
            self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} внесён в ЧС {label}.")
            return

        if cmd == "!снятьчс":
            if len(parts) < 3:
                self.send(ctx.peer_id, "Формат: !снятьчс (фракция) (пользователь)")
                return
            faction, rest = self._extract_faction_and_rest(parts, 1)
            if not faction and parts[1].lower() in {"все", "all"}:
                faction = "__ALL__"
                rest = parts[2:]
            if not faction or not rest:
                self.send(ctx.peer_id, "❌ Не удалось разобрать фракцию/пользователя.")
                return
            target = self._parse_user(rest[0]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
                return
            with self.db.conn() as c:
                if faction == "__ALL__":
                    c.execute("DELETE FROM faction_blacklist WHERE vk_id=?", (target,))
                    chats = c.execute("SELECT chat_id FROM chats").fetchall()
                else:
                    c.execute("DELETE FROM faction_blacklist WHERE faction=? AND vk_id=?", (faction, target))
                    chats = c.execute("SELECT chat_id FROM chats WHERE faction=?", (faction,)).fetchall()
                for row in chats:
                    chat_id = int(row["chat_id"])
                    c.execute("UPDATE chat_members SET banned=0 WHERE chat_id=? AND vk_id=?", (chat_id, target))
                    c.execute("UPDATE ban_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1", (chat_id, target))
            label = "всех фракций" if faction == "__ALL__" else f"фракции {faction}"
            self.send(ctx.peer_id, f"✅ {self._fmt_user(target)} удалён из ЧС {label}.")
            return

        if cmd == "!списокчс":
            if len(parts) < 2:
                self.send(ctx.peer_id, "Укажите фракцию. Доступные: " + ", ".join(ALL_FACTIONS))
                return
            faction = " ".join(parts[1:]).strip()
            if faction.lower() in {"все", "all"}:
                with self.db.conn() as c:
                    rows = c.execute(
                        """
                        SELECT faction, vk_id, reason, issuer_id, created_at
                        FROM faction_blacklist
                        ORDER BY created_at DESC
                        """
                    ).fetchall()
                if not rows:
                    self.send(ctx.peer_id, "📋 ЧС всех фракций: пусто")
                    return
                out = ["📋 ЧС всех фракций:"]
                for r in rows:
                    dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d.%m.%Y %H:%M")
                    out.append(
                        f"• [{r['faction']}] {self._fmt_user(int(r['vk_id']))} — причина: {r['reason']} — внес: {self._fmt_user(int(r['issuer_id']))} — {dt}"
                    )
                self.send(ctx.peer_id, "\n".join(out[:120]))
                return
            faction_match = next((f for f in ALL_FACTIONS if f.lower() == faction.lower()), None)
            if not faction_match:
                self.send(ctx.peer_id, "❌ Укажите корректную фракцию.")
                return
            faction = faction_match
            with self.db.conn() as c:
                rows = c.execute(
                    """
                    SELECT vk_id, reason, issuer_id, created_at
                    FROM faction_blacklist
                    WHERE faction=?
                    ORDER BY created_at DESC
                    """,
                    (faction,),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, f"📋 ЧС фракции {faction}: пусто")
                return
            out = [f"📋 ЧС фракции {faction}:"]
            for r in rows:
                dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d.%m.%Y %H:%M")
                out.append(
                    f"• {self._fmt_user(int(r['vk_id']))} — причина: {r['reason']} — внес: {self._fmt_user(int(r['issuer_id']))} — {dt}"
                )
            self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd == "!копичат" and ctx.is_chat:
            code = f"{random.randint(0, 9999):04d}"
            with self.db.conn() as c:
                roles = [dict(r) for r in c.execute("SELECT level,name FROM chat_roles WHERE chat_id=?", (ctx.chat_id,)).fetchall()]
                admins = [dict(r) for r in c.execute("SELECT vk_id,role_level,immunity_level FROM chat_members WHERE chat_id=? AND role_level>0", (ctx.chat_id,)).fetchall()]
                notes = [dict(r) for r in c.execute("SELECT name,content,attachments,min_role FROM notes WHERE chat_id=?", (ctx.chat_id,)).fetchall()]
                ch = c.execute("SELECT welcome_text FROM chats WHERE chat_id=?", (ctx.chat_id,)).fetchone()
                payload = {
                    "roles": roles,
                    "admins": admins,
                    "notes": notes,
                    "welcome_text": ch["welcome_text"] if ch else None,
                }
                c.execute(
                    "INSERT OR REPLACE INTO chat_templates(code,payload,created_at) VALUES(?,?,?)",
                    (code, json.dumps(payload, ensure_ascii=False), self.now_ts()),
                )
            self.send(ctx.peer_id, f"✅ Настройки чата скопированы. Код: {code}")
            return

        if cmd == "!пастечат" and ctx.is_chat and len(parts) == 2 and re.fullmatch(r"\d{4}", parts[1]):
            code = parts[1]
            with self.db.conn() as c:
                row = c.execute("SELECT payload FROM chat_templates WHERE code=?", (code,)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Код не найден или уже использован.")
                    return
                data = json.loads(row["payload"])
                c.execute("DELETE FROM chat_roles WHERE chat_id=?", (ctx.chat_id,))
                for role in data.get("roles", []):
                    c.execute("INSERT INTO chat_roles(chat_id,level,name) VALUES(?,?,?)", (ctx.chat_id, int(role["level"]), role["name"]))
                c.execute("INSERT OR IGNORE INTO chat_roles(chat_id,level,name) VALUES(?,?,?)", (ctx.chat_id, 0, DEFAULT_ROLE_NAME))

                c.execute("DELETE FROM chat_members WHERE chat_id=? AND role_level>0", (ctx.chat_id,))
                for adm in data.get("admins", []):
                    c.execute(
                        "INSERT OR REPLACE INTO chat_members(chat_id,vk_id,role_level,immunity_level,banned) VALUES(?,?,?,?,0)",
                        (ctx.chat_id, int(adm["vk_id"]), int(adm["role_level"]), int(adm.get("immunity_level", 0))),
                    )

                c.execute("DELETE FROM notes WHERE chat_id=?", (ctx.chat_id,))
                for note in data.get("notes", []):
                    c.execute(
                        "INSERT INTO notes(chat_id,name,content,attachments,min_role) VALUES(?,?,?,?,?)",
                        (ctx.chat_id, note["name"], note["content"], note.get("attachments"), int(note["min_role"])),
                    )
                c.execute(
                    "INSERT OR IGNORE INTO chats(chat_id,title,faction,welcome_text) VALUES(?,?,NULL,?)",
                    (ctx.chat_id, f"Чат {ctx.chat_id}", data.get("welcome_text")),
                )
                c.execute("UPDATE chats SET welcome_text=? WHERE chat_id=?", (data.get("welcome_text"), ctx.chat_id))
                c.execute("DELETE FROM chat_templates WHERE code=?", (code,))
            self.send(ctx.peer_id, "✅ Настройки успешно перенесены и код удалён.")
            return

        self._active_command = None
        return

    def _mod_commands(self, ctx: Ctx, parts: list[str], cmd: str) -> None:
        target_token = parts[1] if len(parts) >= 2 else None
        explicit_target = self._parse_user(target_token) if target_token else None
        target = explicit_target or ctx.reply_user_id
        if target is None:
            self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
            return
        if int(target) == int(ctx.user_id):
            self.send(ctx.peer_id, "⛔ Нельзя применять наказание к самому себе.")
            return
        if not self._can_affect(ctx.user_id, target, ctx.chat_id):
            self.send(ctx.peer_id, "⛔ Нельзя воздействовать на этого пользователя.")
            return

        self._ensure_member(ctx.chat_id, target)
        ts = self.now_ts()
        reason_start = 2 if explicit_target else 1

        if cmd == "!бан":
            reason = " ".join(parts[reason_start:]).strip() or "без причины"
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET banned=1 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                c.execute(
                    "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                    (ctx.chat_id, target, ctx.user_id, reason, ts),
                )
            try:
                self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=target)
            except Exception:
                pass
            self.send(
                ctx.peer_id,
                f"✅ Пользователю {self._fmt_user(target)} был выдан бан. Причина: {reason}.",
            )
            return

        if cmd == "!кик":
            reason = " ".join(parts[reason_start:]).strip() or "без причины"
            with self.db.conn() as c:
                c.execute("UPDATE ban_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1", (ctx.chat_id, target))
            try:
                self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=target)
            except Exception:
                pass
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} кикнут. Причина: {reason}.")
            return

        if cmd == "!разбан":
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET banned=0 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                c.execute(
                    "UPDATE ban_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1",
                    (ctx.chat_id, target),
                )
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} разбанен.")
            return

        if cmd == "!мут":
            duration_sec = 3600
            reason = "без причины"
            duration_idx = 2 if explicit_target else 1
            reason_start_idx = duration_idx  # по умолчанию причина начинается с duration_idx

            if len(parts) > duration_idx:
                # Пробуем распознать время. Возможные форматы:
                # "30мин", "30 мин", "30 минут", "2ч", "2 ч", "2 часа", "2h", "30m", "30min"
                # Берём первый токен (или первые два, если они вместе дают "число + единица")
                tok0 = parts[duration_idx].lower()
                tok1 = parts[duration_idx + 1].lower() if len(parts) > duration_idx + 1 else ""

                # Расширенный regex: число слитно с единицей (24ч, 30мин, 2h, 30m, 30min)
                TIME_RE_COMBINED = re.compile(
                    r"^(\d+)\s*(ч|час|часа|часов|h|hour|hours|мин|минут|минуты|м|m|min|mins|minute|minutes)$",
                    re.I | re.U,
                )
                # Только число — единица в следующем токене
                TIME_RE_NUM = re.compile(r"^(\d+)$")
                TIME_RE_UNIT = re.compile(
                    r"^(ч|час|часа|часов|h|hour|hours|мин|минут|минуты|м|m|min|mins|minute|minutes)$",
                    re.I | re.U,
                )

                matched_time = False
                mc = TIME_RE_COMBINED.match(tok0)
                if mc:
                    # Слитный формат: "24ч", "30мин", "2h"
                    val = int(mc.group(1))
                    unit = mc.group(2).lower()
                    if unit in {"ч", "час", "часа", "часов", "h", "hour", "hours"}:
                        duration_sec = val * 3600
                    else:
                        duration_sec = val * 60
                    reason_start_idx = duration_idx + 1
                    matched_time = True
                elif TIME_RE_NUM.match(tok0) and tok1 and TIME_RE_UNIT.match(tok1):
                    # Раздельный формат: "24 ч", "30 мин", "2 часа"
                    val = int(tok0)
                    unit = tok1.lower()
                    if unit in {"ч", "час", "часа", "часов", "h", "hour", "hours"}:
                        duration_sec = val * 3600
                    else:
                        duration_sec = val * 60
                    reason_start_idx = duration_idx + 2
                    matched_time = True

                # Если время не распознано — весь остаток считается причиной
                if not matched_time:
                    reason_start_idx = duration_idx

                reason_parts = parts[reason_start_idx:]
                if reason_parts:
                    reason = " ".join(reason_parts).strip() or "без причины"
            until = self.now_ts() + duration_sec
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET muted_until=? WHERE chat_id=? AND vk_id=?", (until, ctx.chat_id, target))
                c.execute(
                    "UPDATE mute_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1",
                    (ctx.chat_id, target),
                )
                c.execute(
                    "INSERT INTO mute_logs(chat_id,vk_id,issuer_id,reason,created_at,until_ts,active) VALUES(?,?,?,?,?,?,1)",
                    (ctx.chat_id, target, ctx.user_id, reason, ts, until),
                )
            try:
                vk_ok = self._apply_vk_mute(ctx.peer_id, target, duration_sec)
            except Exception:
                vk_ok = False
            if not vk_ok:
                self._record_failed_access(ctx.user_id, "mute-vk-api-failed")
                self.send(
                    ctx.peer_id,
                    f"⚠️ Локальный мут установлен, но не удалось применить ограничение в беседе для {self._fmt_user(target)}.",
                )
                return
            until_dt = self._fmt_msk_dt(until)
            self.send(
                ctx.peer_id,
                f"✅ Пользователю {self._fmt_user(target)} был выдан мут до {until_dt}. Причина: {reason}.",
            )
            return

        if cmd == "!размут":
            with self.db.conn() as c:
                c.execute("UPDATE chat_members SET muted_until=NULL WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                c.execute(
                    "UPDATE mute_logs SET active=0 WHERE chat_id=? AND vk_id=? AND active=1",
                    (ctx.chat_id, target),
                )
            try:
                vk_ok = self._apply_vk_unmute(ctx.peer_id, target)
            except Exception:
                vk_ok = False
            if not vk_ok:
                self._record_failed_access(ctx.user_id, "unmute-vk-api-failed")
                self.send(
                    ctx.peer_id,
                    f"⚠️ Внутренний мут снят, но VK ограничение у {self._fmt_user(target)} могло остаться. Проверьте права бота.",
                )
                return
            self.send(ctx.peer_id, f"✅ Мут снят с {self._fmt_user(target)}.")
            return

        if cmd == "!пред":
            if len(parts) < 3 and ctx.reply_user_id is None:
                self.send(ctx.peer_id, "Формат: !пред (пользователь) (причина) или ответом на сообщение.")
                return
            reason = " ".join(parts[reason_start:]).strip() or "без причины"
            with self.db.conn() as c:
                c.execute(
                    "INSERT INTO warning_logs(chat_id,vk_id,issuer_id,reason,created_at) VALUES(?,?,?,?,?)",
                    (ctx.chat_id, target, ctx.user_id, reason, ts),
                )
                warns = c.execute(
                    "SELECT COUNT(*) FROM warning_logs WHERE chat_id=? AND vk_id=?",
                    (ctx.chat_id, target),
                ).fetchone()[0]
                if warns >= 3:
                    c.execute("UPDATE chat_members SET banned=1 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                    c.execute(
                        "INSERT INTO ban_logs(chat_id,vk_id,issuer_id,reason,created_at,active) VALUES(?,?,?,?,?,1)",
                        (ctx.chat_id, target, ctx.user_id, "Автобан: 3/3 предупреждений", ts),
                    )
                    try:
                        self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=target)
                    except Exception:
                        pass
                    self.send(ctx.peer_id, f"⛔ У пользователя {self._fmt_user(target)} 3/3 преда. Он забанен.")
                    return
            self.send(
                ctx.peer_id,
                f"✅ Пользователю {self._fmt_user(target)} выдан пред. Причина: {reason}. Теперь предупреждений: {warns}/3.",
            )
            return

        if cmd == "!снятьпред":
            with self.db.conn() as c:
                last = c.execute(
                    "SELECT id FROM warning_logs WHERE chat_id=? AND vk_id=? ORDER BY created_at DESC LIMIT 1",
                    (ctx.chat_id, target),
                ).fetchone()
                if last:
                    c.execute("DELETE FROM warning_logs WHERE id=?", (last["id"],))
                warns = c.execute(
                    "SELECT COUNT(*) FROM warning_logs WHERE chat_id=? AND vk_id=?",
                    (ctx.chat_id, target),
                ).fetchone()[0]
            self.send(ctx.peer_id, f"✅ Пред снят. У пользователя {self._fmt_user(target)}: {warns}/3")

    def _lists(self, ctx: Ctx, cmd: str) -> None:
        with self.db.conn() as c:
            if cmd == "!банлист":
                rows = c.execute(
                    """
                    SELECT b.vk_id, b.created_at, b.issuer_id, b.reason
                    FROM ban_logs b
                    WHERE b.chat_id=? AND b.active=1
                    ORDER BY b.created_at DESC
                    """,
                    (ctx.chat_id,),
                ).fetchall()
                if not rows:
                    self.send(ctx.peer_id, "📋 Банлист:\nпусто")
                    return
                out = ["📋 Банлист:"]
                for r in rows:
                    dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d.%m.%Y %H:%M")
                    out.append(
                        f"• {self._fmt_user(int(r['vk_id']))} — {dt} — кем: {self._fmt_user(int(r['issuer_id']))} — причина: {r['reason'] or 'без причины'}"
                    )
                self.send(ctx.peer_id, "\n".join(out))
            elif cmd == "!мутлист":
                rows = c.execute(
                    """
                    SELECT m.vk_id, m.issuer_id, m.reason, m.until_ts
                    FROM mute_logs m
                    WHERE m.chat_id=? AND m.active=1
                    ORDER BY m.created_at DESC
                    """,
                    (ctx.chat_id,),
                ).fetchall()
                if not rows:
                    self.send(ctx.peer_id, "📋 Мутлист:\nпусто")
                    return
                out = ["📋 Мутлист:"]
                for r in rows:
                    until = datetime.fromtimestamp(int(r["until_ts"])).strftime("%d.%m.%Y %H:%M") if r["until_ts"] else "без срока"
                    out.append(
                        f"• {self._fmt_user(int(r['vk_id']))} — кем: {self._fmt_user(int(r['issuer_id']))} — причина: {r['reason'] or 'без причины'} — до: {until}"
                    )
                self.send(ctx.peer_id, "\n".join(out))
            elif cmd == "!списокпредов":
                rows = c.execute(
                    """
                    SELECT w.vk_id, COUNT(*) AS warns
                    FROM warning_logs w
                    WHERE w.chat_id=?
                    GROUP BY w.vk_id
                    ORDER BY warns DESC
                    """,
                    (ctx.chat_id,),
                ).fetchall()
                if not rows:
                    self.send(ctx.peer_id, "📋 Список предупреждений:\nпусто")
                    return
                out = ["📋 Список предупреждений:"]
                for r in rows:
                    last = c.execute(
                        """
                        SELECT issuer_id, reason
                        FROM warning_logs
                        WHERE chat_id=? AND vk_id=?
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (ctx.chat_id, int(r["vk_id"])),
                    ).fetchone()
                    issuer = self._fmt_user(int(last["issuer_id"])) if last else "неизвестно"
                    reason = last["reason"] if last else "без причины"
                    out.append(
                        f"• {self._fmt_user(int(r['vk_id']))} — {int(r['warns'])} пред(ов) — причина: {reason} — кто выдал: {issuer}"
                    )
                self.send(ctx.peer_id, "\n".join(out))

    def _immunity(self, ctx: Ctx, parts: list[str], cmd: str) -> None:
        if cmd == "!иммунитеты":
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT vk_id,immunity_level FROM chat_members WHERE chat_id=? AND immunity_level>0 ORDER BY immunity_level DESC",
                    (ctx.chat_id,),
                ).fetchall()
            self.send(ctx.peer_id, "📋 Иммунитеты:\n" + ("\n".join([f"• {self._fmt_user(int(r['vk_id']))} — {r['immunity_level']}" for r in rows]) if rows else "пусто"))
            return

        if len(parts) < 2:
            self.send(ctx.peer_id, "Формат: !иммунитет (пользователь) (роль) / !снятьиммунитет (пользователь)")
            return

        target = self._parse_user(parts[1])
        if target is None:
            self.send(ctx.peer_id, "❌ Не удалось распознать пользователя.")
            return

        with self.db.conn() as c:
            self._ensure_member(ctx.chat_id, target)
            if cmd == "!снятьиммунитет":
                c.execute("UPDATE chat_members SET immunity_level=0 WHERE chat_id=? AND vk_id=?", (ctx.chat_id, target))
                self.send(ctx.peer_id, f"✅ Иммунитет пользователя {self._fmt_user(target)} снят.")
                return
            if len(parts) < 3 or not parts[2].isdigit():
                self.send(ctx.peer_id, "Формат: !иммунитет (пользователь) (роль)")
                return
            imm = int(parts[2])
            c.execute("UPDATE chat_members SET immunity_level=? WHERE chat_id=? AND vk_id=?", (imm, ctx.chat_id, target))
            self.send(ctx.peer_id, f"✅ Иммунитет пользователя {self._fmt_user(target)} = {imm}")

    def _notes(self, ctx: Ctx, parts: list[str]) -> None:
        def _extract_name(start_idx: int) -> tuple[Optional[str], int]:
            if len(parts) <= start_idx:
                return None, start_idx
            first = parts[start_idx]
            if first.startswith("("):
                collected = [first]
                i = start_idx + 1
                while i < len(parts) and not parts[i].endswith(")"):
                    collected.append(parts[i])
                    i += 1
                if i < len(parts):
                    collected.append(parts[i])
                    raw = " ".join(collected).strip()
                    name = raw[1:-1].strip() if raw.startswith("(") and raw.endswith(")") else raw
                    return (name or None), i + 1
                return None, len(parts)
            return first, start_idx + 1

        # !заметки
        if parts[0].lower() == "!заметки" and len(parts) == 1:
            with self.db.conn() as c:
                rows = c.execute("SELECT name,min_role FROM notes WHERE chat_id=? ORDER BY name", (ctx.chat_id,)).fetchall()
            self.send(ctx.peer_id, "📒 Заметки:\n" + ("\n".join([f"• {r['name']} (от {r['min_role']})" for r in rows]) if rows else "пусто"))
            return
        if parts[0].lower() == "!заметки" and len(parts) > 1:
            parts = ["!заметка"] + parts[1:]

 
        # !заметка создать (название) / ответом на сообщение
        if len(parts) >= 3 and parts[1].lower() == "создать":
            raw_rest = re.sub(r"^!\s*замет(?:ка|ки)\s+создать\s+", "", ctx.text, flags=re.I | re.S).strip()
            name = raw_rest.split(maxsplit=1)[0].strip() if raw_rest else None
            if not name:
                self.send(ctx.peer_id, "❌ Не удалось распознать название заметки.")
                return
            if " " in name:
                self.send(ctx.peer_id, "❌ Название заметки должно быть одним словом.")
                return
            content = self._compose_reply_note_content(ctx) or ""
            attachment_ids = (ctx.reply_attachment_ids or [])[:10]
            source_message_id = int(ctx.reply_message_id or 0)

            if not source_message_id and not content and not attachment_ids:
                self.send(ctx.peer_id, "Формат: !новая заметка (название) только ответом на сообщение с текстом/вложениями")
                return

            with self.db.conn() as c:
                old = self._find_note_by_name(ctx.chat_id, name)
                stored_name = old["name"] if old else name
                min_role = int(old["min_role"]) if old else 0
                existing_attachments = (old["attachments"] if old else None) or ""
                stored_attachments = ",".join(attachment_ids) if attachment_ids else existing_attachments
                c.execute(
                    "INSERT OR REPLACE INTO notes(chat_id,name,content,attachments,source_message_id,min_role) VALUES(?,?,?,?,?,?)",
                    (ctx.chat_id, stored_name, content, stored_attachments, source_message_id or None, min_role),
                )

            self.send(ctx.peer_id, f"✅ Заметка «{name}» создана.")
            return

        # !заметка удалить (название)
        if len(parts) >= 3 and parts[1].lower() == "удалить":
            name, _ = _extract_name(2)
            if not name:
                self.send(ctx.peer_id, "❌ Не удалось распознать название заметки.")
                return
            old = self._find_note_by_name(ctx.chat_id, name)
            if not old:
                self.send(ctx.peer_id, "❌ Заметка не найдена.")
                return
            with self.db.conn() as c:
                c.execute("DELETE FROM notes WHERE chat_id=? AND name=?", (ctx.chat_id, old["name"]))
            self.send(ctx.peer_id, f"✅ Заметка «{name}» удалена.")
            return

        # !заметка роль (название) (роль)
        if len(parts) >= 4 and parts[1].lower() == "роль":
            name, lvl_idx = _extract_name(2)
            if not name or lvl_idx >= len(parts) or not parts[lvl_idx].isdigit():
                self.send(ctx.peer_id, "Формат: !заметка роль (название) (роль)")
                return
            lvl = int(parts[lvl_idx])
            old = self._find_note_by_name(ctx.chat_id, name)
            if not old:
                self.send(ctx.peer_id, "❌ Заметка не найдена.")
                return
            with self.db.conn() as c:
                c.execute("UPDATE notes SET min_role=? WHERE chat_id=? AND name=?", (lvl, ctx.chat_id, old["name"]))
            self.send(ctx.peer_id, f"✅ Доступ к заметке «{name}» теперь от роли {lvl}.")
            return

        # !заметка (название)
        if len(parts) >= 2:
            name, _ = _extract_name(1)
            if not name:
                self.send(ctx.peer_id, "❌ Не удалось распознать название заметки.")
                return
            row = self._find_note_by_name(ctx.chat_id, name, with_content=True)
            if not row:
                self.send(ctx.peer_id, "❌ Заметка не найдена.")
                return
            role = self._get_role_level(ctx.chat_id, ctx.user_id)
            if role < int(row["min_role"]):
                self.send(ctx.peer_id, "⛔ Недостаточно прав для просмотра заметки.")
                return
            source_message_id = int(row["source_message_id"] or 0)

            if source_message_id:
                try:
                    self.api.messages.send(
                        peer_id=ctx.peer_id,
                        random_id=random.randint(1, 2_147_483_647),
                        message=f"📌 {row['name']}",
                        forward_messages=str(source_message_id),
                        disable_mentions=1,
                    )
                    return
                except Exception as e:
                    logger.error(f"Не удалось отправить заметку forward_messages={source_message_id}: {e}")


            attachments = [a for a in (row["attachments"] or "").split(",") if a]
            body = row["content"] or "(без текста)"
            self.send_with_attachments(ctx.peer_id, f"📌 {row['name']}\n{body}", attachments)
            return

    def _bookmarks(self, ctx: Ctx, parts: list[str]) -> None:
        if parts[0].lower() == "!закладки":
            with self.db.conn() as c:
                rows = c.execute(
                    "SELECT name,message_link FROM message_bookmarks WHERE chat_id=? ORDER BY id ASC",
                    (ctx.chat_id,),
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "🔖 Закладки:\nпусто")
                return
            lines = ["🔖 Закладки:"]
            for idx, row in enumerate(rows, start=1):
                lines.append(f"{idx}. {row['name']} — {row['message_link']}")
            self.send(ctx.peer_id, "\n".join(lines))
            return
        if parts[0].lower() == "!закладка":
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !закладка (название) ответом на сообщение")
                return
            if not ctx.is_chat or not ctx.reply_cmid:
                self.send(ctx.peer_id, "❌ Команда работает только в чате и только ответом на сообщение.")
                return
            name = " ".join(parts[1:]).strip()
            link = f"https://vk.com/im?sel={ctx.peer_id}&msgid={ctx.reply_cmid}"
            with self.db.conn() as c:
                c.execute(
                    """
                    INSERT INTO message_bookmarks(chat_id,name,message_link,creator_id,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(chat_id,name) DO UPDATE SET
                        message_link=excluded.message_link,
                        creator_id=excluded.creator_id,
                        created_at=excluded.created_at
                    """,
                    (ctx.chat_id, name, link, ctx.user_id, self.now_ts()),
                )
            self.send(ctx.peer_id, f"✅ Закладка «{name}» сохранена: {link}")
            return

    # ---------- lifecycle ----------

    def control_pipe_listener(self) -> None:
        try:
            if not os.path.exists(CONTROL_PIPE_PATH):
                os.mkfifo(CONTROL_PIPE_PATH)
        except FileExistsError:
            pass
        except Exception:
            return

        while self.running:
            try:
                with open(CONTROL_PIPE_PATH, "r", encoding="utf-8") as pipe:
                    line = pipe.readline().strip().lower()
            except Exception:
                time.sleep(1)
                continue
            if line == "stopping":
                logger.info("Получена команда stopping из control pipe. Останавливаю бота...")
                self.running = False
                break

    def _safe_handle_command(self, ctx: "Ctx") -> None:
        """Обёртка handle_command для ThreadPoolExecutor — ловит все исключения."""
        try:
            self.handle_command(ctx)
        except Exception as e:
            logger.error(f"Ошибка в потоке обработки команды '{(ctx.text or '')[:40]}': {e}")

    def _submit_command_ctx(self, ctx: "Ctx") -> None:
        if not self._cmd_queue_slots.acquire(blocking=False):
            return
        try:
            fut = self._cmd_executor.submit(self._safe_handle_command, ctx)
            fut.add_done_callback(lambda _f: self._cmd_queue_slots.release())
        except Exception:
            self._cmd_queue_slots.release()
            raise

    # ── Security helpers ──────────────────────────────────────────────────────

    def _alert_senior_admin(self, message: str) -> None:
        """Критическое уведомление старшему админу в ЛС VK."""
        try:
            senior_id = int(self.db.senior_admin_id)
            self.api.messages.send(
                peer_id=senior_id,
                random_id=0,
                message=f"🚨 СИСТЕМНОЕ УВЕДОМЛЕНИЕ\n{message}",
                disable_mentions=1,
            )
            logger.warning(f"Alert→senior_admin({senior_id}): {message[:80]}")
        except Exception as e:
            logger.error(f"_alert_senior_admin failed: {e}")

    def _security_audit_loop(self) -> None:
        """Фоновый аудит безопасности каждые 60 минут."""
        time.sleep(300)  # первая проверка через 5 мин после старта
        while self.running:
            try:
                senior_id = int(self.db.senior_admin_id)
                with self.db.conn() as c:
                    # 1. Чужие пользователи с admin_level >= 100
                    rogue = c.execute(
                        "SELECT vk_id, admin_level FROM users WHERE admin_level >= 100 AND vk_id != ?",
                        (senior_id,),
                    ).fetchall()
                    if rogue:
                        names = ", ".join(f"id{r['vk_id']}(lvl={r['admin_level']})" for r in rogue)
                        logger.critical(f"SECURITY: ТРЕТИЙ+ пользователь с admin>=100: {names}")
                        self._alert_senior_admin(
                            f"🚨 ТРЕВОГА: обнаружен пользователь(и) с admin_level 100+\n"
                            f"(кроме вас, старшего админа):\n{names}\n\n"
                            "Возможно несанкционированное повышение прав! Проверьте немедленно.\n"
                            "Для снятия прав: !админ права (пользователь) 0"
                        )
                    # 2. Целостность записи старшего админа
                    sa = c.execute(
                        "SELECT admin_level, bot_ban FROM users WHERE vk_id=?", (senior_id,)
                    ).fetchone()
                    if sa:
                        if int(sa["admin_level"]) != 100:
                            logger.critical(f"SECURITY: senior admin_level tampered → {sa['admin_level']}")
                            c.execute("UPDATE users SET admin_level=100 WHERE vk_id=?", (senior_id,))
                            self._alert_senior_admin(
                                f"🚨 Уровень старшего админа изменён на {sa['admin_level']}! Восстановлено до 100."
                            )
                        if int(sa["bot_ban"] or 0) == 1:
                            logger.critical("SECURITY: senior admin got bot_ban=1")
                            c.execute("UPDATE users SET bot_ban=0 WHERE vk_id=?", (senior_id,))
                            self._alert_senior_admin("🚨 Старший админ получил ботбан — снято автоматически.")
                    # 3. Аномальная активность
                    total_failed = sum(len(dq) for dq in self.failed_access_window.values())
                    if total_failed > 100:
                        logger.warning(f"SECURITY: high failed_access_window: {total_failed}")
                        self._alert_senior_admin(
                            f"⚠️ Аномальная активность: {total_failed} неудачных попыток доступа за 5 минут."
                        )
            except Exception as e:
                logger.error(f"_security_audit_loop error: {e}")
            time.sleep(3600)

    def _background_cleanup(self) -> None:
        """Фоновая задача: чистит просроченные записи каждые 30 минут."""
        while self.running:
            try:
                time.sleep(1800)
                cutoff_48h = self.now_ts() - 172800
                cutoff_24h = self.now_ts() - 86400
                with self.db.conn() as c:
                    c.execute("DELETE FROM hire_offers WHERE created_at<?", (cutoff_48h,))
                    c.execute("DELETE FROM resign_requests WHERE created_at<?", (cutoff_24h,))
                    c.execute("DELETE FROM registration_sessions WHERE 1=1 AND ROWID NOT IN (SELECT ROWID FROM registration_sessions ORDER BY ROWID DESC LIMIT 500)")
            except Exception as e:
                logger.error(f"Ошибка фоновой очистки: {e}")

    def run(self) -> None:
        logger.info("Запущен VK community bot.")
        if CONTROL_PIPE_ENABLED:
            logger.info(f"Остановка: echo stopping > {CONTROL_PIPE_PATH}")
            threading.Thread(target=self.control_pipe_listener, daemon=True).start()
        else:
            logger.info("Control pipe disabled. Set CONTROL_PIPE_ENABLED=1 to enable local stop pipe.")
        threading.Thread(target=self._background_cleanup, daemon=True).start()
        threading.Thread(target=self._security_audit_loop, daemon=True).start()
        threading.Thread(target=self._subscription_poll_loop, daemon=True).start()
        if self.tg_token:
            logger.info("Telegram bridge enabled.")
            threading.Thread(target=self.telegram_listener, daemon=True).start()

        while self.running:
            try:
                for event in self.longpoll.listen():
                    if not self.running:
                        break
                    if event.type == VkBotEventType.MESSAGE_EVENT:
                        obj = event.object or {}
                        raw_payload = obj.get("payload") or {}
                        # Защита: payload не должен быть слишком большим
                        if isinstance(raw_payload, str):
                            if len(raw_payload) > 2048:
                                continue
                            try:
                                raw_payload = json.loads(raw_payload)
                            except Exception:
                                raw_payload = {}
                        if not isinstance(raw_payload, dict):
                            raw_payload = {}
                        payload = raw_payload
                        cmd = str(payload.get("cmd", "")).lower()[:64]  # limit cmd length
                        req_id = int(payload.get("id", 0) or 0)
                        user_id = int(obj.get("user_id", 0) or 0)
                        peer_id = int(obj.get("peer_id", user_id) or user_id)
                        # ── регистрация: одобрить/отклонить ──────────────────
                        if cmd in {"approve", "reject"} and req_id > 0 and user_id > 0:
                            result = self._approve_reject_by_id(user_id, peer_id, req_id, approve=(cmd == "approve"), actor_platform="vk")
                            self.send(peer_id, result)
                            try:
                                self._api_method(
                                    "messages.sendMessageEventAnswer",
                                    {
                                        "event_id": obj.get("event_id"),
                                        "user_id": user_id,
                                        "peer_id": peer_id,
                                        "event_data": json.dumps({"type": "show_snackbar", "text": result[:80]}, ensure_ascii=False),
                                    },
                                )
                            except Exception:
                                pass

                        # ── синхронизация аккаунта ───────────────────────────
                        elif cmd == "sync_approve" and req_id > 0 and user_id > 0:
                            result = self._approve_sync_request(user_id, req_id)
                            self.send(peer_id, result)

                        # ── увольнение: пользователь нажал Да ───────────────
                        elif cmd == "resign_confirm" and user_id > 0:
                            press_uid = int(payload.get("uid", user_id))
                            if press_uid != user_id:
                                pass  # Чужую кнопку игнорируем
                            else:
                                with self.db.conn() as c:
                                    rr = c.execute(
                                        "SELECT * FROM resign_requests WHERE user_id=? AND step='user_confirm'",
                                        (user_id,),
                                    ).fetchone()
                                if rr:
                                    leader_id = rr["leader_id"]
                                    # Обновляем step
                                    with self.db.conn() as c:
                                        c.execute(
                                            "UPDATE resign_requests SET step='leader_confirm' WHERE user_id=?",
                                            (user_id,),
                                        )
                                    rr_id = rr["id"]
                                    if leader_id:
                                        ok = self._send_resign_leader_confirm(leader_id, user_id, rr["reason"], rr_id)
                                        if ok:
                                            self.send(user_id, "✅ Запрос на увольнение отправлен лидеру вашей фракции. Ожидайте подтверждения.")
                                        else:
                                            # Лидера нет / не доступен — увольняем сразу
                                            result = self._do_resign(user_id)
                                            self.send(user_id, f"ℹ️ Лидер недоступен. {result}")
                                    else:
                                        # Лидер не назначен — увольняем автоматически
                                        result = self._do_resign(user_id)
                                        self.send(user_id, f"ℹ️ Лидер фракции не назначен. {result}")
                                else:
                                    self.send(user_id, "❌ Запрос на увольнение не найден или уже обработан.")

                        # ── увольнение: пользователь нажал Нет (отмена) ─────
                        elif cmd == "resign_cancel" and user_id > 0:
                            press_uid = int(payload.get("uid", user_id))
                            if press_uid == user_id:
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM resign_requests WHERE user_id=?", (user_id,))
                                self.send(user_id, "✅ Увольнение отменено.")

                        # ── увольнение: лидер подтвердил ────────────────────
                        elif cmd == "resign_leader_yes" and req_id > 0 and user_id > 0:
                            with self.db.conn() as c:
                                rr = c.execute(
                                    "SELECT * FROM resign_requests WHERE id=? AND step='leader_confirm'",
                                    (req_id,),
                                ).fetchone()
                            if not rr:
                                self.send(peer_id, "❌ Запрос не найден или уже обработан.")
                            elif rr["leader_id"] and int(rr["leader_id"]) != user_id:
                                self.send(peer_id, "⛔ Подтверждать может только лидер этой фракции.")
                            else:
                                result = self._do_resign(int(rr["user_id"]))
                                self.send(peer_id, result)

                        # ── увольнение: лидер отказал ───────────────────────
                        elif cmd == "resign_leader_no" and req_id > 0 and user_id > 0:
                            with self.db.conn() as c:
                                rr = c.execute(
                                    "SELECT * FROM resign_requests WHERE id=? AND step='leader_confirm'",
                                    (req_id,),
                                ).fetchone()
                            if not rr:
                                self.send(peer_id, "❌ Запрос не найден или уже обработан.")
                            elif rr["leader_id"] and int(rr["leader_id"]) != user_id:
                                self.send(peer_id, "⛔ Отклонять может только лидер этой фракции.")
                            else:
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM resign_requests WHERE id=?", (req_id,))
                                try:
                                    self.send_dm(int(rr["user_id"]), "❌ Ваш запрос на увольнение отклонён лидером.")
                                except Exception:
                                    pass
                                self.send(peer_id, f"✅ Запрос на увольнение от id{rr['user_id']} отклонён.")

                        # ══ Найм: предложение ════════════════════════════════
                        elif cmd == "hire_accept" and req_id > 0 and user_id > 0:
                            # Целевой пользователь принял оффер
                            with self.db.conn() as c:
                                offer = c.execute(
                                    "SELECT * FROM hire_offers WHERE id=? AND step='user_confirm'",
                                    (req_id,),
                                ).fetchone()
                            if not offer or int(offer["target_user_id"]) != user_id:
                                self.send(peer_id, "❌ Предложение не найдено или уже обработано.")
                            else:
                                old_fac = str(offer["old_faction"] or "")
                                old_srv = int(offer["old_server_id"] or 1)
                                new_fac = str(offer["faction"])
                                new_pos = str(offer["position_name"])
                                new_srv = int(offer["server_id"])
                                actor_id = int(offer["actor_user_id"])
                                # Применяем найм
                                with self.db.conn() as c:
                                    c.execute(
                                        "UPDATE users SET faction=?, position=?, server_id=? WHERE vk_id=?",
                                        (new_fac, new_pos, new_srv, user_id),
                                    )
                                    c.execute("UPDATE hire_offers SET step='done' WHERE id=?", (req_id,))
                                self.send(peer_id, f"✅ Вы приняты в {new_fac} на должность «{new_pos}»!")
                                # Уведомляем нанявшего
                                try:
                                    cached = self.user_name_cache.get(user_id)
                                    name = cached[0] if cached else f"id{user_id}"
                                    self.send_dm(actor_id, f"✅ [id{user_id}|{name}] принял предложение и нанят в {new_fac} на должность «{new_pos}».")
                                except Exception:
                                    pass
                                # Если у пользователя была старая фракция — спрашиваем про выход из чатов
                                if old_fac and old_fac != new_fac and old_fac not in {"", "не указана"}:
                                    self._send_hire_old_chats_question(user_id, req_id, old_fac, old_srv)
                                    # Уведомляем лидера старой фракции
                                    with self.db.conn() as c:
                                        old_leader = c.execute(
                                            "SELECT vk_id FROM leaders WHERE faction=? AND server_id=? LIMIT 1",
                                            (old_fac, old_srv),
                                        ).fetchone()
                                    if old_leader:
                                        self._send_leader_kick_question(int(old_leader["vk_id"]), user_id, req_id, old_fac, old_srv)

                        elif cmd == "hire_reject" and req_id > 0 and user_id > 0:
                            with self.db.conn() as c:
                                offer = c.execute(
                                    "SELECT * FROM hire_offers WHERE id=? AND step='user_confirm'",
                                    (req_id,),
                                ).fetchone()
                            if not offer or int(offer["target_user_id"]) != user_id:
                                self.send(peer_id, "❌ Предложение не найдено или уже обработано.")
                            else:
                                actor_id = int(offer["actor_user_id"])
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM hire_offers WHERE id=?", (req_id,))
                                self.send(peer_id, "✅ Вы отклонили предложение о найме.")
                                try:
                                    cached = self.user_name_cache.get(user_id)
                                    name = cached[0] if cached else f"id{user_id}"
                                    self.send_dm(actor_id, f"❌ [id{user_id}|{name}] отклонил предложение о найме в {offer['faction']}.")
                                except Exception:
                                    pass

                        elif cmd == "hire_kick_old" and req_id > 0 and user_id > 0:
                            # Пользователь согласился выйти из старых чатов
                            with self.db.conn() as c:
                                offer = c.execute("SELECT * FROM hire_offers WHERE id=?", (req_id,)).fetchone()
                            if offer and int(offer["target_user_id"]) == user_id:
                                old_fac = str(offer["old_faction"])
                                old_srv = int(offer["old_server_id"])
                                kicked = self._kick_from_faction_chats(user_id, old_fac, old_srv)
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM hire_offers WHERE id=?", (req_id,))
                                self.send(peer_id, f"✅ Вы удалены из {kicked} чатов фракции {old_fac}.")
                            else:
                                self.send(peer_id, "❌ Запрос не найден.")

                        elif cmd == "hire_keep_old" and req_id > 0 and user_id > 0:
                            with self.db.conn() as c:
                                c.execute("DELETE FROM hire_offers WHERE id=? AND target_user_id=?", (req_id, user_id))
                            self.send(peer_id, "✅ Вы остались в старых чатах.")

                        elif cmd == "leader_kick_yes" and user_id > 0:
                            # Лидер решил кикнуть уволившегося
                            fired_uid = int(payload.get("uid", 0))
                            fired_srv = int(payload.get("srv", 1))
                            fired_fac = str(payload.get("fac", ""))
                            if fired_uid > 0 and fired_fac:
                                if (
                                    not self._is_senior_admin_ctx(Ctx(user_id=user_id, peer_id=peer_id, text="", platform="vk"))
                                    and self._get_admin_level(user_id) < 50
                                    and not self._is_leader_for(user_id, fired_fac, fired_srv)
                                ):
                                    self.send(peer_id, "⛔ Недостаточно прав для удаления из чатов этой фракции.")
                                    continue
                                kicked = self._kick_from_faction_chats(fired_uid, fired_fac, fired_srv)
                                self.send(peer_id, f"✅ Пользователь id{fired_uid} удалён из {kicked} чатов фракции {fired_fac}.")
                            else:
                                self.send(peer_id, "❌ Недостаточно данных для кика.")

                        elif cmd == "leader_kick_no" and user_id > 0:
                            self.send(peer_id, "✅ Ладно, пользователь остаётся в чатах.")

                        # ── ДЖ в роли ════════════════════════════════════════
                        elif cmd == "dj_to_roles":
                            chat_id_cb = int(payload.get("chat_id", 0))
                            faction_cb = str(payload.get("faction", ""))
                            server_cb = int(payload.get("server_id", 1))
                            if not chat_id_cb or not faction_cb:
                                self.send(peer_id, "❌ Недостаточно данных.")
                            elif self._get_admin_level(user_id) < 70 and not self._is_leader_for(user_id, faction_cb, server_cb):
                                self.send(peer_id, "⛔ Недостаточно прав для синхронизации ДЖ в роли.")
                            else:
                                positions = self._dj_get_positions(faction_cb, server_cb)
                                if not positions:
                                    self.send(peer_id, "❌ ДЖ список пуст. Добавьте должности через !дж новая.")
                                else:
                                    # Загружаем сотрудников
                                    with self.db.conn() as c:
                                        staff_rows = c.execute(
                                            "SELECT vk_id, position FROM users "
                                            "WHERE faction=? AND server_id=? AND approved=1 "
                                            "AND TRIM(COALESCE(position,''))!='' AND LOWER(COALESCE(position,''))!='не указана'",
                                            (faction_cb, server_cb),
                                        ).fetchall()
                                    staff_rows = list(staff_rows)

                                    # Строим маппинг: lower(должность) → уровень из ДЖ
                                    pos_to_level = {p["name"].strip().lower(): p["level"] for p in positions}
                                    # Группируем по должности
                                    staff_by_pos: dict[str, list[int]] = {}
                                    for s in staff_rows:
                                        key = str(s["position"] or "").strip().lower()
                                        staff_by_pos.setdefault(key, []).append(int(s["vk_id"]))

                                    with self.db.conn() as c:
                                        # 1. Сбрасываем все роли в этом чате
                                        c.execute("UPDATE chat_members SET role_level=0 WHERE chat_id=?", (chat_id_cb,))
                                        # 2. Удаляем старые роли и создаём из ДЖ
                                        c.execute("DELETE FROM chat_roles WHERE chat_id=?", (chat_id_cb,))
                                        for p in positions:
                                            c.execute(
                                                "INSERT OR REPLACE INTO chat_roles(chat_id, level, name) VALUES(?,?,?)",
                                                (chat_id_cb, p["level"], p["name"]),
                                            )
                                        # 3. Назначаем роли сотрудникам — UPSERT в chat_members
                                        applied = 0
                                        for pos_key, uids in staff_by_pos.items():
                                            level = pos_to_level.get(pos_key)
                                            if level is None:
                                                continue  # должность не в ДЖ — пропускаем
                                            for uid in uids:
                                                # UPSERT: если записи нет — создаём, если есть — обновляем
                                                c.execute(
                                                    "INSERT INTO chat_members(chat_id, vk_id, role_level, immunity_level, banned) "
                                                    "VALUES(?,?,?,0,0) "
                                                    "ON CONFLICT(chat_id,vk_id) DO UPDATE SET role_level=excluded.role_level",
                                                    (chat_id_cb, uid, level),
                                                )
                                                applied += 1

                                    self.send(peer_id,
                                        f"✅ Синхронизация завершена.\n"
                                        f"Ролей создано: {len(positions)}\n"
                                        f"Сотрудников назначено: {applied}"
                                    )

                        elif cmd == "dj_to_roles_cancel":
                            self.send(peer_id, "✅ Отменено.")

                        continue
                    if event.type != VkBotEventType.MESSAGE_NEW:
                        continue
                    msg = event.object.get("message", {})
                    if not msg:
                        continue
                    user_id = int(msg.get("from_id", 0))
                    if user_id <= 0:
                        continue
                    reply = msg.get("reply_message") or {}
                    if not reply:
                        fwd_list = msg.get("fwd_messages") or []
                        if fwd_list:
                            reply = fwd_list[0]
                    own_types, own_ids = self._collect_attachment_ids(msg.get("attachments", []) or [])
                    reply_types, reply_ids = self._collect_attachment_ids(reply.get("attachments", []) or [])
                    reply_attachments = own_types + reply_types
                    reply_attachment_ids = list(dict.fromkeys(own_ids + reply_ids))
                    reply_link_urls = list(dict.fromkeys(
                        self._extract_link_urls(msg.get("attachments", []) or [])
                        + self._extract_link_urls(reply.get("attachments", []) or [])
                    ))
                    ctx = Ctx(
                        user_id=user_id,
                        peer_id=int(msg.get("peer_id", user_id)),
                        text=(msg.get("text") or "").strip(),
                        message_cmid=msg.get("conversation_message_id"),
                        message_id=msg.get("id"),
                        reply_user_id=reply.get("from_id"),
                        reply_text=reply.get("text"),
                        reply_cmid=reply.get("conversation_message_id"),
                        reply_message_id=reply.get("id"),
                        reply_attachments=reply_attachments,
                        reply_attachment_ids=reply_attachment_ids,
                        reply_link_urls=reply_link_urls,
                        platform="vk",
                    )
                    self.peer_routes[ctx.peer_id] = ("vk", ctx.peer_id)

                    # Авто-контроль mute/ban в чатах
                    if ctx.is_chat:
                        action = msg.get("action") or {}
                        action_type = str(action.get("type") or "")
                        invited_id = int(action.get("member_id") or 0)
                        if action_type in {"chat_invite_user", "chat_invite_user_by_link"} and invited_id > 0:
                            self._ensure_member(ctx.chat_id, invited_id)
                            invited_member = self._member(ctx.chat_id, invited_id)
                            if invited_member and int(invited_member["banned"] or 0) == 1:
                                try:
                                    self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=invited_id)
                                except Exception:
                                    pass
                                self.send(
                                    ctx.peer_id,
                                    f"🚫 Пользователь {self._fmt_user(invited_id)} находится в бане этого чата,\n"
                                    "поэтому был автоматически удалён из беседы.",
                                )
                                continue

                            # Проверка прав на приглашение: кто добавил (user_id)?
                            with self.db.conn() as c:
                                invite_cfg = c.execute(
                                    "SELECT min_role, min_admin FROM chat_invite_level WHERE chat_id=?",
                                    (ctx.chat_id,),
                                ).fetchone()
                            if invite_cfg:
                                req_role = int(invite_cfg["min_role"])
                                req_adm = int(invite_cfg["min_admin"])
                                inviter_role = self._get_role_level(ctx.chat_id, user_id)
                                inviter_adm = self._get_admin_level(user_id)
                                has_invite_right = (
                                    inviter_adm >= req_adm > 0
                                    or inviter_role >= req_role
                                    or self._is_senior_admin_ctx(ctx)
                                    or int(user_id) == int(self.db.senior_admin_id)
                                )
                                if not has_invite_right:
                                    try:
                                        self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=invited_id)
                                    except Exception:
                                        pass
                                    self.send(
                                        ctx.peer_id,
                                        f"⛔ {self._fmt_user(user_id)} недостаточный уровень прав на приглашение "
                                        f"(нужна роль {req_role}+). {self._fmt_user(invited_id)} удалён из беседы.",
                                    )
                                    continue
                        self._ensure_member(ctx.chat_id, user_id)
                        m = self._member(ctx.chat_id, user_id)
                        if m and int(m["banned"] or 0) == 1:
                            try:
                                self.api.messages.removeChatUser(chat_id=ctx.chat_id, member_id=user_id)
                            except Exception:
                                pass
                            self.send(
                                ctx.peer_id,
                                f"🚫 Пользователь {self._fmt_user(user_id)} находится в бане этого чата,\n"
                                "поэтому был автоматически удалён из беседы.",
                            )
                            continue
                        if m and m["muted_until"] and int(m["muted_until"]) > self.now_ts():
                            cmid = msg.get("conversation_message_id")
                            if cmid is not None:
                                self._api_method(
                                    "messages.delete",
                                    {"peer_id": ctx.peer_id, "cmids": cmid, "delete_for_all": 1},
                                )
                            continue
                        if m and m["muted_until"] and int(m["muted_until"]) <= self.now_ts():
                            with self.db.conn() as c:
                                c.execute("UPDATE chat_members SET muted_until=NULL WHERE chat_id=? AND vk_id=?", (ctx.chat_id, user_id))

                        # Отправляем приветствие новому участнику если он зашёл (не вернулся)
                        if action_type in {"chat_invite_user", "chat_invite_user_by_link"} and invited_id > 0:
                            with self.db.conn() as c:
                                greet = c.execute(
                                    "SELECT text, attachment, sticker_id, source_message_id FROM chat_greetings WHERE chat_id=?",
                                    (ctx.chat_id,),
                                ).fetchone()
                            if greet and (str(greet["text"]).strip() or str(greet["attachment"] or "").strip() or greet["sticker_id"] or greet["source_message_id"]):
                                greet_text = str(greet["text"] or "").strip()
                                user_ref = self._fmt_user(invited_id)
                                # Подставляем упоминание если есть {user} в тексте
                                if greet_text:
                                    greet_text = greet_text.replace("{user}", user_ref)
                                else:
                                    greet_text = f"👋 {user_ref}, добро пожаловать!"
                                source_message_id = int(greet["source_message_id"] or 0)
                                if source_message_id:
                                    try:
                                        self.api.messages.send(
                                            peer_id=ctx.peer_id,
                                            random_id=random.randint(1, 2_147_483_647),
                                            message=greet_text if greet_text else "",
                                            forward_messages=str(source_message_id),
                                            disable_mentions=1,
                                        )
                                        continue
                                    except Exception as e:
                                        logger.error(f"Не удалось отправить автоприветствие forward_messages={source_message_id}: {e}")
                                self._send_greeting_payload(
                                    ctx.peer_id, greet_text, str(greet["attachment"] or ""), greet["sticker_id"],
                                )

                    if ctx.is_chat and self._enforce_chat_silence(ctx, msg.get("conversation_message_id")):
                        continue
                    if ctx.is_chat:
                        with self.db.conn() as c:
                            ra = c.execute(
                                "SELECT source_chat_id,target_chat_id,created_at FROM remote_access_sessions WHERE actor_id=?",
                                (ctx.user_id,),
                            ).fetchone()
                        if ra:
                            src_chat = int(ra["source_chat_id"])
                            dst_chat = int(ra["target_chat_id"])
                            if self.now_ts() - int(ra["created_at"] or 0) > REMOTE_ACCESS_TTL_SEC:
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM remote_access_sessions WHERE actor_id=?", (ctx.user_id,))
                                self.send(ctx.peer_id, "⏳ Удаленный доступ истёк. Активируйте его заново.")
                                continue
                            if not self._has_access(ctx, "!удаленный"):
                                with self.db.conn() as c:
                                    c.execute("DELETE FROM remote_access_sessions WHERE actor_id=?", (ctx.user_id,))
                                self.send(ctx.peer_id, "⛔ Удаленный доступ остановлен: права больше не подтверждаются.")
                                continue
                            if int(ctx.chat_id) == src_chat:
                                if ctx.text:
                                    self.send_chat(dst_chat, f"🔁 {self._fmt_user(ctx.user_id)}: {ctx.text}")
                                if ctx.text.startswith("!"):
                                    remote_ctx = Ctx(
                                        user_id=ctx.user_id,
                                        peer_id=self._chat_peer_id(dst_chat),
                                        text=ctx.text,
                                        platform=ctx.platform,
                                        tg_is_chat=False,
                                    )
                                    self.handle_command(remote_ctx)
                                continue
                            if int(ctx.chat_id) == dst_chat and ctx.text:
                                self.send_chat(src_chat, f"🔁 {self._fmt_user(ctx.user_id)}: {ctx.text}")
                    if ctx.text and ((not ctx.is_chat) or ctx.text.lstrip().startswith("!")):
                        self._submit_command_ctx(ctx)
            except Exception as e:
                err = str(e)
                if isinstance(e, requests.exceptions.ReadTimeout) or "Read timed out" in err:
                    logger.info("LongPoll timeout: переподключаюсь...")
                    try:
                        self.longpoll = VkBotLongPoll(self.vk_session, self.group_id)
                    except Exception:
                        pass
                    time.sleep(1)
                    continue
                logger.error(f"Ошибка цикла: {e}")
                time.sleep(2)

    def telegram_listener(self) -> None:
        if not self.tg_token:
            return
        base = f"https://api.telegram.org/bot{self.tg_token}"
        while self.running:
            try:
                resp = self.http.get(
                    f"{base}/getUpdates",
                    params={"offset": self.tg_offset + 1, "timeout": 20},
                    timeout=25,
                ).json()
                for upd in resp.get("result", []):
                    self.tg_offset = max(self.tg_offset, int(upd.get("update_id", 0)))
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    from_user = msg.get("from", {})
                    user_id = int(from_user.get("id", 0))
                    username = str(from_user.get("username") or "").strip().lstrip("@").lower()
                    if user_id <= 0:
                        continue
                    if TG_SENIOR_ADMIN_USERNAME and username and username == TG_SENIOR_ADMIN_USERNAME:
                        self.tg_senior_admin_runtime_id = user_id
                    chat = msg.get("chat", {})
                    chat_id = int(chat.get("id", user_id))
                    peer_id = TG_PEER_SHIFT + abs(chat_id)
                    self.peer_routes[peer_id] = ("tg", chat_id)
                    with self.db.conn() as c:
                        c.execute(
                            "INSERT OR REPLACE INTO tg_peer_routes(peer_id,tg_chat_id,updated_at) VALUES(?,?,?)",
                            (peer_id, chat_id, self.now_ts()),
                        )
                        if username:
                            c.execute(
                                "INSERT OR REPLACE INTO tg_usernames(username,user_id,updated_at) VALUES(?,?,?)",
                                (username, user_id, self.now_ts()),
                            )
                    reply = msg.get("reply_to_message") or {}
                    reply_attachments: list[str] = []
                    reply_attachment_ids: list[str] = []
                    for key in ("photo", "video", "audio", "document", "voice", "sticker"):
                        if key in reply:
                            reply_attachments.append(key)
                    # Для Telegram сохраняем типы вложений как "идентификаторы"; отправка вложений из заметок
                    # в Telegram требует отдельной логики media API и здесь не выполняется.
                    reply_attachment_ids.extend(reply_attachments)
                    ctx = Ctx(
                        user_id=user_id,
                        peer_id=peer_id,
                        text=(msg.get("text") or msg.get("caption") or "").strip(),
                        reply_user_id=(reply.get("from") or {}).get("id"),
                        reply_text=reply.get("text") or reply.get("caption"),
                        reply_attachments=reply_attachments,
                        reply_attachment_ids=reply_attachment_ids,
                        platform="tg",
                        tg_is_chat=chat.get("type") in {"group", "supergroup"},
                        tg_username=username or None,
                    )
                    if ctx.is_chat and self._enforce_chat_silence(ctx, None):
                        continue
                    if ctx.text and ((not ctx.is_chat) or ctx.text.lstrip().startswith("!")):
                        self.handle_command(ctx)
            except Exception:
                time.sleep(2)


def main() -> None:


    # Проверяем, что все секреты загружены
    if not HARDCODED_GROUP_TOKEN or HARDCODED_GROUP_TOKEN == "":
        print("❌ ОШИБКА: Не найден VK_GROUP_TOKEN в файле .env")
        print("Создайте файл .env с переменной VK_GROUP_TOKEN=ваш_токен")
        sys.exit(1)
    if not WALL_READ_TOKEN:
        print("⚠️ ВНИМАНИЕ: VK_WALL_TOKEN/VK_USER_TOKEN/VK_SERVICE_TOKEN не задан.")
        print("Подписки на сообщества не смогут читать новые посты через wall.get.")
    
    if not WIPE_PASSWORD or WIPE_PASSWORD == "2n3Z5opi":
        print("⚠️ ВНИМАНИЕ: Используется старый пароль WIPE_PASSWORD")
        print("Рекомендуется сменить его в файле .env")
    
    # ... остальной код main() ...

    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    token = HARDCODED_GROUP_TOKEN
    group_id_raw = str(HARDCODED_GROUP_ID)
    db_default_path = globals().get("BOT_DB_PATH_CONFIG", globals().get("BOT_DB_PATH_CONIG", DEFAULT_DB_PATH))
    db_path = os.getenv("BOT_DB_PATH", db_default_path)
    senior_admin_id = int(os.getenv("SENIOR_ADMIN_ID", str(SENIOR_ADMIN_ID_CONFIG)))

    if not token or token == "PUT_YOUR_GROUP_TOKEN_HERE" or not group_id_raw.isdigit():
        print(
            "Ошибка: заполните HARDCODED_GROUP_TOKEN и HARDCODED_GROUP_ID в bot.py"
        )
        sys.exit(1)

    group_id = int(group_id_raw)
    db = DB(db_path, senior_admin_id)
    logger.info("Рекомендуемый запуск в screen/tmux или через systemd с Restart=always.")
    while True:
        bot = FactionBot(token=token, group_id=group_id, db=db)
        try:
            bot.run()
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
            continue
        if not bot.running:
            logger.info("Остановлен вручную. Перезапуск не требуется.")
            break
        logger.info("Неожиданная остановка. Перезапуск через 5 секунд...")
        time.sleep(5)


if __name__ == "__main__":
    main()
