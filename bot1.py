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

from __future__ import annotations

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
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

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

CONTROL_PIPE_PATH = "/tmp/vk_bot_control.pipe"
MSK_TZ = timezone(timedelta(hours=3), name="MSK")
TG_SENIOR_ADMIN_USERNAME = os.getenv("TG_SENIOR_ADMIN_USERNAME", "senior_admin").strip().lstrip("@").lower()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "bot.db")
ISTORIA_DB_PATH = os.path.join(BASE_DIR, "istoria.db")
ENCRYPTION_KEY_A = os.getenv("ENCRYPTION_KEY_A", "key_a_default")
ENCRYPTION_KEY_B = os.getenv("ENCRYPTION_KEY_B", "key_b_default")

TG_PEER_SHIFT = 10_000_000_000_000
PRIVACY_POLICY_URL = os.getenv("PRIVACY_POLICY_URL", "https://vk.ru/@pulse_rwpe-politika-konfedencialnosti").strip()

# ---------------------------- Быстрые настройки версии ----------------------------
# Для тестового стенда достаточно поменять эти 2 строки:
BOT_VERSION = "04,05,2026 15:30 (МСК)"
BOT_DB_PATH_CONFIG = os.path.join(BASE_DIR, "bot.db")
# Backward-compat alias for legacy typo in some deployments/scripts.
BOT_DB_PATH_CONIG = BOT_DB_PATH_CONFIG

# Загружаем переменные из .env файла
from dotenv import load_dotenv
load_dotenv()  

# Все секреты берутся из переменных окружения
HARDCODED_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN", "")
HARDCODED_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))
ENCRYPTION_KEY_A = os.getenv("BOT_KEY_A", "")
ENCRYPTION_KEY_B = os.getenv("BOT_KEY_B", "")
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
    "!выговор": {"user_default": 0, "admin_default": 5},
    "!снятьвыговор": {"user_default": 0, "admin_default": 5},
    "!списоквыговоров": {"user_default": 0, "admin_default": 5},
    "!снять": {"user_default": 70, "admin_default": 0},
    "!чат": {"user_default": 70, "admin_default": 40},
    "!убрать": {"user_default": 70, "admin_default": 40},
    "!пуш": {"user_default": 70, "admin_default": 0},
    "!узнать": {"user_default": 0, "admin_default": 30},
    "!обновить": {"user_default": 0, "admin_default": 30},
    "!супербан": {"user_default": 0, "admin_default": 30},
    "!чаты": {"user_default": 0, "admin_default": 30},
    "!все": {"user_default": 0, "admin_default": 80},
    "!блок": {"user_default": 0, "admin_default": 80},
    "!разблок": {"user_default": 0, "admin_default": 80},
    "!лидер": {"user_default": 0, "admin_default": 100},
    "!снятьлидера": {"user_default": 0, "admin_default": 100},
    "!админ": {"user_default": 0, "admin_default": 100},
    "!стереть": {"user_default": 0, "admin_default": 100},
    "!добавить": {"user_default": 0, "admin_default": 100},
    "!ботбан": {"user_default": 0, "admin_default": 100},
    "!ботразбан": {"user_default": 0, "admin_default": 100},
    "!лимиткоманд": {"user_default": 0, "admin_default": 100},
    "!передать": {"user_default": 0, "admin_default": 100},
    "!админконсоль": {"user_default": 0, "admin_default": 100},
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
    "!банфракция": {"user_default": 70, "admin_default": 10},
    "!изменить": {"user_default": 0, "admin_default": 50},
    "!новая": {"user_default": 70, "admin_default": 0},
    "!команды": {"user_default": 0, "admin_default": 0},
    "!логи": {"user_default": 0, "admin_default": 0},
    "!жалоба": {"user_default": 0, "admin_default": 0},
    "!жалобы": {"user_default": 0, "admin_default": 0},
    "!принять": {"user_default": 0, "admin_default": 0},
    "!отклонить": {"user_default": 0, "admin_default": 0},
    "!оффадминувед": {"user_default": 0, "admin_default": 0},
    "!админувед": {"user_default": 0, "admin_default": 0},
    "!дубль": {"user_default": 0, "admin_default": 0},
    "!одобритьдубль": {"user_default": 0, "admin_default": 0},
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
    "!голос": {"user_default": 0, "admin_default": 50},
    "!допрассм": {"user_default": 0, "admin_default": 100},
    "!списокдопрассм": {"user_default": 0, "admin_default": 100},
    "!допинфа": {"user_default": 0, "admin_default": 0},
    "!инфа": {"user_default": 0, "admin_default": 0},
    "!удалитьинфу": {"user_default": 0, "admin_default": 0},
    "!облик": {"user_default": 0, "admin_default": 100},
    "!проверкачата": {"user_default": 0, "admin_default": 80},
    "!удаленный": {"user_default": 0, "admin_default": 80},
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
}

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
    "!пуш": "!пуш (текст) / !пуш (фракция) (текст) / !пуш all (текст)",
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
    "!уволить": "!уволить (пользователь)",
    "!нанять": "!нанять (пользователь) (должность)",
    "!рейтинг": "!рейтинг (+N|-N) (пользователь) (причина)",
    "!админсчет": "!админсчет",
    "!ботразбан": "!ботразбан (пользователь)",
    "!жалоба": "!жалоба (reply на сообщение)",
    "!жалобы": "!жалобы",
    "!принять": "!принять жалобу (номер)",
    "!отклонить": "!отклонить жалобу (номер)",
    "!оффадминувед": "!оффадминувед (пользователь)",
    "!админувед": "!админувед (пользователь)",
    "!дубль": "!дубль (ник)",
    "!одобритьдубль": "!одобритьдубль (номер)",
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
    "!проверкачата": "!проверкачата (номер чата)",
    "!удаленный": "!удаленный доступ (номер чата) / !удаленный доступ стоп",
    "!чат": "!чат фракции (фракция) (сервер 1..3)",
    "!убрать": "!убрать чат фракции",
    "!чаты": "!чаты фракций (фракция) (сервер 1..3)",
    "!лидер": "!лидер (фракция) (сервер 1..3) (пользователь)",
    "!стереть": "!стереть (пользователь)",
    "!все": "!все чаты",
    "!блок": "!блок чат (номер)",
    "!разблок": "!разблок чат (номер)",
    "!права": "!права [уровень роли]",
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

                CREATE TABLE IF NOT EXISTS notes (
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    attachments TEXT,
                    min_role INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, name)
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
    reply_cmid: Optional[int] = None
    reply_attachments: Optional[list[str]] = None
    reply_attachment_ids: Optional[list[str]] = None
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


class FactionBot:
    def __init__(self, token: str, group_id: int, db: DB):
        self.db = db
        self.group_id = group_id
        self.vk_session = vk_api.VkApi(token=token)
        self.api = self.vk_session.get_api()
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
        self.chat_command_history: dict[int, deque[tuple[int, str, int]]] = defaultdict(lambda: deque(maxlen=200))
        self._current_user_for_parse: Optional[int] = None
        self.tg_senior_admin_runtime_id: Optional[int] = None

    # ---------- infra ----------

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

    def send(self, peer_id: int, text: str) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
        else:
            self.api.messages.send(peer_id=peer_id, random_id=0, message=text, disable_mentions=1)
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

    def send_with_attachments(self, peer_id: int, text: str, attachments: list[str]) -> None:
        route = self._resolve_route(peer_id)
        if route[0] == "tg":
            self._send_tg(route[1], text)
            return
        params = {"peer_id": peer_id, "random_id": 0, "message": text, "disable_mentions": 1}
        if attachments:
            params["attachment"] = ",".join(attachments)
        self.api.messages.send(**params)
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
        self.chat_command_history[ctx.chat_id].append((ctx.user_id, cmd_name, self.now_ts()))

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
            if uname and uname == TG_SENIOR_ADMIN_USERNAME:
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

    def _fmt_user(self, user_id: int) -> str:
        if user_id <= 0:
            return "неизвестно"
        cached = self.user_name_cache.get(user_id)
        now = self.now_ts()
        if cached and now - cached[1] < 3600:
            return f"[id{user_id}|{cached[0]}]"
        try:
            info = self.api.users.get(user_ids=str(user_id))[0]
            full_name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or f"id{user_id}"
            self.user_name_cache[user_id] = (full_name, now)
            return f"[id{user_id}|{full_name}]"
        except Exception:
            return f"[id{user_id}|id{user_id}]"

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
        if not raw_tokens:
            return None
        tokens = [t.strip().lower() for t in raw_tokens if t and t.strip()]
        if not tokens:
            return None
        first = tokens[0]
        first_norm = self._normalize_command_name(first)
        if first_norm == "!допрассм":
            return "!допрассм"
        pair = " ".join(tokens[:2])
        mapping = {
            "!создать роль": "!создать",
            "создать роль": "!создать",
            "!блок чат": "!блок",
            "блок чат": "!блок",
            "!разблок чат": "!разблок",
            "разблок чат": "!разблок",
            "!список допрассм": "!списокдопрассм",
            "список допрассм": "!списокдопрассм",
            "!удалить инфу": "!удалитьинфу",
            "удалить инфу": "!удалитьинфу",
        }
        if pair in mapping:
            return mapping[pair]
        return first_norm

    def _get_admin_min(self, cmd: str) -> int:
        normalized = self._normalize_command_name(cmd)
        with self.db.conn() as c:
            row = c.execute("SELECT min_admin FROM command_admin_rights WHERE command=?", (normalized,)).fetchone()
            if not row and normalized.startswith("!"):
                legacy = normalized[1:]
                row = c.execute("SELECT min_admin FROM command_admin_rights WHERE command=?", (legacy,)).fetchone()
        if row:
            return int(row[0])
        return ADMIN_MIN.get(normalized, 0)

    def _record_failed_access(self, user_id: int, reason: str) -> None:
        now = self.now_ts()
        dq = self.failed_access_window[user_id]
        dq.append(now)
        while dq and now - dq[0] > 300:
            dq.popleft()
        if len(dq) >= 5:
            log_chat_id = int(self._get_setting("log_chat_id", "0") or 0)
            if log_chat_id > 0:
                self._logging_in_progress = True
                try:
                    self.send_chat(log_chat_id, f"⚠️ Аномалия доступа: {self._fmt_user(user_id)} ({reason}), попыток за 5 минут: {len(dq)}")
                except Exception:
                    pass
                finally:
                    self._logging_in_progress = False

    @staticmethod
    def _contains_prompt_injection(text: str) -> bool:
        t = text.lower()
        patterns = [
            "ignore previous instructions",
            "system prompt",
            "developer message",
            "jailbreak",
            "bypass",
            "отключи проверки",
            "игнорируй инструкции",
        ]
        return any(p in t for p in patterns)

    def _enforce_chat_silence(self, ctx: Ctx, conversation_message_id: Optional[int]) -> bool:
        if not ctx.is_chat:
            return False
        if self._is_admin_faction_user(ctx.user_id):
            return False
        now = self.now_ts()
        with self.db.conn() as c:
            rule = c.execute(
                "SELECT window_min,msg_limit,enabled FROM chat_silence WHERE chat_id=?",
                (ctx.chat_id,),
            ).fetchone()
            if not rule or int(rule["enabled"] or 0) == 0:
                return False
            ex = c.execute(
                "SELECT until_ts FROM silence_exceptions WHERE chat_id=? AND vk_id=?",
                (ctx.chat_id, ctx.user_id),
            ).fetchone()
            if ex and int(ex["until_ts"] or 0) > now:
                return False
            c.execute("DELETE FROM silence_exceptions WHERE chat_id=? AND vk_id=? AND until_ts<=?", (ctx.chat_id, ctx.user_id, now))
        window_sec = int(rule["window_min"]) * 60
        limit = int(rule["msg_limit"])
        key = (ctx.chat_id, ctx.user_id)
        dq = self.silence_window[key]
        while dq and now - dq[0] > window_sec:
            dq.popleft()
        if len(dq) >= limit:
            if conversation_message_id is not None:
                self._api_method(
                    "messages.delete",
                    {"peer_id": ctx.peer_id, "cmids": str(conversation_message_id), "delete_for_all": 1},
                )
            return True
        dq.append(now)
        return False

    def _log_command_response(self, peer_id: int, response_text: str) -> None:
        if self._logging_in_progress or not self._active_command:
            return
        actor_id, cmd_text, origin_peer_id = self._active_command
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
        return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))

    def _double_encrypt(self, text: str) -> str:
        b = text.encode("utf-8")
        step1 = self._xor_bytes(b, ENCRYPTION_KEY_A)
        step2 = self._xor_bytes(step1, ENCRYPTION_KEY_B)
        return base64.urlsafe_b64encode(step2).decode("ascii")

    def _double_decrypt(self, token: str) -> str:
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
            "!принять": 3,
            "!отклонить": 3,
            "!оффадминувед": 2,
            "!админувед": 2,
            "!рейтинг": 4,
            "!принять": 3,
            "!отклонить": 3,
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
        for canonical, aliases in COMMAND_ALIASES.items():
            if cmd == canonical or cmd in aliases:
                return canonical
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
        if self._is_senior_admin_ctx(ctx):
            return True
        if self._is_bot_banned(ctx.user_id):
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
        if cmd == "!изменить":
            return admin_lvl >= 50 or (5 <= admin_lvl <= 10)
        if cmd == "!чат" and ctx.is_chat and self._is_leader_user(ctx.user_id):
            return True
        min_admin = self._get_admin_min(cmd)
        if cmd in COMMAND_ACCESS and COMMAND_ACCESS[cmd]["user_default"] == 0 and min_admin > 0:
            if admin_lvl >= min_admin:
                return True
            if ctx.is_chat and self._has_custom_role_right(ctx.chat_id, cmd):
                role_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
                return role_lvl >= self._required_role(cmd, ctx.chat_id)
            return False
        if cmd in {"!чс", "!списокчс", "!снятьчс"}:
            return admin_lvl >= 10
        if admin_lvl >= min_admin:
            return True
        if not ctx.is_chat and cmd in {
            "!бан", "!кик", "!разбан", "!мут", "!размут", "!роли", "!создать", "!роль",
            "!снять", "!переименовать", "!пуш", "!банлист", "!мутлист", "!новоеприветствие", "!удалитьприветствие", "!заметка",
            "!заметки", "!иммунитет", "!снятьиммунитет", "!иммунитеты", "!право", "!пред", "!повысить", "!понизить",
            "!админы", "!снятьпред", "!списокпредов", "!выговор", "!список",
        }:
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
        columns = "name,min_role,attachments,content" if with_content else "name,min_role,attachments"
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
        text = "\n\n".join(p for p in parts if p).strip()
        return text or None

    @staticmethod
    def _attachment_id(att: dict) -> Optional[str]:
        at = att.get("type")
        item = att.get(at, {}) if at else {}
        owner = item.get("owner_id")
        aid = item.get("id")
        if at and owner is not None and aid is not None:
            access_key = item.get("access_key")
            if access_key:
                return f"{at}{owner}_{aid}_{access_key}"
            return f"{at}{owner}_{aid}"
        if at == "doc":
            doc = att.get("doc", {})
            if "owner_id" in doc and "id" in doc:
                return f"doc{doc['owner_id']}_{doc['id']}"
        return None

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
            {"peer_id": peer_id, "member_ids": [target_id], "action": "ro", "for": duration_sec},
            {"peer_id": peer_id, "member_id": target_id, "for": duration_sec},
            {"peer_id": peer_id, "member_id": target_id, "action": "ro", "for": duration_sec},
        ]
        for payload in calls:
            if self._api_method("messages.changeConversationMemberRestrictions", payload):
                return True
            try:
                self.api.messages.changeConversationMemberRestrictions(**payload)
                return True
            except Exception:
                pass
        return False

    def _apply_vk_unmute(self, peer_id: int, target_id: int) -> bool:
        calls = [
            {"peer_id": peer_id, "member_ids": str(target_id), "action": "rw", "for": 0},
            {"peer_id": peer_id, "member_ids": str(target_id), "action": "rw"},
            {"peer_id": peer_id, "member_ids": [target_id], "action": "rw", "for": 0},
            {"peer_id": peer_id, "member_ids": [target_id], "action": "rw"},
            {"peer_id": peer_id, "member_id": target_id, "action": "rw"},
            {"peer_id": peer_id, "member_id": target_id, "for": 0},
            {"peer_id": peer_id, "member_id": target_id, "action": "rw", "for": 0},
            {"peer_id": peer_id, "member_id": target_id, "action": "ro", "for": 0},
            {"peer_id": peer_id, "member_ids": str(target_id), "action": "ro", "for": 0},
            {"peer_id": peer_id, "member_ids": [target_id], "action": "ro", "for": 0},
        ]
        for payload in calls:
            if self._api_method("messages.changeConversationMemberRestrictions", payload):
                return True
            try:
                self.api.messages.changeConversationMemberRestrictions(**payload)
                return True
            except Exception:
                pass
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
                c.execute(
                    "UPDATE registration_sessions SET stage='faction', nickname=? WHERE vk_id=?",
                    (ctx.text.strip(), ctx.user_id),
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
                c.execute(
                    "UPDATE registration_sessions SET stage='position', rp_name=? WHERE vk_id=?",
                    (ctx.text.strip(), ctx.user_id),
                )
                self.send(ctx.peer_id, "💼 Введите должность:")
                return True
            if stage == "position":
                position = ctx.text.strip()
                nickname = sess["nickname"]
                faction = sess["faction"]
                rp_name = sess["rp_name"]
                server_id = int(sess["server_id"] or 1)
                cur = c.execute(
                    "INSERT INTO registration_requests(vk_id,server_id,platform,nickname,faction,rp_name,position,status) VALUES(?,?,?,?,?,?,?,'pending')",
                    (ctx.user_id, server_id, ctx.platform, nickname, faction, rp_name, position),
                )
                req_id = int(cur.lastrowid)
                c.execute(
                    "INSERT OR REPLACE INTO users(vk_id,nickname,rp_name,position,faction,server_id,approved,admin_level,bot_ban,consent_accepted,consent_accepted_at) VALUES(?,?,?,?,?,?,0,0,0,1,?)",
                    (ctx.user_id, nickname, rp_name, position, faction, server_id, self.now_ts()),
                )
                c.execute("DELETE FROM registration_sessions WHERE vk_id=?", (ctx.user_id,))

                text = (
                    f"📥 Новая заявка #{req_id}\n"
                    f"Пользователь: {self._fmt_user(ctx.user_id)}\nNick: {nickname}\nФракция: {faction}\nСервер: {server_id}\nРП ФИО: {rp_name}\n"
                    f"Должность: {position}\nИсточник: {'Telegram' if ctx.platform == 'tg' else 'VK'}\n\n"
                    "Ответ: !одобрить (номер) или !отказать (номер)"
                )
                self.send(
                    ctx.peer_id,
                    f"🕒 Заявка #{req_id} отправлена на одобрение.\n"
                    "До одобрения заявки использование бота ограничено.",
                )
                delivered = False
                try:
                    delivered = self._notify_registration_reviewers(req_id, faction, server_id, text)
                except Exception:
                    delivered = False
                if not delivered:
                    print(
                        f"[BOT] Не удалось отправить заявку #{req_id} получателям рассмотрения (нет ЛС-доступа).",
                        file=sys.stderr,
                    )
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
        position = u["position"] if u and u["position"] else "не указана"
        faction = u["faction"] if u and u["faction"] else "не указана"
        if hidden_in_chat:
            nick = "(скрыто)"
            position = "(скрыто)"
            faction = "(скрыто)"
        pref_line = "\n🧷 Префиксы: нет"
        if pref:
            pref_line = "\n🧷 Префиксы: " + ", ".join([f"{p['name']} {p['emoji']}" for p in pref])
        self.send(
            ctx.peer_id,
            (
                "👤 Ваш профиль\n"
                f"• Ник: {nick}\n"
                f"• Должность: {position}\n"
                f"• Фракция: {faction}\n"
                f"• Сервер: {u['server_id'] if u and u['server_id'] else 1}\n"
                f"• Уровень в чате: {role_lvl}\n"
                f"• Админ уровень: {admin_lvl}"
                f"{pref_line}"
            ),
        )

    def _registration_reviewer_ids(self, faction: str, server_id: int) -> list[int]:
        with self.db.conn() as c:
            leader = c.execute("SELECT vk_id FROM leaders WHERE faction=? AND server_id=?", (faction, server_id)).fetchone()
            rows = c.execute(
                "SELECT vk_id FROM extra_reviewers WHERE faction=? AND server_id=? ORDER BY vk_id",
                (faction, server_id),
            ).fetchall()
        out: list[int] = []
        if leader:
            out.append(int(leader["vk_id"] if isinstance(leader, sqlite3.Row) else leader[0]))
        for r in rows:
            uid = int(r["vk_id"])
            if uid not in out:
                out.append(uid)
        if self.db.senior_admin_id not in out:
            out.append(int(self.db.senior_admin_id))
        return out

    def _notify_registration_reviewers(self, request_id: int, faction: str, server_id: int, text: str) -> bool:
        recipients = self._registration_reviewer_ids(faction, server_id)
        delivered_any = False
        with self.db.conn() as c:
            for to_user in recipients:
                delivered = self.send_dm_vk_with_inline(to_user, text, request_id)
                if delivered:
                    delivered_any = True
                c.execute(
                    "INSERT OR IGNORE INTO registration_review_recipients(request_id,vk_id) VALUES(?,?)",
                    (request_id, to_user),
                )
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
        lines = [
            f"👤 Профиль {self._fmt_user(target_id)}",
            f"• Ник: {u['nickname'] or 'не указан'}",
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

    def handle_command(self, ctx: Ctx) -> None:
        text = self._normalize_bang_command(ctx.text)
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

        if low.startswith("!одобрить"):
            self._approve_reject(ctx, True)
            return
        if low.startswith("!отказать"):
            self._approve_reject(ctx, False)
            return

        if self._handle_admin_console(ctx):
            return

        if not low.startswith("!"):
            with self.db.conn() as c:
                ws = c.execute("SELECT target_id FROM wipe_sessions WHERE actor_id=?", (ctx.user_id,)).fetchone()
            if ws:
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

        cmd = self._resolve_alias(low.split()[0])
        self._current_user_for_parse = ctx.user_id
        if cmd != low.split()[0]:
            tokens = text.split()
            if tokens:
                tokens[0] = cmd
                text = " ".join(tokens)
                low = text.lower()
        pre_parts = text.split()
        if cmd == "!удалить" and len(pre_parts) >= 2 and pre_parts[1].lower() == "инфу":
            cmd = "!удалитьинфу"
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

        if ctx.is_chat:
            with self.db.conn() as c:
                ch = c.execute("SELECT enabled FROM chat_controls WHERE chat_id=?", (ctx.chat_id,)).fetchone()
            enabled = True if not ch else bool(int(ch["enabled"]))
            if (not is_senior) and (not enabled) and not (cmd == "!включить" and len(parts) >= 2 and parts[1].lower() == "чат"):
                self._active_command = None
                return

        self._remember_chat_command(ctx, text)

        if cmd == "!я":
            self.cmd_profile(ctx)
            return

        if cmd == "!версия":
            self.send(
                ctx.peer_id,
                f"🧪 Версия бота: {BOT_VERSION}\n💾 База данных: {self.db.path}",
            )
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
            self.send(ctx.peer_id, "Список команд: https://vk.com/@pulse_rwpe-principy-raboty")
            return

        if cmd in {"!команды", "!комнады"}:
            actor = self._user(ctx.user_id)
            is_admin_faction = bool(actor and actor["faction"] == SECRET_FACTION)
            if len(parts) > 1 and not is_admin_faction:
                self.send(ctx.peer_id, "⛔ Указывать уровни роли/админ прав в !команды могут только пользователи фракции Админ.")
                return
            role_probe: Optional[int] = None
            admin_probe: Optional[int] = None
            if len(parts) >= 2:
                if not parts[1].isdigit():
                    self.send(ctx.peer_id, "Формат: !команды [уровень роли] [уровень админ прав]")
                    return
                role_probe = int(parts[1])
            if len(parts) >= 3:
                if not parts[2].isdigit():
                    self.send(ctx.peer_id, "Формат: !команды [уровень роли] [уровень админ прав]")
                    return
                admin_probe = int(parts[2])
            available: list[str] = []
            for ccmd in sorted(COMMAND_ACCESS.keys()):
                if ccmd == "!облик" and not self._is_senior_admin_ctx(ctx):
                    continue
                usage = COMMAND_USAGE.get(ccmd, ccmd)
                if role_probe is None and admin_probe is None:
                    fake = Ctx(
                        user_id=ctx.user_id,
                        peer_id=ctx.peer_id,
                        text=ccmd,
                        reply_user_id=ctx.reply_user_id,
                        reply_text=ctx.reply_text,
                        platform=ctx.platform,
                        tg_is_chat=ctx.tg_is_chat,
                    )
                    if self._has_access(fake, ccmd):
                        available.append(f"• {usage}")
                else:
                    probe_role = role_probe if role_probe is not None else self._get_role_level(ctx.chat_id, ctx.user_id)
                    probe_admin = admin_probe if admin_probe is not None else self._get_admin_level(ctx.user_id)
                    need_role = self._required_role(ccmd, ctx.chat_id if ctx.is_chat else None)
                    need_admin = self._get_admin_min(ccmd)
                    include = (probe_admin >= need_admin) or (probe_role >= need_role)
                    if include:
                        available.append(f"• {usage}")
            self.send(ctx.peer_id, "📘 Доступные команды:\n" + ("\n".join(sorted(set(available), key=lambda x: x.lower())) if available else "нет доступных команд"))
            return

        if cmd == "!логи" and ctx.is_chat:
            items = list(self.chat_command_history.get(ctx.chat_id, deque()))
            if not items:
                self.send(ctx.peer_id, "📜 Логи команд: пусто.")
                return
            tail = items[-20:]
            lines = ["📜 Последние 20 команд в этом чате:"]
            for idx, (uid, used_cmd, ts) in enumerate(reversed(tail), start=1):
                dt = self._fmt_msk_dt(int(ts))
                lines.append(f"{idx}. {used_cmd} — {self._fmt_user(int(uid))} — {dt}")
            self.send(ctx.peer_id, "\n".join(lines))
            return

        if cmd == "!дж":
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            can_view_hidden = actor_faction == SECRET_FACTION
            target_faction = actor_faction
            target_server = actor_server
            if actor_faction == SECRET_FACTION:
                if len(parts) < 2:
                    self.send(ctx.peer_id, "Формат для админов: !дж (фракция) [сервер 1..3]")
                    return
                rest_parts = parts[1:]
                if rest_parts[-1] in {"1", "2", "3"}:
                    target_server = int(rest_parts[-1])
                    rest_parts = rest_parts[:-1]
                target_faction, _ = self._extract_faction_and_rest(["!дж"] + rest_parts, 1)
                if not target_faction:
                    self.send(ctx.peer_id, "❌ Не удалось распознать фракцию.")
                    return
            if not target_faction:
                self.send(ctx.peer_id, "❌ У вас не указана фракция.")
                return
            with self.db.conn() as c:
                rows = c.execute(
                    """
                    SELECT u.vk_id,u.position,u.hidden,COALESCE(u.approved_at,p.updated_at,0) AS approved_at, u.approved_by
                    FROM users u
                    LEFT JOIN preapproved_profiles p ON p.vk_id=u.vk_id
                    WHERE u.faction=? AND u.server_id=? AND u.approved=1 AND u.position IS NOT NULL AND TRIM(u.position)!='' AND LOWER(u.position)!='не указана'
                    ORDER BY u.position COLLATE NOCASE, u.vk_id
                    """,
                    (target_faction, target_server),
                ).fetchall()
                leader = c.execute("SELECT vk_id FROM leaders WHERE faction=? AND server_id=?", (target_faction, target_server)).fetchone()
            if not rows:
                self.send(ctx.peer_id, f"📋 Должности фракции {target_faction} (сервер {target_server}): пусто")
                return
            out = [f"📋 ДЖ фракции {target_faction} (сервер {target_server}):"]
            for r in rows:
                ts = int(r["approved_at"] or 0)
                dt = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts > 0 else "неизвестно"
                approver = self._fmt_user(int(r["approved_by"])) if r["approved_by"] else (self._fmt_user(int(leader["vk_id"])) if leader else "неизвестно")
                if int(r["hidden"] or 0) == 1 and not can_view_hidden:
                    out.append(f"• {r['position']} — (скрыто) — {dt} — принял: {approver}")
                else:
                    out.append(f"• {r['position']} — {self._fmt_user(int(r['vk_id']))} — {dt} — принял: {approver}")
            self.send(ctx.peer_id, "\n".join(out))
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

        if cmd == "!тишина" and ctx.is_chat and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            window_min = max(1, int(parts[1]))
            msg_limit = max(1, int(parts[2]))
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO chat_silence(chat_id,window_min,msg_limit,enabled) VALUES(?,?,?,1)",
                    (ctx.chat_id, window_min, msg_limit),
                )
            self.send(ctx.peer_id, f"✅ Режим тишины включен: {msg_limit} сообщ. раз в {window_min} мин. на пользователя.")
            return

        if cmd == "!снятьтишину" and ctx.is_chat:
            with self.db.conn() as c:
                c.execute("DELETE FROM chat_silence WHERE chat_id=?", (ctx.chat_id,))
                c.execute("DELETE FROM silence_exceptions WHERE chat_id=?", (ctx.chat_id,))
            self.send(ctx.peer_id, "✅ Режим тишины снят в этом чате.")
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

        if cmd == "!отключить" and len(parts) >= 2 and parts[1].lower() == "чат" and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) < 40:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO chat_controls(chat_id,enabled) VALUES(?,0)", (ctx.chat_id,))
            self.send(ctx.peer_id, "✅ Чат отключен для команд бота.")
            return

        if cmd == "!включить" and len(parts) >= 2 and parts[1].lower() == "чат" and ctx.is_chat:
            if self._get_admin_level(ctx.user_id) < 40:
                self.send(ctx.peer_id, "⛔ Команда доступна админам 40+.")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR REPLACE INTO chat_controls(chat_id,enabled) VALUES(?,1)", (ctx.chat_id,))
            self.send(ctx.peer_id, "✅ Чат снова подключен к системе бота.")
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
                if not deleted:
                    self.send_ephemeral(ctx.peer_id, "❌ Не удалось удалить сообщение(я).", ttl_sec=5)
                return
            if len(parts) < 2:
                self.send(ctx.peer_id, "Формат: !чистка (количество) или !чистка (время: 10м/2ч) или reply без аргументов.")
                return
            arg = parts[1].lower()
            ids_to_delete: list[int] = []
            cmids_to_delete: list[int] = []
            try:
                hist = self.api.messages.getHistory(peer_id=ctx.peer_id, count=200)
                items = hist.get("items", [])
                if arg.isdigit():
                    limit = min(int(arg), 200)
                    ids_to_delete = [int(m["id"]) for m in items[:limit] if m.get("id") is not None]
                    cmids_to_delete = [int(m["conversation_message_id"]) for m in items[:limit] if m.get("conversation_message_id") is not None]
                else:
                    m = re.match(r"^(\\d+)([мmhчh])$", arg)
                    if not m:
                        self.send(ctx.peer_id, "❌ Неверный формат времени. Пример: 10м или 2ч.")
                        return
                    val = int(m.group(1))
                    unit = m.group(2)
                    sec = val * 60 if unit in {"м", "m"} else val * 3600
                    cutoff = self.now_ts() - sec
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
                self.send(ctx.peer_id, f"✅ Очистка выполнена. Удалено сообщений: {deleted}")
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

        if cmd == "!уволить":
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для команды нужны права: роль 70+ или админ 10+.")
                return
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !уволить (пользователь)")
                return
            actor = self._user(ctx.user_id)
            victim = self._user(target)
            if not victim:
                self.send(ctx.peer_id, "❌ Пользователь не найден в системе.")
                return
            actor_faction = actor["faction"] if actor else None
            actor_server = int(actor["server_id"] or 1) if actor else 1
            victim_faction = victim["faction"]
            victim_server = int(victim["server_id"] or 1)
            if actor_faction != SECRET_FACTION and actor_faction != victim_faction:
                self.send(ctx.peer_id, "⛔ Можно увольнять только из своей фракции.")
                return
            if actor_faction != SECRET_FACTION and actor_server != victim_server:
                self.send(ctx.peer_id, "⛔ Можно увольнять только пользователей своего сервера.")
                return
            with self.db.conn() as c:
                c.execute("UPDATE users SET faction='не указана', position='не указана' WHERE vk_id=?", (target,))
            self._add_history(
                nickname=victim["nickname"] or "не указан",
                target_vk_id=target,
                old_faction=victim["faction"],
                old_position=victim["position"],
                new_faction="не указана",
                new_position="не указана",
                actor_vk_id=ctx.user_id,
                event_type="fire",
            )
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} уволен.")
            return

        if cmd == "!нанять":
            actor_admin = self._get_admin_level(ctx.user_id)
            actor_role = self._get_role_level(ctx.chat_id, ctx.user_id) if ctx.is_chat else 0
            if actor_role < 70 and actor_admin < 10:
                self.send(ctx.peer_id, "⛔ Для команды нужны права: роль 70+ или админ 10+.")
                return
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None or len(parts) < 3:
                self.send(ctx.peer_id, "Формат: !нанять (пользователь) (должность)")
                return
            new_position = " ".join(parts[2:]).strip()
            actor = self._user(ctx.user_id)
            victim = self._user(target)
            if not actor or not victim:
                self.send(ctx.peer_id, "❌ Пользователь не найден в системе.")
                return
            actor_faction = actor["faction"]
            actor_server = int(actor["server_id"] or 1)
            victim_faction = victim["faction"]
            victim_server = int(victim["server_id"] or 1)
            if actor_faction != SECRET_FACTION and victim_faction not in {actor_faction, "не указана", None}:
                self.send(ctx.peer_id, "⛔ Можно нанимать только из своей фракции или без фракции.")
                return
            if actor_faction != SECRET_FACTION and victim_server != actor_server:
                self.send(ctx.peer_id, "⛔ Можно нанимать только пользователей своего сервера.")
                return
            if (victim["position"] or "не указана") != "не указана":
                self.send(ctx.peer_id, "⛔ Нанять можно только пользователя с должностью «не указана».")
                return
            with self.db.conn() as c:
                c.execute("UPDATE users SET faction=?, position=?, server_id=? WHERE vk_id=?", (actor_faction, new_position, actor_server, target))
            self._add_history(
                nickname=victim["nickname"] or "не указан",
                target_vk_id=target,
                old_faction=victim["faction"],
                old_position=victim["position"],
                new_faction=actor_faction,
                new_position=new_position,
                actor_vk_id=ctx.user_id,
                event_type="hire",
            )
            self.send(ctx.peer_id, f"✅ Пользователь {self._fmt_user(target)} нанят на должность «{new_position}».")
            return

        if cmd == "!история":
            self.send(ctx.peer_id, "ℹ️ Команда !история удалена.")
            return

        if cmd == "!новый" and len(parts) >= 5 and parts[1].lower() == "префикс":
            if self._get_admin_level(ctx.user_id) < 90:
                self.send(ctx.peer_id, "⛔ Доступно только админам 90+.")
                return
            target = self._parse_user(parts[2]) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !новый префикс (пользователь) (название) (смайлик)")
                return
            name = parts[3]
            emoji = parts[4]
            with self.db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO user_prefixes(vk_id,name,emoji) VALUES(?,?,?)",
                    (target, name, emoji),
                )
            self.send(ctx.peer_id, f"✅ Префикс «{name} {emoji}» добавлен пользователю {self._fmt_user(target)}.")
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

        if cmd == "!узнать":
            target = (self._parse_user(parts[1]) if len(parts) > 1 else None) or ctx.reply_user_id
            if target is None:
                self.send(ctx.peer_id, "Формат: !узнать (пользователь) или ответом на сообщение")
                return
            with self.db.conn() as c:
                tu = c.execute("SELECT hidden FROM users WHERE vk_id=?", (int(target),)).fetchone()
            if tu and int(tu["hidden"] or 0) == 1 and self._get_admin_level(ctx.user_id) < 30:
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
            reply_attachments = [
                a for a in (ctx.reply_attachment_ids or [])
                if str(a).startswith("photo") or str(a).startswith("video")
            ]
            reporter = self._user(ctx.user_id)
            target = self._user(int(ctx.reply_user_id))
            reporter_name = (reporter["nickname"] if reporter and reporter["nickname"] else self._fmt_user(ctx.user_id))
            target_name = (target["nickname"] if target and target["nickname"] else "")
            actor_server = int(reporter["server_id"] or 1) if reporter else 1
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
            mentions = " ".join([self._fmt_user(uid) for uid in self._complaint_admin_mentions(complaint_chat_id, actor_server)]) if ctx.platform == "vk" else ""
            notify_lines = [
                f"⚠️ Поступила новая жалоба #{complaint_id}.",
                f"1. Заявитель: {reporter_name}, {self._platform_profile_ref(ctx.platform, ctx.user_id)}",
            ]
            if target and int(ctx.reply_user_id) != int(ctx.user_id) and target_name and target_name != reporter_name:
                notify_lines.append(
                    f"2. На кого жалоба: {target_name}, {self._platform_profile_ref(ctx.platform, int(ctx.reply_user_id))}"
                )
            notify_lines.append(f"3. Сервер: {actor_server}")
            notify_lines.append(f"4. Доп. информация: {extra_info}")
            if reply_text:
                notify_lines.append("↩️ Текст сообщения:")
                notify_lines.append(reply_text)
            notify_lines.append("#жалоба")
            notify = "\n".join(notify_lines)
            if mentions:
                notify += "\n" + mentions
            if complaint_chat_id > 0:
                if reply_attachments and ctx.platform == "vk":
                    self.send_with_attachments(self._chat_peer_id(complaint_chat_id), notify, reply_attachments)
                else:
                    self.send_chat(complaint_chat_id, notify)
            else:
                if reply_attachments and ctx.platform == "vk":
                    self.send_with_attachments(ctx.peer_id, notify, reply_attachments)
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
                    "SELECT id,reporter_id,target_id,created_at FROM complaints WHERE status='open' ORDER BY id ASC"
                ).fetchall()
            if not rows:
                self.send(ctx.peer_id, "📭 Открытых жалоб нет.")
                return
            out = ["📋 Открытые жалобы:"]
            for r in rows:
                dt = datetime.fromtimestamp(int(r["created_at"])).strftime("%d.%m.%Y %H:%M")
                out.append(f"• #{int(r['id'])} | репортёр: {self._fmt_user(int(r['reporter_id']))} | цель: {self._fmt_user(int(r['target_id']))} | {dt}")
            self.send(ctx.peer_id, "\n".join(out))
            return

        if cmd in {"!принять", "!отклонить"} and len(parts) >= 3 and parts[1].lower() == "жалобу" and parts[2].isdigit():
            if not self._is_admin_faction_user(ctx.user_id):
                self.send(ctx.peer_id, "⛔ Команда доступна только фракции Админ.")
                return
            complaint_id = int(parts[2])
            is_accept = cmd == "!принять"
            with self.db.conn() as c:
                row = c.execute("SELECT id,target_id,status FROM complaints WHERE id=?", (complaint_id,)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Жалоба не найдена.")
                    return
                if row["status"] != "open":
                    self.send(ctx.peer_id, "❌ Жалоба уже закрыта.")
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
            if self._get_admin_level(ctx.user_id) < 5:
                self.send(ctx.peer_id, "⛔ Команда доступна с 5 уровня админ-прав.")
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

        if cmd == "!список" and len(parts) >= 2 and parts[1].lower() == "выговоров":
            if self._get_admin_level(ctx.user_id) < 5:
                self.send(ctx.peer_id, "⛔ Команда доступна с 5 уровня админ-прав.")
                return
            actor = self._user(ctx.user_id)
            actor_faction = actor["faction"] if actor else None
            if actor_faction == SECRET_FACTION:
                faction = " ".join(parts[2:]).strip() if len(parts) > 2 else ""
                if not faction:
                    self.send(ctx.peer_id, "Формат для Админ: !список выговоров (фракция)")
                    return
                faction = next((f for f in ALL_FACTIONS if f.lower() == faction.lower()), faction)
                server_id = 1
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

            target_faction = faction
            push_attachments = ctx.reply_attachment_ids or []
            if faction == SECRET_FACTION:
                if len(parts) < 2:
                    self.send(ctx.peer_id, "Формат для Админ: !пуш (фракция) (текст) или ответом на сообщение.")
                    return
                if parts[1].lower() in {"все", "all"}:
                    target_faction = "__ALL__"
                    rest = parts[2:]
                    text_to_push = " ".join(rest).strip() or (ctx.reply_text or "").strip()
                    if not text_to_push:
                        self.send(ctx.peer_id, "Формат: !пуш все (текст) или ответом на сообщение.")
                        return
                    with self.db.conn() as c:
                        chats = c.execute("SELECT chat_id FROM chats").fetchall()
                    sent = 0
                    for row in chats:
                        chat_id = int(row["chat_id"])
                        try:
                            if push_attachments and ctx.platform == "vk":
                                self.send_with_attachments(self._chat_peer_id(chat_id), f"📢 Глобальный пуш от {self._fmt_user(ctx.user_id)}\n{text_to_push}", push_attachments)
                            else:
                                self.send(self._chat_peer_id(chat_id), f"📢 Глобальный пуш от {self._fmt_user(ctx.user_id)}\n{text_to_push}")
                            sent += 1
                        except Exception:
                            continue
                    self.send(ctx.peer_id, f"✅ Сообщение отправлено в {sent} чат(ов) (все фракции).")
                    return
                tf, rest = self._extract_faction_and_rest(parts, 1)
                if not tf:
                    self.send(ctx.peer_id, "❌ Для Админ-фракции нужно указать целевую фракцию.")
                    return
                target_faction = tf
                text_to_push = " ".join(rest).strip() or (ctx.reply_text or "").strip()
            else:
                text_to_push = " ".join(parts[1:]).strip() or (ctx.reply_text or "").strip()

            if not text_to_push:
                self.send(ctx.peer_id, "Формат: !пуш (текст) или ответом на сообщение с текстом.")
                return

            with self.db.conn() as c:
                if faction == SECRET_FACTION:
                    chats = c.execute("SELECT chat_id FROM chats WHERE faction=?", (target_faction,)).fetchall()
                else:
                    chats = c.execute("SELECT chat_id FROM chats WHERE faction=? AND server_id=?", (target_faction, actor_server)).fetchall()
            sent = 0
            for row in chats:
                chat_id = int(row["chat_id"])
                try:
                    if push_attachments and ctx.platform == "vk":
                        self.send_with_attachments(self._chat_peer_id(chat_id), f"📢 Фракционный пуш от {self._fmt_user(ctx.user_id)}\n{text_to_push}", push_attachments)
                    else:
                        self.send(self._chat_peer_id(chat_id), f"📢 Фракционный пуш от {self._fmt_user(ctx.user_id)}\n{text_to_push}")
                    sent += 1
                except Exception:
                    continue
            self.send(ctx.peer_id, f"✅ Сообщение отправлено в {sent} чат(ов) фракции {target_faction}.")
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
            def _load_roles() -> list[sqlite3.Row]:
                with self.db.conn() as c:
                    return c.execute(
                        "SELECT level,name FROM chat_roles WHERE chat_id=? ORDER BY level DESC",
                        (ctx.chat_id,),
                    ).fetchall()
            with ThreadPoolExecutor(max_workers=1) as pool:
                rows = pool.submit(_load_roles).result()
            if not rows:
                self.send(ctx.peer_id, "ℹ️ Роли не созданы.")
            else:
                lines = [f"• {r['name']} — {r['level']}" for r in rows]
                chunk_size = 20
                for i in range(0, len(lines), chunk_size):
                    header = "📋 Роли:\n" if i == 0 else "📋 Роли (продолжение):\n"
                    self.send(ctx.peer_id, header + "\n".join(lines[i:i + chunk_size]))
            return

        if cmd == "!роль" and ctx.is_chat:
            if len(parts) >= 3 and parts[2].isdigit():
                target = self._parse_user(parts[1]) or ctx.reply_user_id
                if target is None:
                    self.send(ctx.peer_id, "Формат: !роль (пользователь) (уровень)")
                    return
                lvl = int(parts[2])
                actor_lvl = self._get_role_level(ctx.chat_id, ctx.user_id)
                target_lvl = self._get_role_level(ctx.chat_id, target)
                if actor_lvl < target_lvl and self._get_admin_level(ctx.user_id) < 100:
                    self.send(ctx.peer_id, "⛔ Нельзя менять роль пользователю выше вашего уровня.")
                    return
                if lvl > actor_lvl and self._get_admin_level(ctx.user_id) < 100:
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
            grouped: dict[str, list[str]] = {}
            for r in rows:
                role_label = f"{r['name'] or 'Роль'} ({int(r['role_level'])})"
                grouped.setdefault(role_label, []).append(self._fmt_user(int(r["vk_id"])))
            lines = [f"📋 В чате {len(rows)} людей с должностью выше одобренного пользователя:"]
            for role_label, users in grouped.items():
                lines.append(f"• {role_label}: {', '.join(users)}")
            text_out = "\n".join(lines)
            self.send(ctx.peer_id, text_out)
            return

        # admin-only globals
        if cmd == "!стереть" and len(parts) >= 2:
            if not self._is_senior_admin_ctx(ctx):
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
            if not self._is_senior_admin_ctx(ctx):
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
            if not self._is_senior_admin_ctx(ctx):
                self.send(ctx.peer_id, "⛔ Команда !админ права доступна только старшему админу.")
                return
            uid = self._parse_user(parts[2])
            lvl = int(parts[3])
            if uid is None or not (0 <= lvl <= 100):
                self.send(ctx.peer_id, "Формат: !админ права (пользователь) (0..100)")
                return
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (uid,))
                c.execute("UPDATE users SET admin_level=? WHERE vk_id=?", (lvl, uid))
            self.send(ctx.peer_id, f"✅ Админ уровень {self._fmt_user(uid)} = {lvl}")
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

        if cmd == "!снятьлидера" and len(parts) >= 2:
            faction = " ".join(parts[1:]).strip()
            faction = next((f for f in FACTIONS if f.lower() == faction.lower()), faction)
            with self.db.conn() as c:
                row = c.execute("SELECT vk_id FROM leaders WHERE faction=?", (faction,)).fetchone()
                if not row:
                    self.send(ctx.peer_id, "❌ Лидер не установлен.")
                    return
                uid = int(row[0])
                c.execute("DELETE FROM leaders WHERE faction=?", (faction,))
                c.execute("UPDATE users SET admin_level=0 WHERE vk_id=?", (uid,))
            self.send(ctx.peer_id, f"✅ Лидер фракции {faction} снят.")
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
            field_map = {"фракцию": "faction", "фио": "rp_name", "ник": "nickname", "должность": "position", "сервер": "server_id"}
            field = field_map.get(what)
            if not field:
                self.send(ctx.peer_id, "❌ Раздел изменения: фракцию / фио / ник / должность / сервер.")
                return
            if admin_lvl < 50:
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
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO users(vk_id) VALUES(?)", (target,))
                c.execute(f"UPDATE users SET {field}=? WHERE vk_id=?", (new_value, target))
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
            if len(parts) > duration_idx:
                token = parts[duration_idx].lower()
                if token.isdigit() and len(parts) > duration_idx + 1:
                    token = f"{token}{parts[duration_idx + 1].lower()}"
                    reason_start_idx = duration_idx + 2
                else:
                    reason_start_idx = duration_idx + 1
                m = re.match(r"^(\d+)\s*([чh]|мин|м|m)?$", token, re.I)
                if m:
                    val = int(m.group(1))
                    unit = (m.group(2) or "").lower()
                    if unit in {"ч", "h"}:
                        duration_sec = val * 3600
                    else:
                        duration_sec = val * 60
                if len(parts) > reason_start_idx:
                    reason = " ".join(parts[reason_start_idx:]).strip() or "без причины"
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

        # !новая заметка (название) / ответом на сообщение
        if parts[0].lower() == "!новая" and len(parts) >= 3 and parts[1].lower() == "заметка":
            raw_rest = re.sub(r"^!\s*новая\s+заметка\s+", "", ctx.text, flags=re.I | re.S).strip()
            name = raw_rest.split(maxsplit=1)[0].strip() if raw_rest else None
            if not name:
                self.send(ctx.peer_id, "❌ Не удалось распознать название заметки.")
                return
            if " " in name:
                self.send(ctx.peer_id, "❌ Название заметки должно быть одним словом.")
                return
            content = self._compose_reply_note_content(ctx) or ""
            attachment_ids = ctx.reply_attachment_ids or []
            if not content and not attachment_ids:
                self.send(ctx.peer_id, "Формат: !новая заметка (название) только ответом на сообщение с текстом/вложениями")
                return
            with self.db.conn() as c:
                old = self._find_note_by_name(ctx.chat_id, name)
                stored_name = old["name"] if old else name
                min_role = int(old["min_role"]) if old else 0
                existing_attachments = (old["attachments"] if old else None) or ""
                stored_attachments = ",".join(attachment_ids) if attachment_ids else existing_attachments
                c.execute(
                    "INSERT OR REPLACE INTO notes(chat_id,name,content,attachments,min_role) VALUES(?,?,?,?,?)",
                    (ctx.chat_id, stored_name, content, stored_attachments, min_role),
                )
            self.send(ctx.peer_id, f"✅ Заметка «{name}» создана.")
            return

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
            attachment_ids = ctx.reply_attachment_ids or []
            if not content and not attachment_ids:
                self.send(ctx.peer_id, "Формат: !заметка создать (название) только ответом на сообщение")
                return
            with self.db.conn() as c:
                old = self._find_note_by_name(ctx.chat_id, name)
                stored_name = old["name"] if old else name
                min_role = int(old["min_role"]) if old else 0
                existing_attachments = (old["attachments"] if old else None) or ""
                stored_attachments = ",".join(attachment_ids) if attachment_ids else existing_attachments
                c.execute(
                    "INSERT OR REPLACE INTO notes(chat_id,name,content,attachments,min_role) VALUES(?,?,?,?,?)",
                    (ctx.chat_id, stored_name, content, stored_attachments, min_role),
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
                print("[BOT] Получена команда stopping из control pipe. Останавливаю бота...")
                self.running = False
                break

    def run(self) -> None:
        print("[BOT] Запущен VK community bot.")
        print(f"[BOT] Остановка: echo stopping > {CONTROL_PIPE_PATH}")
        threading.Thread(target=self.control_pipe_listener, daemon=True).start()
        if self.tg_token:
            print("[BOT] Telegram bridge enabled.")
            threading.Thread(target=self.telegram_listener, daemon=True).start()

        while self.running:
            try:
                for event in self.longpoll.listen():
                    if not self.running:
                        break
                    if event.type == VkBotEventType.MESSAGE_EVENT:
                        obj = event.object or {}
                        payload = obj.get("payload") or {}
                        cmd = str(payload.get("cmd", "")).lower()
                        req_id = int(payload.get("id", 0) or 0)
                        user_id = int(obj.get("user_id", 0) or 0)
                        peer_id = int(obj.get("peer_id", user_id) or user_id)
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
                        if cmd == "sync_approve" and req_id > 0 and user_id > 0:
                            result = self._approve_sync_request(user_id, req_id)
                            self.send(peer_id, result)
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
                    own_types, own_ids = self._collect_attachment_ids(msg.get("attachments", []) or [])
                    reply_types, reply_ids = self._collect_attachment_ids(reply.get("attachments", []) or [])
                    reply_attachments = own_types + reply_types
                    reply_attachment_ids = list(dict.fromkeys(own_ids + reply_ids))
                    ctx = Ctx(
                        user_id=user_id,
                        peer_id=int(msg.get("peer_id", user_id)),
                        text=(msg.get("text") or "").strip(),
                        message_cmid=msg.get("conversation_message_id"),
                        reply_user_id=reply.get("from_id"),
                        reply_text=reply.get("text"),
                        reply_cmid=reply.get("conversation_message_id"),
                        reply_attachments=reply_attachments,
                        reply_attachment_ids=reply_attachment_ids,
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

                    if ctx.is_chat and self._enforce_chat_silence(ctx, msg.get("conversation_message_id")):
                        continue
                    if ctx.is_chat:
                        with self.db.conn() as c:
                            ra = c.execute(
                                "SELECT source_chat_id,target_chat_id FROM remote_access_sessions WHERE actor_id=?",
                                (ctx.user_id,),
                            ).fetchone()
                        if ra:
                            src_chat = int(ra["source_chat_id"])
                            dst_chat = int(ra["target_chat_id"])
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
                    if ctx.text:
                        self.handle_command(ctx)
            except Exception as e:
                err = str(e)
                if isinstance(e, requests.exceptions.ReadTimeout) or "Read timed out" in err:
                    print("[BOT] LongPoll timeout: переподключаюсь...", file=sys.stderr)
                    try:
                        self.longpoll = VkBotLongPoll(self.vk_session, self.group_id)
                    except Exception:
                        pass
                    time.sleep(1)
                    continue
                print(f"[BOT] Ошибка цикла: {e}", file=sys.stderr)
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
                    if username and username == TG_SENIOR_ADMIN_USERNAME:
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
                    if ctx.text:
                        self.handle_command(ctx)
            except Exception:
                time.sleep(2)


def main() -> None:


    # Проверяем, что все секреты загружены
    if not HARDCODED_GROUP_TOKEN or HARDCODED_GROUP_TOKEN == "":
        print("❌ ОШИБКА: Не найден VK_GROUP_TOKEN в файле .env")
        print("Создайте файл .env с переменной VK_GROUP_TOKEN=ваш_токен")
        sys.exit(1)
    
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
    print("[BOT] Рекомендуемый запуск в screen/tmux или через systemd с Restart=always.")
    while True:
        bot = FactionBot(token=token, group_id=group_id, db=db)
        try:
            bot.run()
        except Exception as e:
            print(f"[BOT] Критическая ошибка: {e}. Перезапуск через 5 секунд...", file=sys.stderr)
            time.sleep(5)
            continue
        if not bot.running:
            print("[BOT] Остановлен вручную. Перезапуск не требуется.")
            break
        print("[BOT] Неожиданная остановка. Перезапуск через 5 секунд...")
        time.sleep(5)


if __name__ == "__main__":
    main()
